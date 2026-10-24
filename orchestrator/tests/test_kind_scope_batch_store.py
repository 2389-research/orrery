# ABOUTME: Orchestrator O(n^2) batch normalizer (store path) is kind-aware for the EXACT
# ABOUTME: tier: exact-name / plural pairs auto-merge across same-kind silos, cross-kind
# ABOUTME: pairs are proposed to graph_issues, fuzzy cross-silo is never auto.

import hashlib

import numpy as np
from unittest.mock import patch

import src.pipeline.embedding_normalizer as norm


def _n(v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


VEC = {"base": _n([1, 0, 0, 0]), "auto": _n([0.95, 0.32, 0, 0])}


def _embedder(mapping):
    def _embed(names):
        return np.array([mapping[n] for n in names], dtype=np.float32)
    return _embed


def _setup(store, kind_a, kind_b):
    for sid, kind in (("A", kind_a), ("B", kind_b)):
        store.conn.execute(
            "INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'vault', ?, ?)",
            (sid, f"/{sid}", kind))
    store.conn.commit()


def _make_entity(store, doc_id, entity_id, name, silo_id):
    store.documents.create(doc_id, "T", "c", hashlib.sha256(doc_id.encode()).hexdigest(), None,
                           silo_id=silo_id)
    eid = store.entities.create(entity_id, name, "concept")
    store.entity_sources.create(entity_id=eid, document_id=doc_id, chunk_id=None,
                                extraction_pass="general", spec_version=0)


def _issues(store):
    return [tuple(r) for r in store.conn.execute("SELECT action, status FROM graph_issues").fetchall()]


def test_exact_name_same_kind_cross_silo_auto_merges(test_store):
    _setup(test_store, "agent_report", "agent_report")
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "http client", "B")
    with patch.object(norm, "embed_entities", _embedder({"http client": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["embedding_merges"] == 1
    assert test_store.entities.count() == 1
    assert _issues(test_store) == []
    survivor = test_store.conn.execute("SELECT id FROM entities").fetchone()[0]
    docs = {r[0] for r in test_store.conn.execute(
        "SELECT document_id FROM entity_sources WHERE entity_id = ?", (survivor,))}
    assert docs == {"doc-a", "doc-b"}


def test_exact_name_cross_kind_proposed_not_merged(test_store):
    _setup(test_store, "agent_report", "human_vault")
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "http client", "B")
    with patch.object(norm, "embed_entities", _embedder({"http client": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["embedding_merges"] == 0
    assert test_store.entities.count() == 2
    assert _issues(test_store) == [("merge", "pending")]


def test_fuzzy_same_kind_cross_silo_proposed_not_auto(test_store):
    _setup(test_store, "agent_report", "agent_report")
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "httpclient", "B")
    mapping = {"http client": VEC["base"], "httpclient": VEC["auto"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)
    assert res["embedding_merges"] == 0
    assert test_store.entities.count() == 2
    assert _issues(test_store) == [("merge", "pending")]


def test_plural_same_kind_cross_silo_collapses(test_store):
    _setup(test_store, "agent_report", "agent_report")
    _make_entity(test_store, "doc-s", "sing", "agent", "B")
    _make_entity(test_store, "doc-p", "plur", "agents", "A")
    with patch.object(norm, "embed_entities", _embedder({"agent": VEC["base"], "agents": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["plural_merges"] == 1
    assert test_store.entities.count() == 1


def test_plural_cross_kind_not_collapsed(test_store):
    _setup(test_store, "agent_report", "human_vault")
    _make_entity(test_store, "doc-s", "sing", "agent", "B")
    _make_entity(test_store, "doc-p", "plur", "agents", "A")
    with patch.object(norm, "embed_entities", _embedder({"agent": VEC["base"], "agents": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["plural_merges"] == 0
    assert test_store.entities.count() == 2
    assert _issues(test_store) == [("merge", "pending")]


def test_within_silo_fuzzy_still_auto_merges(test_store):
    _setup(test_store, "agent_report", "agent_report")
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "httpclient", "A")
    mapping = {"http client": VEC["base"], "httpclient": VEC["auto"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)
    assert res["embedding_merges"] == 1
    assert test_store.entities.count() == 1
    assert _issues(test_store) == []


def test_null_silo_exact_vs_kinded_not_auto_merged(test_store):
    _setup(test_store, "human_vault", "human_vault")
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "http client", None)  # loose-upload pool
    with patch.object(norm, "embed_entities", _embedder({"http client": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["embedding_merges"] == 0
    assert test_store.entities.count() == 2
    assert _issues(test_store) == [("merge", "pending")]


def test_null_silo_plural_vs_kinded_not_collapsed(test_store):
    _setup(test_store, "human_vault", "human_vault")
    _make_entity(test_store, "doc-s", "sing", "agent", "A")
    _make_entity(test_store, "doc-p", "plur", "agents", None)
    with patch.object(norm, "embed_entities", _embedder({"agent": VEC["base"], "agents": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["plural_merges"] == 0
    assert test_store.entities.count() == 2


def test_two_null_silo_exact_still_merge(test_store):
    _make_entity(test_store, "doc-a", "a", "http client", None)
    _make_entity(test_store, "doc-b", "b", "http client", None)
    with patch.object(norm, "embed_entities", _embedder({"http client": VEC["base"]})):
        res = norm.run_batch_normalization(test_store)
    assert res["embedding_merges"] == 1
    assert test_store.entities.count() == 1
