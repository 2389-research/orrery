# ABOUTME: inline exact-name dedup (extract_document_entities) scopes by the doc's provenance KIND,
# ABOUTME: so same-kind silos share one entity; different kinds stay separate. File-backed sqlite.

import json
import uuid
from types import SimpleNamespace

import pytest

from src.db import init_db, get_connection
from src.config import get_settings
from src.jobs.upsert_document import extract_document_entities


class FakeRelay:
    async def complete(self, model, max_tokens, messages, **kwargs):
        return SimpleNamespace(text=json.dumps({"entities": [{"name": "widget", "type": "Thing"}]}))


def _silo(conn, cid, kind):
    conn.execute("INSERT INTO collections (id, name, path, provenance_kind) VALUES (?,?,?,?)",
                 (cid, cid, "/" + cid, kind))
    conn.commit()


def _doc(conn, silo_id):
    doc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, source_path, status, silo_id) "
        "VALUES (?, 'T', 'c', 'h', ?, 'pending', ?)", (doc_id, f"/x/{doc_id}", silo_id))
    conn.commit()
    return doc_id


async def _extract(conn, doc_id):
    return await extract_document_entities(
        conn, FakeRelay(), get_settings(), doc_id=doc_id,
        chunks=[(str(uuid.uuid4()), "t")], spec="s", spec_version=0, scope="general",
        job_id=None, doc_path="x", collection_name="")


def _count(conn):
    return conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]


def _conn(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); return get_connection(db)


@pytest.mark.asyncio
async def test_same_kind_silos_share_one_entity(tmp_path):
    conn = _conn(tmp_path)
    _silo(conn, "A", "neutral_summary"); _silo(conn, "B", "neutral_summary")
    da, db_ = _doc(conn, "A"), _doc(conn, "B")
    await _extract(conn, da); await _extract(conn, db_)
    assert _count(conn) == 1
    eid = conn.execute("SELECT id FROM entities").fetchone()["id"]
    docs = {r["document_id"] for r in conn.execute(
        "SELECT document_id FROM entity_sources WHERE entity_id=?", (eid,))}
    assert docs == {da, db_}


@pytest.mark.asyncio
async def test_same_silo_still_one_entity(tmp_path):
    conn = _conn(tmp_path)
    _silo(conn, "A", "neutral_summary")
    await _extract(conn, _doc(conn, "A")); await _extract(conn, _doc(conn, "A"))
    assert _count(conn) == 1


@pytest.mark.asyncio
async def test_different_kinds_stay_distinct(tmp_path):
    conn = _conn(tmp_path)
    _silo(conn, "A", "neutral_summary"); _silo(conn, "B", "human_vault")
    await _extract(conn, _doc(conn, "A")); await _extract(conn, _doc(conn, "B"))
    assert _count(conn) == 2


@pytest.mark.asyncio
async def test_merge_map_alias_in_other_kind_does_not_short_circuit(tmp_path):
    conn = _conn(tmp_path)
    _silo(conn, "A", "neutral_summary"); _silo(conn, "B", "human_vault")
    await _extract(conn, _doc(conn, "A"))  # creates 'widget' in kind A
    target = conn.execute("SELECT id FROM entities").fetchone()["id"]
    conn.execute("INSERT INTO merge_map (from_name, to_entity_id) VALUES ('widget', ?)", (target,))
    conn.commit()
    db_ = _doc(conn, "B")
    await _extract(conn, db_)
    assert _count(conn) == 2
    assert conn.execute("SELECT COUNT(*) c FROM entity_sources WHERE entity_id=? AND document_id=?",
                        (target, db_)).fetchone()["c"] == 0
