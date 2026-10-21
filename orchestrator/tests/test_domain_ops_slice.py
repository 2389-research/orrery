# ABOUTME: /graph/domain-overview and /graph/domain/{path}/neighbours respect the active slice (#119, PR 2).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(store):
    c = store.conn
    for p in ("X", "Y"):
        c.execute("INSERT INTO domains (id,path) VALUES (?,?)", (p, p))
    docs = (("x_new", 1, "X"), ("x_new2", 2, "X"), ("x_old", 20, "X"), ("y_old", 21, "Y"))
    for did, age, dom in docs:
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')", (f"c_{did}", did))
        c.execute("INSERT INTO document_domains (document_id,domain_path) VALUES (?,?)", (did, dom))
    for e in ("E_recent", "E_both", "E_shared"):
        c.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'Concept')", (e, e))
    for e, d in (("E_recent", "x_new"), ("E_both", "x_new"), ("E_both", "x_new2"), ("E_both", "x_old"),
                 ("E_shared", "x_old"), ("E_shared", "y_old")):
        c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES (?,?,?)",
                  (e, d, f"c_{d}"))
    c.execute("INSERT INTO domain_edges (source,target,weight) VALUES ('X','Y',7)")
    c.commit()


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    _seed(test_store)
    return test_client


def test_overview_unsliced_back_compat(client):
    r = client.get("/graph/domain-overview/X").json()
    assert "scope" not in r
    assert r["domain"]["document_count"] == 3
    assert {e["name"]: e["doc_count"] for e in r["top_entities"]} == {
        "E_both": 3, "E_recent": 1, "E_shared": 1}
    assert [d["path"] for d in r["related_domains"]] == ["Y"]


def test_overview_window(client):
    r = client.get("/graph/domain-overview/X?window=1w").json()
    assert r["domain"]["document_count"] == 2
    assert {d["id"] for d in r["documents"]} == {"x_new", "x_new2"}
    assert {e["name"]: e["doc_count"] for e in r["top_entities"]} == {"E_both": 2, "E_recent": 1}
    assert r["related_domains"] == []
    assert r["scope"]["since"]


def test_neighbours_unsliced_uses_materialized(client):
    r = client.get("/graph/domain/X/neighbours").json()
    assert "scope" not in r
    assert r["neighbours"] == [{"path": "Y", "weight": 7}]


def test_neighbours_window_live_in_slice(client):
    r = client.get("/graph/domain/X/neighbours?window=1w").json()
    assert r["neighbours"] == []
    assert r["scope"]["since"]
    # a window that covers Y's doc computes live (not the materialized weight 7)
    r = client.get("/graph/domain/X/neighbours?window=1m").json()
    assert [n["path"] for n in r["neighbours"]] == ["Y"]
    assert r["neighbours"][0]["weight"] != 7


def test_bad_window_400(client):
    assert client.get("/graph/domain-overview/X?window=bogus").status_code == 400
    assert client.get("/graph/domain/X/neighbours?window=bogus").status_code == 400


def test_virtual_noosphere(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    h = {"X-Workspace-Id": vid}
    r = client.get("/graph/domain-overview/X", headers=h).json()
    assert r["domain"]["document_count"] == 2 and r["related_domains"] == []
    n = client.get("/graph/domain/X/neighbours", headers=h).json()
    assert n["neighbours"] == [] and "scope" in n
