"""`/search` honors a TimeScope window by post-filtering (#116, trimmed).

The FAISS index is flat and all-time, so search over-fetches and then drops hits whose
document falls outside the window — an entity is kept only if it has an in-window source,
a chunk only if its document is in-window. A query whose best all-time hit is outside the
window still returns the in-window hits instead of nothing. No time param ⇒ unchanged.
"""

import numpy as np
import pytest

from src.pipeline.search import pipeline as search_pipeline


def _seed(store):
    """Two entities sharing one embedding (so one query matches both), each in its own
    document with a different created_at, each with a chunk."""
    c = store.conn
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    for doc_id, created, ent in [
        ("d_old", "2026-10-01 09:00:00", "old_entity"),
        ("d_new", "2026-10-19 09:00:00", "new_entity"),
    ]:
        c.execute("INSERT INTO documents (id, title, status, created_at) VALUES (?, ?, 'complete', ?)",
                  (doc_id, f"title-{doc_id}", created))
        c.execute("INSERT INTO chunks (id, document_id, chunk_index, text) VALUES (?, ?, 0, ?)",
                  (f"ch_{doc_id}", doc_id, f"text about {ent}"))
        c.execute("INSERT INTO entities (id, canonical_name, type, embedding) VALUES (?, ?, 'Concept', ?)",
                  (f"e_{ent}", ent, vec.tobytes()))
        c.execute("INSERT INTO entity_sources (entity_id, document_id, chunk_id) VALUES (?, ?, ?)",
                  (f"e_{ent}", doc_id, f"ch_{doc_id}"))
    c.commit()


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    def fake(text):
        v = np.zeros((1, 384), dtype=np.float32)
        v[0, 0] = 1.0
        return v
    monkeypatch.setattr(search_pipeline, "embed_text", fake)


def test_no_params_returns_both(test_store, test_client):
    _seed(test_store)
    body = test_client.get("/search", params={"q": "zzz", "expand": "false"}).json()
    names = {e["name"] for e in body["entities"]}
    assert {"old_entity", "new_entity"} <= names
    assert "scope" not in body, "an unscoped search response is unchanged"


def test_since_drops_out_of_window_entity(test_store, test_client):
    _seed(test_store)
    body = test_client.get("/search", params={"q": "zzz", "expand": "false", "since": "2026-10-15"}).json()
    names = {e["name"] for e in body["entities"]}
    assert "new_entity" in names, "the in-window hit is returned"
    assert "old_entity" not in names, "the out-of-window hit is dropped"
    assert body["scope"]["since"] == "2026-10-15 00:00:00"


def test_until_drops_newer_entity(test_store, test_client):
    _seed(test_store)
    body = test_client.get("/search", params={"q": "zzz", "expand": "false", "until": "2026-10-15"}).json()
    names = {e["name"] for e in body["entities"]}
    assert "old_entity" in names
    assert "new_entity" not in names


def test_chunks_are_window_filtered(test_store, test_client):
    _seed(test_store)
    body = test_client.get("/search", params={"q": "zzz", "expand": "false", "since": "2026-10-15"}).json()
    doc_ids = {c["document_id"] for c in body["chunks"]}
    assert doc_ids <= {"d_new"}, "only in-window chunks survive"


def test_window_helper_batches_past_sqlite_variable_limit(test_store):
    """A large top_k makes the in-window id list exceed SQLITE_MAX_VARIABLE_NUMBER (999);
    the helper must batch the IN(...) list rather than raise OperationalError."""
    from src.pipeline.search.pipeline import _docs_in_window
    _seed(test_store)   # d_old (2026-10-01), d_new (2026-10-19)
    ids = [f"missing-{i}" for i in range(2000)] + ["d_old", "d_new"]
    got = _docs_in_window(test_store.conn, ids, since=None, until="2026-10-15")
    assert set(got) == {"d_old"}, "batched query still filters correctly"
