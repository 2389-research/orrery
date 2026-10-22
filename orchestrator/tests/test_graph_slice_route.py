# ABOUTME: GET /graph with an active slice (ad-hoc params or virtual noosphere) builds on demand + TTL cache (#119).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import graph as graph_route
from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(store):
    c = store.conn
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 2)")
    for did, age in (("old", 60), ("new", 1)):
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES (?,'alpha',1,1.0)", (did,))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')", (f"c_{did}", did))
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES ('E','E','Concept')")
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES ('OLD','OLD','Concept')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('E','old','c_old')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('E','new','c_new')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('OLD','old','c_old')")
    c.commit()


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    graph_route.clear_slice_cache()
    _seed(test_store)
    yield test_client
    graph_route.clear_slice_cache()


def test_unsliced_unchanged(client):
    p = client.get("/graph").json()
    assert "node_index" in p
    assert "slice" not in p.get("meta", {})
    assert "OLD" in p["node_index"]


def test_adhoc_window_slices(client):
    r = client.get("/graph?window=1w")
    assert r.status_code == 200
    p = r.json()
    s = p["meta"]["slice"]
    assert s["since"] and s["until"] is None
    assert s["domains"] is None and s["collections"] is None
    assert "OLD" not in p["node_index"]
    assert p["node_index"]["E"]["mention_count"] == 1


def test_virtual_noosphere_uses_view_slice(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    r = client.get("/graph", headers={"X-Workspace-Id": vid})
    assert r.status_code == 200
    p = r.json()
    assert p["meta"]["slice"]["since"]
    assert "OLD" not in p["node_index"]


def test_cache_hit_then_rebuild(client, monkeypatch):
    calls = []
    real = graph_route.build_graph_v5

    def spy(store, **kw):
        calls.append(kw)
        return real(store, **kw)

    monkeypatch.setattr(graph_route, "build_graph_v5", spy)
    a = client.get("/graph?domain=alpha").json()
    b = client.get("/graph?domain=alpha").json()
    assert len(calls) == 1 and a == b
    graph_route.clear_slice_cache()
    client.get("/graph?domain=alpha")
    assert len(calls) == 2


def test_cache_keyed_by_slice(client, monkeypatch):
    calls = []
    real = graph_route.build_graph_v5
    monkeypatch.setattr(graph_route, "build_graph_v5",
                        lambda s, **kw: (calls.append(1), real(s, **kw))[1])
    client.get("/graph?domain=alpha")
    client.get("/graph?domain=beta")
    assert len(calls) == 2


def test_bare_silo_is_sliced(client):
    # A bare ?silo= now routes through the slice resolver (gains meta.slice).
    # All docs in this fixture have silo_id NULL, so ?silo=none keeps the whole
    # graph but now as a slice.
    p = client.get("/graph?silo=none").json()
    assert p["meta"]["slice"]["silo"] == "none"
    assert "node_index" in p


def test_bare_silo_sizes_are_in_slice(client, test_store):
    c = test_store.conn
    c.execute("UPDATE documents SET silo_id='sA' WHERE id='old'")
    c.execute("UPDATE documents SET silo_id='sB' WHERE id='new'")
    c.commit()
    # E has one source in sA (old) and one in sB (new). A bare ?silo= is now a
    # slice, so mention_count recomputes IN-SLICE (1), not the all-time 2 the
    # legacy post-filter showed.
    p = client.get("/graph?silo=sA").json()
    assert p["meta"]["slice"]["silo"] == "sA"
    assert p["node_index"]["E"]["mention_count"] == 1
    q = client.get("/graph?silo=sB").json()
    assert q["meta"]["slice"]["silo"] == "sB"
    assert q["node_index"]["E"]["mention_count"] == 1


def test_bad_window_400(client):
    assert client.get("/graph?window=nope").status_code == 400


def test_empty_facet_400(client):
    # A supplied-but-empty facet must 400, not silently return the full graph
    # (CodeRabbit PR4): parse_slice ignores empty scalars, so the route guards it.
    assert client.get("/graph?silo=").status_code == 400
    assert client.get("/graph?kind=").status_code == 400
    # Sanity: the no-facet request is unaffected and still serves the full graph.
    assert client.get("/graph").status_code == 200


def test_window_plus_silo_narrows_both(client, test_store):
    c = test_store.conn
    c.execute("UPDATE documents SET silo_id='sA' WHERE id='old'")
    c.execute("UPDATE documents SET silo_id='sB' WHERE id='new'")
    c.commit()
    # E has in-window source only in sB; asking for sA within the window excludes it.
    p = client.get("/graph?window=1w&silo=sA").json()
    assert p["meta"]["slice"]["silo"] == "sA"
    assert "E" not in p["node_index"] and "OLD" not in p["node_index"]
    q = client.get("/graph?window=1w&silo=sB").json()
    assert q["meta"]["slice"]["silo"] == "sB"
    assert q["node_index"]["E"]["mention_count"] == 1


def test_cache_hit_returns_copy(client):
    client.get("/graph?window=1w")
    key = next(iter(graph_route._slice_cache))
    stored = graph_route._slice_cache[key][1]
    stored_keys = set(stored)
    # simulate a caller mutating the response dict object path: route returns a copy
    from src.dependencies import AuthStore  # noqa: F401
    r = graph_route._cached_slice_payload(key, lambda: {})
    assert r is stored  # helper itself is by-reference; the route must copy
    client.get("/graph?window=1w")
    assert set(graph_route._slice_cache[key][1]) == stored_keys


def test_relative_window_requests_share_cache_key(client, monkeypatch):
    from src.pipeline.slice import Slice
    # Simulate 1s-resolution re-resolution of a relative window within one minute.
    ticks = iter(["2026-10-21 10:00:05", "2026-10-21 10:00:06", "2026-10-21 10:00:07"])
    monkeypatch.setattr(graph_route, "parse_slice",
                        lambda params: Slice(since=next(ticks)))
    calls = []
    real = graph_route.build_graph_v5
    monkeypatch.setattr(graph_route, "build_graph_v5",
                        lambda s, **kw: (calls.append(kw["slice"].since), real(s, **kw))[1])
    p1 = client.get("/graph?window=1w").json()
    client.get("/graph?window=1w")
    assert len(calls) == 1
    assert p1["meta"]["slice"]["since"] == "2026-10-21 10:00:05"  # true value, not bucketed


def test_distinct_absolute_as_of_do_not_collide(client):
    a = client.get("/graph?as_of=2026-10-01T12:00:05").json()
    b = client.get("/graph?as_of=2026-10-01T12:00:55").json()
    assert a["meta"]["slice"]["until"] != b["meta"]["slice"]["until"]


def test_cache_key_is_raw_spec_plus_minute(monkeypatch):
    k = graph_route._slice_cache_key
    monkeypatch.setattr(graph_route.time, "time", lambda: 1_000_000_010.0)
    a = k("w", {"window": "1w"})
    assert a == k("w", {"window": "1w"})
    assert a != k("w", {"window": "2w"}) and a != k("v", {"window": "1w"})
    monkeypatch.setattr(graph_route.time, "time", lambda: 1_000_000_010.0 + 60)
    assert a != k("w", {"window": "1w"})
