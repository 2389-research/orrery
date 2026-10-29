# ABOUTME: run_batch_normalization's store path (_run_batch_store, the orchestrator's
# ABOUTME: O(n^2) all-pairs normalizer) is silo-aware (#50): auto-merge only within a
# ABOUTME: silo; a cross-silo high-similarity pair is NOT merged but proposed to the
# ABOUTME: human-gated graph_issues table via graph_repair.propose_correction. Mirrors
# ABOUTME: worker/tests/test_silo_scope_batch_faiss.py's semantics for the store path.

import hashlib

import numpy as np
from unittest.mock import patch

import src.pipeline.embedding_normalizer as norm


def _n(v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


# Same controlled 4-d vectors as the worker's faiss test:
# dot(base, auto) ~ 0.95 (>= AUTO_MERGE_THRESHOLD 0.85)
VEC = {
    "base": _n([1, 0, 0, 0]),
    "auto": _n([0.95, 0.32, 0, 0]),
    "orth": _n([0, 1, 0, 0]),
}


def _embedder(mapping):
    def _embed(names):
        return np.array([mapping[n] for n in names], dtype=np.float32)
    return _embed


def _make_entity(store, doc_id, entity_id, name, silo_id, etype="concept"):
    """Insert an entity sourced from a single document in the given silo."""
    content_hash = hashlib.sha256(doc_id.encode()).hexdigest()
    store.documents.create(doc_id, "T", "c", content_hash, None, silo_id=silo_id)
    eid = store.entities.create(entity_id, name, etype)
    store.entity_sources.create(entity_id=eid, document_id=doc_id, chunk_id=None,
                                 extraction_pass="general", spec_version=0)
    return eid


def test_cross_silo_high_similarity_not_merged_but_proposed(test_store):
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "httpclient", "B")

    mapping = {"http client": VEC["base"], "httpclient": VEC["auto"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)

    assert res["embedding_merges"] == 0
    assert res["cross_silo_proposed"] == 1
    assert test_store.entities.count() == 2

    issues = test_store.conn.execute(
        "SELECT action, target_entity_id, target_b_entity_id, status FROM graph_issues"
    ).fetchall()
    assert len(issues) == 1
    action, target_id, target_b_id, status = issues[0]
    assert action == "merge"
    assert status == "pending"
    assert {target_id, target_b_id} == {"a", "b"}


def test_same_silo_high_similarity_still_auto_merges(test_store):
    _make_entity(test_store, "doc-a", "a", "http client", "A")
    _make_entity(test_store, "doc-b", "b", "httpclient", "A")

    mapping = {"http client": VEC["base"], "httpclient": VEC["auto"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)

    assert res["embedding_merges"] == 1
    assert res.get("cross_silo_proposed", 0) == 0
    assert test_store.entities.count() == 1
    assert test_store.conn.execute("SELECT COUNT(*) FROM graph_issues").fetchone()[0] == 0


def test_cross_silo_plural_pair_not_collapsed(test_store):
    # Cross-silo AND cross-kind: the exact/plural tier widens to kind (see
    # test_kind_scope_batch_store.py), so the silos must carry DIFFERENT kinds here.
    for sid, kind in (("A", "agent_report"), ("B", "human_vault")):
        test_store.conn.execute(
            "INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'vault', ?, ?)",
            (sid, f"/{sid}", kind))
    test_store.conn.commit()
    _make_entity(test_store, "doc-sing", "sing", "agent", "B")
    _make_entity(test_store, "doc-plur", "plur", "agents", "A")

    mapping = {"agent": VEC["base"], "agents": VEC["orth"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)

    assert res["plural_merges"] == 0
    assert test_store.entities.count() == 2


def test_same_silo_plural_pair_still_collapses(test_store):
    _make_entity(test_store, "doc-sing", "sing", "agent", "A")
    _make_entity(test_store, "doc-plur", "plur", "agents", "A")

    mapping = {"agent": VEC["base"], "agents": VEC["orth"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)

    assert res["plural_merges"] == 1
    assert test_store.entities.count() == 1


def test_plural_collapses_onto_same_silo_singular_despite_cross_silo_homonym_inserted_first(test_store):
    """#50 regression: the singular name exists as TWO distinct rows — one in a
    silo that does NOT overlap the plural (inserted FIRST, so an unscoped
    first-row lookup would pick it), one in the plural's OWN silo (inserted
    second). The plural must still collapse onto the same-silo singular;
    insertion order must not matter."""
    # Cross-silo AND cross-kind: the exact/plural tier widens to kind (see
    # test_kind_scope_batch_store.py), so the silos must carry DIFFERENT kinds here.
    for sid, kind in (("A", "agent_report"), ("B", "human_vault")):
        test_store.conn.execute(
            "INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'vault', ?, ?)",
            (sid, f"/{sid}", kind))
    test_store.conn.commit()
    _make_entity(test_store, "doc-b", "b", "agent", "B")   # cross-silo homonym, inserted FIRST
    _make_entity(test_store, "doc-a", "a", "agent", "A")   # same-silo singular
    _make_entity(test_store, "doc-p", "p", "agents", "A")  # plural, silo A

    mapping = {"agent": VEC["base"], "agents": VEC["base"]}
    with patch.object(norm, "embed_entities", _embedder(mapping)):
        res = norm.run_batch_normalization(test_store)

    assert res["plural_merges"] == 1
    remaining = {r[0] for r in test_store.conn.execute(
        "SELECT id FROM entities WHERE invalid_at IS NULL"
    ).fetchall()}
    assert "a" in remaining, "agents@A must have collapsed onto agent@A"
    assert "p" not in remaining, "the plural must have been merged away"
    assert "b" in remaining, "agent@B (different silo) must stay distinct"
