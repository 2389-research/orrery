# ABOUTME: Tests for the work_unit backfill job — metadata-only tagging of existing meridian group docs.
# ABOUTME: Fake fetch, file-backed sqlite, no LLM; asserts idempotence and that nothing else changes.

import asyncio
import json
import uuid

import src.jobs.work_unit_backfill as mod
from src.db import get_connection

SK = "c1/sess-1"
ROOT = "/Users/x/Documents/GitHub/meadow"

MAIN = {"agent_id": "main", "agent_type": None, "description": None, "parent_agent_id": None,
        "parent_tool_use_id": None, "relation": "observed", "workflow_id": None, "rollup": {}}
SUB = {"agent_id": "a1", "agent_type": "general-purpose", "description": "dig", "parent_agent_id": "main",
       "parent_tool_use_id": "tu9", "relation": "observed", "workflow_id": None, "rollup": {}}


def _rec(uuid_, agent, text, ts):
    raw = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
           "cwd": ROOT,
           "serverClassifierContext": {"context": {"git_state": {
               "root": ROOT, "branch": "b1",
               "visibility": {"origin": {"host": "github.com", "remote": "acme/meadow"}}}}}}
    return {"uuid": uuid_, "agent_id": agent, "inherited": False, "sort_ts": ts, "seq": 1,
            "type": "user", "raw": raw}


RECS = [_rec("m1", "main", "main work", "2026-10-01T00:00:01Z"),
        _rec("s1", "a1", "sub work", "2026-10-01T00:00:02Z")]


def _fetch(path, params=None):
    session = {"session_key": SK, "revision": 2, "cwd": ROOT, "rollup": {}}
    if path.endswith("/agents"):
        return {"items": [MAIN, SUB], "has_more": False}
    if path.endswith("/records"):
        return {"items": RECS, "has_more": False}
    return session


def _insert_doc(conn, did, cid, role, md, title):
    conn.execute("INSERT INTO documents (id, title, source_path, content, content_hash, metadata) "
                 "VALUES (?, ?, ?, 'summary text', ?, ?)",
                 (did, title, f"meridian:{SK}", did, json.dumps(md) if md is not None else None))
    conn.execute("INSERT INTO document_collections (document_id, collection_id, role, parent_path) "
                 "VALUES (?, ?, ?, ?)", (did, cid, role, title))


def _setup(monkeypatch, test_db):
    monkeypatch.setattr(mod, "make_fetch", lambda url: _fetch)
    cid = str(uuid.uuid4())
    conn = get_connection(test_db)
    conn.execute("INSERT INTO collections (id, name, path, root_path, kind, provenance_kind, document_count) "
                 "VALUES (?, 'sessions', 'sessions', '/tmp/s', 'meridian', 'agent_report', 0)", (cid,))
    _insert_doc(conn, "sess-doc", cid, "group", {"session_key": SK, "keep": 1}, "S")
    _insert_doc(conn, "main-doc", cid, "group", {"agent_id": "main", "keep": 2}, "M")
    _insert_doc(conn, "a1-doc", cid, "group", {"agent_id": "a1"}, "A")
    _insert_doc(conn, "leaf-doc", cid, "leaf", {"agent_id": "main", "segment": 0}, "L")
    conn.execute("INSERT INTO meridian_sessions (session_key, collection_id, last_revision, group_doc_id, "
                 "lifecycle, classified_final) VALUES (?, ?, 2, 'sess-doc', 'inactive', 1)", (SK, cid))
    job = {"id": str(uuid.uuid4()), "type": "work_unit_backfill", "target": cid,
           "config": json.dumps({"collection_id": cid, "server_url": "http://m"})}
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, ?, ?, 'running', ?)",
                 (job["id"], job["type"], cid, job["config"]))
    conn.commit(); conn.close()
    return cid, job


def _snapshot(test_db):
    conn = get_connection(test_db)
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT id, title, content, content_hash, metadata FROM documents")}
    finally:
        conn.close()


def test_backfill_tags_group_docs_only_and_is_idempotent(monkeypatch, test_db):
    _, job = _setup(monkeypatch, test_db)
    before = _snapshot(test_db)
    asyncio.run(mod.run_work_unit_backfill(job, test_db))
    after = _snapshot(test_db)

    md = {k: json.loads(v[3]) for k, v in after.items()}
    assert md["sess-doc"]["work_unit"]["key"] and "github.com/acme/meadow" in md["sess-doc"]["work_unit"]["keys"]
    assert md["main-doc"]["work_unit"]["key"] == "github.com/acme/meadow"
    assert md["a1-doc"]["work_unit"]["key"] == "github.com/acme/meadow"
    # existing metadata is merged, not replaced
    assert md["sess-doc"]["keep"] == 1 and md["main-doc"]["keep"] == 2
    # leaf untouched; nothing but group metadata changed
    assert after["leaf-doc"] == before["leaf-doc"]
    for k in after:
        assert after[k][:3] == before[k][:3]

    asyncio.run(mod.run_work_unit_backfill(job, test_db))
    assert _snapshot(test_db) == after


def test_fully_failed_backfill_raises_and_counts(monkeypatch, test_db):
    import pytest
    _, job = _setup(monkeypatch, test_db)

    def boom(path, params=None):
        raise ConnectionError("meridian unreachable")
    monkeypatch.setattr(mod, "make_fetch", lambda url: boom)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.run_work_unit_backfill(job, test_db))
    conn = get_connection(test_db)
    try:
        res = json.loads(conn.execute("SELECT result FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
    finally:
        conn.close()
    assert res == {"sessions": 0, "docs_tagged": 0, "failed": 1}
