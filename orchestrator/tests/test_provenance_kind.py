"""Provenance kind (spec: per-source silos + provenance, task 9).

`kind` is a property of the SOURCE (the silo), not the document: a user can
re-classify a source later, and a per-doc copy would go stale. So it lives on the silo
rows (watched_sources / collections) and is resolved for a document via the
`silo_kind` view (documents.silo_id -> silo_kind.silo_id) — never materialized
per-document.
"""
from src.db import init_db, get_connection, backfill_provenance_kind
from src.pipeline.silo import KINDS, FLOW_DEFAULT_KIND, flow_default_kind, resolve_kind


# ── resolve_kind / flow_default_kind ────────────────────────────────────────────────

def test_flow_default_kind_known_types():
    assert flow_default_kind("vault") == "human_vault"
    assert flow_default_kind("repo") == "neutral_summary"
    assert flow_default_kind("git_repo") == "neutral_summary"
    assert flow_default_kind("tracker_run") == "neutral_summary"


def test_flow_default_kind_unknown_returns_none():
    assert flow_default_kind("something_new") is None


def test_resolve_kind_uses_default_when_no_override():
    assert resolve_kind("human_vault", None) == "human_vault"


def test_resolve_kind_override_wins():
    assert resolve_kind("neutral_summary", "agent_report") == "agent_report"


def test_resolve_kind_invalid_override_falls_back_to_default():
    # Not a member of KINDS -> ignored, default wins.
    assert resolve_kind("neutral_summary", "not_a_real_kind") == "neutral_summary"
    assert resolve_kind("human_vault", "") == "human_vault"


def test_kinds_vocabulary():
    # horton_transcript added for office-mic transcripts (emits co-occurrence; see silo.py).
    assert KINDS == {"neutral_summary", "human_vault", "agent_report", "human_reviewed",
                     "horton_transcript"}


# ── schema ───────────────────────────────────────────────────────────────────────────

def test_watched_sources_and_collections_have_provenance_kind(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = get_connection(db)
    ws_cols = {r[1] for r in conn.execute("PRAGMA table_info(watched_sources)")}
    coll_cols = {r[1] for r in conn.execute("PRAGMA table_info(collections)")}
    assert "provenance_kind" in ws_cols
    assert "provenance_kind" in coll_cols


def test_silo_kind_view_returns_silo_id_and_kind(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO watched_sources (id, type, uri, provenance_kind) "
        "VALUES ('ws1', 'vault', '/tmp/v', 'human_vault')")
    conn.execute(
        "INSERT INTO collections (id, name, path, kind, provenance_kind) "
        "VALUES ('c1', 'repo1', 'repo1', 'git_repo', 'neutral_summary')")
    conn.commit()
    rows = {r["silo_id"]: r["kind"] for r in
            conn.execute("SELECT silo_id, kind FROM silo_kind").fetchall()}
    assert rows == {"ws1": "human_vault", "c1": "neutral_summary"}


# ── stamp at registration ───────────────────────────────────────────────────────────

def test_create_watched_source_stamps_flow_default_for_vault(test_client):
    resp = test_client.post("/watched-sources", json={"type": "vault", "uri": "/tmp/vault"})
    assert resp.status_code == 200
    assert resp.json()["provenance_kind"] == "human_vault"


def test_create_watched_source_stamps_flow_default_for_repo(test_client):
    resp = test_client.post("/watched-sources", json={"type": "repo", "uri": "/tmp/repo"})
    assert resp.status_code == 200
    assert resp.json()["provenance_kind"] == "neutral_summary"


def test_create_watched_source_override_wins(test_client):
    resp = test_client.post("/watched-sources", json={
        "type": "vault", "uri": "/tmp/vault2", "provenance_kind": "agent_report"})
    assert resp.status_code == 200
    assert resp.json()["provenance_kind"] == "agent_report"


def test_collections_create_stamps_flow_default(test_store):
    cid = test_store.collections.create("c2", "name", "path2", "/tmp/root")
    row = test_store._conn.execute(
        "SELECT provenance_kind FROM collections WHERE id = ?", (cid,)).fetchone()
    assert row["provenance_kind"] == "neutral_summary"  # default kind is git_repo


def test_collections_create_override_wins(test_store):
    cid = test_store.collections.create("c3", "name", "path3", "/tmp/root",
                                         provenance_kind="human_reviewed")
    row = test_store._conn.execute(
        "SELECT provenance_kind FROM collections WHERE id = ?", (cid,)).fetchone()
    assert row["provenance_kind"] == "human_reviewed"


# ── no-staleness property ───────────────────────────────────────────────────────────

def test_kind_resolves_via_view_and_reflects_source_update_with_no_doc_change(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO watched_sources (id, type, uri, provenance_kind) "
        "VALUES ('ws2', 'vault', '/tmp/v2', 'human_vault')")
    conn.execute(
        "INSERT INTO documents (id, title, source_id, silo_id) VALUES ('d1', 'doc', 'ws2', 'ws2')")
    conn.commit()

    def resolved_kind():
        row = conn.execute(
            "SELECT sk.kind FROM documents d JOIN silo_kind sk ON sk.silo_id = d.silo_id "
            "WHERE d.id = 'd1'").fetchone()
        return row["kind"] if row else None

    assert resolved_kind() == "human_vault"

    # Re-classify the SOURCE, not the document.
    conn.execute("UPDATE watched_sources SET provenance_kind = 'agent_report' WHERE id = 'ws2'")
    conn.commit()

    # Same query, no per-doc write — the resolved kind follows the source immediately.
    assert resolved_kind() == "agent_report"
    doc_row = conn.execute("SELECT silo_id FROM documents WHERE id = 'd1'").fetchone()
    assert doc_row["silo_id"] == "ws2"  # untouched


def test_loose_upload_has_no_silo_kind_row(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO documents (id, title) VALUES ('d2', 'loose')")
    conn.commit()
    row = conn.execute("SELECT silo_id FROM documents WHERE id = 'd2'").fetchone()
    assert row["silo_id"] is None
    match = conn.execute(
        "SELECT sk.kind FROM documents d LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id "
        "WHERE d.id = 'd2'").fetchone()
    assert match["kind"] is None


# ── backfill ─────────────────────────────────────────────────────────────────────────

def test_backfill_provenance_kind_fills_existing_null_rows(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = get_connection(db)
    # init_db's own guarded ALTER + backfill already ran on these fresh inserts'
    # ancestors, so insert explicit NULLs directly to simulate a pre-existing corpus.
    conn.execute(
        "INSERT INTO watched_sources (id, type, uri, provenance_kind) "
        "VALUES ('ws3', 'vault', '/tmp/v3', NULL)")
    conn.execute(
        "INSERT INTO collections (id, name, path, kind, provenance_kind) "
        "VALUES ('c4', 'repo4', 'repo4', 'tracker_run', NULL)")
    conn.commit()
    backfill_provenance_kind(conn)
    ws_kind = conn.execute(
        "SELECT provenance_kind FROM watched_sources WHERE id = 'ws3'").fetchone()[0]
    coll_kind = conn.execute(
        "SELECT provenance_kind FROM collections WHERE id = 'c4'").fetchone()[0]
    assert ws_kind == "human_vault"
    assert coll_kind == "neutral_summary"


def test_backfill_provenance_kind_is_idempotent_and_null_only(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO watched_sources (id, type, uri, provenance_kind) "
        "VALUES ('ws4', 'vault', '/tmp/v4', 'agent_report')")
    conn.commit()
    backfill_provenance_kind(conn)
    kind = conn.execute(
        "SELECT provenance_kind FROM watched_sources WHERE id = 'ws4'").fetchone()[0]
    # Already set -> NOT overwritten by the flow default.
    assert kind == "agent_report"
