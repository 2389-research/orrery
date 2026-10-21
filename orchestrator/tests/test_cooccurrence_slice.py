# ABOUTME: /cooccurrences, /star-graph, /graph/shared-context respect the active slice (graph slices #119, PR 2).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(store):
    """A-B co-occur only LAST period (d_old/c_old); A-C only THIS week (d_new/c_new). D only old."""
    c = store.conn
    for did, age in (("d_old", 20), ("d_new", 1)):
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')",
                  (f"c_{did}", did))
    for e in ("A", "B", "C", "D"):
        c.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'Concept')", (e, e))
    for e, d in (("A", "d_old"), ("B", "d_old"), ("D", "d_old"), ("A", "d_new"), ("C", "d_new")):
        c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES (?,?,?)",
                  (e, d, f"c_{d}"))
    # Stored (all-time) co-occurrence edges, as ingest would have written them.
    for i, (x, y) in enumerate((("A", "B"), ("A", "C"), ("A", "D"), ("B", "D"))):
        c.execute("INSERT INTO relationships (id,from_entity,to_entity,type,weight) "
                  "VALUES (?,?,?,'co_occurs',1)", (f"r{i}", x, y))
    c.commit()


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    _seed(test_store)
    return test_client


def test_cooccurrences_unsliced(client):
    r = client.get("/entities/A/cooccurrences").json()
    assert isinstance(r, list)
    assert {c["id"] for c in r} == {"B", "C", "D"}


def test_cooccurrences_window(client):
    r = client.get("/entities/A/cooccurrences?window=1w").json()
    assert set(r) == {"cooccurrences", "scope"}
    assert [c["id"] for c in r["cooccurrences"]] == ["C"]
    c = r["cooccurrences"][0]
    assert c["canonical_name"] == "C" and c["type"] == "Concept" and c["weight"] == 1
    assert r["scope"]["since"]


def test_star_graph_unsliced(client):
    r = client.get("/entities/A/star-graph").json()
    assert "scope" not in r
    assert {c["id"] for c in r["co_entities"]} == {"B", "C", "D"}
    assert {d["id"] for d in r["documents"]} == {"d_old", "d_new"}


def test_star_graph_window(client):
    r = client.get("/entities/A/star-graph?window=1w").json()
    assert [c["id"] for c in r["co_entities"]] == ["C"]
    assert r["co_entities"][0]["shared_doc_ids"] == ["d_new"]
    assert [d["id"] for d in r["documents"]] == ["d_new"]
    assert r["documents"][0]["n_entities"] == 2   # A and C only in-slice
    assert r["entity"]["source_count"] == 1
    assert r["scope"]["since"]


def test_star_graph_no_in_slice_presence_404(client):
    assert client.get("/entities/B/star-graph?window=1w").status_code == 404
    assert client.get("/entities/B/star-graph").status_code == 200


def test_shared_context_unsliced(client):
    r = client.get("/graph/shared-context?a=A&b=B").json()
    assert "scope" not in r
    assert [d["id"] for d in r["shared_documents"]] == ["d_old"]
    assert [n["id"] for n in r["shared_neighbors"]] == ["D"]


def test_shared_context_window(client):
    r = client.get("/graph/shared-context?a=A&b=B&window=1w").json()
    assert r["shared_documents"] == []
    assert r["shared_neighbors"] == []
    assert r["direct_weight"] == 0
    assert r["scope"]["since"]


def test_shared_context_window_shared_in_slice(client):
    # A and C share d_new this week; direct weight is the in-slice 1.
    r = client.get("/graph/shared-context?a=A&b=C&window=1w").json()
    assert [d["id"] for d in r["shared_documents"]] == ["d_new"]
    assert r["direct_weight"] == 1


@pytest.mark.parametrize("path", ["/entities/A/cooccurrences", "/entities/A/star-graph",
                                  "/graph/shared-context?a=A&b=B&x=1"])
def test_bad_window_400(client, path):
    sep = "&" if "?" in path else "?"
    assert client.get(f"{path}{sep}window=bogus").status_code == 400


def test_virtual_noosphere(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    h = {"X-Workspace-Id": vid}
    r = client.get("/entities/A/cooccurrences", headers=h).json()
    assert [c["id"] for c in r["cooccurrences"]] == ["C"]
    s = client.get("/entities/A/star-graph", headers=h).json()
    assert [c["id"] for c in s["co_entities"]] == ["C"]
    assert client.get("/entities/B/star-graph", headers=h).status_code == 404
    sc = client.get("/graph/shared-context?a=A&b=B", headers=h).json()
    assert sc["shared_documents"] == [] and "scope" in sc
