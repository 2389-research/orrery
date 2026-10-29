# ABOUTME: kind_match SQL fragment: entity has >=1 source in a silo of a given provenance kind.
# ABOUTME: File-backed sqlite (never :memory:), schema via init_db.

from src.db import init_db, get_connection
from src.silo import kind_match


def _seed(conn):
    for cid, kind in (("c_ns", "neutral_summary"), ("c_hv", "human_vault"), ("c_null", None)):
        conn.execute("INSERT INTO collections (id, name, path, provenance_kind) VALUES (?,?,?,?)",
                     (cid, cid, "/" + cid, kind))
        conn.execute("INSERT INTO documents (id, silo_id) VALUES (?,?)", ("d_" + cid, cid))
    for eid, cid in (("e_ns", "c_ns"), ("e_hv", "c_hv"), ("e_null", "c_null")):
        conn.execute("INSERT INTO entities (id, canonical_name) VALUES (?,?)", (eid, eid))
        conn.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?,?)",
                     (eid, "d_" + cid))
    conn.commit()


def _match(conn, kind):
    sql = f"SELECT e.id FROM entities e WHERE {kind_match('e.id')} ORDER BY e.id"
    return [r["id"] for r in conn.execute(sql, (kind,)).fetchall()]


def test_kind_match_scopes_by_kind_and_is_null_safe(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    _seed(conn)
    assert _match(conn, "neutral_summary") == ["e_ns"]
    assert _match(conn, "human_vault") == ["e_hv"]
    assert _match(conn, "agent_report") == []
    assert _match(conn, None) == ["e_null"]
