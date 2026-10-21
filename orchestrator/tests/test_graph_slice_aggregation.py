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


# ── review follow-ups ────────────────────────────────────────────────────────

def _doc(c, did, created, domain, invalid_at=None):
    c.execute("INSERT INTO documents (id,title,created_at,invalid_at) VALUES (?,?,?,?)",
              (did, did, created, invalid_at))
    c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES (?,?,1,1.0)",
              (did, domain))
    c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')", (f"c_{did}", did))


def _src(c, eid, did):
    c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES (?,?,?)", (eid, did, f"c_{did}"))


def _ent(c, eid):
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'Concept')", (eid, eid))


def test_as_of_slice_is_self_consistent(test_store):
    c = test_store.conn
    _doc(c, "a1", "2026-10-05 00:00:00", "alpha")
    # invalidated AFTER the cutoff -> as-of the cutoff it still counts
    _doc(c, "b1", "2026-10-06 00:00:00", "beta", invalid_at="2026-10-20 00:00:00")
    _ent(c, "E"); _src(c, "E", "a1"); _src(c, "E", "b1"); c.commit()
    p = build_graph_v5(test_store, slice=Slice(until="2026-10-12 00:00:00"))
    n = p["node_index"]["E"]
    assert n["mention_count"] == 2
    w = {m["id"]: m["weight"] for m in n["memberships"] if m["container_type"] == "domain"}
    assert w == {"alpha": 0.5, "beta": 0.5}
    assert ("alpha", "beta") in _domain_routes(p)


def test_document_nodes_come_from_in_slice_docs(test_store):
    c = test_store.conn
    _doc(c, "old1", "2026-10-01 00:00:00", "alpha")
    for i in range(60):
        _doc(c, f"new{i}", "2026-10-19 00:00:00", "beta")
    c.commit()
    p = build_graph_v5(test_store, slice=Slice(until="2026-10-10 00:00:00"))
    assert [n["id"] for n in p["nodes"] if n["type"] == "document"] == ["old1"]


def test_route_one_sided_joins_both_fail(test_store):
    c = test_store.conn
    # in-slice docs: x1(alpha), y_in(beta). out-of-slice: z1(gamma), w1(delta)
    _doc(c, "x1", "2026-10-19 00:00:00", "alpha")
    _doc(c, "y_in", "2026-10-19 00:00:00", "beta")
    _doc(c, "z1", "2026-10-01 00:00:00", "gamma")
    _doc(c, "w1", "2026-10-01 00:00:00", "delta")
    _ent(c, "E1"); _src(c, "E1", "x1"); _src(c, "E1", "z1")   # alpha(in) < gamma(out)
    _ent(c, "E2"); _src(c, "E2", "w1"); _src(c, "E2", "y_in")  # delta(out) < ... mirror
    c.commit()
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    assert _domain_routes(p) == set()
    full = build_graph_v5(test_store)
    assert ("alpha", "gamma") in _domain_routes(full) and ("beta", "delta") in _domain_routes(full)


def test_entity_with_two_sources_one_in_slice(test_store):
    _seed(test_store)
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    n = p["node_index"]["E"]
    assert n["mention_count"] == 1 and n["degree"] == n["mention_count"]
    # first/last seen stay all-time
    assert n["first_seen"] == "2026-10-05 00:00:00" and n["last_seen"] == "2026-10-19 00:00:00"


def _coll(c, cid, docs):
    c.execute("INSERT INTO collections (id,name,path,document_count) VALUES (?,?,?,?)",
              (cid, cid, f"/{cid}", len(docs)))
    for d in docs:
        c.execute("INSERT INTO document_collections (document_id,collection_id,emits_cooccurrence) VALUES (?,?,1)", (d, cid))


def test_collection_routes_and_edges_sliced(test_store):
    c = test_store.conn
    _doc(c, "a1", "2026-10-19 00:00:00", "alpha")
    _doc(c, "b1", "2026-10-19 00:00:00", "alpha")
    _doc(c, "o1", "2026-10-01 00:00:00", "alpha")
    _ent(c, "E"); _src(c, "E", "a1"); _src(c, "E", "b1")
    _ent(c, "F"); _src(c, "F", "a1"); _src(c, "F", "o1")
    _coll(c, "cA", ["a1"]); _coll(c, "cB", ["b1"]); _coll(c, "cO", ["o1"])
    c.execute("INSERT INTO collection_edges (source,target,type) VALUES ('cA','cB','uses')")
    c.execute("INSERT INTO collection_edges (source,target,type) VALUES ('cA','cO','uses')")
    c.commit()
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    coll = {tuple(sorted((e["source"], e["target"]))): e["type"] for e in p["edges"] if e["scope"] == "collection"}
    assert coll[("cA", "cB")] in ("cooccurrence", "uses")
    assert ("cA", "cO") not in coll          # cO has no in-slice doc; F route (a1<->o1) gone, edge hidden
    assert {n["id"] for n in p["nodes"] if n["type"] == "collection"} == {"cA", "cB"}
    assert any(e["type"] == "cooccurrence" and e["scope"] == "collection" for e in p["edges"])
    assert any(e["type"] == "uses" for e in p["edges"])


def test_in_slice_without_temp_table_raises(test_store):
    import pytest
    test_store.conn.execute("DROP TABLE IF EXISTS _slice_docs")
    with pytest.raises(RuntimeError, match="ensure_slice_doc_temp_table"):
        test_store.relationships.get_trade_routes(in_slice=True)
    with pytest.raises(RuntimeError, match="ensure_slice_doc_temp_table"):
        test_store.collections.get_collection_routes(in_slice=True)
    with pytest.raises(RuntimeError, match="ensure_slice_doc_temp_table"):
        test_store.collections.get_collection_weights(in_slice=True)


def test_entity_collection_memberships_are_sliced(test_store):
    c = test_store.conn
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 2)")
    _doc(c, "in1", "2026-10-19 00:00:00", "alpha")
    _doc(c, "in2", "2026-10-19 00:00:00", "alpha")
    _doc(c, "out1", "2026-10-01 00:00:00", "alpha")
    _ent(c, "E")
    for d in ("in1", "in2", "out1"):
        _src(c, "E", d)
    _coll(c, "CA", ["in1", "in2"]); _coll(c, "CB", ["out1"])
    c.commit()
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    colls = {m["id"]: m["weight"] for m in p["node_index"]["E"]["memberships"]
             if m["container_type"] == "collection"}
    assert colls == {"CA": 1.0}  # CB's out-of-slice mass is gone, CA renormalised in-slice
    # no dangling container ids anywhere
    coll_ids = {n["id"] for n in p["nodes"] if n["type"] == "collection"}
    dom_ids = {t["path"] for t in p["taxonomy"]}
    for n in list(p["node_index"].values()) + list(p["nodes"]):
        for m in n["memberships"]:
            assert m["id"] in (coll_ids if m["container_type"] == "collection" else dom_ids)


def test_entity_silo_is_in_slice(test_store):
    c = test_store.conn
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 2)")
    _doc(c, "n1", "2026-10-19 00:00:00", "alpha")
    _doc(c, "o1", "2026-10-01 00:00:00", "alpha")
    _doc(c, "o2", "2026-10-01 00:00:00", "alpha")
    c.execute("UPDATE documents SET silo_id='sNew' WHERE id='n1'")
    c.execute("UPDATE documents SET silo_id='sOld' WHERE id IN ('o1','o2')")
    _ent(c, "E")
    for d in ("n1", "o1", "o2"):
        _src(c, "E", d)
    c.commit()
    p = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    assert p["node_index"]["E"]["silo_id"] == "sNew"
