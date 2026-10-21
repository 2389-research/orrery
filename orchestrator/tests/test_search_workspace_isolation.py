"""Semantic search index is isolated per workspace (#113).

The FAISS indexes and the "built & current" flag used to be single process-global
values, so in an orchestrator serving several workspaces a search after switching
`X-Workspace-Id` ran against whichever workspace was searched FIRST: the global
`_indexes_ready` latched True on the first build and the second workspace never built
its own index. Hits whose ids don't exist in the second workspace were then silently
dropped during enrichment, so its recall collapsed to near-empty.

The fix keys the cache by the connection's DB file path (one file per workspace).
"""

import numpy as np
import pytest

from src.db import init_db
from src.repositories.sqlite_store import SQLiteDataStore
from src.pipeline.search import search_knowledge_graph, rebuild_indexes_now, mark_indexes_stale
from src.pipeline.search import retrieval
from src.pipeline.search import pipeline as search_pipeline


@pytest.fixture(autouse=True)
def _clear_index_state():
    """The index caches are module-level, so a test leaks into the next one in the same
    process unless reset. Clear both the views and the readiness set around each test."""
    retrieval._entity_views.clear()
    retrieval._chunk_views.clear()
    search_pipeline._indexes_ready.clear()
    yield
    retrieval._entity_views.clear()
    retrieval._chunk_views.clear()
    search_pipeline._indexes_ready.clear()


def _make_store(path):
    init_db(str(path))
    return SQLiteDataStore(str(path))


def _seed(store, name, idx):
    """One entity with a one-hot embedding at `idx`, plus a doc + source so enrichment
    resolves it. No model download — build_indexes reuses the stored embedding."""
    c = store.conn
    c.execute("INSERT INTO documents (id, title) VALUES ('d0', 'doc')")
    vec = np.zeros(384, dtype=np.float32)
    vec[idx] = 1.0
    c.execute("INSERT INTO entities (id, canonical_name, type, embedding) VALUES (?, ?, ?, ?)",
              (f"e_{name}", name, "Concept", vec.tobytes()))
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, 'd0')", (f"e_{name}",))
    c.commit()


@pytest.mark.asyncio
async def test_second_workspace_builds_and_searches_its_own_index(tmp_path, monkeypatch):
    a = _make_store(tmp_path / "a.db")
    b = _make_store(tmp_path / "b.db")
    _seed(a, "alpha_thing", idx=0)
    _seed(b, "bravo_thing", idx=1)

    # Query maps to the one-hot vector of B's entity. (No SentenceTransformer download.)
    def fake_embed(text):
        v = np.zeros((1, 384), dtype=np.float32)
        v[0, 1] = 1.0
        return v
    monkeypatch.setattr(search_pipeline, "embed_text", fake_embed)

    # A is "searched first" — this is what latched the global ready flag before the fix.
    await search_knowledge_graph(a.conn, "q", expand=False)
    # B must build and search its OWN index, not reuse A's.
    res = await search_knowledge_graph(b.conn, "q", expand=False)

    names = [e["name"] for e in res.entities]
    assert any("bravo" in n for n in names), (
        "B's own entity must surface; before the fix B reused A's index and its ids "
        "were dropped as non-existent, returning nothing")
    assert not any("alpha" in n for n in names), "A's corpus must not leak into B"

    a.close()
    b.close()


@pytest.mark.asyncio
async def test_mark_stale_is_scoped_to_one_workspace(tmp_path):
    a = _make_store(tmp_path / "a.db")
    b = _make_store(tmp_path / "b.db")
    _seed(a, "alpha_thing", idx=0)
    _seed(b, "bravo_thing", idx=1)

    rebuild_indexes_now(a.conn)
    rebuild_indexes_now(b.conn)
    assert retrieval._ws_key(a.conn) in search_pipeline._indexes_ready
    assert retrieval._ws_key(b.conn) in search_pipeline._indexes_ready

    mark_indexes_stale(a.conn)
    assert retrieval._ws_key(a.conn) not in search_pipeline._indexes_ready, "A was marked stale"
    assert retrieval._ws_key(b.conn) in search_pipeline._indexes_ready, (
        "a correction in A must not force a rebuild of B")

    a.close()
    b.close()
