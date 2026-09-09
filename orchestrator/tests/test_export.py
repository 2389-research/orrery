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
        # A collection-scope edge is REQUIRED here: `or` short-circuits, so a fixture
        # of domain-scope edges alone never evaluates the collection branch — and a
        # NameError in it passed 568 tests.
        "edges": [{"source": "a/b", "target": "a/c", "scope": "domain", "weight": 3},
                  {"source": "a/b", "target": "a/d", "scope": "domain", "weight": 1},
                  {"source": "c0", "target": "e0", "scope": "collection", "weight": 2}],
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
    routes = [e for e in p["edges"] if e["scope"] == "domain"]
    assert [e["weight"] for e in routes] == [3]          # the weight-1 route is gone


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


# ── the zip has to be COMPLETE and the patched page has to PARSE ───────────────

def _module_specifiers(text):
    """Relative import specifiers in an ES module."""
    import re
    return set(re.findall(r"""(?:import|export)[^;'"]*?from\s+['"](\./[^'"]+)['"]""", text)) | \
           set(re.findall(r"""import\s+['"](\./[^'"]+)['"]""", text))


def test_zip_contains_every_module_the_page_imports(test_client, test_store):
    """A missing member is a blank page with a 404 in the console — an export that
    "succeeded". The viz tree is flat today, so nothing would catch a nested module
    being dropped except this."""
    z = zipfile.ZipFile(io.BytesIO(test_client.get("/export/html").content))
    names = set(z.namelist())

    pending = _module_specifiers(z.read("index.html").decode())
    seen = set()
    while pending:
        spec = pending.pop()
        member = spec[2:] if spec.startswith("./") else spec
        # index.html imports './core/x.js'; core/x.js imports './y.js' beside itself
        cands = [member] + [f"{p}/{member}" for p in ("core", "renderers")]
        hit = next((c for c in cands if c in names), None)
        assert hit, f"index.html (or a module) imports {spec!r}, absent from the zip: {sorted(names)}"
        if hit in seen:
            continue
        seen.add(hit)
        base = hit.rsplit("/", 1)[0] if "/" in hit else ""
        for nxt in _module_specifiers(z.read(hit).decode()):
            n = nxt[2:] if nxt.startswith("./") else nxt
            pending.add(f"./{base}/{n}" if base else f"./{n}")
    assert seen, "no modules resolved — the walk itself is broken"


def test_patched_index_is_valid_javascript(test_client, test_store):
    """The tests otherwise only assert substrings, so a patch that lands textually but
    leaves an unbalanced brace exports 200 OK and renders nothing — the exact failure
    the design exists to prevent."""
    import re as _re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    index = zipfile.ZipFile(io.BytesIO(test_client.get("/export/html").content)) \
        .read("index.html").decode()
    scripts = _re.findall(r"<script type=\"module\">(.*?)</script>", index, _re.S)
    assert scripts, "no module script found in the patched index"
    for body in scripts:
        r = subprocess.run([node, "--input-type=module", "--check"], input=body,
                           capture_output=True, text=True)
        assert r.returncode == 0, f"patched module does not parse:\n{r.stderr}"


def test_patch_rejects_a_missing_anchor():
    """The n==0 case shares a branch with n==2 but is the likelier one in practice —
    a refactor deletes or renames a line rather than duplicating it."""
    viz = viz_asset_dir()
    src = (viz / "index.html").read_text()
    gutted = src.replace("const state = new WorldState();", "const state = makeWorld();", 1)
    with pytest.raises(RuntimeError, match="found 0x"):
        patch_viz_index(gutted)


def test_force_bypasses_the_size_guard(test_client, monkeypatch):
    """force= is the flag that disables the only size guard, so it needs a test that
    says what it does."""
    import src.routes.export as ex
    big = {"meta": {"schema_version": "5.1.0"}, "taxonomy": [], "node_index": {},
           "edges": [], "layout": {},
           "nodes": [{"id": f"e{i}", "type": "entity", "label": f"e{i}",
                      "degree": i, "memberships": []} for i in range(5)]}
    monkeypatch.setattr(ex, "get_or_build", lambda store: big)
    monkeypatch.setattr(ex, "MAX_EXPORTABLE_ENTITIES", 2)

    assert test_client.get("/export/html").status_code == 413
    ok = test_client.get("/export/html?force=true")
    assert ok.status_code == 200
    payload = json.loads(zipfile.ZipFile(io.BytesIO(ok.content)).read("graph.json"))
    assert sum(1 for n in payload["nodes"] if n["type"] == "entity") == 5   # nothing capped


def test_export_info_reports_snapshot_freshness(test_client, test_store):
    """The export ships the snapshot as-is and does not rebuild a dirty one, so the
    caller has to be able to see that."""
    body = test_client.get("/export/info").json()
    assert "stale" in body and "built_at" in body
    assert isinstance(body["stale"], bool)


def test_offline_cli_loads_the_canonical_panel_by_path():
    """The panel used to be duplicated in embed/build_embed.py and the copies drifted —
    an HTML-escaping fix landed in one and not the other. The CLI now *assigns* from
    this module, so equality is structural rather than an invariant under test; what
    this actually guards is that the by-path load still resolves and that the CLI's own
    patcher still finds its anchors in the current viz."""
    import importlib.util
    import sys as _sys
    from pathlib import Path as _P
    from src.pipeline import graph_export as ge

    root = _P(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("_be_panel", root / "embed" / "build_embed.py")
    be = importlib.util.module_from_spec(spec)
    _sys.modules["_be_panel"] = be
    spec.loader.exec_module(be)

    # Equality, not identity: the CLI loads graph_export by file path, so it holds a
    # distinct module object. What must hold is that the text is the same text.
    assert be._PANEL_CSS == ge.PANEL_CSS
    assert be._PANEL_HELPER == ge.PANEL_JS

    # The part that CAN regress: build_embed keeps its own patcher (the single-file
    # build bakes a window global instead of fetching ./graph.json), and it has no
    # equivalent of test_patch_rejects_a_missing_anchor.
    viz = viz_asset_dir()
    patched = be._patch_index((viz / "index.html").read_text())
    assert "window.__ORRERY_DETAIL__" in patched and "renderPanel(" in patched
    assert "${API_URL}/graph`" not in patched


def test_collection_edges_are_filtered_to_what_shipped(test_store):
    """Collection-scope edges are keyed by NODE id, so they need filtering against the
    render set. The filter is also the ONLY place `kept_ids` is read: `or`
    short-circuits past it whenever every fixture edge is domain-scope, so a NameError
    here once passed the whole suite and 500'd on any noosphere with two collections
    sharing an entity — i.e. every repo-ingested one."""
    snap = _snapshot(n_entities=1, n_colls=1)
    snap["edges"] = [
        {"source": "c0", "target": "e0", "scope": "collection", "weight": 2},
        {"source": "c0", "target": "gone", "scope": "collection", "weight": 9},
    ]
    p = build_export_payload(test_store.conn, snap, detail_neighbours=0)
    shipped = {n["id"] for n in p["nodes"]}
    assert [(e["source"], e["target"]) for e in p["edges"]] == [("c0", "e0")]
    assert all(e["source"] in shipped and e["target"] in shipped
               for e in p["edges"] if e["scope"] != "domain")


def test_export_info_stale_follows_the_dirty_bit(test_store, test_client):
    """Asserting only that `stale` is a bool would pass a hardcoded value."""
    c = test_store.conn
    c.execute("UPDATE graph_snapshot SET dirty = 0 WHERE id = 'current'")
    c.commit()
    assert test_client.get("/export/info").json()["stale"] is False

    c.execute("UPDATE graph_snapshot SET dirty = 1 WHERE id = 'current'")
    c.commit()
    assert test_client.get("/export/info").json()["stale"] is True


def test_export_reports_the_entity_count_it_actually_shipped(test_client, monkeypatch):
    """The UI used to compute min(entities, limit) client-side, but the per-collection
    quota can overshoot max_entities — so the number shown could exceed the zip's
    contents. The server states it instead."""
    import src.routes.export as ex
    snap = _snapshot(n_entities=4, n_colls=0)
    monkeypatch.setattr(ex, "get_or_build", lambda store: snap)

    r = test_client.get("/export/html?max_entities=2")
    shipped = int(r.headers["X-Orrery-Exported-Entities"])
    payload = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("graph.json"))
    assert shipped == sum(1 for n in payload["nodes"] if n["type"] == "entity")
