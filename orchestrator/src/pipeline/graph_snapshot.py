# ABOUTME: Materialized read-model of the /graph payload — build it once, serve
# ABOUTME: it cached. Positions for ALL nodes, edges/render only for the top-N.

"""Graph snapshot: precompute + level-of-detail rendering.

`GET /graph` used to recompute the whole payload on every request
(UMAP transforms per collection, big aggregations) and cache nothing, so it timed out
on a real graph. The graph only changes when a job writes to it, so we compute
the payload once (`build_graph_payload`) and serve the cached blob; writers flip
`graph_snapshot.dirty` and a background task rebuilds.

Two orthogonal ideas (see the design doc):
  - Latency: materialize the payload, serve cached.
  - Legibility: a node's *identity* is cheap, its *edges* are expensive. So ship a
    record for EVERY node (`node_index`, which makes a search hit on the un-rendered
    tail resolvable) but only render the top-N by degree (`nodes`).

Pruning is a viz concern, not data loss: the full graph stays queryable via
MCP / /search / neighborhood.
"""

from __future__ import annotations

import json
import math

GOLDEN_RATIO = 0.618033988749895
BASE_SATURATION = 65
BASE_LIGHTNESS = 62
DESAT_PER_LEVEL = 8

# Default size of the rendered node set. Positions are stored for every node;
# only the top-N by source_count get drawn as full render nodes.
DEFAULT_MAX_RENDER_NODES = 3000


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    """Convert HSL (h: 0-360, s: 0-100, l: 0-100) to hex color."""
    s /= 100
    l /= 100
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2

    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    r, g, b = int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def assign_domain_colors(domains: list[dict]) -> dict[str, str]:
    """Hierarchy-aware golden ratio color distribution.

    Top-level domains each own an equal slice of the hue wheel. Within each
    slice, subdomains are spaced using the golden ratio for maximum perceptual
    distance. Deeper domains desaturate slightly.
    """
    paths = [d["path"] for d in domains]
    if not paths:
        return {}

    parts_list = [p.split("/") for p in paths]
    branch_level = 0
    for level in range(min(len(p) for p in parts_list)):
        values = set(p[level] for p in parts_list)
        if len(values) > 1:
            break
        branch_level = level + 1

    top_level_names = sorted(set(
        "/".join(p.split("/")[:branch_level + 1]) for p in paths
    ))

    top_level_hues = {}
    for i, name in enumerate(top_level_names):
        top_level_hues[name] = (i / max(len(top_level_names), 1)) * 360

    slice_size = 360 / max(len(top_level_names), 1)

    color_map = {}
    for top_name in top_level_names:
        range_start = top_level_hues[top_name]
        # Match on a path SEGMENT, not a character prefix: `startswith("ai")` also
        # matches "aider/tools", so that path joined both families and the later loop
        # overwrote its hue with one from a range it does not belong to.
        family = sorted([p for p in paths
                         if p == top_name or p.startswith(top_name + "/")])
        for i, path in enumerate(family):
            offset = ((i * GOLDEN_RATIO) % 1) * slice_size
            hue = (range_start + offset) % 360
            depth = path.count("/") - branch_level
            saturation = max(30, BASE_SATURATION - depth * DESAT_PER_LEVEL)
            color_map[path] = _hsl_to_hex(hue, saturation, BASE_LIGHTNESS)

    return color_map


def domain_palette(conn) -> dict[str, str]:
    """The domain→hex map, computed over the SAME domain set the galaxy uses.

    `assign_domain_colors` is set-dependent (branch_level, slice_size and each
    family's golden-ratio index all derive from the input list), so any consumer
    that wants to agree with the galaxy MUST feed it the identical set: domains with
    document_count >= 1, ordered by path. This is that single source — used by the
    galaxy payload (graph_v5) and the star page (get_star_graph) so the two cannot
    drift.
    """
    rows = conn.execute(
        "SELECT path FROM domains WHERE document_count >= 1 ORDER BY path"
    ).fetchall()
    domains = [{"path": r["path"]} for r in rows]
    palette = assign_domain_colors(domains)
    # Region fallback, identical to the galaxy's (graph_v5): a top-level region key so
    # a node with no exact-path colour still gets its family hue.
    for d in domains:
        region = d["path"].split("/")[0]
        if region not in palette:
            palette[region] = palette.get(d["path"], "#81d4fa")
    return palette


def _circular_collection_positions(collections: list[dict], missing: list[dict]) -> dict:
    positions = {}
    for i, coll in enumerate(missing):
        angle = (2 * math.pi * i) / max(len(missing), 1) - math.pi / 2
        positions[coll["id"]] = {"x": 0.5 + 0.4 * math.cos(angle),
                                 "y": 0.5 + 0.4 * math.sin(angle)}
    return positions


def _collection_positions(store, collections: list[dict], domain_positions: dict) -> dict:
    """Place each collection at the weighted centroid of its own documents' domain
    positions — so a collection sits *inside* its content, in the SAME frame as the
    domains. (Projecting the collection's long summary through the domain-label UMAP
    via transform_text extrapolates it to a corner, far from the domains — the
    "repos placed very differently" artifact.) Circular fallback if a collection has
    no positioned domains yet.
    """
    conn = store.conn
    positions: dict = {}
    for coll in collections:
        rows = conn.execute(
            "SELECT dd.domain_path, COUNT(*) c FROM document_collections dc "
            "JOIN document_domains dd ON dd.document_id = dc.document_id "
            "WHERE dc.collection_id = ? GROUP BY dd.domain_path",
            (coll["id"],),
        ).fetchall()
        sx = sy = den = 0.0
        for r in rows:
            p = domain_positions.get(r["domain_path"])
            if p:
                w = r["c"]
                sx += p["x"] * w
                sy += p["y"] * w
                den += w
        if den > 0:
            positions[coll["id"]] = {"x": sx / den, "y": sy / den}

    missing = [c for c in collections if c["id"] not in positions]
    positions.update(_circular_collection_positions(collections, missing))
    return positions


def build_graph_payload(store, *, max_render_nodes: int = DEFAULT_MAX_RENDER_NODES) -> dict:
    """Build the graph payload (contract v5) from the store.

    The v4 adapter and its legacy oracle were removed once every consumer had
    migrated — see docs/superpowers/specs/2026-08-05-graph-contract-design.md and the
    ablation-phase commits. Regressions are now caught by the v5 golden in
    tests/test_graph_v5_contract.py, not by comparison against v4.
    """
    from .graph_v5 import build_graph_v5
    return build_graph_v5(store, max_render_nodes=max_render_nodes)


# ── Snapshot persistence ────────────────────────────────────────────────

def is_dirty(store) -> bool:
    row = store.conn.execute(
        "SELECT dirty FROM graph_snapshot WHERE id = 'current'"
    ).fetchone()
    # No row yet → treat as dirty (needs a first build).
    return row is None or bool(row["dirty"])


def load_snapshot(store) -> dict | None:
    """Return the cached payload dict, or None if no usable snapshot has been built.

    A payload from before the v5 contract is treated as absent rather than served.
    The snapshot is materialized and long-lived — the office display's was 13 days old
    — so after a deploy the cache can still hold the legacy shape, and serving it to a
    v5-only client would break the galaxy until something happened to dirty the graph.
    Discarding it forces one inline rebuild instead. `meta` is the marker: it exists in
    every v5 payload and in no v4 one.
    """
    row = store.conn.execute(
        "SELECT payload FROM graph_snapshot WHERE id = 'current'"
    ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or "meta" not in payload:
        return None
    # A payload from a DIFFERENT contract version is discarded, not served. The
    # snapshot is materialized and long-lived — the office display's was 13 days old —
    # so after a deploy the cache still holds the previous shape. Serving it is worse
    # than rebuilding: the 5.0.0 -> 5.1.0 rename kept every layer and key in place and
    # changed only VALUES (`scope: "repo"` -> `"collection"`), so a stale payload
    # parses fine and simply goes quiet — the structure endpoint filtered on the new
    # scope and lost every shared-entity link with no error anywhere.
    from .graph_v5 import SCHEMA_VERSION
    if payload.get("meta", {}).get("schema_version") != SCHEMA_VERSION:
        return None
    return payload


def _set_dirty(store, value: int) -> None:
    store.conn.execute(
        "INSERT INTO graph_snapshot (id, dirty) VALUES ('current', ?) "
        "ON CONFLICT(id) DO UPDATE SET dirty = excluded.dirty",
        (value,),
    )
    store.conn.commit()


def save_snapshot(store, payload: dict, *, commit: bool = True) -> None:
    """Persist the payload (one logical row). Does NOT touch the dirty bit —
    the rebuild owns dirty (it clears it BEFORE building; see rebuild_snapshot)."""
    blob = json.dumps(payload)
    # Informational metadata, read from the v5 layers. These used to sum the v4 route
    # keys, which the payload no longer has — so every rebuild persisted zeros.
    edge_count = len(payload.get("edges", ()))
    entity_count = payload.get("meta", {}).get("counts", {}).get("nodes_total", 0)
    store.conn.execute(
        "INSERT INTO graph_snapshot (id, payload, built_at, entity_count, edge_count) "
        "VALUES ('current', ?, CURRENT_TIMESTAMP, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
        "built_at = excluded.built_at, entity_count = excluded.entity_count, "
        "edge_count = excluded.edge_count",
        (blob, entity_count, edge_count),
    )
    if commit:
        store.conn.commit()


def rebuild_snapshot(store, *, max_render_nodes: int = DEFAULT_MAX_RENDER_NODES) -> dict:
    """Build the payload from the store and persist it.

    Clears `dirty` BEFORE building, not after. The build takes seconds; a writer
    that commits + flips dirty during that window must NOT have its flag cleared
    by our post-build write (or its change would stay invisible until the next
    unrelated write). Clearing up-front means such a write leaves dirty=1, so the
    next sweep rebuilds. On build failure we re-flag so the sweep retries.
    """
    _set_dirty(store, 0)
    try:
        payload = build_graph_payload(store, max_render_nodes=max_render_nodes)
        # One transaction: a reader must never see the new payload alongside the
        # PREVIOUS materialized edges, and a crash between the two must not leave that
        # mismatch until some later rebuild happens to succeed.
        save_snapshot(store, payload, commit=False)
        _save_domain_edges(store, payload, commit=False)
        store.conn.commit()
        return payload
    except Exception:
        _set_dirty(store, 1)
        raise


def _save_domain_edges(store, payload: dict, *, commit: bool = True) -> None:
    """Persist the domain co-occurrence edges the payload already contains.

    Free: build_graph_v5 computed them for the galaxy, so this only writes them where a
    scoped read can seek. Replaces the whole set rather than merging — the payload is
    the truth, and a stale row would outlive a deleted domain.
    """
    rows = [(e["source"], e["target"], e["weight"]) for e in payload.get("edges", ())
            if e.get("type") == "cooccurrence" and e.get("scope") == "domain"]
    conn = store.conn
    conn.execute("DELETE FROM domain_edges")
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_edges (source, target, weight) VALUES (?, ?, ?)", rows)
    if commit:
        conn.commit()


def get_or_build(store, *, max_render_nodes: int = DEFAULT_MAX_RENDER_NODES) -> dict:
    """Serve the cached snapshot; build inline on the first-ever request.

    Does NOT rebuild on a stale (dirty) snapshot — the background task owns
    that, and serving seconds-stale data is fine for a knowledge map. Only a
    completely missing snapshot triggers an inline build.
    """
    cached = load_snapshot(store)
    if cached is not None:
        return cached
    return rebuild_snapshot(store, max_render_nodes=max_render_nodes)
