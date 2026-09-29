# ABOUTME: upsert_document create / skip / update / adoption branches.
# ABOUTME: Verifies co-occurrence stays a from-scratch projection across an in-place edit.

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from src.db import init_db, get_connection, recompute_cooccurrence
from src.config import get_settings


class FakeRelay:
    """complete() -> entities keyed on chunk text; complete_structured() -> a domain."""

    @classmethod
    def from_settings(cls, settings, **overrides):
        return cls()

    async def complete(self, model, max_tokens, messages, **kwargs):
        text = messages[0]["content"]
        if "GAMMA" in text:
            ents = [{"name": "gamma", "type": "Thing"}, {"name": "delta", "type": "Thing"}]
        else:
            ents = [{"name": "alpha", "type": "Thing"}, {"name": "beta", "type": "Thing"}]
        return SimpleNamespace(text=json.dumps({"entities": ents}))

    async def complete_structured(self, model, max_tokens, messages, schema=None,
                                  tool_name=None, tool_description=None,
                                  ollama_options=None, **kwargs):
        return {"primary_domain": "techniques/test", "secondary_domains": [],
                "new_domains": [], "confidence": 0.9}


def _valid_edges(conn):
    out = []
    for r in conn.execute(
        "SELECT ea.canonical_name a, eb.canonical_name b, r.weight w FROM relationships r "
        "JOIN entities ea ON ea.id = r.from_entity JOIN entities eb ON eb.id = r.to_entity "
        "WHERE r.type='co_occurs' AND r.invalid_at IS NULL"):
        pair = tuple(sorted((r["a"], r["b"])))
        out.append((pair[0], pair[1], r["w"]))
    return sorted(out)


def _names(conn, doc_id):
    return {r["canonical_name"] for r in conn.execute(
        "SELECT DISTINCT e.canonical_name FROM entity_sources es "
        "JOIN entities e ON e.id = es.entity_id WHERE es.document_id = ?", (doc_id,))}


async def _upsert(conn, **kw):
    from src.jobs.upsert_document import upsert_document
    return await upsert_document(conn, FakeRelay(), get_settings(), **kw)


@pytest.mark.asyncio
async def test_create_then_skip_then_update(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)

    # CREATE
    r1 = await _upsert(conn, source_path="/v/note.md", title="Note",
                       content="a note about ALPHA topics", source_id="src1")
    assert r1["action"] == "created"
    doc_id = r1["document_id"]
    assert _names(conn, doc_id) == {"alpha", "beta"}
    assert _valid_edges(conn) == [("alpha", "beta", 1)]
    assert all(r["source_chunk"] is None for r in conn.execute(
        "SELECT source_chunk FROM relationships WHERE type='co_occurs'"))
    row = conn.execute("SELECT source_id, invalid_at FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["source_id"] == "src1" and row["invalid_at"] is None

    # SKIP (identical content)
    r2 = await _upsert(conn, source_path="/v/note.md", title="Note",
                       content="a note about ALPHA topics", source_id="src1")
    assert r2["action"] == "skipped" and r2["document_id"] == doc_id
    assert _valid_edges(conn) == [("alpha", "beta", 1)]

    # UPDATE (changed content -> different entities)
    r3 = await _upsert(conn, source_path="/v/note.md", title="Note",
                       content="a note about GAMMA topics", source_id="src1")
    assert r3["action"] == "updated" and r3["document_id"] == doc_id
    assert _names(conn, doc_id) == {"gamma", "delta"}
    # old alpha-beta edge retracted; new gamma-delta edge present
    assert _valid_edges(conn) == [("delta", "gamma", 1)]
    assert conn.execute("SELECT modified_at FROM documents WHERE id=?", (doc_id,)).fetchone()["modified_at"] is not None

    # graph == from-scratch projection
    after = _valid_edges(conn)
    active = [r["id"] for r in conn.execute("SELECT id FROM entities WHERE invalid_at IS NULL")]
    conn.execute("DELETE FROM relationships WHERE type='co_occurs' AND invalid_at IS NULL")
    recompute_cooccurrence(conn, active); conn.commit()
    assert _valid_edges(conn) == after


@pytest.mark.asyncio
async def test_adopts_unmanaged_document_at_same_path(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    content = "a note about ALPHA topics"
    chash = hashlib.sha256(content.encode()).hexdigest()
    # A prior unmanaged upload (source_id NULL) at the same path, same content.
    conn.execute("INSERT INTO documents (id, title, content, content_hash, source_path, status) "
                 "VALUES (?,?,?,?,?, 'extracted')", ("pre", "Note", content, chash, "/v/note.md"))
    conn.commit()

    r = await _upsert(conn, source_path="/v/note.md", title="Note",
                      content=content, source_id="src1")
    # Adopted, not duplicated.
    assert r["action"] == "skipped" and r["document_id"] == "pre"
    assert conn.execute("SELECT source_id FROM documents WHERE id='pre'").fetchone()["source_id"] == "src1"
    assert conn.execute("SELECT COUNT(*) c FROM documents WHERE source_path='/v/note.md'").fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_does_not_steal_a_path_owned_by_another_source(tmp_path):
    db = str(tmp_path / "t.db"); init_db(db); conn = get_connection(db)
    conn.execute("INSERT INTO documents (id, title, content, content_hash, source_path, source_id, status) "
                 "VALUES (?,?,?,?,?,?, 'extracted')", ("owned", "N", "x", "h", "/v/note.md", "other"))
    conn.commit()
    r = await _upsert(conn, source_path="/v/note.md", title="N", content="new", source_id="src1")
    assert r["action"] == "conflict"
    assert conn.execute("SELECT source_id FROM documents WHERE id='owned'").fetchone()["source_id"] == "other"


def test_recount_collection_counts_active_members(tmp_path):
    """_recount_collection rebuilds collections.document_count from active membership —
    the fix for sync_repo (watched-source repos) leaving it at 0. Excludes soft-deleted."""
    from src.jobs.upsert_document import _recount_collection
    db = str(tmp_path / "rc.db"); init_db(db); conn = get_connection(db)
    conn.execute("INSERT INTO collections (id, name, path, root_path, document_count) "
                 "VALUES ('c1','c1','c1','/c1', 0)")   # stale 0, like a synced repo
    for i in range(3):
        conn.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (f"d{i}", f"f{i}"))
        conn.execute("INSERT INTO document_collections (document_id, collection_id, role) "
                     "VALUES (?, 'c1', 'leaf')", (f"d{i}",))
    # one soft-deleted doc must NOT be counted
    conn.execute("INSERT INTO documents (id, title, invalid_at) VALUES ('dx','fx', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO document_collections (document_id, collection_id, role) VALUES ('dx','c1','leaf')")
    conn.commit()

    _recount_collection(conn, "c1"); conn.commit()
    assert conn.execute("SELECT document_count FROM collections WHERE id='c1'").fetchone()[0] == 3
    _recount_collection(conn, None)   # no-op, must not raise
    conn.close()
