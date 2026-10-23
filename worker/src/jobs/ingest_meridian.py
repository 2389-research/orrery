# ABOUTME: Ingest meridian agent sessions (pull API) into the "Code Sessions" noosphere (agent_report).
# ABOUTME: Each session -> group(session rollup) > group per agent (subagent-aware) > leaf per segment.
# ABOUTME: Backfill on first sight; memoized incremental re-derive on a revision bump (only new/changed
# ABOUTME: segments re-summarize). Per-session atomic with its meridian_sessions/segments rows.

import hashlib
import json
import os
import uuid

from orrery_relay import Relay
from ..classifier import classify_document
from ..config import get_settings
from ..db import get_connection, mark_graph_dirty
from .meridian_reader import (
    make_fetch, list_changed_sessions, get_session, get_agents, get_records, iter_agent_segments,
)
from .upsert_document import _recount_collection
from .session_featurizer import summarize_segment, summarize_rollup, write_doc, upsert_domain

SESSION_CONTENT_TYPE = "session_intent"
_UNCLASSIFIED_DOMAIN = "unclassified/needs-review"


def _session_title(session_key: str, session: dict) -> str:
    sid = session_key.rsplit("/", 1)[-1]
    base = os.path.basename((session.get("cwd") or "").rstrip("/"))
    t = f"Session {sid[:8]} — {base}" if base else f"Session {sid[:8]}"
    return t


def _agent_title(root: str, agent: dict) -> str:
    # FULL agent_id: titles are the parent_path linkage key, so they must be collision-free.
    if agent["agent_id"] == "main":
        return f"{root} · main"
    return f"{root} · {agent.get('agent_type') or 'agent'} {agent['agent_id']}"


def _tool_use_owner(records: list[dict]) -> dict:
    """tool_use id -> agent_id that issued it (to resolve a spawn link from parent_tool_use_id)."""
    owners = {}
    for r in records:
        raw = r.get("raw")
        msg = raw.get("message") if isinstance(raw, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                owners.setdefault(b["id"], r.get("agent_id") or "main")
    return owners


def _resolve_parents(agents: list[dict], records: list[dict]) -> dict:
    """agent_id -> parent agent_id, or None when it hangs from the session root. Subagents whose
    parent can't be resolved map to None too; callers flag them (never guess)."""
    ids = {a["agent_id"] for a in agents}
    owners = _tool_use_owner(records)
    parents = {}
    for a in agents:
        aid = a["agent_id"]
        if aid == "main":
            parents[aid] = None
            continue
        p = a.get("parent_agent_id")
        if not p and a.get("parent_tool_use_id"):
            p = owners.get(a["parent_tool_use_id"])
        parents[aid] = p if p in ids and p != aid else None
    # break cycles: an agent whose ancestry loops is unresolved
    for aid in list(parents):
        seen, cur = {aid}, parents[aid]
        while cur:
            if cur in seen:
                parents[aid] = None
                break
            seen.add(cur)
            cur = parents.get(cur)
    return parents


def _children_hash(summaries: list[str]) -> str:
    return hashlib.sha256(json.dumps(summaries).encode()).hexdigest()


async def _rollup(relay, model, ctx, summaries, cache: dict) -> str:
    """Rollup memoized (within a pass) by the hash of its ordered child summaries."""
    h = _children_hash(summaries)
    if h not in cache:
        cache[h] = await summarize_rollup(relay, model, ctx, summaries)
    return cache[h]


def _layout(agents, records, segments, title):
    """agent_by_id, parents, titles, depth-fn for the agents that own segments (+ ancestors)."""
    parents = _resolve_parents(agents, records)
    agent_by_id = {a["agent_id"]: a for a in agents}
    for seg in segments:
        agent_by_id.setdefault(seg["agent_id"], {"agent_id": seg["agent_id"], "agent_type": seg["agent_type"],
                                                 "parent_tool_use_id": seg["parent_tool_use_id"]})
        parents.setdefault(seg["agent_id"], None)
    depth = {s["agent_id"]: s["spawn_depth"] for s in segments}
    titles = {aid: _agent_title(title, a) for aid, a in agent_by_id.items()}

    def _depth(aid):
        d, cur, seen = 0, parents.get(aid), {aid}
        while cur and cur not in seen:
            seen.add(cur)
            d += 1
            cur = parents.get(cur)
        return depth.get(aid, d)

    return agent_by_id, parents, titles, _depth


def _agent_meta(a, aid, parent_id, depth):
    meta = {"agent_id": aid, "agent_type": a.get("agent_type"), "description": a.get("description"),
            "parent_tool_use_id": a.get("parent_tool_use_id"), "spawn_depth": depth,
            "workflow_id": a.get("workflow_id"), "relation": a.get("relation"),
            "rollup": a.get("rollup")}
    if aid != "main" and parent_id is None:
        meta["parent_unresolved"] = True
    return meta


def _session_meta(session_key, session, pr_links, children_hash):
    return {"session_key": session_key, "contributor": session.get("contributor"),
            "cwd": session.get("cwd"), "git_branch": session.get("git_branch"),
            "lifecycle": session.get("lifecycle"), "started_at": session.get("started_at"),
            "last_activity_at": session.get("last_activity_at"), "pr_links": pr_links,
            "rollup": session.get("rollup"), "children_hash": children_hash}


def _set_doc_content(conn, doc_id, content, metadata):
    """Update a group doc in place (content + its single chunk + metadata)."""
    conn.execute("UPDATE documents SET content=?, content_hash=?, metadata=? WHERE id=?",
                 (content, hashlib.sha256(content.encode()).hexdigest(), json.dumps(metadata), doc_id))
    conn.execute("UPDATE chunks SET text=?, length=? WHERE document_id=? AND chunk_index=0",
                 (content, len(content), doc_id))


def _invalidate_doc(conn, doc_id):
    conn.execute("UPDATE documents SET invalid_at = CURRENT_TIMESTAMP WHERE id=? AND invalid_at IS NULL",
                 (doc_id,))


async def _rederive_session(conn, relay, sum_model, fetch, collection_id, session_key, session,
                            revision, known) -> str:
    """Memoized incremental update. LLM calls happen first (nothing written until they all
    succeed); then one transaction applies leaf/group changes and bumps last_revision."""
    agents = get_agents(fetch, session_key)
    records = get_records(fetch, session_key)
    segments = [s for s in iter_agent_segments(records, agents) if s["text"].strip()]
    title = _session_title(session_key, session)
    ctx = f"Session project: {session.get('cwd') or '(unknown)'}"
    group_id = known[1]

    stored = {(r[0], r[1]): {"hash": r[2], "summary": r[3], "leaf": r[4]} for r in conn.execute(
        "SELECT agent_id, seq, segment_hash, summary, leaf_doc_id FROM meridian_segments "
        "WHERE session_key=?", (session_key,))}
    by_hash = {v["hash"]: v["summary"] for v in stored.values()}

    plan = []  # (seg, summary, action) action: reuse | replace | new
    for seg in segments:
        key = (seg["agent_id"], seg["seq"])
        old = stored.get(key)
        if old and old["hash"] == seg["content_hash"]:
            plan.append((seg, old["summary"], "reuse"))
            continue
        summ = by_hash.get(seg["content_hash"])  # same text seen elsewhere in this session
        if summ is None:
            summ = await summarize_segment(relay, sum_model, ctx, seg["text"])
            if not summ:
                print(f"[ingest_meridian] session {session_key}: empty segment summary, will retry", flush=True)
                return "retry"
            by_hash[seg["content_hash"]] = summ
        plan.append((seg, summ, "replace" if old else "new"))

    by_agent: dict[str, list] = {}
    for seg, summ, _ in plan:
        by_agent.setdefault(seg["agent_id"], []).append(summ)
    all_summaries = [summ for _, summ, _ in plan]
    rollup_ctx = f"{ctx}\nStarted: {session.get('started_at')}"

    agent_by_id, parents, titles, _depth = _layout(agents, records, segments, title)
    cache: dict = {}

    def _existing_agent_doc(aid):
        return conn.execute(
            "SELECT d.id, d.content, d.metadata FROM documents d JOIN document_collections dc "
            "ON dc.document_id = d.id WHERE d.source_path=? AND dc.role='group' AND d.invalid_at IS NULL "
            "AND json_extract(d.metadata,'$.agent_id')=?", (f"meridian:{session_key}", aid)).fetchone()

    async def _group_content(existing, summaries, fallback):
        h = _children_hash(summaries)
        if existing:
            em = json.loads(existing[2]) if existing[2] else {}
            if em.get("children_hash") == h and existing[1]:
                return existing[1], h
        if not summaries:
            return fallback, h
        r = await _rollup(relay, sum_model, rollup_ctx, summaries, cache)
        return r, h

    sess_row = conn.execute("SELECT content, metadata FROM documents WHERE id=?", (group_id,)).fetchone()
    session_rollup, sess_hash = await _group_content(
        (group_id, sess_row[0], sess_row[1]) if sess_row else None, all_summaries, "(no recorded activity)")
    if not session_rollup:
        return "retry"
    agent_content = {}
    for aid in by_agent_or_ancestors(by_agent, parents):
        a = agent_by_id[aid]
        ex = _existing_agent_doc(aid)
        c, h = await _group_content(ex, by_agent.get(aid, []),
                                    a.get("description") or "(no recorded activity)")
        if not c:
            return "retry"
        agent_content[aid] = (c, h, ex)

    dom_row = conn.execute("SELECT domain_path FROM document_domains WHERE document_id=? AND is_primary=1",
                           (group_id,)).fetchone()
    dom = dom_row[0] if dom_row else _UNCLASSIFIED_DOMAIN
    source_path = f"meridian:{session_key}"
    pr_links = ((session.get("rollup") or {}).get("outputs") or {}).get("pr_links") or []
    try:
        upsert_domain(conn, dom)
        _set_doc_content(conn, group_id, session_rollup, _session_meta(session_key, session, pr_links, sess_hash))
        for aid in sorted(agent_content, key=_depth):
            a = agent_by_id[aid]
            parent_id = parents.get(aid)
            meta = _agent_meta(a, aid, parent_id, _depth(aid))
            meta["children_hash"] = agent_content[aid][1]
            c, _, ex = agent_content[aid]
            if ex:
                _set_doc_content(conn, ex[0], c, meta)
            else:
                parent_title = title if parent_id is None else titles[parent_id]
                write_doc(conn, collection_id, c, SESSION_CONTENT_TYPE, "group", parent_title,
                          "classified", titles[aid], dom, metadata=meta, source_path=source_path)
        current = set()
        for seg, summ, action in plan:
            aid, seq = seg["agent_id"], seg["seq"]
            current.add((aid, seq))
            if action == "reuse":
                continue
            if action == "replace":
                _invalidate_doc(conn, stored[(aid, seq)]["leaf"])
            leaf_id = write_doc(conn, collection_id, summ, SESSION_CONTENT_TYPE, "leaf", titles[aid],
                                "classified", f"{titles[aid]} · part {seq + 1}", dom, source_path=source_path)
            conn.execute(
                "INSERT INTO meridian_segments (session_key, segment_hash, agent_id, seq, summary, leaf_doc_id) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_key, agent_id, seq) DO UPDATE SET "
                "segment_hash=excluded.segment_hash, summary=excluded.summary, leaf_doc_id=excluded.leaf_doc_id",
                (session_key, seg["content_hash"], aid, seq, summ, leaf_id))
        for key, v in stored.items():
            if key not in current:
                _invalidate_doc(conn, v["leaf"])
                conn.execute("DELETE FROM meridian_segments WHERE session_key=? AND agent_id=? AND seq=?",
                             (session_key, key[0], key[1]))
        conn.execute("UPDATE meridian_sessions SET last_revision=?, lifecycle=?, updated_at=CURRENT_TIMESTAMP "
                     "WHERE session_key=?", (revision, session.get("lifecycle"), session_key))
        # invalidations never decrement; recount from active (invalid_at IS NULL) docs
        _recount_collection(conn, collection_id)
        conn.execute(
            "UPDATE domains SET document_count = (SELECT COUNT(*) FROM document_domains dd "
            "JOIN documents d ON d.id = dd.document_id WHERE dd.domain_path = ? AND d.invalid_at IS NULL) "
            "WHERE path = ?", (dom, dom))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"[ingest_meridian] session {session_key} re-derived at rev {revision}: "
          f"{sum(1 for p in plan if p[2] != 'reuse')} segments changed", flush=True)
    return "updated"


async def _process_session(conn, relay, sum_model, classify_model, taxonomy, fetch,
                           collection_id, item) -> str:
    """Returns 'ingested' | 'updated' | 'skipped' | 'retry' (empty model output)."""
    session_key = item.get("session_key")
    if not session_key:
        return "skipped"
    session = get_session(fetch, session_key)
    revision = session.get("revision", item.get("revision"))
    known = conn.execute("SELECT last_revision, group_doc_id FROM meridian_sessions WHERE session_key=?",
                         (session_key,)).fetchone()
    if known is not None:
        if revision is None or known[0] is None or revision <= known[0]:
            return "skipped"  # nothing new: no LLM calls, no writes
        return await _rederive_session(conn, relay, sum_model, fetch, collection_id, session_key,
                                       session, revision, known)

    agents = get_agents(fetch, session_key)
    records = get_records(fetch, session_key)
    segments = [s for s in iter_agent_segments(records, agents) if s["text"].strip()]
    if not segments:
        return "skipped"

    title = _session_title(session_key, session)
    ctx = f"Session project: {session.get('cwd') or '(unknown)'}"

    summaries = []  # (segment, summary)
    for seg in segments:
        s = await summarize_segment(relay, sum_model, ctx, seg["text"])
        if not s:
            print(f"[ingest_meridian] session {session_key}: empty segment summary, will retry", flush=True)
            return "retry"
        summaries.append((seg, s))

    by_agent: dict[str, list] = {}
    for seg, s in summaries:
        by_agent.setdefault(seg["agent_id"], []).append((seg, s))
    rollup_ctx = f"{ctx}\nStarted: {session.get('started_at')}"
    rcache: dict = {}
    session_rollup = await _rollup(relay, sum_model, rollup_ctx, [s for _, s in summaries], rcache)
    agent_rollups = {}
    for aid, items in by_agent.items():
        r = await _rollup(relay, sum_model, rollup_ctx, [s for _, s in items], rcache)
        if not r:
            return "retry"
        agent_rollups[aid] = r
    if not session_rollup:
        print(f"[ingest_meridian] session {session_key}: empty rollup, will retry", flush=True)
        return "retry"

    cls = await classify_document(relay=relay, title=title, excerpt=session_rollup[:4000],
                                  existing_taxonomy=taxonomy, model=classify_model)
    dom = cls.get("primary_domain") or _UNCLASSIFIED_DOMAIN
    if dom not in taxonomy:
        taxonomy.append(dom)

    source_path = f"meridian:{session_key}"
    pr_links = ((session.get("rollup") or {}).get("outputs") or {}).get("pr_links") or []
    parents = _resolve_parents(agents, records)
    # agents with segments, plus any agent on the path to the root of one (so parents exist)
    agent_by_id = {a["agent_id"]: a for a in agents}
    for seg in segments:
        agent_by_id.setdefault(seg["agent_id"], {"agent_id": seg["agent_id"], "agent_type": seg["agent_type"],
                                                 "parent_tool_use_id": seg["parent_tool_use_id"]})
        parents.setdefault(seg["agent_id"], None)
    depth = {s["agent_id"]: s["spawn_depth"] for s in segments}
    titles = {aid: _agent_title(title, a) for aid, a in agent_by_id.items()}

    def _depth(aid):
        d, cur, seen = 0, parents.get(aid), {aid}
        while cur and cur not in seen:
            seen.add(cur)
            d += 1
            cur = parents.get(cur)
        return depth.get(aid, d)

    try:
        upsert_domain(conn, dom)
        group_id = write_doc(
            conn, collection_id, session_rollup, SESSION_CONTENT_TYPE, "group", None, "classified",
            title, dom, source_path=source_path,
            metadata=_session_meta(session_key, session, pr_links,
                                   _children_hash([s for _, s in summaries])))
        for aid in sorted(by_agent_or_ancestors(by_agent, parents), key=_depth):
            a = agent_by_id[aid]
            parent_id = parents.get(aid)
            unresolved = aid != "main" and parent_id is None
            meta = {"agent_id": aid, "agent_type": a.get("agent_type"), "description": a.get("description"),
                    "parent_tool_use_id": a.get("parent_tool_use_id"), "spawn_depth": _depth(aid),
                    "workflow_id": a.get("workflow_id"), "relation": a.get("relation"),
                    "rollup": a.get("rollup"),
                    "children_hash": _children_hash([s for _, s in by_agent.get(aid, [])])}
            if unresolved:
                meta["parent_unresolved"] = True
            parent_title = title if parent_id is None else titles[parent_id]
            content = agent_rollups.get(aid) or a.get("description") or "(no recorded activity)"
            write_doc(conn, collection_id, content, SESSION_CONTENT_TYPE, "group", parent_title,
                      "classified", titles[aid], dom, metadata=meta, source_path=source_path)
        for aid, items in by_agent.items():
            for seg, s in items:
                leaf_title = f"{titles[aid]} · part {seg['seq'] + 1}"
                leaf_id = write_doc(conn, collection_id, s, SESSION_CONTENT_TYPE, "leaf", titles[aid],
                                    "classified", leaf_title, dom, source_path=source_path)
                conn.execute(
                    "INSERT INTO meridian_segments (session_key, segment_hash, agent_id, seq, "
                    "summary, leaf_doc_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_key, seg["content_hash"], aid, seg["seq"], s, leaf_id))
        conn.execute(
            "INSERT INTO meridian_sessions (session_key, collection_id, last_revision, group_doc_id, "
            "lifecycle) VALUES (?, ?, ?, ?, ?)",
            (session_key, collection_id, revision, group_id, session.get("lifecycle")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"[ingest_meridian] session {session_key} -> {dom}: {len(segments)} segment leaves", flush=True)
    return "ingested"


def by_agent_or_ancestors(by_agent: dict, parents: dict) -> set:
    out = set()
    for aid in by_agent:
        cur = aid
        while cur and cur not in out:
            out.add(cur)
            cur = parents.get(cur)
    return out


async def run_ingest_meridian(job: dict, db_path: str) -> None:
    """Pull changed sessions from meridian and write each as an agent_report tree. The poll
    cursor advances only after the whole drained page set is processed; a session that hit
    transient empty model output holds the cursor so it is retried on the next pass."""
    settings = get_settings()
    relay = Relay.from_settings(settings)
    config = json.loads(job["config"]) if job["config"] else {}
    collection_id = config["collection_id"]
    spec_id = config.get("spec_id")
    fetch = make_fetch(config["server_url"])
    sum_model = settings.extraction_model
    classify_model = settings.classification_model

    conn = get_connection(db_path)
    counts = {"ingested": 0, "updated": 0, "skipped": 0, "retry": 0}
    try:
        row = conn.execute("SELECT updated_after FROM meridian_cursor WHERE collection_id=?",
                           (collection_id,)).fetchone()
        cursor = row[0] if row else None
        taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains")]

        items, next_cursor, _ = list_changed_sessions(fetch, cursor)
        for item in items:
            outcome = await _process_session(conn, relay, sum_model, classify_model, taxonomy,
                                             fetch, collection_id, item)
            counts[outcome] += 1

        if counts["retry"] == 0 and next_cursor and next_cursor != cursor:
            conn.execute("INSERT INTO meridian_cursor (collection_id, updated_after) VALUES (?, ?) "
                         "ON CONFLICT(collection_id) DO UPDATE SET updated_after=excluded.updated_after",
                         (collection_id, next_cursor))

        stranded = conn.execute("SELECT 1 FROM documents WHERE content_type = ? AND status = 'classified' "
                                "AND silo_id = ? LIMIT 1", (SESSION_CONTENT_TYPE, collection_id)).fetchone()
        pending = conn.execute("SELECT 1 FROM jobs WHERE type = 'extract_batch' AND status IN "
                               "('queued','running') LIMIT 1").fetchone()
        if stranded and not pending and spec_id:
            conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'extract_batch', ?, "
                         "'queued', ?)", (str(uuid.uuid4()), collection_id,
                                          json.dumps({"spec_id": spec_id, "scope": SESSION_CONTENT_TYPE})))
        if counts["ingested"] or counts["updated"]:
            mark_graph_dirty(conn)
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(counts), job["id"]))
        conn.commit()
        print(f"[ingest_meridian] done: {counts}", flush=True)
    finally:
        conn.close()
