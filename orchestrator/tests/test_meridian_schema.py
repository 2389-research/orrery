from src.db import get_connection


def test_meridian_tables_exist(test_db):
    conn = get_connection(test_db)
    for t in ("meridian_sessions", "meridian_segments", "meridian_cursor"):
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()


def test_meridian_flow_kind_is_agent_report():
    from src.pipeline.silo import flow_default_kind
    assert flow_default_kind("meridian") == "agent_report"


def test_meridian_request_defaults():
    from src.models import MeridianIngestRequest
    r = MeridianIngestRequest(workspace_id="w")
    assert r.server_url == "http://localhost:8080"
    assert r.name == "Code Sessions"
