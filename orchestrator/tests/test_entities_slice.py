# ABOUTME: /entities and /entities/{id} count + return within the active slice (graph slices #119, PR 2).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(store):
    c = store.conn
    for did, age in (("d_old", 20), ("d_new1", 1), ("d_new2", 2)):
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')", (f"c_{did}", did))
    for e in ("E_recent", "E_old", "E_both"):
        c.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'Concept')", (e, e))
    for e, d in (("E_recent", "d_new1"), ("E_old", "d_old"), ("E_both", "d_old"),
                 ("E_both", "d_new1"), ("E_both", "d_new2")):
        c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES (?,?,?)",
                  (e, d, f"c_{d}"))
    c.commit()


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    _seed(test_store)
    return test_client


def test_unsliced_back_compat(client):
    r = client.get("/entities").json()
    assert isinstance(r, list)
    counts = {e["id"]: e["source_count"] for e in r}
    assert counts == {"E_recent": 1, "E_old": 1, "E_both": 3}
    d = client.get("/entities/E_both").json()
    assert "scope" not in d and len(d["sources"]) == 3


def test_list_window(client):
    r = client.get("/entities?window=1w").json()
    assert set(r) == {"entities", "scope"}
    ents = r["entities"]
    assert [e["id"] for e in ents] == ["E_both", "E_recent"]
    assert [e["source_count"] for e in ents] == [2, 1]
    assert r["scope"]["since"] and r["scope"]["until"] is None


def test_list_window_with_type_filter(client):
    r = client.get("/entities?window=1w&type=Concept").json()
    assert len(r["entities"]) == 2
    assert client.get("/entities?window=1w&type=Nope").json()["entities"] == []


def test_get_in_slice(client):
    d = client.get("/entities/E_both?window=1w").json()
    assert {s["document_id"] for s in d["sources"]} == {"d_new1", "d_new2"}
    assert d["scope"]["since"] and d["source_count"] == 2


def test_get_not_in_slice_404(client):
    assert client.get("/entities/E_old?window=1w").status_code == 404
    assert client.get("/entities/E_old").status_code == 200


def test_bad_window_400(client):
    assert client.get("/entities?window=bogus").status_code == 400
    assert client.get("/entities/E_both?window=bogus").status_code == 400


def test_virtual_noosphere(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    h = {"X-Workspace-Id": vid}
    r = client.get("/entities", headers=h).json()
    assert [e["id"] for e in r["entities"]] == ["E_both", "E_recent"]
    assert client.get("/entities/E_old", headers=h).status_code == 404
    d = client.get("/entities/E_both", headers=h).json()
    assert len(d["sources"]) == 2 and d["source_count"] == 2 and "scope" in d


def test_domain_filter_is_in_slice(client, test_store):
    c = test_store.conn
    c.execute("INSERT INTO domains (id,path) VALUES ('X','X')")
    c.execute("INSERT INTO domains (id,path) VALUES ('Y','Y')")
    # d_old (out of 1w slice) in X; d_new1 in Y; d_new2 in X.
    for d, dom in (("d_old", "X"), ("d_new1", "Y"), ("d_new2", "X")):
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary) VALUES (?,?,1)", (d, dom))
    c.commit()
    # E_old: only out-of-slice doc. E_recent: in-slice doc in Y only. E_both: in-slice doc d_new2 in X.
    # Add E_leak: out-of-slice doc in X + in-slice doc NOT in X.
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES ('E_leak','E_leak','Concept')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('E_leak','d_old','c_d_old')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('E_leak','d_new1','c_d_new1')")
    c.commit()
    # Virtual noosphere slice (window) wins; ?domain= is then only the legacy filter.
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    r = client.get("/entities?domain=X", headers={"X-Workspace-Id": vid}).json()
    ids = {e["id"] for e in r["entities"]}
    assert "E_leak" not in ids
    assert "E_both" in ids


def test_domain_only_is_unsliced_legacy_bare_list(client, test_store):
    """A `?domain=X`-only request stays the legacy BARE LIST with all-time source_count —
    `domain` is a filter, not a slice trigger (CodeRabbit PR2 back-compat fix)."""
    c = test_store.conn
    c.execute("INSERT INTO domains (id,path) VALUES ('X','X')")
    for d in ("d_old", "d_new2"):
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary) VALUES (?,'X',1)", (d,))
    c.commit()
    body = client.get("/entities?domain=X").json()
    assert isinstance(body, list), "domain-only must be a bare list, not a sliced {entities,scope}"
    by_id = {e["id"]: e["source_count"] for e in body}
    assert "E_recent" not in by_id          # its only doc (d_new1) is not in domain X
    assert by_id.get("E_both") == 3         # all-time source_count, NOT in-slice
    assert by_id.get("E_old") == 1
