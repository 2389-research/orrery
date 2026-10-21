# ABOUTME: /graph/neighborhood, /graph/subgraph, /graph/paths traverse within the active slice (#119, PR 2).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(store):
    """A-B and B-D only last week (~20d ago); A-C only this week."""
    c = store.conn
    docs = {"d_ab": 20, "d_bd": 21, "d_ac": 1}
    chunks = {"d_ab": ("A", "B"), "d_bd": ("B", "D"), "d_ac": ("A", "C")}
    for did, age in docs.items():
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')",
                  (f"c_{did}", did))
        for e in chunks[did]:
            c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES (?,?,?)",
                      (e, did, f"c_{did}"))
    for i, (a, b) in enumerate((("A", "B"), ("B", "D"), ("A", "C"))):
        c.execute("INSERT INTO relationships (id,from_entity,to_entity,type,weight) "
                  "VALUES (?,?,?,'co_occurs',1)", (f"r{i}", a, b))
    c.commit()


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    c = test_store.conn
    for e in "ABCD":
        c.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'Concept')", (e, e))
    c.commit()
    _seed(test_store)
    return test_client


def _ids(r):
    return {n["id"] for n in r["nodes"]}


def test_neighborhood_unsliced_back_compat(client):
    r = client.get("/graph/neighborhood/A").json()
    assert _ids(r) == {"A", "B", "C"} and "scope" not in r


def test_neighborhood_window(client):
    r = client.get("/graph/neighborhood/A?window=1w&depth=3").json()
    assert _ids(r) == {"A", "C"}
    assert {(e["source"], e["target"]) for e in r["edges"]} == {("A", "C")}
    assert r["scope"]["since"]
    counts = {n["id"]: n["source_count"] for n in r["nodes"]}
    assert counts == {"A": 1, "C": 1}  # A has 2 docs overall, 1 in-slice


def test_neighborhood_seed_not_in_slice_404(client):
    assert client.get("/graph/neighborhood/D?window=1w").status_code == 404
    assert client.get("/graph/neighborhood/D").status_code == 200


def test_neighborhood_bad_window(client):
    assert client.get("/graph/neighborhood/A?window=bogus").status_code == 400


def test_subgraph(client):
    body = {"entity_names": ["A"], "max_hops": 2}
    full = client.post("/graph/subgraph", json=body).json()
    assert _ids(full) == {"A", "B", "C", "D"} and "scope" not in full
    r = client.post("/graph/subgraph?window=1w", json=body).json()
    assert _ids(r) == {"A", "C"} and r["edge_count"] == 1 and r["scope"]["since"]


def test_subgraph_seed_not_in_slice_404(client):
    assert client.post("/graph/subgraph?window=1w", json={"entity_names": ["D"]}).status_code == 404


def test_subgraph_bad_window(client):
    assert client.post("/graph/subgraph?window=bogus", json={"entity_names": ["A"]}).status_code == 400


def test_paths(client):
    assert client.get("/graph/paths?a=A&b=D").json()["path_count"] == 1
    r = client.get("/graph/paths?a=A&b=D&window=1w")
    # D has no in-slice presence at all -> not a valid node under the slice
    assert r.status_code == 404
    # last-week slice: A-B-D exists
    r = client.get(f"/graph/paths?a=A&b=D&since={_ts(30)}&until={_ts(10)}").json()
    assert r["path_count"] == 1 and [n["id"] for n in r["paths"][0]["nodes"]] == ["A", "B", "D"]
    assert r["scope"]["until"]


def test_paths_out_of_slice_edge_not_walkable(client, test_store):
    # D gets a this-week mention, but B-D is still only a last-week edge.
    c = test_store.conn
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d_d','d_d',?)", (_ts(1),))
    c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES ('c_d','d_d',0,'x')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('D','d_d','c_d')")
    c.commit()
    r = client.get("/graph/paths?a=A&b=D&window=1w&max_depth=4").json()
    assert r["path_count"] == 0 and r["scope"]["since"]


def test_paths_bad_window(client):
    assert client.get("/graph/paths?a=A&b=D&window=bogus").status_code == 400


def test_virtual_noosphere(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    h = {"X-Workspace-Id": vid}
    r = client.get("/graph/neighborhood/A", headers=h).json()
    assert _ids(r) == {"A", "C"} and "scope" in r
    r = client.post("/graph/subgraph", json={"entity_names": ["A"], "max_hops": 2}, headers=h).json()
    assert _ids(r) == {"A", "C"}
