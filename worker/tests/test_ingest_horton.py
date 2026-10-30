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


@pytest.mark.asyncio
async def test_enrich_job_enqueued_once_after_ingest_creates_docs(test_db, feed):
    feed["items"] = [chunk(1)]
    conn, _ = await run(test_db)
    rows = conn.execute("SELECT status FROM jobs WHERE type='enrich_stretches'").fetchall()
    assert [r[0] for r in rows] == ["queued"]
    feed["items"] = [chunk(2)]
    conn, _ = await run(test_db)  # one already pending -> no pile-up
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='enrich_stretches'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_scoped_pending_enrich_does_not_suppress_arrival_job(test_db, feed):
    conn = get_connection(test_db)
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'enrich_stretches', "
                 "'horton', 'queued', ?)", (str(uuid.uuid4()), json.dumps({"collection": "other"})))
    conn.commit()
    conn.close()
    feed["items"] = [chunk(1)]
    conn, _ = await run(test_db)
    cfgs = [r[0] for r in conn.execute("SELECT config FROM jobs WHERE type='enrich_stretches'")]
    assert sorted(cfgs) == sorted([json.dumps({"collection": "other"}), "{}"])


@pytest.mark.asyncio
async def test_unscoped_pending_enrich_suppresses_arrival_job(test_db, feed):
    conn = get_connection(test_db)
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'enrich_stretches', "
                 "'horton', 'running', ?)", (str(uuid.uuid4()), json.dumps({})))
    conn.commit()
    conn.close()
    feed["items"] = [chunk(1)]
    conn, _ = await run(test_db)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='enrich_stretches'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_no_enrich_job_when_nothing_ingested(test_db, feed):
    feed["items"] = []
    conn, _ = await run(test_db)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='enrich_stretches'").fetchone()[0] == 0


# ---- window-start cutoff (Clover 15 -> 5 minute switch) ----

def chunk_at(start, end=None, day="2026-10-26"):
    c = chunk(1, day)
    c["id"] = f"horton:chunk/{MIC}/{start}"
    c["body"] = f"[{start}] line"
    c["window"] = {"start": start, "end": end or start}
    return c


async def run_cutoff(test_db, cutoff, capsys=None):
    conn = get_connection(test_db)
    settings = ingest_horton.get_settings()
    out = await ingest_horton.ingest_horton_chunks(
        conn, FakeRelay(), settings, server_url=URL, cursor_key=URL, min_window_start=cutoff)
    return conn, out


@pytest.mark.asyncio
async def test_cutoff_skips_earlier_keeps_boundary_and_later(test_db, feed):
    feed["items"] = [chunk_at("2026-10-26T19:10:00Z"), chunk_at("2026-10-26T19:15:00Z"),
                     chunk_at("2026-10-26T19:20:00Z")]
    conn, out = await run_cutoff(test_db, "2026-10-26T19:15:00Z")
    assert out["actions"]["before_cutoff"] == 1 and out["actions"]["created"] == 2
    paths = sorted(r[0] for r in conn.execute("SELECT source_path FROM documents"))
    assert len(paths) == 2 and not any("19:10:00" in p for p in paths)
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone()[0] == "c1"


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", ["2026-10-26T19:15:00Z", "2026-10-26T19:15:00+00:00",
                                    "2026-10-26T19:15:00.000Z", "2026-10-26T19:15:00", "2026-10-26T20:15:00+01:00"])
@pytest.mark.parametrize("start,expected_skipped", [
    ("2026-10-26T19:15:00Z", 0), ("2026-10-26T19:15:00+00:00", 0), ("2026-10-26T19:15:00.000Z", 0),
    ("2026-10-26T19:14:59.999Z", 1), ("2026-10-26T19:14:59+00:00", 1)])
async def test_cutoff_format_equivalence(test_db, feed, cutoff, start, expected_skipped):
    feed["items"] = [chunk_at(start)]
    conn, out = await run_cutoff(test_db, cutoff)
    assert out["actions"]["before_cutoff"] == expected_skipped
    assert out["actions"]["created"] == 1 - expected_skipped


@pytest.mark.asyncio
async def test_all_before_cutoff_creates_nothing_no_enrich_cursor_advances(test_db, feed):
    feed["items"] = [chunk_at("2026-10-26T19:00:00Z"), chunk_at("2026-10-26T19:05:00Z")]
    conn, out = await run_cutoff(test_db, "2026-10-26T19:15:00Z")
    assert out["actions"]["before_cutoff"] == 2 and out["failed"] == 0
    assert out["actions"]["created"] == 0 and out["actions"]["updated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='enrich_stretches'").fetchone()[0] == 0
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone()[0] == "c1"


@pytest.mark.asyncio
async def test_unparseable_cutoff_raises_before_fetch_or_ingest(test_db, feed, monkeypatch):
    called = []
    monkeypatch.setattr(ingest_horton, "make_fetch", lambda url: called.append("fetch"))
    feed["items"] = [chunk_at("2026-10-26T19:20:00Z")]
    conn = get_connection(test_db)
    with pytest.raises(ValueError):
        await ingest_horton.ingest_horton_chunks(
            conn, FakeRelay(), ingest_horton.get_settings(), server_url=URL, cursor_key=URL,
            min_window_start="not-a-date")
    assert called == []
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone() is None


@pytest.mark.asyncio
async def test_missing_window_start_ingested_with_warning(test_db, feed, capsys):
    c = chunk_at("2026-10-26T19:00:00Z")
    c["window"] = {"end": "2026-10-26T19:05:00Z"}
    feed["items"] = [c]
    conn, out = await run_cutoff(test_db, "2026-10-26T19:15:00Z")
    assert out["actions"]["created"] == 1 and out["actions"]["before_cutoff"] == 0
    assert "warning" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_no_cutoff_ingests_everything(test_db, feed):
    feed["items"] = [chunk_at("2026-10-26T19:00:00Z"), chunk_at("2026-10-26T19:20:00Z")]
    conn, out = await run_cutoff(test_db, None)
    assert out["actions"]["created"] == 2 and out["actions"]["before_cutoff"] == 0
