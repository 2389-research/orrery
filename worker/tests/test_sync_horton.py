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


async def _run(conn, ws, sid, source_config=None):
    from src.config import get_settings
    return await sync_horton(conn, FakeRelay(), get_settings(), ws, source_config or {}, sid)


@pytest.mark.asyncio
async def test_min_window_start_in_source_config_skips_earlier_chunks(test_db, feed):
    feed["items"] = [chunk(1, "2026-10-26"), chunk(2, "2026-10-26")]  # starts 19:01, 19:02
    conn = get_connection(test_db)
    sid, ws = make_ws(conn)
    out = await _run(conn, ws, sid, {"min_window_start": "2026-10-26T19:02:00Z"})
    assert out["actions"]["before_cutoff"] == 1 and out["actions"]["created"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", None])
async def test_empty_min_window_start_means_no_cutoff(test_db, feed, empty):
    feed["items"] = [chunk(1, "2026-10-26"), chunk(2, "2026-10-26")]
    conn = get_connection(test_db)
    sid, ws = make_ws(conn)
    out = await _run(conn, ws, sid, {"min_window_start": empty})
    assert out["actions"]["created"] == 2 and out["actions"].get("before_cutoff", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, False])
async def test_falsy_non_empty_cutoff_is_not_treated_as_unset(test_db, feed, bad):
    feed["items"] = [chunk(1, "2026-10-26")]
    conn = get_connection(test_db)
    sid, ws = make_ws(conn)
    with pytest.raises(ValueError, match="min_window_start"):
        await _run(conn, ws, sid, {"min_window_start": bad})
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_logs_effective_cutoff_and_warns_on_unknown_key(test_db, feed, capsys):
    feed["items"] = []
    conn = get_connection(test_db)
    sid, ws = make_ws(conn)
    await _run(conn, ws, sid, {"min_window_start": "2026-10-26T19:02:00Z", "minWindowStart ": "x"})
    out = capsys.readouterr().out
    assert "min_window_start='2026-10-26T19:02:00Z'" in out
    assert "unknown" in out.lower() and "minWindowStart " in out
    await _run(conn, ws, sid, {})
    assert "min_window_start=None" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_failed_chunks_raise_after_ingest(test_db, feed):
    bad = chunk(2, "2026-10-26")
    del bad["id"]
    feed["items"] = [chunk(1, "2026-10-26"), bad]
    conn = get_connection(test_db)
    sid, ws = make_ws(conn)
    with pytest.raises(RuntimeError, match="1 horton chunk"):
        await _run(conn, ws, sid)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1  # others ingested
    assert conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (URL,)).fetchone() is None


def _scan_ws(conn, config_json):
    conn.execute("INSERT INTO watched_sources (id, type, uri, noosphere, provenance_kind, config_json) "
                 "VALUES ('h1', 'horton', ?, 'default', 'horton_transcript', ?)", (URL, config_json))
    conn.commit()


async def _scan(test_db):
    import json
    from src.jobs import scan_source
    job = {"id": str(uuid.uuid4()), "type": "scan_source", "target": "h1",
           "config": json.dumps({"source_id": "h1"})}
    await scan_source.run_scan_source(job, test_db)


@pytest.mark.asyncio
async def test_run_scan_source_applies_config_json_cutoff(test_db, feed, monkeypatch):
    import json
    from src.jobs import scan_source
    monkeypatch.setattr(scan_source, "Relay", type("R", (FakeRelay,), {
        "from_settings": classmethod(lambda cls, settings, **kw: cls())}))
    feed["items"] = [chunk(1, "2026-10-26"), chunk(2, "2026-10-26"), chunk(3, "2026-10-26")]
    conn = get_connection(test_db)
    _scan_ws(conn, json.dumps({"min_window_start": "2026-10-26T19:02:00Z"}))
    await _scan(test_db)
    conn = get_connection(test_db)
    paths = sorted(r[0] for r in conn.execute("SELECT source_path FROM documents"))
    assert len(paths) == 2 and not any("19:01:00" in p for p in paths)
    row = conn.execute("SELECT last_status, last_error FROM watched_sources WHERE id='h1'").fetchone()
    assert row[0] == "ok" and row[1] is None


@pytest.mark.asyncio
async def test_run_scan_source_bad_cutoff_records_error_naming_key(test_db, feed, monkeypatch):
    import json
    from src.jobs import scan_source
    monkeypatch.setattr(scan_source, "Relay", type("R", (FakeRelay,), {
        "from_settings": classmethod(lambda cls, settings, **kw: cls())}))
    feed["items"] = [chunk(1, "2026-10-26")]
    conn = get_connection(test_db)
    _scan_ws(conn, json.dumps({"min_window_start": "2026-10-30"}))
    with pytest.raises(ValueError):
        await _scan(test_db)
    conn = get_connection(test_db)
    row = conn.execute("SELECT last_status, last_error FROM watched_sources WHERE id='h1'").fetchone()
    assert row[0] == "error" and "min_window_start" in row[1]
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
