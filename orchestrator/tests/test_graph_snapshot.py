# ABOUTME: Tests for the materialized /graph snapshot — build shape (positions
# ABOUTME: for all, render top-N), dirty flip, cached serve, cached repo positions.

from tests.graph_payload import domain_weights, entity_id, node_index, render_entities
from src.pipeline.graph_snapshot import (
    build_graph_payload,
    rebuild_snapshot,
    load_snapshot,
    save_snapshot,
    is_dirty,
    get_or_build,
)
from src.db import mark_graph_dirty


def _seed_entities(store, n=5, doc="d1", domain="alpha"):
    """One doc in one domain; entity i gets (i+1) sources → deterministic rank."""
    c = store.conn
    c.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (doc, doc))
    c.execute(
        "INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) "
        "VALUES (?, ?, 1, 1.0)",
        (doc, domain),
    )
    for i in range(n):
        eid = f"e{i}"
        c.execute(
            "INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, 'Concept')",
            (eid, f"ent{i}"),
        )
        for _ in range(i + 1):
            c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)", (eid, doc))
    c.commit()


def test_positions_cover_all_nodes_render_set_is_capped(test_store):
    _seed_entities(test_store, n=5)

    payload = build_graph_payload(test_store, max_render_nodes=2)

    # Positions exist for EVERY entity (the Phase-2 hydration enabler)...
    assert set(node_index(payload).keys()) == {"e0", "e1", "e2", "e3", "e4"}
    assert len(node_index(payload)) == 5   # was total_node_count (migrated, phase 1)
    # ...but only the top-N by source_count are in the rendered set.
    assert len(render_entities(payload)) == 2    # was render_node_count (migrated, phase 1)
    rendered_ids = [entity_id(e) for e in render_entities(payload)]
    assert rendered_ids == ["e4", "e3"]  # 5 and 4 sources — highest degree first

    # Every render node also appears in positions (positions is a superset).
    for e in render_entities(payload):
        assert entity_id(e) in node_index(payload)

    # Position records carry what the viz needs to place + label a node.
    p = node_index(payload)["e4"]
    # field names moved with the vocabulary: name/videoCount -> label/degree
    assert (p.get("label") or p.get("name")) == "ent4"
    assert (p.get("degree") if "degree" in p else p.get("videoCount")) == 5
    assert domain_weights(p) == {"alpha": 1.0}


def test_invalidated_entities_excluded(test_store):
    _seed_entities(test_store, n=3)
    test_store.conn.execute(
        "UPDATE entities SET invalid_at = CURRENT_TIMESTAMP WHERE id = 'e2'"
    )
    test_store.conn.commit()

    payload = build_graph_payload(test_store)
    assert "e2" not in node_index(payload)
    assert {"e0", "e1"} == set(node_index(payload).keys())


def test_save_and_load_snapshot_clears_dirty(test_store):
    _seed_entities(test_store, n=3)
    # Seed row starts dirty (needs a first build).
    assert is_dirty(test_store) is True

    payload = rebuild_snapshot(test_store)
    assert is_dirty(test_store) is False

    cached = load_snapshot(test_store)
    assert cached is not None
    assert len(node_index(cached)) == len(node_index(payload)) == 3


def test_mark_dirty_flips_bit(test_store):
    _seed_entities(test_store, n=2)
    rebuild_snapshot(test_store)
    assert is_dirty(test_store) is False

    mark_graph_dirty(test_store.conn)
    test_store.conn.commit()
    assert is_dirty(test_store) is True


def test_get_or_build_serves_stale_cache_not_a_rebuild(test_store):
    """get_or_build serves the cached blob even when dirty — the background task
    owns rebuilds, so a new write is NOT reflected until then."""
    _seed_entities(test_store, n=2)
    rebuild_snapshot(test_store)  # cache 2 nodes

    # Add a third entity and flag dirty — but don't rebuild.
    c = test_store.conn
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e9', 'late', 'Concept')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('e9', 'd1')")
    mark_graph_dirty(c)
    c.commit()

    served = get_or_build(test_store)
    assert len(node_index(served)) == 2  # stale cache, not the new 3-node graph


def test_get_or_build_builds_when_no_snapshot(test_store):
    _seed_entities(test_store, n=2)
    # No payload yet (only the dirty seed row) → inline build.
    assert load_snapshot(test_store) is None
    served = get_or_build(test_store)
    assert len(node_index(served)) == 2
    # ...and it persisted the build.
    assert load_snapshot(test_store) is not None


def test_repo_positioned_at_domain_centroid(test_store):
    """A repo sits at the weighted centroid of its docs' domain positions — in
    the same frame as the domains (not projected to a corner)."""
    from src.pipeline.graph_snapshot import _collection_positions
    c = test_store.conn
    c.execute("INSERT INTO documents (id, title) VALUES ('rd1', 'f1')")
    c.execute("INSERT INTO documents (id, title) VALUES ('rd2', 'f2')")
    c.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES ('rd1','alpha',1,1.0)")
    c.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES ('rd2','beta',1,1.0)")
    test_store.collections.create("r1", "repo-one", "repo-one", "/collections/repo-one")
    test_store.collections.link_document("rd1", "r1", role="leaf")
    test_store.collections.link_document("rd2", "r1", role="leaf")
    c.commit()

    domain_positions = {"alpha": {"x": 0.2, "y": 0.4}, "beta": {"x": 0.8, "y": 0.6}}
    pos = _collection_positions(test_store, [{"id": "r1", "name": "repo-one"}], domain_positions)
    # centroid of (0.2,0.4) and (0.8,0.6)
    assert abs(pos["r1"]["x"] - 0.5) < 1e-6
    assert abs(pos["r1"]["y"] - 0.5) < 1e-6


def test_graph_route_serves_snapshot(test_client, test_store):
    _seed_entities(test_store, n=3)
    r = test_client.get("/graph")
    assert r.status_code == 200
    body = r.json()
    assert "node_index" in body or "positions" in body
    assert len(node_index(body)) == 3
    assert len(render_entities(body)) == 3


def test_a_snapshot_from_another_contract_version_is_discarded(test_store):
    """A cached payload built under a different contract must be rebuilt, not served.

    The 5.0.0 -> 5.1.0 rename kept every layer and key in place and changed only
    VALUES (`scope: "repo"` -> `"collection"`, `container_type`, the node `type`). So a
    stale payload still parses, still has `meta`, and simply goes QUIET: the collection
    structure endpoint filters on the new scope and finds nothing, losing every
    shared-entity link with no error raised anywhere. Version equality is the only
    check that catches a change of that shape.
    """
    import json

    rebuild_snapshot(test_store)
    assert load_snapshot(test_store) is not None

    stale = load_snapshot(test_store)
    stale["meta"]["schema_version"] = "5.0.0"
    test_store.conn.execute("UPDATE graph_snapshot SET payload = ? WHERE id = 'current'",
                            (json.dumps(stale),))
    test_store.conn.commit()

    assert load_snapshot(test_store) is None, \
        "a payload from another contract version must be discarded so it gets rebuilt"


def _seed_collection_in_domain(store, cid, domain):
    """One collection whose single doc is classified into `domain`."""
    c = store.conn
    store.collections.create(cid, cid, cid, f"/{cid}")
    c.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (f"{cid}-d", cid))
    store.collections.link_document(f"{cid}-d", cid, role="leaf")
    c.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) "
              "VALUES (?, ?, 1, 1.0)", (f"{cid}-d", domain))
    c.commit()


def test_same_domain_collections_are_declumped(test_store):
    # Three collections whose docs are ~entirely in ONE domain resolve to that domain's
    # exact centroid — the real pile-up (e.g. pure llm-orchestration repos). They must be
    # fanned out to distinct points, all still near the domain.
    from src.pipeline.graph_snapshot import _collection_positions
    import math

    for cid in ("ca", "cb", "cc"):
        _seed_collection_in_domain(test_store, cid, "alpha")
    dom = {"alpha": {"x": 0.40, "y": 0.90}}
    pos = _collection_positions(test_store, [{"id": "ca"}, {"id": "cb"}, {"id": "cc"}], dom)

    pts = {(round(p["x"], 6), round(p["y"], 6)) for p in pos.values()}
    assert len(pts) == 3, "same-domain collections must not stack on one pixel"
    for k in ("ca", "cb", "cc"):
        assert math.dist((pos[k]["x"], pos[k]["y"]), (0.40, 0.90)) < 0.05, "stay inside the domain"


def test_single_collection_position_is_unchanged_by_declump(test_store):
    # A collection alone at its centroid must NOT be moved (declump only touches collisions).
    from src.pipeline.graph_snapshot import _collection_positions
    _seed_collection_in_domain(test_store, "solo", "beta")
    dom = {"beta": {"x": 0.30, "y": 0.70}}
    pos = _collection_positions(test_store, [{"id": "solo"}], dom)
    assert pos["solo"] == {"x": 0.30, "y": 0.70}
