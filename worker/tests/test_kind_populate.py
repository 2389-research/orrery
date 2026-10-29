# ABOUTME: provenance_kind stamped at collection-creation time (spec: per-source silos
# ABOUTME: + provenance, task 9). Mirrors test_silo_populate.py's tmp-DB pattern.

from src.db import init_db, get_connection, backfill_provenance_kind
from src.silo import KINDS, FLOW_DEFAULT_KIND, flow_default_kind, resolve_kind


def test_flow_default_kind_and_resolve_kind():
    assert flow_default_kind("vault") == "human_vault"
    assert flow_default_kind("git_repo") == "neutral_summary"
    assert flow_default_kind("tracker_run") == "neutral_summary"
    assert flow_default_kind("nope") is None
    assert resolve_kind("neutral_summary", None) == "neutral_summary"
    assert resolve_kind("neutral_summary", "agent_report") == "agent_report"
    assert resolve_kind("neutral_summary", "garbage") == "neutral_summary"
    assert KINDS == {"neutral_summary", "human_vault", "agent_report", "human_reviewed",
                     "horton_transcript"}


def test_resolve_collection_stamps_flow_default_for_git_repo(tmp_path):
    """_resolve_collection is the worker's collection-create helper for a `repo`
    watched source (sync_repo.py) — its collections get 'git_repo' -> 'neutral_summary'."""
    from src.jobs.sync_repo import _resolve_collection

    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    collection_id, _path = _resolve_collection(conn, "src1", "/tmp/some-repo", {})
    row = conn.execute(
        "SELECT kind, provenance_kind FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()
    assert row["kind"] == "git_repo"
    assert row["provenance_kind"] == "neutral_summary"


def test_resolve_collection_honors_config_override(tmp_path):
    from src.jobs.sync_repo import _resolve_collection

    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    collection_id, _path = _resolve_collection(
        conn, "src2", "/tmp/other-repo", {"provenance_kind": "human_reviewed"})
    row = conn.execute(
        "SELECT provenance_kind FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()
    assert row["provenance_kind"] == "human_reviewed"


def test_backfill_provenance_kind_fills_existing_null_rows(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    conn.execute(
        "INSERT INTO watched_sources (id, type, uri, provenance_kind) "
        "VALUES ('ws1', 'vault', '/tmp/v', NULL)")
    conn.execute(
        "INSERT INTO collections (id, name, path, kind, provenance_kind) "
        "VALUES ('c1', 'r', 'r', 'tracker_run', NULL)")
    conn.commit()
    backfill_provenance_kind(conn)
    assert conn.execute(
        "SELECT provenance_kind FROM watched_sources WHERE id = 'ws1'"
    ).fetchone()[0] == "human_vault"
    assert conn.execute(
        "SELECT provenance_kind FROM collections WHERE id = 'c1'"
    ).fetchone()[0] == "neutral_summary"


def test_silo_kind_view(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    conn.execute(
        "INSERT INTO watched_sources (id, type, uri, provenance_kind) "
        "VALUES ('ws2', 'vault', '/tmp/v2', 'human_vault')")
    conn.commit()
    row = conn.execute(
        "SELECT kind FROM silo_kind WHERE silo_id = 'ws2'").fetchone()
    assert row["kind"] == "human_vault"
