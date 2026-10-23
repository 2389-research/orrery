# ABOUTME: Ingest meridian agent sessions (pull API) into the "Code Sessions" noosphere (agent_report).
# ABOUTME: Each session -> group(session rollup) > group per agent (subagent-aware) > leaf per segment.
# ABOUTME: Backfill (first sight) only; per-session atomic with its meridian_sessions/segments rows.

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
from .session_featurizer import summarize_segment, summarize_rollup, write_doc, upsert_domain

SESSION_CONTENT_TYPE = "session_intent"
_UNCLASSIFIED_DOMAIN = "unclassified/needs-review"


def _session_title(session_key: str, session: dict) -> str:
    sid = session_key.rsplit("/", 1)[-1]
    base = os.path.basename((session.get("cwd") or "").rstrip("/"))
    t = f"Session {sid[:8]} — {base}" if base else f"Session {sid[:8]}"
    return t


def _agent_title(root: str, agent: dict) -> str:
    kind = agent.get("agent_type") or ("main" if agent["agent_id"] == "main" else "agent")
    return f"{root} · {kind} {agent['agent_id'][:8]}"


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


async def _process_session(conn, relay, sum_model, classify_model, taxonomy, fetch,
                           collection_id, item) -> str:
    """Returns 'ingested' | 'skipped' | 'retry' (empty model output)."""
    session_key = item.get("session_key")
    if not session_key:
        return "skipped"
    session = get_session(fetch, session_key)
    revision = session.get("revision", item.get("revision"))
    known = conn.execute("SELECT last_revision FROM meridian_sessions WHERE session_key=?",
                         (session_key,)).fetchone()
    if known is not None:
        # Re-derive on a revision bump is the incremental path (not backfill).
        return "skipped"

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
    session_rollup = await summarize_rollup(relay, sum_model, rollup_ctx, [s for _, s in summaries])
    agent_rollups = {}
    for aid, items in by_agent.items():
        r = await summarize_rollup(relay, sum_model, rollup_ctx, [s for _, s in items])
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
            metadata={"session_key": session_key, "contributor": session.get("contributor"),
                      "cwd": session.get("cwd"), "git_branch": session.get("git_branch"),
                      "lifecycle": session.get("lifecycle"), "started_at": session.get("started_at"),
                      "last_activity_at": session.get("last_activity_at"), "pr_links": pr_links,
                      "rollup": session.get("rollup")})
        for aid in sorted(by_agent_or_ancestors(by_agent, parents), key=_depth):
            a = agent_by_id[aid]
            parent_id = parents.get(aid)
            unresolved = aid != "main" and parent_id is None
            meta = {"agent_id": aid, "agent_type": a.get("agent_type"), "description": a.get("description"),
                    "parent_tool_use_id": a.get("parent_tool_use_id"), "spawn_depth": _depth(aid),
                    "workflow_id": a.get("workflow_id"), "relation": a.get("relation"),
                    "rollup": a.get("rollup")}
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
                    "INSERT OR IGNORE INTO meridian_segments (session_key, segment_hash, agent_id, seq, "
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
    counts = {"ingested": 0, "skipped": 0, "retry": 0}
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
        if counts["ingested"]:
            mark_graph_dirty(conn)
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(counts), job["id"]))
        conn.commit()
        print(f"[ingest_meridian] done: {counts}", flush=True)
    finally:
        conn.close()
