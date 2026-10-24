# ABOUTME: backfill connects pre-feature exact-name duplicates within a kind, never across kinds.
# ABOUTME: reuses the reversible _merge_entities (normalization_log); idempotent. File-backed sqlite.

import asyncio
import uuid

from src.db import init_db, get_connection
from src.jobs.connect_within_kind import run_connect_within_kind


def _conn(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); return db, get_connection(db)


def _silo(conn, cid, kind):
    conn.execute("INSERT INTO collections (id, name, path, provenance_kind) VALUES (?,?,?,?)",
                 (cid, cid, "/" + cid, kind))
    conn.commit()


def _ent(conn, silo, name="widget", etype="Thing"):
    did, eid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, source_path, status, silo_id) "
        "VALUES (?, 'T', 'c', 'h', ?, 'pending', ?)", (did, f"/x/{did}", silo))
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?,?,?)", (eid, name, etype))
    conn.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?,?)",
                 (eid, did))
    conn.commit()
    return eid, did


def _job(conn, db):
    jid = str(uuid.uuid4())
    conn.execute("INSERT INTO jobs (id, type, status, config) VALUES (?, 'connect_within_kind', 'running', '{}')", (jid,))
    conn.commit()
    return {"id": jid, "type": "connect_within_kind", "target": None, "config": "{}"}


def _active(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM entities WHERE invalid_at IS NULL")]


def test_same_kind_duplicates_merge_and_idempotent(tmp_path):
    db, conn = _conn(tmp_path)
    _silo(conn, "A", "neutral_summary"); _silo(conn, "B", "neutral_summary")
    _, da = _ent(conn, "A"); _, db_ = _ent(conn, "B")
    asyncio.run(run_connect_within_kind(_job(conn, db), db))
    act = _active(conn)
    assert len(act) == 1
    docs = {r["document_id"] for r in conn.execute(
        "SELECT document_id FROM entity_sources WHERE entity_id=?", (act[0],))}
    assert docs == {da, db_}
    assert conn.execute("SELECT COUNT(*) c FROM normalization_log WHERE method='kind_backfill'").fetchone()["c"] == 1
    asyncio.run(run_connect_within_kind(_job(conn, db), db))
    assert conn.execute("SELECT COUNT(*) c FROM normalization_log WHERE method='kind_backfill'").fetchone()["c"] == 1
    assert _active(conn) == act


def test_cross_kind_not_merged(tmp_path):
    db, conn = _conn(tmp_path)
    _silo(conn, "A", "neutral_summary"); _silo(conn, "B", "human_vault")
    _ent(conn, "A"); _ent(conn, "B")
    asyncio.run(run_connect_within_kind(_job(conn, db), db))
    assert len(_active(conn)) == 2
