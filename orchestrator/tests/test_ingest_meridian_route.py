def test_route_creates_collection_and_queues_job(test_client, test_store):
    r = test_client.post("/ingest/meridian", json={"workspace_id": "default", "server_url": "http://x"})
    assert r.status_code == 202 and set(r.json()) == {"job_id", "collection_id"}
    assert test_store.conn.execute("SELECT 1 FROM jobs WHERE type='ingest_meridian'").fetchone()


def test_route_is_idempotent_on_the_collection(test_client, test_store):
    a = test_client.post("/ingest/meridian", json={"workspace_id": "default"}).json()
    b = test_client.post("/ingest/meridian", json={"workspace_id": "default"}).json()
    assert a["collection_id"] == b["collection_id"]
