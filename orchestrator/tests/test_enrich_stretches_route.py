import json


def test_enrich_stretches_enqueues_job(test_client, test_store):
    r = test_client.post("/enrich/stretches", json={"limit": 50},
                         headers={"X-Workspace-Id": "default"})
    assert r.status_code == 202
    assert set(r.json()) == {"job_id"}
    row = test_store.conn.execute(
        "SELECT config, status FROM jobs WHERE type='enrich_stretches'").fetchone()
    assert row
    cfg = json.loads(row[0])
    assert cfg["limit"] == 50
    assert cfg["collection"] is None and cfg["since"] is None and cfg["until"] is None
    assert row[1] == "queued"
