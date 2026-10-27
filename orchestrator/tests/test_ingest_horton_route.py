import json


def test_ingest_horton_enqueues_job(test_client, test_store):
    r = test_client.post("/ingest/horton", json={"server_url": "http://x:8080"})
    assert r.status_code == 202
    assert set(r.json()) == {"job_id"}
    row = test_store.conn.execute(
        "SELECT config FROM jobs WHERE type='ingest_horton'").fetchone()
    assert row
    cfg = json.loads(row[0])
    assert cfg["server_url"] == "http://x:8080" and cfg["spec_id"]


def test_ingest_horton_does_not_create_collection(test_client, test_store):
    test_client.post("/ingest/horton", json={})
    assert test_store.conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
