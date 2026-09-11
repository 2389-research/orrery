# ABOUTME: The graph payload — one typed node/edge vocabulary in layered sections.

"""The graph payload (contract v5).

v4 (`cosmic_data_v4`) is a galaxy *view-model*: node types are top-level keys
(`entities` / `repos` / `videos`), each with its own positions map and its own edge
collection, and presentation is mixed in with data. Adding a node type therefore
means adding keys, which is why the repo layer had to reach into the read path in
several places.

v5 separates that into layers:

    meta        counts, pruning policy, live activity, edge availability
    taxonomy    the domain tree (paths, counts, spec versions)
    nodes[]     typed render-set nodes — `type` is a VALUE, so a new node type is data
    node_index  a full record for every entity, not just the rendered top-N
    edges[]     one typed collection (`cooccurrence` / `uses` / `contains` / ...)
    layout      positions, palette, declared hierarchies — OPTIONAL

This replaced `cosmic_data_v4` one field-group at a time (the ablation phases in
docs/superpowers/specs/2026-08-05-graph-contract-design.md). Throughout the migration
an adapter projected v5 back onto v4 and an independent legacy builder served as the
oracle, so every phase could be proven equivalent before the next began; both were
deleted once the last consumer had moved. Regressions are now caught by the golden in
tests/test_graph_v5_contract.py.

Ordering is part of the contract, not an accident — found the hard way by the
round-trip probe (experiments/2026-08-06-graph-contract-roundtrip):

- `taxonomy` is in the store's domain-list order; `layout.positions` is in
  `ensure_layout` order. They are NOT the same order, and the client relies on both:
  it enumerates domains from positions (only those can be drawn) and looks up facts
  by path in the taxonomy.
- `node_index` is in full degree-ranked order, and `nodes` is its PREFIX — the
  renderer treats the render set as "the strongest N", so that must hold.
- `edges` groups domain co-occurrence, then collection co-occurrence, then `uses`.
  The renderer draws the strongest trade routes as the domain backbone, so their
  relative order carries meaning.
"""

from __future__ import annotations

from collections import defaultdict

from .graph_snapshot import (
    DEFAULT_MAX_RENDER_NODES,
    assign_domain_colors,
    _collection_positions,
)
from ..repositories.graph_reads import entity_silos, collection_silos, silo_kinds_of

# Bump on ANY change a cached payload could not survive — new/renamed layers, and
# renamed VALUES in the vocabulary (node type, edge scope, container_type). 5.1.0
# renamed the `repo` vocabulary to `collection`; a 5.0.0 snapshot still says "repo",
# and readers that filter on scope would silently find nothing. load_snapshot
# discards anything that is not this exact version.
SCHEMA_VERSION = "5.1.0"

# Storage vocabulary for `collection_edges.type` → the contract's edge-type vocabulary.
# Unmapped values pass THROUGH rather than being coerced: a new asserted edge kind
# should show up in the payload as itself, so the client ignores what it doesn't know
# (`state.js` filters by type, so an unrecognized type is simply not drawn) instead of
# being silently rendered as the wrong relationship. NULL is a legacy row → `uses`.
# `repo_uses` is the repo-era spelling; the migration rewrites it to `uses` and
# writers emit `uses` directly, so this entry is a belt-and-braces fallback for a
# database opened by an older process between deploy steps.
_EDGE_TYPE = {"repo_uses": "uses"}

def build_graph_v5(store, *, max_render_nodes: int = DEFAULT_MAX_RENDER_NODES) -> dict:
    """Build the v5 payload from the store."""
    conn = store.conn

    # ── taxonomy (domain-list order) ────────────────────────────────────
    domain_objs = store.domains.list(min_doc_count=1)
    domains = [{"id": d.id, "path": d.path, "parent_path": d.parent_path,
                "doc_count": d.document_count, "spec_version": d.spec_version}
               for d in domain_objs]

    taxonomy = [{"id": d["id"], "path": d["path"], "parent_path": d["parent_path"],
                 "document_count": d["doc_count"], "spec_version": d["spec_version"],
                 "is_subdomain": d["path"].count("/") >= 2}
                for d in domains]

    # ── layout: domain positions (ensure_layout order), then collections ──
    from .domain_layout import ensure_layout
    domain_positions = ensure_layout(store)

    # ensure_layout normalizes within the 100-anchor UMAP space, so a cluster of
    # semantically-similar domains can land in a tiny corner and everything renders
    # bunched. Rescale to fill [0.05, 0.95]; collections and entities derive from these,
    # so they follow automatically.
    if len(domain_positions) > 2:
        xs = [p["x"] for p in domain_positions.values()]
        ys = [p["y"] for p in domain_positions.values()]
        rx, ry = max(xs) - min(xs), max(ys) - min(ys)
        if rx > 1e-6 and ry > 1e-6:
            mnx, mny = min(xs), min(ys)
            domain_positions = {
                k: {"x": 0.05 + 0.90 * (p["x"] - mnx) / rx,
                    "y": 0.05 + 0.90 * (p["y"] - mny) / ry}
                for k, p in domain_positions.items()
            }

    from .graph_snapshot import domain_palette
    palette = domain_palette(conn)

    # ── entity nodes: per-entity domain + collection membership ─────────
    weight_rows = conn.execute("""
        SELECT es.entity_id, dd.domain_path, COUNT(*) as weight
        FROM entity_sources es
        JOIN document_domains dd ON es.document_id = dd.document_id
        JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL
        JOIN documents d ON d.id = es.document_id AND d.invalid_at IS NULL
        GROUP BY es.entity_id, dd.domain_path
    """).fetchall()
    raw: dict[str, dict[str, int]] = defaultdict(dict)
    for r in weight_rows:
        raw[r[0]][r[1]] = r[2]
    domain_weights: dict[str, dict[str, float]] = {}
    for eid, dw in raw.items():
        total = sum(dw.values())
        if total > 0:
            domain_weights[eid] = {dp: round(w / total, 3) for dp, w in dw.items()}

    collection_weights = store.collections.get_collection_weights()

    # One LEFT JOIN + GROUP BY, NOT a correlated per-row COUNT — the latter was
    # ~30s on a 16.7k-node graph. Relies on idx_entity_sources_entity.
    entity_rows = conn.execute("""
        SELECT e.id, e.canonical_name, e.type, COUNT(es.entity_id) AS source_count
        FROM entities e
        LEFT JOIN entity_sources es ON es.entity_id = e.id
        WHERE e.invalid_at IS NULL
        GROUP BY e.id
        ORDER BY source_count DESC, e.id ASC
    """).fetchall()

    # entity_id -> dominant {silo_id, kind}, live-resolved via the silo_kind view
    # (task 11a). One whole-graph pass here, not per-entity — the same shape as the
    # domain/collection weight queries above.
    entity_silo_info = entity_silos(conn)

    node_index: dict[str, dict] = {}
    render_entities: list[dict] = []
    unplaceable = 0
    for e in entity_rows:
        dw = domain_weights.get(e["id"], {})
        if not dw:
            # No domain weight → nothing to place it against. Counted, not hidden.
            unplaceable += 1
            continue
        memberships = [{"container_type": "domain", "id": k, "weight": w}
                       for k, w in dw.items()]
        memberships += [{"container_type": "collection", "id": k, "weight": w}
                        for k, w in collection_weights.get(e["id"], {}).items()]
        sinfo = entity_silo_info.get(e["id"], {})
        node = {"id": e["id"], "type": "entity", "subtype": e["type"],
                "label": e["canonical_name"], "degree": e["source_count"],
                "silo_id": sinfo.get("silo_id"), "kind": sinfo.get("kind"),
                "memberships": memberships}
        node_index[e["id"]] = node
        if len(render_entities) < max_render_nodes:
            render_entities.append(node)

    # ── collections + documents ─────────────────────────────────────────
    collection_rows = conn.execute(
        "SELECT id, name, path, document_count FROM collections"
    ).fetchall()
    # Dominant silo + live-resolved kind (task 11a), one batch pass rather than a
    # query per collection — same shape as entity_silo_info above, and the same
    # dominant-pick (`_pick_dominant`) tie-break everywhere: favor a NAMED silo over
    # the null pool, then break lexicographically. In practice uniform: every
    # document in a collection resolves to the SAME silo_id via `resolve_silo_id`'s
    # precedence, so this only matters for a collection predating that precedence.
    collection_silo_info = collection_silos(conn)
    collection_nodes = []
    for r in collection_rows:
        dom_row = conn.execute(
            """SELECT dd.domain_path, COUNT(*) c
               FROM document_collections dc
               JOIN document_domains dd ON dc.document_id = dd.document_id
               WHERE dc.collection_id = ?
               GROUP BY dd.domain_path ORDER BY c DESC LIMIT 1""",
            (r["id"],),
        ).fetchone()
        dom = dom_row["domain_path"] if dom_row else None
        csinfo = collection_silo_info.get(r["id"], {})
        collection_nodes.append({
            "id": r["id"], "type": "collection", "label": r["name"], "path": r["path"],
            "degree": r["document_count"],
            "silo_id": csinfo.get("silo_id"),
            "kind": csinfo.get("kind"),
            "memberships": ([{"container_type": "domain", "id": dom, "weight": 1.0}]
                            if dom else []),
        })

    collection_boxes = [{"id": n["id"], "name": n["label"], "path": n["path"],
                 "document_count": n["degree"],
                 "domain": next((m["id"] for m in n["memberships"]), None)}
                for n in collection_nodes]
    collection_positions = _collection_positions(store, collection_boxes, domain_positions)

    total_documents = conn.execute("SELECT COUNT(*) FROM documents WHERE invalid_at IS NULL").fetchone()[0]
    recent_docs = store.documents.get_recent(limit=50)
    # kind is resolved live via the silo_kind view — `silo_id` itself is already a
    # documents column, so this is a plain batch lookup, not a per-doc aggregate.
    doc_kinds = silo_kinds_of(conn, [d.silo_id for d in recent_docs])
    document_nodes = [
        {"id": d.id, "type": "document", "subtype": getattr(d, "content_type", "text"),
         "label": d.title,
         "silo_id": d.silo_id, "kind": doc_kinds.get(d.silo_id),
         "memberships": [{"container_type": "domain", "id": p, "weight": 1.0}
                         for p in d.domains],
         "primary_domain": d.domains[0] if d.domains else None}
        for d in recent_docs
    ]

    # ── edges: derived co-occurrence, then the ASSERTED collection edges ──────
    #
    # Two kinds, and the distinction is a property of the edge TYPE, not of the
    # collection kind: co-occurrence is DERIVED (symmetric, weighted, computed from
    # shared entities, and therefore free to every collection kind), while `uses` and
    # `chain_next` are ASSERTED by whichever ingest path knows about them.
    edges = []
    for r in store.relationships.get_trade_routes():
        edges.append({"source": r["source"], "target": r["target"],
                      "type": "cooccurrence", "scope": "domain", "weight": r["weight"]})
    for r in store.collections.get_collection_routes():
        edges.append({"source": r["source"], "target": r["target"],
                      "type": "cooccurrence", "scope": "collection", "weight": r["weight"]})
    # `type` was previously not selected at all and every row was emitted as "uses", so
    # a tracker run's `chain_next` trajectory shipped to the client labelled as a
    # manifest import dependency — the one thing that distinguishes a run from a repo,
    # erased at the payload boundary.
    for r in conn.execute("SELECT source, target, type, weight FROM collection_edges"):
        edges.append({"source": r["source"], "target": r["target"],
                      "type": _EDGE_TYPE.get(r["type"], r["type"] or "uses"),
                      "scope": "collection", "weight": r["weight"]})

    running = store.jobs.list(status_filter="running")
    simmering = [j.target for j in running if j.type.startswith("simmer_")]

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            # Counts describe the nodes actually emitted, not just the entities:
            # `nodes` also carries every collection and recent document, so a consumer
            # comparing meta.counts against len(nodes) would otherwise disagree.
            "counts": {
                "nodes_included": len(render_entities) + len(collection_nodes) + len(document_nodes),
                "nodes_total": len(node_index) + len(collection_nodes) + len(document_nodes),
            },
            "counts_by_type": {
                "entity": {"total": len(node_index), "included": len(render_entities)},
                "domain": {"total": len(taxonomy), "included": len(taxonomy)},
                "collection": {"total": len(collection_nodes), "included": len(collection_nodes)},
                "document": {"total": total_documents, "included": len(document_nodes)},
            },
            "pruning": {
                "policy": "top_n_by_degree",
                "max_render_nodes": max_render_nodes,
                # v4 silently dropped these AND left them out of its total, so a
                # consumer could not tell. Declared here instead.
                "excluded": [{"reason": "no_domain_membership", "count": unplaceable}],
            },
            "activity": {"simmering_domains": simmering},
            "edge_availability": {
                "inline": ["cooccurrence@domain", "cooccurrence@collection", "uses",
                           "chain_next"],
                "on_demand": [{"type": "cooccurrence@entity",
                               "endpoint": "/entities/{id}/cooccurrences"}],
            },
        },
        "taxonomy": taxonomy,
        "nodes": collection_nodes + document_nodes + render_entities,
        "node_index": node_index,
        "edges": edges,
        "layout": {
            "frame": {"space": "unit_square", "bounds": [0, 0, 1, 1]},
            "positions": {**domain_positions, **collection_positions},
            "palette": palette,
            # "collection", not "repo" — the nodes this payload emits carry
            # `"type": "collection"`, so declaring the level as "repo" left two
            # vocabularies inside a single 5.1.0 payload.
            "hierarchies": [{"name": "cosmic",
                             "levels": ["domain", "collection", "entity"], "default": True}],
            "generator": {"name": "umap-domain-anchors", "entity_positions": "client_derived"},
        },
    }
