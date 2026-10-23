# ABOUTME: Tests for ingest_meridian's memoized re-derive on a revision bump — growable fake fetch +
# ABOUTME: counting fake relay; only new/changed segments (and changed rollups) hit the model.

import asyncio

import src.jobs.ingest_meridian as mod
from src.db import get_connection
from tests.test_ingest_meridian import _setup, _rec, MAIN, SK, _Resp

BIG = "x" * 3100  # one record per segment (target_chars=3000)


class _CountingRelay:
    calls = 0

    async def complete(self, model, max_tokens, messages):
        type(self).calls += 1
        return _Resp(f"Summary {type(self).calls}.")

    @classmethod
    def from_settings(cls, settings, **kw): return cls()


class _Growable:
    def __init__(self, n=3, tail_small=False):
        self.revision = 2
        self.records = [_rec(f"r{i}", "main", f"{i}{BIG}", f"2026-10-01T00:00:{i:02d}Z") for i in range(n)]
        if tail_small:
            self.records.append(_rec("tail", "main", "tail", "2026-10-01T00:00:50Z"))

    def append(self, text, uid):
        self.records.append(_rec(uid, "main", text, f"2026-10-01T00:01:{len(self.records):02d}Z"))
        self.revision += 1

    def fetch(self, path, params=None):
        session = {"session_key": SK, "revision": self.revision, "contributor": "n", "cwd": "/r",
                   "git_branch": "b", "lifecycle": "active", "started_at": "t0", "last_activity_at": "t1",
                   "rollup": {}}
        if path == "/v1/sessions":
            return {"items": [session], "has_more": False, "next_cursor": f"cur-{self.revision}"}
        if path.endswith("/agents"):
            return {"items": [MAIN], "has_more": False}
        if path.endswith("/records"):
            return {"items": list(self.records), "has_more": False}
        return session


def _go(monkeypatch, test_db, g):
    _CountingRelay.calls = 0
    _, job = _setup(monkeypatch, test_db, g.fetch, relay=_CountingRelay)
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    return job


def _counts(test_db):
    conn = get_connection(test_db)
    try:
        return (conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM documents d JOIN document_collections dc ON dc.document_id=d.id "
                             "WHERE dc.role='leaf' AND d.invalid_at IS NULL").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM documents d JOIN document_collections dc ON dc.document_id=d.id "
                             "WHERE dc.role='leaf' AND d.invalid_at IS NOT NULL").fetchone()[0])
    finally:
        conn.close()


def test_same_revision_is_noop(monkeypatch, test_db):
    g = _Growable()
    job = _go(monkeypatch, test_db, g)
    n, docs = _CountingRelay.calls, _counts(test_db)
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    assert _CountingRelay.calls == n
    assert _counts(test_db) == docs


def test_growth_resummarizes_only_the_new_segment(monkeypatch, test_db):
    g = _Growable()
    job = _go(monkeypatch, test_db, g)
    before = _CountingRelay.calls
    docs_before = _counts(test_db)
    g.append(f"new{BIG}", "r-new")
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    assert _CountingRelay.calls - before == 1 + 1  # new segment + one rollup (session == main agent)
    d, active, invalid = _counts(test_db)
    assert active == 4 and invalid == 0 and d == docs_before[0] + 1
    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT last_revision FROM meridian_sessions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM meridian_segments").fetchone()[0] == 4
    finally:
        conn.close()
    # idempotent once caught up
    n = _CountingRelay.calls
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    assert _CountingRelay.calls == n


def test_changed_tail_invalidates_old_leaf_not_duplicates(monkeypatch, test_db):
    g = _Growable(tail_small=True)
    job = _go(monkeypatch, test_db, g)
    _, active0, _ = _counts(test_db)
    assert active0 == 4
    conn = get_connection(test_db)
    old_leaf = conn.execute("SELECT leaf_doc_id FROM meridian_segments WHERE seq=3").fetchone()[0]
    conn.close()
    before = _CountingRelay.calls
    g.append("tail grew", "r-more")  # joins the open tail run -> same position, new hash
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    assert _CountingRelay.calls - before == 2  # changed tail + rollup; the 3 stable ones untouched
    _, active, invalid = _counts(test_db)
    assert active == 4 and invalid == 1  # one more leaf doc total, old one superseded
    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT invalid_at FROM documents WHERE id=?", (old_leaf,)).fetchone()[0] is not None
        new_leaf = conn.execute("SELECT leaf_doc_id FROM meridian_segments WHERE seq=3").fetchone()[0]
        assert new_leaf != old_leaf
    finally:
        conn.close()


def test_rederive_counters_exclude_invalidated_leaves(monkeypatch, test_db):
    g = _Growable(tail_small=True)
    job = _go(monkeypatch, test_db, g)
    g.append("tail grew", "r-more")
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE invalid_at IS NOT NULL").fetchone()[0] == 1
        active = conn.execute("SELECT COUNT(*) FROM documents WHERE invalid_at IS NULL").fetchone()[0]
        assert conn.execute("SELECT document_count FROM collections").fetchone()[0] == active
        assert conn.execute("SELECT document_count FROM domains WHERE path='software/auth'").fetchone()[0] == active
    finally:
        conn.close()
