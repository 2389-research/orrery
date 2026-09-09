# ABOUTME: GET /export/html must produce a zip that renders the galaxy with NO calls
# ABOUTME: back to Orrery — and must fail loudly if the canonical viz moves under it.

"""The export reuses the real viz by string-patching its index.html, which makes it
fragile in exactly one way: if `frontend/public/viz/index.html` is refactored, an
anchor stops matching. That must be a loud failure, not an exported page that
silently never loads its data — hence `test_patches_apply_to_the_real_viz`, which is
the regression guard for the whole feature.
"""

import io
import json
import zipfile

import pytest

from src.pipeline.graph_export import (
    build_export_payload,
    entity_detail,
    export_counts,
    patch_viz_index,
    viz_asset_dir,
)


# ── the regression guard: patches must fit the CANONICAL viz ────────────────────

def test_patches_apply_to_the_real_viz():
    viz = viz_asset_dir()
    assert viz is not None, "canonical viz not found — export cannot work"
    out = patch_viz_index((viz / "index.html").read_text())

    # data comes from the baked file, not the API
    assert "./graph.json" in out
    assert "fetch(graphUrl" in out                       # still the same load path
    assert "${API_URL}/graph`" not in out                # but no longer the API
    # the shell-provided pieces are substituted locally
    assert 'id="detail"' in out and "renderPanel(" in out
    assert "window.__ORRERY_DETAIL__" in out             # baked neighbours/docs
    # the live cooccurrence call is gone
    assert "cooccurrences?limit=20" not in out


def test_patch_rejects_a_duplicated_anchor():
    """A duplicated anchor would inject a helper twice and yield a blank page on an
    otherwise successful export, so uniqueness is asserted, not just presence."""
    viz = viz_asset_dir()
    src = (viz / "index.html").read_text()
    doubled = src.replace("const state = new WorldState();",
                          "const state = new WorldState();\nconst state2 = new WorldState();", 1)
    doubled = doubled.replace("const state2 = new WorldState();", "const state = new WorldState();")
    with pytest.raises(RuntimeError, match="found 2x"):
        patch_viz_index(doubled)


# ── payload shape ───────────────────────────────────────────────────────────────

def _snapshot(n_entities=3, n_colls=1):
    nodes = [{"id": f"e{i}", "type": "entity", "label": f"ent{i}", "degree": 10 - i,
              "memberships": [{"container_type": "domain", "id": "a/b", "weight": 1.0}]}
             for i in range(n_entities)]
    nodes += [{"id": f"c{i}", "type": "collection", "label": f"repo{i}", "degree": 5,
               "memberships": [{"container_type": "domain", "id": "a/b", "weight": 1.0}]}
              for i in range(n_colls)]
    return {
        "meta": {"schema_version": "5.1.0", "counts_by_type": {"entity": {"total": 99999}},
                 "pruning": {"policy": "top_n_by_degree"}},
        "taxonomy": [{"id": "d1", "path": "a/b", "document_count": 4}],
        "nodes": nodes,
        "node_index": {n["id"]: n for n in nodes},
        "edges": [{"source": "a/b", "target": "a/c", "scope": "domain", "weight": 3},
                  {"source": "a/b", "target": "a/d", "scope": "domain", "weight": 1}],
        "layout": {"positions": {"a/b": {"x": 0.5, "y": 0.5}}, "palette": {"a/b": "#abc"}},
    }


def test_payload_keeps_render_set_and_slims_node_index(test_store):
    p = build_export_payload(test_store.conn, _snapshot(), detail_neighbours=0)
    assert len(p["nodes"]) == 4
    # node_index exists for hydrating nodes OUTSIDE the render set; here it is the
    # whole payload, so it must not duplicate every full record
    assert set(p["node_index"]) == {n["id"] for n in p["nodes"]}
    assert set(p["node_index"]["e0"]) == {"id", "type", "label"}


def test_payload_drops_whole_graph_telemetry(test_store):
    p = build_export_payload(test_store.conn, _snapshot(), detail_neighbours=0)
    assert "pruning" not in p["meta"] and "counts_by_type" not in p["meta"]
    assert p["meta"]["export"] is True


def test_min_route_weight_prunes_routes(test_store):
    p = build_export_payload(test_store.conn, _snapshot(), min_route_weight=2, detail_neighbours=0)
    assert [e["weight"] for e in p["edges"]] == [3]      # the weight-1 route is gone


def test_cap_reserves_a_quota_per_collection(test_store):
    """A flat cap lets globally-recurring entities crowd out a repo's own, leaving a
    collection marker with nothing around it."""
    snap = _snapshot(n_entities=0, n_colls=1)
    # two hubby entities with no collection, one modest entity inside the repo
    snap["nodes"] += [
        {"id": "hub1", "type": "entity", "label": "hub1", "degree": 900, "memberships": []},
        {"id": "hub2", "type": "entity", "label": "hub2", "degree": 800, "memberships": []},
        {"id": "own", "type": "entity", "label": "own", "degree": 1,
         "memberships": [{"container_type": "collection", "id": "c0", "weight": 1.0}]},
    ]
    kept = {n["id"] for n in build_export_payload(
        test_store.conn, snap, max_entities=2, per_repo_min=1, detail_neighbours=0)["nodes"]}
    assert "own" in kept, "the repo's own entity must survive the cap"

    flat = {n["id"] for n in build_export_payload(
        test_store.conn, snap, max_entities=2, per_repo_min=0, detail_neighbours=0)["nodes"]}
    assert "own" not in flat, "without a quota the hubs win — the bug this guards"


def test_entity_detail_uses_shared_documents(test_store):
    c = test_store.conn
    c.execute("INSERT INTO documents (id, title, content, content_hash) VALUES ('d1','content/posts/my-post/index.md','x','h1')")
    for eid, name in (("e1", "alpha"), ("e2", "beta")):
        c.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?,?,'Concept')", (eid, name))
        c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, 'd1')", (eid,))
    c.commit()

    det = entity_detail(c, ["e1", "e2"])
    assert det["e1"]["nbrs"] == [["e2", 1]]              # co-occurrence via the shared doc
    assert det["e1"]["docs"] == ["my post"]             # path prettified for a public panel


# ── the endpoints ───────────────────────────────────────────────────────────────

def test_export_info_reports_exportability(test_client, test_store):
    r = test_client.get("/export/info")
    assert r.status_code == 200
    body = r.json()
    assert body["exportable"] is True                    # an empty graph is trivially fine
    assert body["viz_assets_available"] is True


def test_export_html_zip_runs_without_orrery(test_client, test_store):
    r = test_client.get("/export/html")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"index.html", "graph.json", "README.txt"} <= names
    assert any(n.startswith("core/") for n in names)
    assert any(n.startswith("renderers/") for n in names)

    index = z.read("index.html").decode()
    assert "${API_URL}/graph`" not in index              # no call back to Orrery
    assert "cooccurrences?limit=20" not in index
    json.loads(z.read("graph.json"))                     # payload is valid JSON


def test_export_refuses_a_graph_too_large_to_be_usable(test_client, monkeypatch):
    """Emitting a page nobody can load is worse than refusing with the dials named.

    The snapshot is stubbed rather than leaning on the fixture graph's size: relying
    on "the test DB happens to be empty" made this pass or fail depending on test
    ORDER under pytest-randomly.
    """
    import src.routes.export as ex
    big = {"meta": {"schema_version": "5.1.0"}, "taxonomy": [], "node_index": {},
           "edges": [], "layout": {},
           "nodes": [{"id": f"e{i}", "type": "entity", "label": f"e{i}",
                      "degree": i, "memberships": []} for i in range(5)]}
    monkeypatch.setattr(ex, "get_or_build", lambda store: big)
    monkeypatch.setattr(ex, "MAX_EXPORTABLE_ENTITIES", 2)

    r = test_client.get("/export/html")
    assert r.status_code == 413
    body = json.dumps(r.json())
    assert "max_entities" in body and "min_route_weight" in body   # names the dials
    assert '"entities": 5' in body or "'entities': 5" in body      # and the real size

    # capping is accepted as the answer, and the cap is honoured
    ok = test_client.get("/export/html?max_entities=3")
    assert ok.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(ok.content))
    payload = json.loads(z.read("graph.json"))
    assert sum(1 for n in payload["nodes"] if n["type"] == "entity") == 3


def test_export_counts_reports_pruning_rather_than_hiding_it():
    """A snapshot is a RENDER set. Exporting a big graph does not fail on size — it
    quietly ships the top-N by degree — so the counts must expose that."""
    snap = _snapshot(n_entities=3, n_colls=0)
    snap["meta"]["counts_by_type"] = {"entity": {"total": 97220}}
    c = export_counts(snap)
    assert c["entities"] == 3 and c["entities_total"] == 97220 and c["pruned"] is True

    snap["meta"]["counts_by_type"] = {"entity": {"total": 3}}
    assert export_counts(snap)["pruned"] is False


def test_export_info_says_when_it_is_only_a_slice(test_client, monkeypatch):
    import src.routes.export as ex
    snap = _snapshot(n_entities=2, n_colls=0)
    snap["meta"]["counts_by_type"] = {"entity": {"total": 50000}}
    monkeypatch.setattr(ex, "get_or_build", lambda store: snap)
    body = test_client.get("/export/info").json()
    assert body["pruned"] is True and body["entities_total"] == 50000
    assert body["exportable"] is True          # a top-N export is still allowed
    assert "not the whole graph" in body["note"]
