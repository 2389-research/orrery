# ABOUTME: POST /normalize/connect-within-kind enqueues the one-time backfill job.

def test_connect_within_kind_route_enqueues_job(test_client, test_store):
    r = test_client.post("/normalize/connect-within-kind")
    assert r.status_code == 202
    jid = r.json()["job_id"]
    row = test_store.conn.execute(
        "SELECT type, status FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row and row[0] == "connect_within_kind" and row[1] == "queued"
