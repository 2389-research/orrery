# ABOUTME: Export the active noosphere's galaxy as a self-contained static site.
# ABOUTME: GET /export/html returns a zip that runs offline; /export/graph.json is
# ABOUTME: the payload alone; /export/info reports whether this graph is exportable.
"""Static HTML export of a noosphere.

`GET /export/html` returns a zip holding the canonical viz plus the graph baked in.
Unzip it anywhere, open `index.html` (or `<iframe src=".../index.html">`) and it
runs with **no requests back to Orrery**.

The zip is the FOLDER form on purpose: `index.html` + `core/` + `renderers/` +
`graph.json`. It needs no bundler (so this endpoint is pure Python — the
orchestrator image has no node), it lets a web server gzip and cache the data
separately from the code, and it is the form you actually want on a site. The
single-file variant needs esbuild and stays in the offline CLI
(`embed/build_embed.py`).

Scope: the export is **galaxy-level**. The drill-in pages (`star.html`,
`collection.html`) fetch their own APIs and search is shell-side, so neither is
included — see `pipeline/graph_export.py` for what is substituted locally instead.

Size: `/export/html` refuses above `MAX_EXPORTABLE_ENTITIES`, naming the two dials
(`max_entities` for framerate, `min_route_weight` for bytes) instead of emitting an
unusable page. In practice that rarely fires, because the snapshot is ALREADY pruned
to `graph_render_max_nodes` — so a 97k-entity noosphere exports as its top 3000 by
degree and succeeds. `/export/info` reports `entities_total` and `pruned` so the UI
can say the export is a slice rather than implying it is complete.

Freshness: the export ships the materialized snapshot as-is and does not rebuild a
dirty one (see the note in `_payload`), so `/export/info` also reports `built_at` and
`stale`.
"""

import io
import json
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..dependencies import get_auth_store, AuthStore
from ..pipeline.graph_export import (
    MAX_EXPORTABLE_ENTITIES,
    build_export_payload,
    export_counts,
    patch_viz_index,
    viz_asset_dir,
)
from ..pipeline.graph_snapshot import get_or_build

router = APIRouter()


def _payload(auth: AuthStore, max_entities: int, per_repo_min: int,
             min_route_weight: int, detail_neighbours: int, force: bool):
    store = auth.store
    try:
        # NOTE: get_or_build does NOT rebuild a dirty snapshot — the background task
        # owns that, and only a completely missing snapshot builds inline. So an export
        # taken right after a large ingest can predate it. Rebuilding inline here would
        # make the request take as long as a full graph build, so instead /export/info
        # reports `stale` + `built_at` and the caller can wait.
        snapshot = get_or_build(store)
        # Guard on the RENDER set — that is what actually ships. It is normally
        # already <= graph_render_max_nodes, so this fires only if that cap was
        # raised; the "your graph is much bigger than this export" case is reported
        # by /export/info rather than refused, since a top-N export is still a
        # legitimate thing to want.
        total = export_counts(snapshot)["entities"]
        if total > MAX_EXPORTABLE_ENTITIES and not max_entities and not force:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "graph too large to export as a usable page",
                    "entities": total,
                    "limit": MAX_EXPORTABLE_ENTITIES,
                    "hint": ("pass max_entities to cap it (entities cost framerate), and/or "
                             "min_route_weight to shrink the download (trade routes are most "
                             "of the bytes but cost no framerate). A dedicated, smaller "
                             "noosphere exports best."),
                },
            )
        payload = build_export_payload(
            store.conn, snapshot,
            max_entities=max_entities, per_repo_min=per_repo_min,
            min_route_weight=min_route_weight, detail_neighbours=detail_neighbours,
        )
        return payload
    finally:
        store.close()


@router.get("/export/info")
def export_info(auth: AuthStore = Depends(get_auth_store)):
    """Can this noosphere be exported, and how big would it be? For the UI to ask
    before offering the download."""
    store = auth.store
    try:
        counts = export_counts(get_or_build(store))
        row = store.conn.execute(
            "SELECT built_at, dirty FROM graph_snapshot WHERE id = 'current'").fetchone()
        out = {
            **counts,
            "limit": MAX_EXPORTABLE_ENTITIES,
            "exportable": counts["entities"] <= MAX_EXPORTABLE_ENTITIES,
            "viz_assets_available": viz_asset_dir() is not None,
            # The export ships the snapshot as-is; say when that predates recent work
            # instead of letting the zip look current.
            "built_at": row["built_at"] if row else None,
            "stale": bool(row["dirty"]) if row else True,
        }
        if counts["pruned"]:
            # Say it plainly: this is not the whole noosphere.
            out["note"] = (
                f"this noosphere holds {counts['entities_total']} entities but a graph "
                f"snapshot renders the top {counts['entities']} by degree, so the export "
                f"is that slice — not the whole graph. A smaller, dedicated noosphere "
                f"exports as itself."
            )
        return out
    finally:
        store.close()


@router.get("/export/graph.json")
def export_graph_json(
    auth: AuthStore = Depends(get_auth_store),
    max_entities: int = Query(0, ge=0),
    per_repo_min: int = Query(8, ge=0),
    min_route_weight: int = Query(2, ge=1),
    detail_neighbours: int = Query(12, ge=0),
    force: bool = Query(False, description="bypass the size guard and export anyway"),
):
    """The export payload on its own — for someone assembling their own page."""
    return _payload(auth, max_entities, per_repo_min, min_route_weight,
                    detail_neighbours, force)


@router.get("/export/html")
def export_html(
    auth: AuthStore = Depends(get_auth_store),
    max_entities: int = Query(0, ge=0),
    per_repo_min: int = Query(8, ge=0),
    min_route_weight: int = Query(2, ge=1),
    detail_neighbours: int = Query(12, ge=0),
    force: bool = Query(False, description="bypass the size guard and export anyway"),
):
    """A zip that renders this noosphere's galaxy offline."""
    viz = viz_asset_dir()
    if viz is None:
        raise HTTPException(
            status_code=501,
            detail=("viz assets not found: expected orchestrator/viz/ (image) or "
                    "frontend/public/viz/ (checkout)"),
        )
    payload = _payload(auth, max_entities, per_repo_min, min_route_weight,
                       detail_neighbours, force)
    try:
        index = patch_viz_index((viz / "index.html").read_text())
    except RuntimeError as e:
        # A patch anchor moved: the viz changed shape. Fail loudly rather than
        # shipping a page that silently never loads its data.
        raise HTTPException(status_code=500, detail=str(e)) from e

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", index)
        z.writestr("graph.json", json.dumps(payload, separators=(",", ":")))
        for sub in ("core", "renderers"):
            d = viz / sub
            if not d.is_dir():
                continue
            # rglob, not glob("*.js"): the tree is flat today, but a nested module or a
            # non-.js asset added to the viz would be silently dropped and the export
            # would be a blank page with a 404 in the console.
            for f in sorted(x for x in d.rglob("*") if x.is_file()):
                z.writestr(f"{sub}/{f.relative_to(d).as_posix()}", f.read_bytes())
        z.writestr("README.txt",
                   "Orrery galaxy export\n\n"
                   "Open index.html from a web server (ES modules need http://, not file://),\n"
                   "or embed it: <iframe src=\"path/to/index.html\">\n\n"
                   "Everything is self-contained; it makes no requests back to Orrery.\n"
                   "Galaxy level only — drill-in and search need the running app.\n")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="orrery-galaxy-export.zip"'},
    )
