"""A search must never mix one build's FAISS positions with another build's ids.

A FAISS position is meaningless on its own — it is an offset into the id list built
alongside that index. Held as two separate globals, a rebuild could swap one while a
search sat between reading them, and the search would return entities that were never
the match: not a ranking error, a WRONG ANSWER, with no error raised anywhere.

The fix is that the pair is published as a single tuple, and readers bind it once. Since
#113 the tuple lives at `_entity_views[key]` (one entry per workspace); a dict value
rebind is atomic under the GIL just as a name rebind was, so the protocol is unchanged —
writers assign the tuple last, readers bind it once.
These tests pin both halves of that, because either alone is insufficient — an atomic
publish that readers ignore, or careful readers with a torn publish, both fail.

On isolation: `test_store` IS the tmp_path fixture — see conftest, `def test_store(
tmp_path)` building its database at `tmp_path / "test.db"`. Requesting it is how a test
here gets a per-test, file-backed SQLite database with WAL enabled. It is not a shared
or in-memory store, and `:memory:` could not work in its place: it is per-connection, so
`SQLiteDataStore`'s `init_db()` and `get_connection()` would land on two different empty
databases, and `PRAGMA journal_mode=WAL` silently returns "memory".
"""

import threading

import numpy as np

from src.pipeline.search import retrieval


def _seed(store, names):
    c = store.conn
    c.execute("INSERT INTO documents (id, title) VALUES ('d0', 'doc')")
    for i, name in enumerate(names):
        vec = np.zeros(384, dtype=np.float32)
        vec[i % 384] = 1.0
        c.execute("INSERT INTO entities (id, canonical_name, type, embedding) VALUES (?, ?, ?, ?)",
                  (f"e{i}", name, "Concept", vec.tobytes()))
        c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, 'd0')", (f"e{i}",))
    c.commit()


def test_the_index_and_its_ids_are_published_as_one_value(test_store):
    """The publish side: an index is never visible without the ids built with it."""
    _seed(test_store, ["alpha", "beta", "gamma"])
    retrieval.build_indexes(test_store.conn)

    key = retrieval._ws_key(test_store.conn)
    index, ids = retrieval._entity_views[key]
    assert index is not None
    assert index.ntotal == len(ids) == 3, (
        "the index and id list must describe the same build; a mismatch here is exactly "
        "the torn pair that makes positions resolve to the wrong entities")


def test_a_search_never_observes_a_half_swapped_index(test_store):
    """The read side, under an actually concurrent rebuild.

    Hammers `_entity_view` from reader threads while a writer thread republishes a
    DIFFERENT-SIZED pair. If a reader could observe the tuple mid-swap it would see an
    index whose `ntotal` disagrees with its id list — the condition that produces wrong
    entities. Sizes differ so a torn read is detectable at all.
    """
    _seed(test_store, ["alpha", "beta", "gamma"])
    retrieval.build_indexes(test_store.conn)
    key = retrieval._ws_key(test_store.conn)
    small = retrieval._entity_views[key]

    class _FakeIndex:
        def __init__(self, n): self.ntotal = n

    big = (_FakeIndex(9), [f"x{i}" for i in range(9)])
    stop = threading.Event()
    torn = []

    def _writer():
        while not stop.is_set():
            retrieval._entity_views[key] = big
            retrieval._entity_views[key] = small

    def _reader():
        while not stop.is_set():
            index, ids = retrieval._entity_views[key]   # ONE bind — the whole protocol
            if index is not None and index.ntotal != len(ids):
                torn.append((index.ntotal, len(ids)))

    threads = [threading.Thread(target=_writer)] + [
        threading.Thread(target=_reader) for _ in range(3)]
    for t in threads:
        t.start()
    stop.wait(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    retrieval._entity_views[key] = small   # module state; restore for later tests
    assert not torn, f"observed {len(torn)} torn reads, e.g. {torn[:3]}"


def test_a_failed_build_does_not_leave_a_partial_pair(test_store):
    """An empty graph publishes `(None, [])`, not a stale index with empty ids.

    The else-branches assign both halves together for the same reason the success path
    does: a leftover index next to an emptied id list is the same wrong-answer bug.
    """
    retrieval.build_indexes(test_store.conn)   # nothing seeded
    index, ids = retrieval._entity_views[retrieval._ws_key(test_store.conn)]
    assert index is None and ids == []
