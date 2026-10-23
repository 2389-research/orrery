# ABOUTME: Tests for ingest_meridian backfill — fake fetch + fake relay, file-backed sqlite, no network.
# ABOUTME: A session becomes group(session) > group(agent)... > leaf(segment) agent_report tree.

import asyncio
import json
import uuid

import src.jobs.ingest_meridian as mod
from src.db import get_connection

SK = "c1/sess-1"


class _Resp:
    def __init__(self, text): self.text = text


class _FakeRelay:
    async def complete(self, model, max_tokens, messages):
        return _Resp("Neutral summary of the work done.")

    @classmethod
    def from_settings(cls, settings, **kw): return cls()


class _EmptyRelay(_FakeRelay):
    async def complete(self, model, max_tokens, messages):
        return _Resp("")


def _rec(uuid_, agent, text, ts, typ="user", tool_use_id=None):
    content = [{"type": "text", "text": text}]
    if tool_use_id:
        content.append({"type": "tool_use", "id": tool_use_id, "name": "Task", "input": {"p": "x"}})
    return {"uuid": uuid_, "agent_id": agent, "inherited": False, "sort_ts": ts, "seq": 1, "type": typ,
            "raw": {"type": typ, "message": {"role": "assistant" if tool_use_id else "user", "content": content}}}


def _fake_fetch(agents, records, revision=2):
    session = {"session_key": SK, "id": f"meridian:session/{SK}", "revision": revision,
               "contributor": "nomzor", "cwd": "/fixture/repo", "git_branch": "b1",
               "lifecycle": "inactive", "started_at": "2026-10-01T00:00:00Z",
               "last_activity_at": "2026-10-01T01:00:00Z", "title": "Fix auth",
               "rollup": {"outputs": {"pr_links": ["https://x/pr/1"]}}}

    def fetch(path, params=None):
        if path == "/v1/sessions":
            return {"items": [session], "has_more": False, "next_cursor": "cur-1"}
        if path.endswith("/agents"):
            return {"items": agents, "has_more": False}
        if path.endswith("/records"):
            return {"items": records, "has_more": False}
        return session
    return fetch


MAIN = {"agent_id": "main", "agent_type": None, "description": None, "parent_agent_id": None,
        "parent_tool_use_id": None, "relation": "observed", "workflow_id": None, "rollup": {}}
SUB = {"agent_id": "a1", "agent_type": "general-purpose", "description": "dig", "parent_agent_id": "main",
       "parent_tool_use_id": "tu9", "relation": "observed", "workflow_id": None, "rollup": {}}
RECS = [_rec("m1", "main", "main work", "2026-10-01T00:00:01Z", "assistant", tool_use_id="tu9"),
        _rec("s1", "a1", "sub work", "2026-10-01T00:00:02Z")]


def _setup(monkeypatch, test_db, fetch, relay=_FakeRelay):
    async def fake_classify(relay, title, excerpt, existing_taxonomy, model):
        return {"primary_domain": "software/auth", "secondary_domains": []}
    monkeypatch.setattr(mod, "classify_document", fake_classify)
    monkeypatch.setattr(mod, "Relay", relay)
    monkeypatch.setattr(mod, "make_fetch", lambda url: fetch)
    cid = str(uuid.uuid4())
    conn = get_connection(test_db)
    conn.execute("INSERT INTO collections (id, name, path, root_path, kind, provenance_kind, document_count) "
                 "VALUES (?, 'sessions', 'sessions', '/tmp/s', 'meridian', 'agent_report', 0)", (cid,))
    job = {"id": str(uuid.uuid4()), "type": "ingest_meridian", "target": cid,
           "config": json.dumps({"collection_id": cid, "spec_id": "spec-x", "server_url": "http://m"})}
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, ?, ?, 'running', ?)",
                 (job["id"], job["type"], cid, job["config"]))
    conn.commit(); conn.close()
    return cid, job


def _agent_docs(test_db):
    """agent_id -> (title, parent_path, metadata) for the per-agent group docs."""
    conn = get_connection(test_db)
    try:
        out = {}
        for title, parent, m in conn.execute(
                "SELECT d.title, dc.parent_path, d.metadata FROM documents d "
                "JOIN document_collections dc ON dc.document_id = d.id WHERE d.metadata IS NOT NULL"):
            md = json.loads(m)
            if "agent_id" in md:
                out[md["agent_id"]] = (title, parent, md)
        return out
    finally:
        conn.close()


def test_backfill_writes_session_agent_and_leaf_docs(monkeypatch, test_db):
    cid, job = _setup(monkeypatch, test_db, _fake_fetch([MAIN, SUB], RECS))
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    conn = get_connection(test_db)
    try:
        roles = dict(conn.execute("SELECT role, COUNT(*) FROM document_collections GROUP BY role").fetchall())
        assert roles["group"] == 1 + 2 and roles["leaf"] == 2
        sess = conn.execute("SELECT last_revision, lifecycle, group_doc_id FROM meridian_sessions "
                            "WHERE session_key=?", (SK,)).fetchone()
        assert sess[0] == 2 and sess[1] == "inactive" and sess[2]
        segs = conn.execute("SELECT segment_hash, agent_id, seq, leaf_doc_id FROM meridian_segments "
                            "ORDER BY agent_id").fetchall()
        assert [(r[1], r[2]) for r in segs] == [("a1", 0), ("main", 0)]
        leaf_ids = {r[0] for r in conn.execute("SELECT document_id FROM document_collections WHERE role='leaf'")}
        assert {r[3] for r in segs} == leaf_ids
        assert all(len(r[0]) == 64 for r in segs)
        assert conn.execute("SELECT updated_after FROM meridian_cursor WHERE collection_id=?",
                            (cid,)).fetchone()[0] == "cur-1"
        assert {r[0] for r in conn.execute("SELECT source_path FROM documents")} == {f"meridian:{SK}"}
        assert conn.execute("SELECT COUNT(*) FROM document_collections WHERE emits_cooccurrence != 0").fetchone()[0] == 0
        # leaves hang under their agent's group title
        ad = _agent_docs(test_db)
        leaf_parents = {r[0] for r in conn.execute("SELECT parent_path FROM document_collections WHERE role='leaf'")}
        assert leaf_parents == {ad["main"][0], ad["a1"][0]}
        # session group metadata
        sm = json.loads(conn.execute("SELECT metadata FROM documents WHERE id=?", (sess[2],)).fetchone()[0])
        assert sm["session_key"] == SK and sm["pr_links"] == ["https://x/pr/1"]
    finally:
        conn.close()


def test_subagent_group_links_to_spawn_tool_use(monkeypatch, test_db):
    _, job = _setup(monkeypatch, test_db, _fake_fetch([MAIN, SUB], RECS))
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    ad = _agent_docs(test_db)
    meta = ad["a1"][2]
    assert meta["parent_tool_use_id"] == "tu9" and meta["spawn_depth"] == 1
    assert not meta.get("parent_unresolved")
    assert ad["a1"][1] == ad["main"][0]


def test_missing_parent_link_flags_not_guesses(monkeypatch, test_db):
    orphan = dict(SUB, parent_agent_id=None, parent_tool_use_id=None)
    _, job = _setup(monkeypatch, test_db, _fake_fetch([MAIN, orphan], RECS))
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    ad = _agent_docs(test_db)
    assert ad["a1"][2]["parent_unresolved"] is True
    conn = get_connection(test_db)
    try:  # attached under the session root, not guessed onto main
        root_title = conn.execute("SELECT title FROM documents WHERE json_extract(metadata,'$.session_key')=?", (SK,)).fetchone()[0]
    finally:
        conn.close()
    assert ad["a1"][1] == root_title


def test_empty_model_output_does_not_watermark(monkeypatch, test_db):
    cid, job = _setup(monkeypatch, test_db, _fake_fetch([MAIN, SUB], RECS), relay=_EmptyRelay)
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM meridian_cursor WHERE collection_id=?", (cid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM meridian_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    finally:
        conn.close()


def test_identical_segment_text_keeps_one_row_per_position(monkeypatch, test_db):
    _, job = _setup(monkeypatch, test_db, _fake_fetch([MAIN, SUB], [
        _rec("m1", "main", "same", "2026-10-01T00:00:01Z", tool_use_id="tu9"),
        _rec("s1", "a1", "same", "2026-10-01T00:00:02Z")]))
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM meridian_segments").fetchone()[0] == 2
    finally:
        conn.close()


# ---- work_unit tagging -------------------------------------------------------------------------

_ROOT = "/Users/x/Documents/GitHub/meadow"


def _with_git(rec):
    rec["raw"]["cwd"] = _ROOT
    rec["raw"]["serverClassifierContext"] = {"context": {"git_state": {
        "root": _ROOT, "branch": "b1",
        "visibility": {"origin": {"host": "github.com", "remote": "acme/meadow"}}}}}
    return rec


def _wu_recs():
    return [_with_git(_rec("m1", "main", "main work", "2026-10-01T00:00:01Z", "assistant", tool_use_id="tu9")),
            _with_git(_rec("s1", "a1", "sub work", "2026-10-01T00:00:02Z"))]


def _group_wu(test_db):
    conn = get_connection(test_db)
    try:
        sess = conn.execute("SELECT group_doc_id FROM meridian_sessions").fetchone()[0]
        sm = json.loads(conn.execute("SELECT metadata FROM documents WHERE id=?", (sess,)).fetchone()[0])
        leaf_md = [r[0] for r in conn.execute(
            "SELECT d.metadata FROM documents d JOIN document_collections dc ON dc.document_id=d.id "
            "WHERE dc.role='leaf'")]
        ndocs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()
    ad = _agent_docs(test_db)
    return sm.get("work_unit"), {k: v[2].get("work_unit") for k, v in ad.items()}, leaf_md, ndocs


def test_backfill_tags_session_and_agent_docs_with_work_unit(monkeypatch, test_db):
    _, job = _setup(monkeypatch, test_db, _fake_fetch([MAIN, SUB], _wu_recs()))
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    sess_wu, agent_wu, leaf_md, _ = _group_wu(test_db)
    # (the shared fixture's session PR link is a non-github URL, so the session key may be PR-derived;
    # the git remote must still be among the session's keys)
    assert sess_wu and sess_wu["key"] and "github.com/acme/meadow" in sess_wu["keys"]
    # fixture session carries a PR link, which the main agent inherits (source "pr"); without it git_remote
    assert agent_wu["main"]["source"] in ("pr", "git_remote")
    assert agent_wu["main"]["key"] in ("x/pr/1", "github.com/acme/meadow")
    assert any(u["key"] == "github.com/acme/meadow" for u in agent_wu["main"]["units"])
    assert agent_wu["a1"]["key"]
    assert all(m is None or "work_unit" not in json.loads(m) for m in leaf_md)


def test_rederive_keeps_work_unit_and_no_duplicate_docs(monkeypatch, test_db):
    fetch = _fake_fetch([MAIN, SUB], _wu_recs(), revision=2)
    _, job = _setup(monkeypatch, test_db, fetch)
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    first = _group_wu(test_db)
    # same revision: skipped
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    assert _group_wu(test_db) == first
    # revision bump, unchanged content: re-derive path
    monkeypatch.setattr(mod, "make_fetch", lambda url: _fake_fetch([MAIN, SUB], _wu_recs(), revision=3))
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT last_revision FROM meridian_sessions").fetchone()[0] == 3
    finally:
        conn.close()
    again = _group_wu(test_db)
    assert again[0] and again[0] == first[0]
    assert again[1] == first[1]
    assert again[3] == first[3]
