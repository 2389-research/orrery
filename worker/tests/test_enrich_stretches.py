# ABOUTME: enrich_stretches job — REAL split_document + stretches table; only the relay is faked.
# ABOUTME: Verifies upsert, content_hash idempotency, no graph mutation, and config scoping.

import json
import re
import uuid

import pytest

from src.db import get_connection
from src.jobs import enrich_stretches
from src.jobs.enrich_stretches import run_enrich_stretches

WORDS = " ".join(f"word{i}" for i in range(20))


class CountingRelay:
    calls = 0

    @classmethod
    def from_settings(cls, settings, **overrides):
        return cls()

    async def complete(self, model, max_tokens, messages, **kwargs):
        return None


@pytest.fixture
def fake_enrich(monkeypatch):
    state = {"calls": 0}

    async def fake(relay, model, stretches):
        state["calls"] += 1
        return {s["stretch_id"]: {"summary": f"sum {s['start']}", "concepts": [{"tag": "t", "statement": "s"}],
                                  "ideas": [], "nouns": ["n"]} for s in stretches}

    monkeypatch.setattr(enrich_stretches, "Relay", CountingRelay)
    monkeypatch.setattr(enrich_stretches, "enrich_batch", fake)
    return state


def seed(conn, n=2, silo="coll1", metadata=True):
    ids = []
    for i in range(n):
        did = str(uuid.uuid4())
        content = f"[10:0{i}:00] {WORDS}\n[10:0{i}:10] {WORDS}"
        conn.execute(
            "INSERT INTO documents (id, title, content, content_hash, source_path, status, content_type, silo_id, metadata) "
            "VALUES (?, 't', ?, ?, ?, 'pending', 'transcript', ?, ?)",
            (did, content, f"h{i}", f"horton:chunk/MIC{i}/2026-10-15T10:0{i}:00Z", silo,
             json.dumps({"day": "2026-10-15", "window_start": "2026-10-15T17:00:00Z"}) if metadata else None))
        ids.append(did)
    conn.commit()
    return ids


def make_job(conn, config):
    jid = str(uuid.uuid4())
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'enrich_stretches', 'x', 'running', ?)",
                 (jid, json.dumps(config)))
    conn.commit()
    return {"id": jid, "config": json.dumps(config)}


async def run(test_db, config=None):
    conn = get_connection(test_db)
    job = make_job(conn, config or {})
    conn.close()
    await run_enrich_stretches(job, test_db)
    conn = get_connection(test_db)
    return conn, json.loads(conn.execute("SELECT result FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])


@pytest.mark.asyncio
async def test_rows_written_with_facets(test_db, fake_enrich):
    conn = get_connection(test_db)
    ids = seed(conn)
    conn.close()
    conn, result = await run(test_db)
    rows = conn.execute('SELECT doc_id, mic_id, silo_id, start, "end", summary, concepts, nouns FROM stretches').fetchall()
    assert len(rows) == 2 and result["enriched"] == 2 and result["docs"] == 2
    by_doc = {r[0]: r for r in rows}
    assert set(by_doc) == set(ids)
    r = by_doc[ids[0]]
    assert r[1] == "MIC0" and r[2] == "coll1" and r[3] == "2026-10-15T10:00:00" and r[4] == "2026-10-15T10:00:10"
    assert r[5] == "sum 2026-10-15T10:00:00" and json.loads(r[6])[0]["tag"] == "t" and json.loads(r[7]) == ["n"]


@pytest.mark.asyncio
async def test_second_run_makes_no_model_calls(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn)
    conn.close()
    await run(test_db)
    first = fake_enrich["calls"]
    assert first >= 1
    _, result = await run(test_db)
    assert fake_enrich["calls"] == first
    assert result["skipped"] == 2 and result["enriched"] == 0


@pytest.mark.asyncio
async def test_graph_tables_unchanged(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn)
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1','X','concept')")
    conn.commit()
    tables = ("entities", "entity_sources", "relationships")
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    conn.close()
    conn, _ = await run(test_db)
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]
    assert before == after


@pytest.mark.asyncio
async def test_limit_caps_docs(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn, n=3)
    conn.close()
    conn, result = await run(test_db, {"limit": 1})
    assert result["docs"] == 1
    assert conn.execute("SELECT COUNT(DISTINCT doc_id) FROM stretches").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_collection_scopes(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn, n=1, silo="a")
    seed(conn, n=1, silo="b")
    conn.close()
    conn, result = await run(test_db, {"collection": "b"})
    assert result["docs"] == 1
    assert conn.execute("SELECT silo_id FROM stretches").fetchone()[0] == "b"


@pytest.mark.asyncio
async def test_stored_start_end_are_full_iso_datetimes(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn)
    conn.close()
    conn, _ = await run(test_db)
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    rows = conn.execute('SELECT start, "end" FROM stretches').fetchall()
    assert rows and all(iso.match(a) and iso.match(b) for a, b in rows)


@pytest.mark.asyncio
async def test_doc_without_day_is_doc_error_others_continue(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn, n=1, silo="a", metadata=False)
    seed(conn, n=1, silo="a")
    conn.close()
    conn, result = await run(test_db)
    assert result["doc_errors"] == 1 and result["enriched"] == 1


@pytest.mark.asyncio
async def test_exception_in_one_doc_does_not_abort_job(test_db, monkeypatch):
    calls = {"n": 0}

    async def flaky(relay, model, stretches):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {s["stretch_id"]: {"summary": "x", "concepts": [], "ideas": [], "nouns": []} for s in stretches}

    monkeypatch.setattr(enrich_stretches, "Relay", CountingRelay)
    monkeypatch.setattr(enrich_stretches, "enrich_batch", flaky)
    conn = get_connection(test_db)
    seed(conn, n=2)
    conn.close()
    conn, result = await run(test_db)
    assert result["doc_errors"] == 1 and result["enriched"] == 1 and result["docs"] == 2


@pytest.mark.asyncio
async def test_force_bypasses_content_hash_skip(test_db, fake_enrich):
    conn = get_connection(test_db)
    seed(conn)
    conn.close()
    await run(test_db)
    first = fake_enrich["calls"]
    _, result = await run(test_db, {"force": True})
    assert fake_enrich["calls"] > first
    assert result["enriched"] == 2 and result["skipped"] == 0
