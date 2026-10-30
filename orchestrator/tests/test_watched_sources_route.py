import pytest
def test_create_and_list_watched_source(test_client):
    r = test_client.post("/watched-sources", json={
        "type": "vault", "uri": "/vault", "noosphere": "ns",
        "cadence_hours": 12, "config_json": {"ext": [".md"]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "vault" and body["enabled"] == 1
    assert body["cadence_hours"] == 12 and body["config_json"] == {"ext": [".md"]}
    sid = body["id"]

    listing = test_client.get("/watched-sources").json()
    assert any(x["id"] == sid for x in listing)


def test_patch_toggles_enabled_and_cadence(test_client):
    sid = test_client.post("/watched-sources", json={"type": "repo", "uri": "/repo"}).json()["id"]
    r = test_client.patch(f"/watched-sources/{sid}", json={"enabled": False, "cadence_hours": 6})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] == 0 and body["cadence_hours"] == 6


def test_patch_unknown_source_404(test_client):
    assert test_client.patch("/watched-sources/nope", json={"enabled": False}).status_code == 404


def _scan_jobs(store):
    return [j for j in store.jobs.list() if j.type == "scan_source"]


def test_scan_trigger_enqueues_job_and_is_idempotent(test_client, test_store):
    sid = test_client.post("/watched-sources", json={"type": "vault", "uri": "/v"}).json()["id"]

    r = test_client.post(f"/watched-sources/{sid}/scan")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued" and r.json()["source_id"] == sid
    jobs = _scan_jobs(test_store)
    assert len(jobs) == 1 and jobs[0].target == sid

    # A second trigger while one is pending must not pile on a duplicate.
    r2 = test_client.post(f"/watched-sources/{sid}/scan")
    assert r2.json()["status"] == "already_pending"
    assert len(_scan_jobs(test_store)) == 1


def test_scan_trigger_unknown_source_404(test_client):
    assert test_client.post("/watched-sources/nope/scan").status_code == 404


def test_scan_due_triggers_only_due_sources(test_client, test_store):
    due = test_client.post("/watched-sources", json={"type": "vault", "uri": "/due"}).json()["id"]
    recent = test_client.post("/watched-sources",
                              json={"type": "vault", "uri": "/recent", "cadence_hours": 24}).json()["id"]
    test_store.conn.execute("UPDATE watched_sources SET last_scanned_at = datetime('now') WHERE id = ?", (recent,))
    test_store.conn.commit()

    triggered = {t["source_id"] for t in test_client.post("/watched-sources/scan-due").json()["triggered"]}
    assert due in triggered and recent not in triggered      # only the due one

    # force=true ignores cadence and covers all enabled sources.
    forced = {t["source_id"] for t in test_client.post("/watched-sources/scan-due?force=true").json()["triggered"]}
    assert due in forced and recent in forced


def _mk(test_client, **cfg):
    body = {"type": "horton", "uri": "http://clover:8080"}
    if cfg:
        body["config_json"] = cfg
    return test_client.post("/watched-sources", json=body).json()["id"]


def test_patch_config_merges_and_keeps_other_keys(test_client):
    sid = _mk(test_client, keep="me", other=1)
    r = test_client.patch(f"/watched-sources/{sid}", json={"config": {"min_window_start": "2026-10-26T19:15:00Z", "other": 2}})
    assert r.status_code == 200, r.text
    assert r.json()["config_json"] == {"keep": "me", "other": 2, "min_window_start": "2026-10-26T19:15:00Z"}
    listed = [x for x in test_client.get("/watched-sources").json() if x["id"] == sid][0]
    assert listed["config_json"]["keep"] == "me"


def test_patch_config_on_source_without_config(test_client):
    sid = _mk(test_client)
    r = test_client.patch(f"/watched-sources/{sid}", json={"config": {"a": 1}})
    assert r.json()["config_json"] == {"a": 1}


def test_patch_config_null_removes_key(test_client):
    sid = _mk(test_client, keep=1, drop=2)
    r = test_client.patch(f"/watched-sources/{sid}", json={"config": {"drop": None}})
    assert r.json()["config_json"] == {"keep": 1}


def test_patch_config_with_enabled_and_cadence(test_client):
    sid = _mk(test_client, keep=1)
    r = test_client.patch(f"/watched-sources/{sid}", json={
        "config": {"min_window_start": "T"}, "cadence_hours": 0.0333, "enabled": False})
    body = r.json()
    assert body["enabled"] == 0 and body["cadence_hours"] == 0.0333
    assert body["config_json"] == {"keep": 1, "min_window_start": "T"}


def test_patch_without_config_leaves_config_alone(test_client):
    sid = _mk(test_client, keep=1)
    r = test_client.patch(f"/watched-sources/{sid}", json={"enabled": False})
    assert r.json()["config_json"] == {"keep": 1}


@pytest.mark.parametrize("raw", ["not json{", "[1, 2]", '"str"'])
def test_patch_config_refuses_to_clobber_unreadable_config(test_client, test_store, raw):
    sid = _mk(test_client)
    test_store.conn.execute("UPDATE watched_sources SET config_json = ? WHERE id = ?", (raw, sid))
    test_store.conn.commit()
    r = test_client.patch(f"/watched-sources/{sid}", json={"config": {"a": 1}, "enabled": False})
    assert r.status_code == 422
    row = test_store.conn.execute("SELECT config_json, enabled FROM watched_sources WHERE id=?", (sid,)).fetchone()
    assert row[0] == raw and row[1] == 1  # untouched, nothing partially applied
