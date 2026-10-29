# ABOUTME: Tests for ingest_meridian classification lifecycle — classify on backfill, reclassify exactly
# ABOUTME: once when the session settles to `ended`, migrating the whole tree + domain counts.

import asyncio

import src.jobs.ingest_meridian as mod
from src.db import get_connection
from tests.test_ingest_meridian import _setup, _rec, MAIN, SK, _Resp

BIG = "x" * 3100


class _Relay:
    @classmethod
    def from_settings(cls, settings, **kw): return cls()

    async def complete(self, model, max_tokens, messages):
        return _Resp("Neutral summary.")


class _Endable:
    def __init__(self):
        self.revision = 2
        self.lifecycle = "active"
        self.records = [_rec(f"r{i}", "main", f"{i}{BIG}", f"2026-10-01T00:00:{i:02d}Z") for i in range(2)]

    def bump(self, lifecycle=None):
        self.revision += 1
        if lifecycle:
            self.lifecycle = lifecycle

    def fetch(self, path, params=None):
        session = {"session_key": SK, "revision": self.revision, "contributor": "n", "cwd": "/r",
                   "git_branch": "b", "lifecycle": self.lifecycle, "started_at": "t0",
                   "last_activity_at": "t1", "rollup": {}}
        if path == "/v1/sessions":
            return {"items": [session], "has_more": False, "next_cursor": f"cur-{self.revision}"}
        if path.endswith("/agents"):
            return {"items": [MAIN], "has_more": False}
        if path.endswith("/records"):
            return {"items": list(self.records), "has_more": False}
        return session


def test_reclassifies_once_when_session_ends(monkeypatch, test_db):
    g = _Endable()
    _, job = _setup(monkeypatch, test_db, g.fetch, relay=_Relay)
    calls = []

    async def spy(relay, title, excerpt, existing_taxonomy, model):
        calls.append(title)
        return {"primary_domain": "software/auth" if len(calls) == 1 else "business/ops",
                "secondary_domains": []}
    monkeypatch.setattr(mod, "classify_document", spy)

    asyncio.run(mod.run_ingest_meridian(job, test_db))   # backfill, active -> classify #1
    assert len(calls) == 1
    g.bump()                                              # still active bump: no reclassify
    asyncio.run(mod.run_ingest_meridian(job, test_db))
    assert len(calls) == 1
    g.bump("ended")
    asyncio.run(mod.run_ingest_meridian(job, test_db))   # ended -> reclassify #2
    assert len(calls) == 2
    g.bump()
    asyncio.run(mod.run_ingest_meridian(job, test_db))   # still ended -> no reclassify
    assert len(calls) == 2

    conn = get_connection(test_db)
    try:
        assert conn.execute("SELECT classified_final FROM meridian_sessions").fetchone()[0] == 1
        doms = {r[0] for r in conn.execute("SELECT domain_path FROM document_domains")}
        assert doms == {"business/ops"}
        active = conn.execute("SELECT COUNT(*) FROM documents WHERE invalid_at IS NULL").fetchone()[0]
        assert conn.execute("SELECT document_count FROM domains WHERE path='business/ops'").fetchone()[0] == active
        assert conn.execute("SELECT document_count FROM domains WHERE path='software/auth'").fetchone()[0] == 0
    finally:
        conn.close()
