"""Search must hide soft-deleted entities, like every other surface.

Search was the one read that ignored `invalid_at`, so a node removed through the
corrections flow still surfaced there. Three separate holes, because there are three
ways an entity reaches a result: the FAISS index build, the lexical (exact/substring)
lookup, and per-hit hydration. An entity invalidated AFTER the last index build is
still in the index, so the read-time filters matter independently of the build one.

Targets `src.pipeline.search`, the PACKAGE. A same-named `search.py` sat beside it and
shadowed-out — unreachable, and it briefly attracted a fix meant for this code. It has
been deleted.
"""

import numpy as np
import pytest

from src.pipeline.search import retrieval


@pytest.fixture(autouse=True)
def _restore_index_ready_flag():
    """`_indexes_ready` (now a per-workspace set, #113) and the per-workspace view caches
    are module-level state, so a test that mutates them leaks into every test that runs
    after it in the same process. The staleness tests below deliberately mark a workspace
    ready to simulate an already-built index; without this they would leave that state
    for unrelated tests and the pollution would only show up as an order-dependent
    failure somewhere else."""
    from src.pipeline.search import pipeline as search_pipeline
    before = set(search_pipeline._indexes_ready)
    before_e = dict(retrieval._entity_views)
    before_c = dict(retrieval._chunk_views)
    yield
    search_pipeline._indexes_ready.clear()
    search_pipeline._indexes_ready.update(before)
    retrieval._entity_views.clear(); retrieval._entity_views.update(before_e)
    retrieval._chunk_views.clear(); retrieval._chunk_views.update(before_c)


def _mark_ready(store):
    """Simulate 'an index was built earlier for this workspace' under the new keyed set."""
    from src.pipeline.search import pipeline as search_pipeline
    search_pipeline._indexes_ready.add(retrieval._ws_key(store.conn))


def _is_ready(store) -> bool:
    from src.pipeline.search import pipeline as search_pipeline
    return retrieval._ws_key(store.conn) in search_pipeline._indexes_ready


def _seed(store):
    """Seed WITH stored embeddings, which is also the production case.

    `build_indexes` only reaches for the SentenceTransformer when a row is missing one,
    so pre-storing them keeps this test off the model-download path (CI installs
    sentence-transformers but configures no HF cache) and keeps it fast. The vectors
    are arbitrary — nothing here asserts on semantic distance, only on membership.
    """
    c = store.conn
    c.execute("INSERT INTO documents (id, title) VALUES ('d0', 'doc')")
    for i, name in enumerate(["alpha widget", "beta widget", "gamma widget"]):
        vec = np.zeros(384, dtype=np.float32)
        vec[i] = 1.0
        c.execute("INSERT INTO entities (id, canonical_name, type, embedding) VALUES (?, ?, ?, ?)",
                  (f"e{i}", name, "Concept", vec.tobytes()))
        c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, 'd0')", (f"e{i}",))
    c.commit()


def _invalidate(store, eid):
    store.conn.execute("UPDATE entities SET invalid_at = datetime('now') WHERE id = ?", (eid,))
    store.conn.commit()


def test_invalidated_entity_never_enters_the_index(test_store, monkeypatch):
    _seed(test_store)
    _invalidate(test_store, "e1")

    # Pin the laziness as well as the filter: with every embedding stored there is
    # nothing to encode, so touching the model at all is the bug.
    def _no_model():
        raise AssertionError("build_indexes loaded the embedding model with nothing to encode")
    monkeypatch.setattr(retrieval, "_get_model", _no_model)

    retrieval.build_indexes(test_store.conn)
    # The index and its ids are published as one tuple, so read the pair — that is also
    # what every reader does, making this an assertion about what searches actually see.
    _index, entity_ids = retrieval._entity_views[retrieval._ws_key(test_store.conn)]
    assert "e1" not in entity_ids
    assert {"e0", "e2"} <= set(entity_ids)


def test_lexical_lookup_hides_invalidated_entities(test_store):
    """Exact and substring matching bypass the vector index entirely, so they need the
    filter in their own right."""
    _seed(test_store)
    _invalidate(test_store, "e1")
    hits = retrieval.search_entities_exact(test_store.conn, "beta widget")
    assert "e1" not in {h.entity_id for h in hits}

    partial = retrieval.search_entities_exact(test_store.conn, "widget")
    ids = {h.entity_id for h in partial}
    assert "e1" not in ids
    assert ids & {"e0", "e2"}, "active entities should still match"


def test_hydration_drops_entities_invalidated_since_the_last_index_build(test_store):
    """The third hole, and the one the build-time filter cannot close.

    The FAISS index is built periodically, so an entity invalidated AFTER the last
    build is still a live hit. Enrichment used to merely skip filling in its name and
    leave it in the list, so fusion returned a soft-deleted entity with blank metadata.
    It has to be DROPPED. Ranked order among the survivors must not change.
    """
    from src.pipeline.search.models import ScoredEntity, SubQueryResults
    from src.pipeline.search.pipeline import _enrich_results

    _seed(test_store)
    hits = [ScoredEntity(entity_id=f"e{i}", name="", entity_type="", score=1.0 - i / 10,
                         rank=i + 1, source="semantic") for i in range(3)]
    results = SubQueryResults(query="widget", semantic_entities=hits)

    # Invalidate AFTER the hits exist — i.e. after the index that produced them.
    _invalidate(test_store, "e1")
    _enrich_results(test_store.conn, results)

    surviving = [e.entity_id for e in results.semantic_entities]
    assert surviving == ["e0", "e2"], "the invalidated hit is dropped, order otherwise kept"
    assert all(e.name and e.entity_type for e in results.semantic_entities), \
        "survivors are enriched, not just retained"
    assert all(e.source_count == 1 for e in results.semantic_entities), \
        "each seeded entity is mentioned in exactly one document"


def test_a_correction_marks_the_search_index_stale(test_store):
    """Filtering the results is necessary but not sufficient.

    The FAISS index is built once per process. Dropping invalidated hits at read time
    stops a deleted entity from APPEARING, but the stale vector still occupied a top-k
    slot on the way through — so an active entity ranked just below it silently never
    surfaces. Found by review on the base repo; the same gap existed here.
    """
    from src.pipeline import graph_repair
    from src.pipeline.search import pipeline as search_pipeline

    _seed(test_store)
    _mark_ready(test_store)          # simulate an index built earlier
    graph_repair.apply_invalidation(test_store.conn, "e1", reason="test")

    assert not _is_ready(test_store), (
        "a soft delete must force a rebuild; otherwise the removed entity keeps "
        "consuming a result slot an active entity should have had")


def test_rolling_a_correction_back_also_marks_the_index_stale(test_store):
    """The inverse: an index built WHILE the entity was invalid does not contain it, so
    restoring the entity has to rebuild or it stays invisible."""
    from src.pipeline import graph_repair
    from src.pipeline.search import pipeline as search_pipeline

    _seed(test_store)
    graph_repair.apply_invalidation(test_store.conn, "e1", reason="test")
    _mark_ready(test_store)
    graph_repair.rollback_invalidation(test_store.conn, "e1")

    assert not _is_ready(test_store)


def test_a_merge_marks_the_search_index_stale(test_store):
    """Merge is the other way a node leaves the active graph.

    Invalidate and merge are separate code paths that happen to share a consequence:
    both soft-delete an entity, so both leave a vector in the index with no active row
    behind it. Covering only invalidation would let a regression in the merge path
    through.

    Scope, so this is not read as more coverage than it is: this is the CORRECTIONS
    merge. Batch normalization (`POST /normalize`) does not come through here — it goes
    through `embedding_normalizer._merge_entities_conn`, which hard-deletes and still
    never marks the index stale. Same gap in `sqlite_store`'s document-delete and
    `EntityRepo.delete`. Pre-existing, and untouched by this change.
    """
    from src.pipeline import graph_repair
    from src.pipeline.search import pipeline as search_pipeline

    _seed(test_store)
    _mark_ready(test_store)
    graph_repair.apply_merge(test_store.conn, "e1", "e0", reason="test")

    assert not _is_ready(test_store), (
        "the merged-away entity is soft-deleted, so its vector must not keep "
        "occupying a top-k slot")


def test_rolling_a_merge_back_also_marks_the_index_stale(test_store):
    """The inverse, for the same reason rollback_invalidation has one: an index built
    while the loser was merged away does not contain it, so restoring it has to
    rebuild or the entity stays invisible to search."""
    from src.pipeline import graph_repair
    from src.pipeline.search import pipeline as search_pipeline

    _seed(test_store)
    graph_repair.apply_merge(test_store.conn, "e1", "e0", reason="test")
    _mark_ready(test_store)
    graph_repair.rollback_merge(test_store.conn, "e1")

    assert not _is_ready(test_store)

# ── Tests that originated HERE, in the base repo ─────────────────────────────
# The fork never received these, so they are not in the file this was ported from.
# Keep them: they cover two holes the staleness work does not — the entity-boost
# expansion reaching THROUGH an invalidated node, and the shadowed-module trap.

def test_entity_boost_does_not_reach_through_an_invalidated_entity(test_store):
    """The boost joins chunks via shared entities, so an unfiltered join lets a
    soft-deleted entity keep pulling chunks into the results."""
    from src.pipeline.search.config import SearchConfig
    from src.pipeline.search.entity_boost import boost_chunks_via_entities
    from src.pipeline.search.models import ScoredEntity, SubQueryResults

    _seed(test_store)
    c = test_store.conn
    c.execute("INSERT INTO chunks (id, document_id, chunk_index, text) VALUES ('c0', 'd0', 0, 'x')")
    c.execute("UPDATE entity_sources SET chunk_id = 'c0'")
    c.commit()
    _invalidate(test_store, "e1")

    def boost_for(entity_id):
        hit = ScoredEntity(entity_id=entity_id, name="w", entity_type="Concept",
                           score=1.0, rank=1, source="semantic", source_count=1)
        out = boost_chunks_via_entities(
            c, SubQueryResults(query="widget", semantic_entities=[hit]),
            SearchConfig(), total_docs=1)
        return [ch.chunk_id for ch in out.boosted_chunks]

    assert boost_for("e1") == [], "an invalidated entity must not surface chunks"
    # ...and the mechanism still works, so the empty result above is the filter
    # rather than a broken query.
    assert boost_for("e0") == ["c0"]


def test_the_search_package_is_not_shadowed_by_a_module(test_store):
    """`pipeline/search.py` sat beside `pipeline/search/` and lost to it.

    Python prefers the package, so the module was unreachable — but it looked like the
    implementation, and a fix aimed at search could land there and change nothing. It
    was deleted; this keeps it deleted.
    """
    from pathlib import Path

    import src.pipeline as pipeline_pkg

    pipeline_dir = Path(pipeline_pkg.__file__).parent
    assert not (pipeline_dir / "search.py").exists(), (
        "pipeline/search.py is shadowed by pipeline/search/ and can never run — "
        "edits to it are silently inert")
    assert (pipeline_dir / "search" / "retrieval.py").exists()


def test_renaming_an_entity_drops_its_stored_embedding(test_store):
    """Marking the index stale is not enough on its own.

    `build_indexes` re-embeds only rows whose `embedding` is NULL and reuses every other
    one verbatim, so a rebuild after a rename would faithfully re-index the vector for
    the OLD name — the entity stays findable by what it used to be called and is not
    findable by what it now is. Worse than a stale index, because the rebuild ran and
    everything looks current.

    Asserts the stored embedding is gone, which is the precondition the next build needs;
    the re-embed itself is `build_indexes`' existing behaviour for a NULL.
    """
    from src.pipeline import graph_repair

    _seed(test_store)
    before = test_store.conn.execute(
        "SELECT embedding FROM entities WHERE id='e1'").fetchone()[0]
    assert before is not None, "precondition: the seed stores an embedding"

    graph_repair.apply_rename(test_store.conn, "e1", "renamed widget")

    after = test_store.conn.execute(
        "SELECT canonical_name, embedding FROM entities WHERE id='e1'").fetchone()
    assert after[0] == "renamed widget"
    assert after[1] is None, (
        "the embedding still encodes the old name; the next rebuild would index it "
        "unchanged and the entity would be searchable only under its former name")


def test_a_direct_rename_marks_the_search_index_stale(test_store):
    """`resolve_correction` is not the only caller.

    It owns the commit=False path and marks after its own commit, so the mark inside
    apply_* is what covers a DIRECT call. Without it a direct rename cleared the
    embedding, committed, and left the index ready — still serving the old vector, with
    nothing scheduled to replace it. Clearing the embedding makes this worse, not
    better: the stored data is now right and only a rebuild propagates it.
    """
    from src.pipeline import graph_repair
    from src.pipeline.search import pipeline as search_pipeline

    _seed(test_store)
    _mark_ready(test_store)
    graph_repair.apply_rename(test_store.conn, "e1", "renamed widget")

    assert not _is_ready(test_store)
