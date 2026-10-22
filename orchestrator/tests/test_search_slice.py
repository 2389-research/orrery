"""`/search` respects the active slice (#119 PR3): ad-hoc facets and virtual noosphere."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from src.pipeline.search import pipeline as search_pipeline
from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    def fake(text):
        v = np.zeros((1, 384), dtype=np.float32)
        v[0, 0] = 1.0
        return v
    monkeypatch.setattr(search_pipeline, "embed_text", fake)


@pytest.fixture
def seeded(test_store, tmp_path, monkeypatch):
    # Isolate the view registry (POST /views writes it) from the real data dir.
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    c = test_store.conn
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    c.execute("INSERT INTO domains (id,path) VALUES ('X','X')")
    # (doc, age days, entity, domain)
    for doc, age, ent, dom in [("d_old", 14, "E_old", None), ("d_new", 1, "E_new", None),
                               ("d_newx", 1, "E_newx", "X")]:
        c.execute("INSERT INTO documents (id, title, status, created_at) VALUES (?, ?, 'complete', ?)",
                  (doc, f"t-{doc}", _ts(age)))
        c.execute("INSERT INTO chunks (id, document_id, chunk_index, text) VALUES (?, ?, 0, ?)",
                  (f"ch_{doc}", doc, f"text about {ent}"))
        c.execute("INSERT INTO entities (id, canonical_name, type, embedding) VALUES (?, ?, 'Concept', ?)",
                  (ent, ent, vec.tobytes()))
        c.execute("INSERT INTO entity_sources (entity_id, document_id, chunk_id) VALUES (?, ?, ?)",
                  (ent, doc, f"ch_{doc}"))
        if dom:
            c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary) VALUES (?,?,1)",
                      (doc, dom))
    c.commit()
    return test_store


def _get(client, **params):
    return client.get("/search", params={"q": "zzz", "expand": "false", **params})


def test_unsliced_unchanged(seeded, test_client):
    body = _get(test_client).json()
    assert {"E_old", "E_new", "E_newx"} <= {e["name"] for e in body["entities"]}
    assert "scope" not in body


def test_window_excludes_old(seeded, test_client):
    body = _get(test_client, window="1w").json()
    names = {e["name"] for e in body["entities"]}
    assert "E_new" in names and "E_old" not in names
    assert {c["document_id"] for c in body["chunks"]} <= {"d_new", "d_newx"}
    assert body["scope"]["since"]


def test_virtual_noosphere_is_sliced(seeded, test_client):
    vid = test_client.post("/views", json={"name": "wk", "parent": "default",
                                           "slice": {"window": "1w"}}).json()["id"]
    body = _get(test_client).json()
    assert "E_old" in {e["name"] for e in body["entities"]}   # default still unsliced
    body = test_client.get("/search", params={"q": "zzz", "expand": "false"},
                           headers={"X-Workspace-Id": vid}).json()
    names = {e["name"] for e in body["entities"]}
    assert "E_old" not in names and "E_new" in names
    assert "scope" in body


def test_domain_narrows(seeded, test_client):
    body = _get(test_client, domain="X").json()
    assert {e["name"] for e in body["entities"]} == {"E_newx"}
    assert {c["document_id"] for c in body["chunks"]} == {"d_newx"}


def test_bad_window_400(seeded, test_client):
    assert _get(test_client, window="bogus").status_code == 400
