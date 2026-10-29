# ABOUTME: horton watched-source syncer — real upsert_document on a file-backed DB; only the relay
# ABOUTME: and the HTTP feed are faked. Verifies source ownership, cursor, enrich enqueue, no deletions.

import uuid
from types import SimpleNamespace

import pytest

from src.db import get_connection
from src.jobs import ingest_horton
from src.jobs.scan_source import _resolve_syncer
from src.jobs.sync_horton import sync_horton

URL = "http://clover:8080"
MIC = "AA:BB:CC:DD:EE:01"


class FakeRelay:
    async def complete(self, model, max_tokens, messages, **kwargs):
        return SimpleNamespace(text='{"entities": []}')

    async def complete_structured(self, model, max_tokens, messages, schema=None, **kwargs):
        return {"primary_domain": "people/conversation", "secondary_domains": [],
                "new_domains": [], "confidence": 0.9}


def chunk(n, day):
    return {
        "id": f"horton:chunk/{MIC}/{day}T19:0{n}:00Z", "revision": 1, "body": f"[14:0{n}:00] line {n}",
        "day": day, "folder": f"horton/{MIC}/{day}", "complete": True, "gaps": [], "line_count": 1,
        "mic": {"id": MIC, "name": "desk"}, "window": {"start": f"{day}T19:0{n}:00Z", "end": f"{day}T19:1{n}:00Z"},
        "source": "mqtt",
    }


@pytest.fixture
def feed(monkeypatch):
    state = {"items": [], "cursor": "c1"}
    monkeypatch.setattr(ingest_horton, "make_fetch", lambda url: None)
    monkeypatch.setattr(ingest_horton, "drain_chunks",
                        lambda fetch, after: (list(state["items"]), state["cursor"], False))
    return state


def make_ws(conn):
    sid = str(uuid.uuid4())
    conn.execute("INSERT INTO watched_sources (id, type, uri, noosphere, provenance_kind) "
                 "VALUES (?, 'horton', ?, 'default', 'horton_transcript')", (sid, URL))
    conn.commit()
    return sid, conn.execute("SELECT * FROM watched_sources WHERE id=?", (sid,)).fetchone()


def test_dispatch_resolves_horton():
    assert _resolve_syncer("horton") is sync_horton


@pytest.mark.asyncio
async def test_sync_owns_docs_advances_cursor_and_enqueues_enrich(test_db, feed):
    feed["items"] = [chunk(1, "2026-10-26"), chunk(2, "2026-10-15")]
    conn = get_connection(test_db)
    sid, ws = make_ws(conn)

    out = await _run(conn, ws, sid)

    assert out["deleted"] == 0 and out["actions"]["created"] == 2
    paths = {r[0] for r in conn.execute("SELECT path FROM collections")}
    assert paths == {f"horton/{MIC}/2026-10-26", f"horton/{MIC}/2026-10-15"}
    owners = [r[0] for r in conn.execute("SELECT source_id FROM documents")]
    assert owners == [sid, sid]
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone()[0] == "c1"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='enrich_stretches' AND status='queued'").fetchone()[0] == 1

    # idempotent: second pass skips, no new enrich job, still no deletions
    out2 = await _run(conn, ws, sid)
    assert out2["actions"]["created"] == 0 and out2["actions"]["skipped"] == 2 and out2["deleted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='enrich_stretches'").fetchone()[0] == 1


async def _run(conn, ws, sid):
    from src.config import get_settings
    return await sync_horton(conn, FakeRelay(), get_settings(), ws, {}, sid)
