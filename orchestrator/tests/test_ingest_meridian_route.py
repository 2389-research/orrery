import json

import pytest


def test_route_creates_collection_and_queues_job(test_client, test_store):
    r = test_client.post("/ingest/meridian", json={"server_url": "http://x"})
    assert r.status_code == 202 and set(r.json()) == {"job_id", "collection_id"}
    cid = r.json()["collection_id"]
    conn = test_store.conn
    job = conn.execute("SELECT config FROM jobs WHERE type='ingest_meridian'").fetchone()
    assert job
    cfg = json.loads(job[0])
    assert cfg["collection_id"] == cid and cfg["spec_id"] and cfg["server_url"] == "http://x"
    assert conn.execute("SELECT 1 FROM meridian_cursor WHERE collection_id=?", (cid,)).fetchone()
    kind = conn.execute("SELECT kind FROM silo_kind WHERE silo_id=?", (cid,)).fetchone()
    assert kind and kind[0] == "agent_report"


def test_route_is_idempotent_on_the_collection(test_client, test_store):
    a = test_client.post("/ingest/meridian", json={}).json()
    b = test_client.post("/ingest/meridian", json={}).json()
    assert a["collection_id"] == b["collection_id"]


def test_enqueue_failure_removes_created_collection_and_cursor(test_client, test_store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("enqueue failed")
    monkeypatch.setattr(type(test_store.jobs), "create", boom)
    from fastapi.testclient import TestClient
    from src.main import app
    with pytest.raises(RuntimeError):
        TestClient(app, raise_server_exceptions=True).post("/ingest/meridian", json={})
    conn = test_store.conn
    assert conn.execute("SELECT COUNT(*) FROM meridian_cursor").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
