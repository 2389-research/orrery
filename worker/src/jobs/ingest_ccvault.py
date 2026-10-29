# ABOUTME: Ingest ccvault agent-session archives into the graph (docs/ccvault-ingestion.md).
# ABOUTME: One ccvault silo (agent_report). Each SESSION is recursively summarized like a repo:
# ABOUTME: a 'group' doc (session rollup) over 'leaf' docs (segments). Graph-using segments are
# ABOUTME: leaves anchored by entity-id links (active_work); neutral segments are extracted.

import json
import os
import uuid

from orrery_relay import Relay
from ..classifier import classify_document
from ..config import get_settings
from ..db import get_connection, mark_graph_dirty
from .ccvault_reader import open_archive, list_sessions, iter_segments
from .session_featurizer import summarize_segment, summarize_rollup, write_doc, upsert_domain

# Dedicated content types so extract_batch's session sweep never collides with the
# repo/tracker `code_intent` sweep. active_work leaves are never extracted (they're linked
# by entity-id directly), so they carry a terminal status from the start.
SESSION_CONTENT_TYPE = "session_intent"
ACTIVE_WORK_CONTENT_TYPE = "active_work"
# Same fallback ingest_repo uses: a local model can return a payload with no usable
# primary_domain, and we must not crash the whole archive or write a null-path domain.
_UNCLASSIFIED_DOMAIN = "unclassified/needs-review"

def _session_context(meta: dict, rollup: bool = False) -> str:
    ctx = f"Session project: {meta.get('project_path') or '(unknown)'}"
    return f"{ctx}\nStarted: {meta.get('started_at')}" if rollup else ctx


def _session_title(meta: dict) -> str:
    sid = meta["session_id"]
    proj = meta.get("project_path") or ""
    base = os.path.basename(proj.rstrip("/")) if proj else ""
    return f"Session {sid[:8]} — {base}" if base else f"Session {sid[:8]}"


def _resolve_entities(conn, entity_ids) -> list[str]:
    """Keep only ids that exist and are active in THIS workspace (the clone). Flow B links
    to these directly — ids that don't resolve (stale, or a different source noosphere) are
    dropped."""
    ids = [e for e in entity_ids if e]
    if not ids:
        return []
    out = []
    # chunk to stay under SQLite's variable limit
    for i in range(0, len(ids), 400):
        batch = ids[i:i + 400]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT id FROM entities WHERE id IN ({ph}) AND invalid_at IS NULL", batch).fetchall()
        out.extend(r[0] for r in rows)
    return out


async def run_ingest_ccvault(job: dict, db_path: str) -> None:
    """Recursively summarize each ccvault session into the ONE ccvault collection (silo,
    agent_report). A session becomes a 'group' doc (rollup) over 'leaf' docs (segments):
    framing flows down, evidence flows up, exactly like a repo (codesum). A graph-using
    segment becomes an entity-anchored 'active_work' leaf instead of a neutral one — active
    work is part of the session's recursive structure, not a separate doc. Segment order is
    carried by the leaf titles ("part N") + created_at. Per-session atomic: a session's whole
    tree + its ccvault_sessions_seen watermark commit together.
    """
    settings = get_settings()
    relay = Relay.from_settings(settings)
    config = json.loads(job["config"]) if job["config"] else {}
    archive_path = config["archive_path"]
    collection_id = config["collection_id"]
    spec_id = config["spec_id"]
    # Summaries use the SUMMARIZATION model (defaults to the classification model) — same
    # as codesum/tracksum: one call per segment + rollup. Entity extraction stays on the
    # EXTRACTION model; the single per-session classify uses the CLASSIFICATION model.
    sum_model = settings.summarization_model
    classify_model = settings.classification_model

    arc = open_archive(archive_path)
    conn = get_connection(db_path)
    sessions_done = skipped_seen = leaf_docs = active_work_docs = 0
    try:
        seen = {r[0] for r in conn.execute("SELECT session_id FROM ccvault_sessions_seen")}
        processed = {r[0] for r in conn.execute("SELECT query_id FROM ccvault_processed")}
        taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains")]

        for meta in list_sessions(arc):
            sid = meta["session_id"]
            if sid in seen:
                skipped_seen += 1
                continue

            segments = [s for s in iter_segments(arc, sid) if s["text"].strip()]
            if not segments:
                # Empty session (no user/assistant text) — watermark so we don't re-read it.
                conn.execute("INSERT OR IGNORE INTO ccvault_sessions_seen (session_id) VALUES (?)", (sid,))
                conn.commit()
                seen.add(sid)
                continue

            # Leaves: summarize each segment node-locally (evidence flows up).
            leaves = []  # (segment, summary)
            for seg in segments:
                s = await summarize_segment(relay, sum_model, _session_context(meta), seg["text"])
                if s:
                    leaves.append((seg, s))
            # Root/group: roll the session up from its segment summaries.
            rollup = await summarize_rollup(relay, sum_model, _session_context(meta, rollup=True), [s for _, s in leaves]) if leaves else ""
            if not leaves or not rollup:
                # Transient EMPTY MODEL OUTPUT (local models can return empty .text without
                # raising). Do NOT watermark — retry next pass rather than dropping the session.
                print(f"[ingest_ccvault] session {sid[:8]}: empty summary output, will retry", flush=True)
                continue

            # Classify the session once, on the rollup; the whole tree shares that domain.
            title = _session_title(meta)
            cls = await classify_document(relay=relay, title=title, excerpt=rollup[:4000],
                                          existing_taxonomy=taxonomy, model=classify_model)
            dom = cls.get("primary_domain") or _UNCLASSIFIED_DOMAIN
            if dom not in taxonomy:
                taxonomy.append(dom)
            upsert_domain(conn, dom)

            # group = the session rollup; leaves = its segments (parent_path = the group title).
            write_doc(conn, collection_id, rollup, SESSION_CONTENT_TYPE, "group",
                       None, "classified", title, dom)
            for i, (seg, s) in enumerate(leaves):
                leaf_title = f"{title} · part {i + 1}"
                resolved = _resolve_entities(conn, seg["entity_ids"]) if seg["is_graph_work"] else []
                unseen = [q for q in seg["query_ids"] if q not in processed]
                if resolved and unseen:
                    # Graph-using segment → active_work leaf: entity-anchored, terminal status
                    # (never re-extracted), still chunked for semantic recall.
                    leaf_id = write_doc(conn, collection_id, s, ACTIVE_WORK_CONTENT_TYPE, "leaf",
                                         title, "extracted", leaf_title, dom)
                    for eid in resolved:
                        conn.execute("INSERT INTO entity_sources (entity_id, document_id, extraction_pass) "
                                     "VALUES (?, ?, 'ccvault_flowb')", (eid, leaf_id))
                    active_work_docs += 1
                else:
                    # Neutral segment (or a graph segment whose ids don't resolve here) → a
                    # session_intent leaf that extract_batch will mine for entities.
                    leaf_id = write_doc(conn, collection_id, s, SESSION_CONTENT_TYPE, "leaf",
                                         title, "classified", leaf_title, dom)
                # Record every query_id this segment touched (dedup key), pointing at its leaf.
                for q in seg["query_ids"]:
                    conn.execute("INSERT OR IGNORE INTO ccvault_processed (query_id, session_id, document_id) "
                                 "VALUES (?, ?, ?)", (q, sid, leaf_id))
                    processed.add(q)
                # Segment order is carried by the leaf titles ("part N") + created_at. We do NOT
                # write it to collection_edges: that table is collection↔collection (chain_next /
                # uses), and graph_v5 ships every row as a scope:"collection" edge — putting
                # leaf-document ids there would masquerade as collection edges. A dedicated `seq`
                # column on document_collections is the right home if explicit ordering is needed.
                leaf_docs += 1

            conn.execute("INSERT OR IGNORE INTO ccvault_sessions_seen (session_id) VALUES (?)", (sid,))
            conn.commit()
            seen.add(sid)
            sessions_done += 1
            print(f"[ingest_ccvault] session {sid[:8]} -> {dom}: {len(leaves)} segment leaves", flush=True)

        # Phase 2: extract entities over session-summary docs. Gate on the DB, not the
        # in-memory counter: if a prior run committed session docs per-session but died
        # before this enqueue, those docs are durably 'classified' with no extraction job,
        # and a re-ingest (all sessions watermarked → summarized==0) would never repair
        # them. Enqueue whenever any session_intent doc is still 'classified'; a redundant
        # extract_batch is a harmless no-op (nothing left in scope). Flow B docs are excluded
        # (different content_type, already linked). Skip if one is already pending.
        stranded = conn.execute(
            "SELECT 1 FROM documents WHERE content_type = ? AND status = 'classified' LIMIT 1",
            (SESSION_CONTENT_TYPE,)).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM jobs WHERE type = 'extract_batch' AND status IN ('queued','running') LIMIT 1"
        ).fetchone()
        if stranded and not pending:
            conn.execute(
                "INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'extract_batch', ?, 'queued', ?)",
                (str(uuid.uuid4()), collection_id, json.dumps({"spec_id": spec_id, "scope": SESSION_CONTENT_TYPE})))
        if sessions_done:
            mark_graph_dirty(conn)
        conn.commit()

        result = {"sessions_ingested": sessions_done, "sessions_skipped_seen": skipped_seen,
                  "segment_leaves": leaf_docs, "active_work_leaves": active_work_docs}
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(result), job["id"]))
        conn.commit()
        print(f"[ingest_ccvault] done: {sessions_done} sessions ({leaf_docs} segment leaves, "
              f"{active_work_docs} active-work), {skipped_seen} already seen", flush=True)
    finally:
        conn.close()
        arc.close()
