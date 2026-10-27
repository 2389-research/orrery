# ABOUTME: ingest_horton job — REAL upsert_document against a file-backed DB; only the relay
# ABOUTME: (LLM) and the HTTP feed are faked. Verifies routing, skip-on-unchanged, cursor discipline.

import json
import uuid
from types import SimpleNamespace

import pytest

from src.db import get_connection
from src.jobs import ingest_horton
from src.jobs.ingest_horton import get_or_create_collection, run_ingest_horton

URL = "http://horton.test"
MIC = "AA:BB:CC:DD:EE:01"


class FakeRelay:
    @classmethod
    def from_settings(cls, settings, **overrides):
        return cls()

    async def complete(self, model, max_tokens, messages, **kwargs):
        return SimpleNamespace(text='{"entities": []}')

    async def complete_structured(self, model, max_tokens, messages, schema=None, **kwargs):
        return {"primary_domain": "people/conversation", "secondary_domains": [],
                "new_domains": [], "confidence": 0.9}


def chunk(n, day="2026-10-26", body=None):
    return {
        "id": f"horton:chunk/{MIC}/{day}T19:0{n}:00Z", "revision": 1, "body": body or f"[14:0{n}:00] line {n}",
        "day": day, "folder": f"horton/{MIC}/{day}", "complete": True, "gaps": [], "line_count": 1,
        "mic": {"id": MIC, "name": "desk"}, "window": {"start": f"{day}T19:0{n}:00Z", "end": f"{day}T19:1{n}:00Z"},
        "source": "mqtt",
    }


@pytest.fixture
def feed(monkeypatch):
    state = {"items": [], "cursor": "c1"}
    monkeypatch.setattr(ingest_horton, "Relay", FakeRelay)
    monkeypatch.setattr(ingest_horton, "make_fetch", lambda url: None)
    monkeypatch.setattr(ingest_horton, "drain_chunks",
                        lambda fetch, after: (list(state["items"]), state["cursor"], False))
    return state


def make_job(conn):
    jid = str(uuid.uuid4())
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'ingest_horton', 'x', 'running', ?)",
                 (jid, json.dumps({"server_url": URL})))
    conn.commit()
    return {"id": jid, "config": json.dumps({"server_url": URL})}


async def run(test_db):
    conn = get_connection(test_db)
    job = make_job(conn)
    conn.close()
    await run_ingest_horton(job, test_db)
    conn = get_connection(test_db)
    result = json.loads(conn.execute("SELECT result FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
    return conn, result


@pytest.mark.asyncio
async def test_late_revised_chunk_routes_to_its_original_day_collection(test_db, feed):
    feed["items"] = [chunk(1, "2026-10-26"), chunk(2, "2026-10-15")]
    conn, result = await run(test_db)
    paths = {r[0] for r in conn.execute("SELECT path FROM collections")}
    assert paths == {f"horton/{MIC}/2026-10-26", f"horton/{MIC}/2026-10-15"}
    assert result["created"] == 2


@pytest.mark.asyncio
async def test_cursor_held_when_a_chunk_fails(test_db, feed):
    bad = chunk(2)
    del bad["id"]  # chunk_to_doc raises KeyError
    feed["items"] = [chunk(1), bad, chunk(3)]
    conn, result = await run(test_db)
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone() is None
    assert result["failed"] == 1 and result["created"] == 2  # drain not aborted


@pytest.mark.asyncio
async def test_cursor_advances_on_clean_pass(test_db, feed):
    feed["items"] = [chunk(1)]
    conn, _ = await run(test_db)
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone()[0] == "c1"


@pytest.mark.asyncio
async def test_unchanged_chunk_on_second_pass_is_skipped(test_db, feed):
    feed["items"] = [chunk(1)]
    await run(test_db)
    conn, result = await run(test_db)
    assert result["skipped"] == 1 and result["created"] == 0 and result["updated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_revised_chunk_updates_in_place(test_db, feed):
    feed["items"] = [chunk(1)]
    await run(test_db)
    feed["items"] = [chunk(1, body="[14:01:00] revised")]
    conn, result = await run(test_db)
    assert result["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_collection_created_with_horton_kind_and_provenance(test_db, feed):
    feed["items"] = [chunk(1)]
    conn, _ = await run(test_db)
    row = conn.execute("SELECT kind, provenance_kind, remote_url FROM collections").fetchone()
    assert tuple(row) == ("horton", "horton_transcript", URL)
    assert conn.execute("SELECT COUNT(*) FROM document_collections").fetchone()[0] == 1


def test_get_or_create_collection_is_idempotent_and_cached(test_db):
    conn = get_connection(test_db)
    cache = {}
    cid1 = get_or_create_collection(conn, MIC, "2026-10-15", URL, cache)
    cid2 = get_or_create_collection(conn, MIC, "2026-10-15", URL, cache)
    cid3 = get_or_create_collection(conn, MIC, "2026-10-15", URL, {})  # cold cache -> re-select
    assert cid1 == cid2 == cid3
    assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 1
    assert cache[(MIC, "2026-10-15")] == cid1


class RaisingRelay(FakeRelay):
    """Extraction (complete) raises when the prompt contains the poison marker."""
    async def complete(self, model, max_tokens, messages, **kwargs):
        if "POISON" in json.dumps(messages):
            raise RuntimeError("extraction boom")
        return await super().complete(model, max_tokens, messages, **kwargs)


@pytest.mark.asyncio
async def test_failed_chunk_is_rolled_back_and_retried_next_pass(test_db, feed, monkeypatch):
    feed["items"] = [chunk(1), chunk(2, body="[14:02:00] POISON"), chunk(3)]
    monkeypatch.setattr(ingest_horton, "Relay", RaisingRelay)
    conn, result = await run(test_db)
    assert result["failed"] == 1 and result["created"] == 2
    titles = [r[0] for r in conn.execute("SELECT source_path FROM documents")]
    assert len(titles) == 2 and not any("19:02:00" in t for t in titles)
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone() is None

    monkeypatch.setattr(ingest_horton, "Relay", FakeRelay)
    conn, result = await run(test_db)
    assert result["failed"] == 0 and result["created"] == 1 and result["skipped"] == 2
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    assert any("19:02:00" in r[0] for r in conn.execute("SELECT source_path FROM documents"))
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone()[0] == "c1"


@pytest.mark.asyncio
async def test_collection_document_count_is_recounted(test_db, feed):
    feed["items"] = [chunk(1), chunk(2), chunk(3, "2026-10-15")]
    conn, _ = await run(test_db)
    rows = conn.execute("SELECT id, document_count FROM collections").fetchall()
    assert sorted(r[1] for r in rows) == [1, 2]
    for cid, n in rows:
        assert n == conn.execute("SELECT COUNT(*) FROM document_collections WHERE collection_id=?", (cid,)).fetchone()[0]


@pytest.mark.asyncio
async def test_chunk_missing_mic_or_day_counts_failed(test_db, feed, monkeypatch):
    monkeypatch.setattr(ingest_horton, "chunk_to_doc",
                        lambda item: {"mic_id": None, "day": None, "source_path": "x", "title": "x",
                                      "content": "x", "parent_path": "x", "metadata": {}})
    feed["items"] = [chunk(1)]
    conn, result = await run(test_db)
    assert result["failed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
