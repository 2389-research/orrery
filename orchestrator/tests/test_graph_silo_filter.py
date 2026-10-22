# ABOUTME: GET /graph exposes silo_id + kind on every node; ?silo=/?kind= are now
# ABOUTME: SLICE FACETS routed through the resolver, not a post-filter over the cache.

"""Task 11a / PR4: silo + kind in the graph read path.

`kind` is never stored on a node — it's resolved via the `silo_kind` view join
(documents.silo_id -> silo_kind) INSIDE the builder (`graph_v5.build_graph_v5`).
A bare `?silo=`/`?kind=` now routes through the slice resolver like every other
facet: the sliced builder recomputes node sizes IN-SLICE and includes an entity
iff it has ≥1 in-slice source. The unsliced `get_or_build` snapshot is untouched
by a sliced request (a later unfiltered request still sees every node).
"""


def _seed_silo_graph(store):
    """Three documents, one per provenance shape:

    - d1 -> silo ws1 (a watched_source), kind agent_report
    - d2 -> no silo at all (a loose upload), kind None
    - d3 -> silo c1 (a collection acting as its own silo), kind human_reviewed

    Each has exactly one entity anchored to it, all in the same domain so none of
    them fall out as "unplaceable" (no domain membership).
    """
    c = store.conn
    c.execute("INSERT INTO watched_sources (id, type, uri, provenance_kind) "
              "VALUES ('ws1', 'repo', '/tmp/ws1', 'agent_report')")
    c.execute("INSERT INTO collections (id, name, path, root_path, document_count, provenance_kind) "
              "VALUES ('c1', 'coll1', 'coll1', '/tmp/c1', 1, 'human_reviewed')")
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 3)")

    c.execute("INSERT INTO documents (id, title, silo_id) VALUES ('d1', 'doc1', 'ws1')")
    c.execute("INSERT INTO documents (id, title, silo_id) VALUES ('d2', 'doc2', NULL)")
    c.execute("INSERT INTO documents (id, title, silo_id) VALUES ('d3', 'doc3', 'c1')")
    for d in ("d1", "d2", "d3"):
        c.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) "
                  "VALUES (?, 'alpha', 1, 1.0)", (d,))
    c.execute("INSERT INTO document_collections (document_id, collection_id, role, emits_cooccurrence) "
              "VALUES ('d3', 'c1', 'leaf', 1)")

    for eid, doc in (("e1", "d1"), ("e2", "d2"), ("e3", "d3")):
        c.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, 'Concept')",
                  (eid, f"Entity {eid}"))
        c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)", (eid, doc))
    c.commit()


def _by_id(nodes):
    return {n["id"]: n for n in nodes}


# ── nodes carry silo_id + kind ───────────────────────────────────────────────

def test_graph_nodes_carry_silo_id_and_kind(test_client, test_store):
    _seed_silo_graph(test_store)
    payload = test_client.get("/graph").json()
    nodes = _by_id(payload["nodes"])

    assert nodes["e1"]["silo_id"] == "ws1" and nodes["e1"]["kind"] == "agent_report"
    assert nodes["e2"]["silo_id"] is None and nodes["e2"]["kind"] is None
    assert nodes["e3"]["silo_id"] == "c1" and nodes["e3"]["kind"] == "human_reviewed"

    assert nodes["d1"]["silo_id"] == "ws1" and nodes["d1"]["kind"] == "agent_report"
    assert nodes["d2"]["silo_id"] is None and nodes["d2"]["kind"] is None
    assert nodes["d3"]["silo_id"] == "c1" and nodes["d3"]["kind"] == "human_reviewed"

    assert nodes["c1"]["silo_id"] == "c1" and nodes["c1"]["kind"] == "human_reviewed"

    # node_index carries the same fields for every entity, not just the render set.
    assert payload["node_index"]["e2"]["silo_id"] is None
    assert payload["node_index"]["e3"]["kind"] == "human_reviewed"


# ── ?silo= post-filter ───────────────────────────────────────────────────────

def test_silo_filter_returns_only_that_silos_nodes(test_client, test_store):
    _seed_silo_graph(test_store)
    payload = test_client.get("/graph?silo=ws1").json()
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {"e1", "d1"}
    assert set(payload["node_index"]) == {"e1"}


def test_silo_none_returns_the_null_silo_pool(test_client, test_store):
    _seed_silo_graph(test_store)
    payload = test_client.get("/graph?silo=none").json()
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {"e2", "d2"}


def test_silo_filter_does_not_mutate_the_cached_snapshot(test_client, test_store):
    """The cache stays whole: a filtered request followed by an unfiltered one must
    still see every node."""
    _seed_silo_graph(test_store)
    test_client.get("/graph?silo=ws1")
    full = test_client.get("/graph").json()
    assert {n["id"] for n in full["nodes"]} == {"e1", "e2", "e3", "d1", "d2", "d3", "c1"}


# ── ?kind= post-filter ───────────────────────────────────────────────────────

def test_kind_filter_returns_only_that_kind(test_client, test_store):
    _seed_silo_graph(test_store)
    payload = test_client.get("/graph?kind=agent_report").json()
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {"e1", "d1"}

    payload2 = test_client.get("/graph?kind=human_reviewed").json()
    ids2 = {n["id"] for n in payload2["nodes"]}
    assert ids2 == {"e3", "d3", "c1"}


def test_silo_and_kind_combine_as_and(test_client, test_store):
    _seed_silo_graph(test_store)
    # ws1 IS agent_report, so this should match; a mismatched pair matches nothing.
    payload = test_client.get("/graph?silo=ws1&kind=agent_report").json()
    assert {n["id"] for n in payload["nodes"]} == {"e1", "d1"}

    payload_empty = test_client.get("/graph?silo=ws1&kind=human_reviewed").json()
    assert payload_empty["nodes"] == []
