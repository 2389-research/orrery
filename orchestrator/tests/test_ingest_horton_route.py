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


def test_one_shot_blocked_when_watched_horton_source_has_same_uri(test_client, test_store):
    test_client.post("/watched-sources", json={"type": "horton", "uri": "http://clover:8080/"})
    r = test_client.post("/ingest/horton", json={"server_url": "http://clover:8080"})
    assert r.status_code == 409
    assert "watched source" in r.json()["detail"] and "cutoff" in r.json()["detail"]
    assert test_store.conn.execute("SELECT COUNT(*) FROM jobs WHERE type='ingest_horton'").fetchone()[0] == 0


def test_one_shot_allowed_for_other_uri_or_other_type(test_client):
    test_client.post("/watched-sources", json={"type": "horton", "uri": "http://clover:8080"})
    test_client.post("/watched-sources", json={"type": "vault", "uri": "http://other:1"})
    assert test_client.post("/ingest/horton", json={"server_url": "http://elsewhere:8080"}).status_code == 202
    assert test_client.post("/ingest/horton", json={"server_url": "http://other:1"}).status_code == 202
