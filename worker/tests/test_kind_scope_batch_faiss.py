# ABOUTME: Worker faiss batch normalizer is kind-aware for the EXACT tier: exact-name /
# ABOUTME: plural pairs auto-merge across silos of the same provenance kind, cross-kind
# ABOUTME: pairs are proposed to graph_issues, fuzzy cross-silo is never auto.

import numpy as np
from unittest.mock import patch

import src.normalizer as norm
from src.db import get_connection


def _n(v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


VEC = {"base": _n([1, 0, 0, 0]), "auto": _n([0.95, 0.32, 0, 0])}


def _embedder(mapping):
    def _embed(names):
        return np.array([mapping[n] for n in names], dtype=np.float32)
    return _embed


def _silo(conn, silo_id, kind):
    conn.execute("INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'vault', ?, ?)",
                 (silo_id, f"/{silo_id}", kind))


def _add_entity(conn, eid, name, silo_id, stored_vec=None):
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, 'concept')", (eid, name))
    doc_id = f"doc-{eid}"
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, source_path, status, silo_id) "
        "VALUES (?, 'T', 'c', ?, ?, 'pending', ?)", (doc_id, doc_id, f"/x/{doc_id}", silo_id))
    conn.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)", (eid, doc_id))
    if stored_vec is not None:
        conn.execute("INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                     (eid, np.asarray(stored_vec, dtype=np.float32).tobytes()))
    conn.commit()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM entities WHERE invalid_at IS NULL").fetchone()[0]


def _issues(conn):
    return [tuple(r) for r in conn.execute("SELECT action, status FROM graph_issues").fetchall()]


def _setup(conn, kind_a, kind_b):
    _silo(conn, "A", kind_a)
    _silo(conn, "B", kind_b)


def test_exact_name_same_kind_cross_silo_auto_merges(test_db):
    conn = get_connection(test_db)
    _setup(conn, "agent_report", "agent_report")
    _add_entity(conn, "a", "http client", "A", stored_vec=VEC["base"])
    _add_entity(conn, "b", "http client", "B")
    with patch.object(norm, "embed_entities", _embedder({"http client": VEC["base"]})):
        res = norm.run_batch_normalization(conn)
    assert res["embedding_merges"] == 1
    assert _count(conn) == 1
    assert _issues(conn) == []
    survivor = conn.execute("SELECT id FROM entities").fetchone()[0]
    docs = {r[0] for r in conn.execute("SELECT document_id FROM entity_sources WHERE entity_id = ?", (survivor,))}
    assert docs == {"doc-a", "doc-b"}
    conn.close()


def test_exact_name_cross_kind_proposed_not_merged(test_db):
    conn = get_connection(test_db)
    _setup(conn, "agent_report", "human_vault")
    _add_entity(conn, "a", "http client", "A", stored_vec=VEC["base"])
    _add_entity(conn, "b", "http client", "B")
    with patch.object(norm, "embed_entities", _embedder({"http client": VEC["base"]})):
        res = norm.run_batch_normalization(conn)
    assert res["embedding_merges"] == 0
    assert _count(conn) == 2
    assert _issues(conn) == [("merge", "pending")]
    conn.close()


def test_fuzzy_same_kind_cross_silo_proposed_not_auto(test_db):
    conn = get_connection(test_db)
    _setup(conn, "agent_report", "agent_report")
    _add_entity(conn, "a", "http client", "A", stored_vec=VEC["base"])
    _add_entity(conn, "b", "httpclient", "B")
    with patch.object(norm, "embed_entities", _embedder({"httpclient": VEC["auto"]})):
        res = norm.run_batch_normalization(conn)
    assert res["embedding_merges"] == 0
    assert _count(conn) == 2
    assert _issues(conn) == [("merge", "pending")]
    conn.close()


def test_plural_same_kind_cross_silo_collapses(test_db):
    conn = get_connection(test_db)
    _setup(conn, "agent_report", "agent_report")
    _add_entity(conn, "sing", "agent", "B", stored_vec=VEC["base"])
    _add_entity(conn, "plur", "agents", "A")
    with patch.object(norm, "embed_entities", _embedder({"agents": VEC["base"]})):
        res = norm.run_batch_normalization(conn)
    assert res["plural_merges"] == 1
    assert _count(conn) == 1
    conn.close()


def test_plural_cross_kind_not_collapsed(test_db):
    conn = get_connection(test_db)
    _setup(conn, "agent_report", "human_vault")
    _add_entity(conn, "sing", "agent", "B", stored_vec=VEC["base"])
    _add_entity(conn, "plur", "agents", "A")
    with patch.object(norm, "embed_entities", _embedder({"agents": VEC["base"]})):
        res = norm.run_batch_normalization(conn)
    assert res["plural_merges"] == 0
    assert _count(conn) == 2
    assert _issues(conn) == [("merge", "pending")]
    conn.close()


def test_within_silo_fuzzy_still_auto_merges(test_db):
    conn = get_connection(test_db)
    _setup(conn, "agent_report", "agent_report")
    _add_entity(conn, "a", "http client", "A", stored_vec=VEC["base"])
    _add_entity(conn, "b", "httpclient", "A")
    with patch.object(norm, "embed_entities", _embedder({"httpclient": VEC["auto"]})):
        res = norm.run_batch_normalization(conn)
    assert res["embedding_merges"] == 1
    assert _count(conn) == 1
    assert _issues(conn) == []
    conn.close()
