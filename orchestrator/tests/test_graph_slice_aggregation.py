# ABOUTME: build_graph_v5(slice=...) counts entities/routes/domains WITHIN the slice (#119).
from src.pipeline.graph_v5 import build_graph_v5
from src.pipeline.slice import Slice


def _seed(store):
    c = store.conn
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 1)")
    c.execute("INSERT INTO domains (path, document_count) VALUES ('beta', 1)")
    # last week: d1(alpha) & d2(beta) share entity E -> alpha<->beta route.
    # this week: d3(alpha) mentions E once; no beta doc -> no route, E count=1.
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d1','a','2026-10-05 00:00:00')")
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d2','b','2026-10-05 00:00:00')")
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d3','c','2026-10-19 00:00:00')")
    for d, dom in (("d1", "alpha"), ("d2", "beta"), ("d3", "alpha")):
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES (?,?,1,1.0)", (d, dom))
    for d in ("d1", "d2", "d3"):
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')", (f"c_{d}", d))
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES ('E','E','Concept')")
    for d in ("d1", "d2", "d3"):
        c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('E',?,?)", (d, f"c_{d}"))
    c.commit()


def _domain_routes(payload):
    return {tuple(sorted((e["source"], e["target"]))) for e in payload["edges"] if e.get("scope") == "domain"}


def test_this_week_slice_node_count(test_store):
    _seed(test_store)
    payload = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    assert payload["node_index"]["E"]["mention_count"] == 1


def test_domain_route_is_sliced(test_store):
    _seed(test_store)
    lw = build_graph_v5(test_store, slice=Slice(since="2026-10-01 00:00:00", until="2026-10-12 00:00:00"))
    assert ("alpha", "beta") in _domain_routes(lw)
    tw = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    assert ("alpha", "beta") not in _domain_routes(tw)


def test_unsliced_build_is_unchanged(test_store):
    _seed(test_store)
    p = build_graph_v5(test_store)
    assert p["node_index"].get("E") is not None
    assert ("alpha", "beta") in _domain_routes(p)
    assert "mention_count" not in p["node_index"]["E"]


def test_entity_without_in_slice_source_is_dropped(test_store):
    _seed(test_store)
    c = test_store.conn
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES ('OLD','OLD','Concept')")
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('OLD','d1','c_d1')")
    c.commit()
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    assert "OLD" not in p["node_index"]
    assert "E" in p["node_index"]


def test_slice_domains_only_those_with_in_slice_docs(test_store):
    _seed(test_store)
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    tax = {t["path"]: t["document_count"] for t in p["taxonomy"]}
    assert tax == {"alpha": 1}
    lw = build_graph_v5(test_store, slice=Slice(until="2026-10-12 00:00:00"))
    assert {t["path"]: t["document_count"] for t in lw["taxonomy"]} == {"alpha": 1, "beta": 1}
