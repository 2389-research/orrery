# ABOUTME: run_batch_normalization (the worker's faiss-based batch normalizer) is
# ABOUTME: silo-aware (#50): auto-merge only within a silo; a cross-silo high-similarity
# ABOUTME: pair is NOT merged but proposed to the human-gated graph_issues table.

import uuid

import numpy as np
import pytest
from unittest.mock import patch

import src.normalizer as norm
from src.db import get_connection


def _n(v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


# Same controlled 4-d vectors as test_normalizer_incremental.py:
# dot(base, auto) ~ 0.95 (>= AUTO_MERGE_THRESHOLD 0.85)
VEC = {
    "base": _n([1, 0, 0, 0]),
    "auto": _n([0.95, 0.32, 0, 0]),
}


def _embedder(mapping):
    def _embed(names):
        return np.array([mapping[n] for n in names], dtype=np.float32)
    return _embed


def _make_doc(conn, doc_id, silo_id):
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, source_path, status, silo_id) "
        "VALUES (?, 'T', 'c', ?, ?, 'pending', ?)",
        (doc_id, doc_id, f"/x/{doc_id}", silo_id))


def _add_entity(conn, eid, name, silo_id, etype="concept", stored_vec=None):
    """Insert an entity sourced from a single document in the given silo.

    stored_vec present -> 'old' (pre-embedded, skipped by the embed step);
    absent -> 'new' (embedded this run via the patched embed_entities)."""
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, ?)", (eid, name, etype))
    doc_id = f"doc-{eid}"
    _make_doc(conn, doc_id, silo_id)
    conn.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)", (eid, doc_id))
    if stored_vec is not None:
        conn.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (eid, np.asarray(stored_vec, dtype=np.float32).tobytes()),
        )
    conn.commit()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM entities WHERE invalid_at IS NULL").fetchone()[0]


def test_cross_silo_high_similarity_not_merged_but_proposed(test_db):
    conn = get_connection(test_db)
    _add_entity(conn, "a", "http client", "A", stored_vec=VEC["base"])
    _add_entity(conn, "b", "httpclient", "B")  # new, near-duplicate, different silo

    with patch.object(norm, "embed_entities", _embedder({"httpclient": VEC["auto"]})):
        res = norm.run_batch_normalization(conn)

    assert res["embedding_merges"] == 0
    assert res["cross_silo_proposed"] == 1
    assert _count(conn) == 2, "both entities must remain distinct"

    issues = conn.execute(
        "SELECT action, target_entity_id, target_b_entity_id, status FROM graph_issues"
    ).fetchall()
    assert len(issues) == 1
    action, target_id, target_b_id, status = issues[0]
    assert action == "merge"
    assert status == "pending"
    assert {target_id, target_b_id} == {"a", "b"}
    conn.close()


def test_same_silo_high_similarity_still_auto_merges(test_db):
    conn = get_connection(test_db)
    _add_entity(conn, "a", "http client", "A", stored_vec=VEC["base"])
    _add_entity(conn, "b", "httpclient", "A")  # new, near-duplicate, SAME silo

    with patch.object(norm, "embed_entities", _embedder({"httpclient": VEC["auto"]})):
        res = norm.run_batch_normalization(conn)

    assert res["embedding_merges"] == 1
    assert res["cross_silo_proposed"] == 0
    assert _count(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM graph_issues").fetchone()[0] == 0
    conn.close()


def test_cross_silo_plural_pair_not_collapsed(test_db):
    # Cross-silo AND cross-kind: the exact/plural tier widens to kind (see
    # test_kind_scope_batch_faiss.py), so the silos must carry DIFFERENT kinds here.
    conn = get_connection(test_db)
    for sid, kind in (("A", "agent_report"), ("B", "human_vault")):
        conn.execute("INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'vault', ?, ?)",
                     (sid, f"/{sid}", kind))
    _add_entity(conn, "sing", "agent", "B", stored_vec=VEC["base"])
    _add_entity(conn, "plur", "agents", "A")  # new plural, DIFFERENT silo than the singular

    with patch.object(norm, "embed_entities", _embedder({"agents": VEC["base"]})):
        res = norm.run_batch_normalization(conn)

    assert res["plural_merges"] == 0
    assert _count(conn) == 2
    conn.close()


def test_same_silo_plural_pair_still_collapses(test_db):
    conn = get_connection(test_db)
    _add_entity(conn, "sing", "agent", "A", stored_vec=VEC["base"])
    _add_entity(conn, "plur", "agents", "A")  # new plural, SAME silo as the singular

    with patch.object(norm, "embed_entities", _embedder({"agents": VEC["base"]})):
        res = norm.run_batch_normalization(conn)

    assert res["plural_merges"] == 1
    assert _count(conn) == 1
    conn.close()


def test_plural_collapses_onto_same_silo_singular_despite_cross_silo_homonym_inserted_first(test_db):
    """#50 regression: the singular name exists as TWO distinct rows — one in a
    silo that does NOT overlap the plural (inserted FIRST, so an unscoped
    first-row/no-ORDER-BY lookup would pick it), one in the plural's OWN silo
    (inserted second). The plural must still collapse onto the same-silo
    singular; insertion order must not matter."""
    conn = get_connection(test_db)
    _add_entity(conn, "b", "agent", "B", stored_vec=VEC["base"])   # cross-silo homonym, inserted FIRST
    _add_entity(conn, "a", "agent", "A", stored_vec=VEC["base"])   # same-silo singular
    _add_entity(conn, "plur", "agents", "A")                       # new plural, silo A

    with patch.object(norm, "embed_entities", _embedder({"agents": VEC["base"]})):
        res = norm.run_batch_normalization(conn)

    assert res["plural_merges"] == 1
    remaining = {r[0] for r in conn.execute(
        "SELECT id FROM entities WHERE invalid_at IS NULL"
    ).fetchall()}
    assert "a" in remaining, "agents@A must have collapsed onto agent@A"
    assert "plur" not in remaining, "the plural must have been merged away"
    assert "b" in remaining, "agent@B (different silo) must stay distinct"
    conn.close()
