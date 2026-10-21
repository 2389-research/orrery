# ABOUTME: End-to-end proof that every PR-2 read route honors a selected view uniformly (graph slices #119).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(store):
    """A: both weeks. B: last week only. C: this week only. A-B edge + domain X-Y overlap: last week only."""
    c = store.conn
    for p in ("X", "Y"):
        c.execute("INSERT INTO domains (id,path) VALUES (?,?)", (p, p))
    for e in "ABC":
        c.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'Concept')", (e, e))
    docs = (("d_new", 1, "X", ("A", "C")), ("d_old", 20, "X", ("A", "B")), ("d_y", 21, "Y", ("B",)))
    for did, age, dom, ents in docs:
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')", (f"c_{did}", did))
        c.execute("INSERT INTO document_domains (document_id,domain_path) VALUES (?,?)", (did, dom))
        for e in ents:
            c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES (?,?,?)",
                      (e, did, f"c_{did}"))
    for i, (a, b) in enumerate((("A", "B"), ("A", "C"))):
        c.execute("INSERT INTO relationships (id,from_entity,to_entity,type,weight) "
                  "VALUES (?,?,?,'co_occurs',1)", (f"r{i}", a, b))
    c.execute("INSERT INTO domain_edges (source,target,weight) VALUES ('X','Y',5)")
    c.commit()


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    _seed(test_store)
    return test_client


@pytest.fixture
def view_h(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    return {"X-Workspace-Id": vid}


def _ids(r):
    return {n["id"] for n in r["nodes"]}


def _get(client, path, headers=None):
    return client.get(path, headers=headers or {})


def _post_subgraph(client, qs="", headers=None):
    return client.post(f"/graph/subgraph{qs}", json={"entity_names": ["A"], "max_hops": 2},
                       headers=headers or {})


def test_every_route_in_view_is_in_slice(client, view_h):
    h = view_h
    r = _get(client, "/entities", h).json()
    assert {e["canonical_name"] for e in r["entities"]} == {"A", "C"} and r["scope"]["since"]
    assert {e["canonical_name"]: e["source_count"] for e in r["entities"]}["A"] == 1

    r = _get(client, "/entities/A", h).json()
    assert r["scope"]["since"]
    assert _get(client, "/entities/B/star-graph", h).status_code == 404

    r = _get(client, "/entities/A/cooccurrences", h).json()
    assert [c["id"] for c in r["cooccurrences"]] == ["C"] and r["scope"]["since"]

    r = _get(client, "/entities/A/star-graph", h).json()
    assert [c["id"] for c in r["co_entities"]] == ["C"] and r["scope"]["since"]

    r = _get(client, "/graph/shared-context?a=A&b=C", h).json()
    assert [d["id"] for d in r["shared_documents"]] == ["d_new"] and r["scope"]["since"]

    r = _get(client, "/graph/neighborhood/A?depth=3", h).json()
    assert _ids(r) == {"A", "C"} and r["scope"]["since"]

    r = _post_subgraph(client, headers=h).json()
    assert _ids(r) == {"A", "C"} and r["scope"]["since"]

    assert _get(client, "/graph/paths?a=A&b=B", h).status_code == 404  # B not in slice

    r = _get(client, "/graph/domain-overview/X", h).json()
    assert r["domain"]["document_count"] == 1 and r["related_domains"] == [] and r["scope"]["since"]

    r = _get(client, "/graph/domain/X/neighbours", h).json()
    assert r["neighbours"] == [] and r["scope"]["since"]


def test_every_route_without_slice_is_unchanged(client):
    r = _get(client, "/entities").json()
    assert isinstance(r, list) and {e["canonical_name"] for e in r} == {"A", "B", "C"}
    assert "scope" not in _get(client, "/entities/A").json()
    r = _get(client, "/entities/A/cooccurrences").json()
    assert isinstance(r, list) and {c["id"] for c in r} == {"B", "C"}
    r = _get(client, "/entities/A/star-graph").json()
    assert {c["id"] for c in r["co_entities"]} == {"B", "C"} and "scope" not in r
    r = _get(client, "/graph/shared-context?a=A&b=B").json()
    assert [d["id"] for d in r["shared_documents"]] == ["d_old"] and "scope" not in r
    r = _get(client, "/graph/neighborhood/A").json()
    assert _ids(r) == {"A", "B", "C"} and "scope" not in r
    r = _post_subgraph(client).json()
    assert _ids(r) == {"A", "B", "C"} and "scope" not in r
    r = _get(client, "/graph/paths?a=A&b=B").json()
    assert r["path_count"] >= 1 and "scope" not in r
    r = _get(client, "/graph/domain-overview/X").json()
    assert r["domain"]["document_count"] == 2 and [d["path"] for d in r["related_domains"]] == ["Y"]
    assert "scope" not in r
    r = _get(client, "/graph/domain/X/neighbours").json()
    assert r["neighbours"] == [{"path": "Y", "weight": 5}] and "scope" not in r


@pytest.mark.parametrize("path", [
    "/entities", "/entities/A", "/entities/A/cooccurrences", "/entities/A/star-graph",
    "/graph/shared-context?a=A&b=C", "/graph/neighborhood/A", "/graph/paths?a=A&b=C",
    "/graph/domain-overview/X", "/graph/domain/X/neighbours",
])
def test_bad_window_400_get(client, path):
    sep = "&" if "?" in path else "?"
    assert client.get(f"{path}{sep}window=nope").status_code == 400


def test_bad_window_400_subgraph(client):
    assert _post_subgraph(client, "?window=nope").status_code == 400


def test_view_beats_adhoc_params(client, view_h):
    # under a view, auth.slice wins: a conflicting ad-hoc window=all is ignored
    r = _get(client, "/entities?window=all", view_h).json()
    assert {e["canonical_name"] for e in r["entities"]} == {"A", "C"}
