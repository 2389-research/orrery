# ABOUTME: Graph exploration endpoints for agent-driven knowledge graph traversal.
# ABOUTME: Neighborhood, shared context, path finding, subgraph, and domain overview.
# ABOUTME: All endpoints broadcast entity names to the viz via WebSocket.

from collections import defaultdict, deque
from fastapi import APIRouter, HTTPException, Depends, Query
from ..dependencies import get_auth_store, AuthStore, query_id
from ..broadcast import broadcast_search
from ..pipeline.slice import ensure_slice_temp_table, ensure_slice_doc_temp_table
from ..pipeline.slice_edges import in_slice_adjacency
from ..repositories.interfaces import CoEntity
from .entities import _active_slice, _scope
from ..repositories.graph_reads import (_chunks, domain_neighbours, entities_in_domain,
                                         entity_by_name, entity_silos)

router = APIRouter(prefix="/graph")


def _one_of(preferred, fallback):
    """The query-parameter spelling wins; the path form is the compatibility path."""
    value = preferred if preferred is not None else fallback
    if value is None:
        raise HTTPException(status_code=422, detail="an entity id or name is required")
    return value


def _resolve_arg(store, preferred, fallback):
    """Resolve an entity from either spelling, releasing the store if it raises.

    `get_auth_store()` hands back a real SQLiteDataStore with no dependency teardown, so
    every early exit has to close it or the connection leaks. Both steps here raise on
    ordinary input — `_one_of` a 422 when neither spelling is supplied, `_resolve_entity`
    a 404 for an unknown name — and both happen BEFORE the handler's own close.

    A try/finally around the whole handler would be the blunter fix, but these handlers
    close deliberately EARLY, before `await broadcast_search(...)`, so as not to hold a
    connection across an await. This keeps that and adds only the missing error path.
    """
    try:
        return _resolve_entity(store, _one_of(preferred, fallback))
    except Exception:
        store.close()
        raise


def _resolve_entity(store, name_or_id: str):
    """Look up an entity by ID or case-insensitive name.

    The name fallback resolves in SQL. It previously fetched `list(limit=500)` and
    scanned client-side, so an entity past position 500 was reported "not found" — and
    which 500 you got depended on the default ordering, making it look intermittent
    rather than broken. Every traversal endpoint enters through here.
    """
    entity = store.entities.get(name_or_id)
    if entity:
        return entity
    match = entity_by_name(store.entities._conn, name_or_id)
    if not match:
        raise HTTPException(status_code=404, detail=f"Entity not found: {name_or_id}")
    return store.entities.get(match["id"])


def _get_adjacency(conn, entity_ids: list[str]) -> dict[str, list[dict]]:
    """Get co-occurrence neighbors for a set of entity IDs. Returns {entity_id: [{id, name, type, weight}]}."""
    if not entity_ids:
        return {}
    # Chunked: this builds one IN clause per id, twice, so an unbounded list blows past
    # SQLite's bound-parameter limit. Reachable from a wide subgraph request, where it
    # would raise rather than degrade.
    rows = []
    for chunk in _chunks(entity_ids, 400):   # 400 ids -> 800 bound params
        ph = ",".join("?" * len(chunk))
        # SUM + GROUP BY the entity pair: a pair can span several rows (per-chunk
        # rows from the upload path, plus the aggregated code-intent row), and each
        # is one contribution to the SAME edge. Reading them as separate edges
        # returned the neighbour repeatedly with a fraction of its true weight.
        rows += conn.execute(f"""
            SELECT r.from_entity, r.to_entity, e1.canonical_name as from_name, e1.type as from_type,
                   e2.canonical_name as to_name, e2.type as to_type, SUM(r.weight) as weight
            FROM relationships r
            JOIN entities e1 ON r.from_entity = e1.id
            JOIN entities e2 ON r.to_entity = e2.id
            WHERE r.type = 'co_occurs' AND (r.from_entity IN ({ph}) OR r.to_entity IN ({ph}))
              AND r.invalid_at IS NULL AND e1.invalid_at IS NULL AND e2.invalid_at IS NULL
            GROUP BY r.from_entity, r.to_entity
        """, list(chunk) + list(chunk)).fetchall()

    wanted = set(entity_ids)
    adj: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["from_entity"] in wanted:
            adj[r["from_entity"]].append({
                "id": r["to_entity"], "name": r["to_name"],
                "type": r["to_type"], "weight": r["weight"],
            })
        if r["to_entity"] in wanted:
            adj[r["to_entity"]].append({
                "id": r["from_entity"], "name": r["from_name"],
                "type": r["from_type"], "weight": r["weight"],
            })
    return adj


@router.get("/neighborhood", name="neighborhood_by_query")
@router.get("/neighborhood/{entity_id_or_name}")
async def get_neighborhood(
    entity_id_or_name: str | None = None,
    name: str | None = Query(None, description="Entity id or name; prefer this over the path form."),
    depth: int = Query(1, ge=1, le=3),
    max_nodes: int = Query(30, ge=1, le=100),
    auth: AuthStore = Depends(get_auth_store),
    qid: str = Depends(query_id),
):
    """Multi-hop neighborhood around an entity. Returns nodes and edges within `depth` hops.

    Two spellings, and `?name=` is the correct one. A path segment cannot carry an
    entity name faithfully: 2.3% of names in a real graph contain `/`, and the server
    percent-DECODES the path before routing, so `%2F` becomes a separator again and the
    lookup 404s for an entity that exists. Dot segments are worse than useless — a name
    of `..` normalizes client-side into a request for a DIFFERENT endpoint.

    The path form stays for existing callers and ids, which have neither problem.
    """
    store = auth.store
    conn = store._conn if hasattr(store, '_conn') else store.entities._conn
    entity = _resolve_arg(store, name, entity_id_or_name)
    seed_id = entity.id

    # BFS expansion
    visited: dict[str, dict] = {}  # id -> {name, type, depth, source_count}
    edges: list[dict] = []
    edge_set: set[tuple] = set()
    frontier = [seed_id]

    # Add seed
    visited[seed_id] = {
        "id": seed_id, "name": entity.canonical_name,
        "type": entity.type, "depth": 0, "source_count": entity.source_count,
    }

    for current_depth in range(1, depth + 1):
        if len(visited) >= max_nodes or not frontier:
            break
        adj = _get_adjacency(conn, frontier)
        next_frontier = []
        for src_id in frontier:
            for neighbor in sorted(adj.get(src_id, []), key=lambda x: -x["weight"]):
                nid = neighbor["id"]
                # Add edge
                edge_key = tuple(sorted([src_id, nid]))
                if edge_key not in edge_set:
                    edge_set.add(edge_key)
                    edges.append({
                        "source": src_id, "target": nid,
                        "weight": neighbor["weight"],
                    })
                # Add node if new
                if nid not in visited and len(visited) < max_nodes:
                    visited[nid] = {
                        "id": nid, "name": neighbor["name"],
                        "type": neighbor["type"], "depth": current_depth,
                        "source_count": 0,
                    }
                    next_frontier.append(nid)
        frontier = next_frontier

    # Backfill source counts for discovered nodes
    if visited:
        ph = ",".join("?" * len(visited))
        counts = conn.execute(f"""
            SELECT entity_id, COUNT(DISTINCT document_id) as cnt
            FROM entity_sources WHERE entity_id IN ({ph}) GROUP BY entity_id
        """, list(visited.keys())).fetchall()
        for r in counts:
            if r["entity_id"] in visited:
                visited[r["entity_id"]]["source_count"] = r["cnt"]

    # Silo + kind, resolved LIVE via the silo_kind view join (task 11a) — every node
    # here is an entity, so this is the same dominant-silo pick the snapshot builder
    # uses, just scoped to this neighborhood instead of the whole graph.
    silo_info = entity_silos(conn, list(visited.keys()))
    for nid, node in visited.items():
        sinfo = silo_info.get(nid, {})
        node["silo_id"] = sinfo.get("silo_id")
        node["kind"] = sinfo.get("kind")

    store.close()

    seed_sinfo = silo_info.get(seed_id, {})
    result = {
        "query_id": qid,
        "seed": {"id": seed_id, "name": entity.canonical_name, "type": entity.type,
                 "silo_id": seed_sinfo.get("silo_id"), "kind": seed_sinfo.get("kind")},
        "depth": depth,
        "nodes": list(visited.values()),
        "edges": edges,
        "node_count": len(visited),
        "edge_count": len(edges),
    }

    # Broadcast to viz — light up the neighborhood
    entity_names = [n["name"] for n in result["nodes"]]
    await broadcast_search(f"neighborhood:{entity.canonical_name}", entity_names)

    return result


@router.get("/shared-context", name="shared_context_by_query")
@router.get("/shared-context/{entity_a}/{entity_b}")
async def get_shared_context(
    entity_a: str | None = None,
    entity_b: str | None = None,
    a: str | None = Query(None, description="First entity id or name; prefer this."),
    b: str | None = Query(None, description="Second entity id or name; prefer this."),
    auth: AuthStore = Depends(get_auth_store),
    qid: str = Depends(query_id),
    since: str | None = None, until: str | None = None, as_of: str | None = None,
    window: str | None = None, domain: str | None = None, collection: str | None = None,
):
    """Find shared context between two entities: shared documents, shared neighbors, shared domains."""
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    sliced = slc is not None
    store = auth.store
    conn = store._conn if hasattr(store, '_conn') else store.entities._conn
    ea = _resolve_arg(store, a, entity_a)
    eb = _resolve_arg(store, b, entity_b)
    if sliced:
        ensure_slice_temp_table(store.conn, slc)
        ensure_slice_doc_temp_table(store.conn, slc)
    # Sliced: `_slice_docs` is the sole document-validity filter (no stacked invalid_at).
    doc_sql = ("SELECT DISTINCT d.id, d.title FROM entity_sources es JOIN documents d "
               "ON es.document_id = d.id "
               + ("JOIN _slice_docs sd ON sd.doc_id = d.id WHERE es.entity_id = ?" if sliced
                  else "WHERE es.entity_id = ? AND d.invalid_at IS NULL"))

    # Shared documents
    docs_a = set()
    docs_b = set()
    doc_titles = {}
    rows_a = conn.execute(doc_sql, (ea.id,)).fetchall()
    for r in rows_a:
        docs_a.add(r["id"])
        doc_titles[r["id"]] = r["title"]
    rows_b = conn.execute(doc_sql, (eb.id,)).fetchall()
    for r in rows_b:
        docs_b.add(r["id"])
        doc_titles[r["id"]] = r["title"]

    shared_doc_ids = docs_a & docs_b
    shared_documents = [{"id": did, "title": doc_titles[did]} for did in shared_doc_ids]

    # Shared neighbors (entities that co-occur with BOTH)
    if sliced:
        # Intersection of the two in-slice neighbour sets, from live in-slice edges.
        adj = in_slice_adjacency(store.conn, [ea.id, eb.id])
        nb_a, nb_b = dict(adj.get(ea.id, [])), dict(adj.get(eb.id, []))
        ids = list(set(nb_a) | set(nb_b))
        meta = {}
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            ph = ",".join("?" * len(part))
            for r in conn.execute(f"SELECT id, canonical_name, type FROM entities "
                                  f"WHERE id IN ({ph}) AND invalid_at IS NULL", part):
                meta[r["id"]] = r

        def _mk(nbrs):
            return {nid: CoEntity(id=nid, canonical_name=meta[nid]["canonical_name"],
                                  type=meta[nid]["type"], weight=w)
                    for nid, w in nbrs.items() if nid in meta}
        neighbors_a, neighbors_b = _mk(nb_a), _mk(nb_b)
    else:
        neighbors_a = {n.id: n for n in store.relationships.get_cooccurrences(ea.id, limit=50)}
        neighbors_b = {n.id: n for n in store.relationships.get_cooccurrences(eb.id, limit=50)}
    shared_neighbor_ids = set(neighbors_a.keys()) & set(neighbors_b.keys())
    shared_neighbors = []
    for nid in shared_neighbor_ids:
        n = neighbors_a[nid]
        shared_neighbors.append({
            "id": n.id, "name": n.canonical_name, "type": n.type,
            "weight_to_a": n.weight, "weight_to_b": neighbors_b[nid].weight,
        })
    shared_neighbors.sort(key=lambda x: -(x["weight_to_a"] + x["weight_to_b"]))

    # Shared domains
    domains_a = set()
    for did in docs_a:
        for dd in store.domains.get_domains_for_document(did):
            domains_a.add(dd.domain_path)
    domains_b = set()
    for did in docs_b:
        for dd in store.domains.get_domains_for_document(did):
            domains_b.add(dd.domain_path)
    shared_domains = sorted(domains_a & domains_b)

    # Direct relationship weight
    direct_weight = 0
    if ea.id in neighbors_b:
        direct_weight = neighbors_b[ea.id].weight
    elif eb.id in neighbors_a:
        direct_weight = neighbors_a[eb.id].weight

    store.close()

    result = {
        "query_id": qid,
        "entity_a": {"id": ea.id, "name": ea.canonical_name, "type": ea.type},
        "entity_b": {"id": eb.id, "name": eb.canonical_name, "type": eb.type},
        "direct_weight": direct_weight,
        "shared_documents": shared_documents,
        "shared_neighbors": shared_neighbors[:15],
        "shared_domains": shared_domains,
        "summary": {
            "docs_in_common": len(shared_doc_ids),
            "neighbors_in_common": len(shared_neighbor_ids),
            "domains_in_common": len(shared_domains),
        },
    }
    if sliced:
        result["scope"] = _scope(slc)

    # Broadcast to viz — light up both entities + their shared neighbors
    entity_names = [ea.canonical_name, eb.canonical_name] + [n["name"] for n in shared_neighbors[:10]]
    await broadcast_search(f"shared:{ea.canonical_name}↔{eb.canonical_name}", entity_names)

    return result


@router.get("/paths", name="paths_by_query")
@router.get("/paths/{entity_a}/{entity_b}")
async def find_paths(
    entity_a: str | None = None,
    entity_b: str | None = None,
    a: str | None = Query(None, description="First entity id or name; prefer this."),
    b: str | None = Query(None, description="Second entity id or name; prefer this."),
    max_depth: int = Query(4, ge=1, le=6),
    auth: AuthStore = Depends(get_auth_store),
    qid: str = Depends(query_id),
):
    """Find shortest path(s) between two entities through co-occurrence edges."""
    store = auth.store
    conn = store._conn if hasattr(store, '_conn') else store.entities._conn
    ea = _resolve_arg(store, a, entity_a)
    eb = _resolve_arg(store, b, entity_b)

    if ea.id == eb.id:
        store.close()
        return {"paths": [], "message": "Same entity"}

    # BFS for shortest path(s)
    queue = deque([(ea.id, [ea.id])])
    visited = {ea.id}
    found_paths = []
    found_depth = None

    while queue:
        current, path = queue.popleft()
        if found_depth is not None and len(path) > found_depth:
            break
        if len(path) > max_depth + 1:
            break

        # GROUP BY the neighbour: a pair can span several rows, and without folding
        # them the same neighbour is visited repeatedly — redundant traversal and
        # duplicate paths for what is one edge.
        neighbors = conn.execute("""
            SELECT CASE WHEN r.from_entity = ? THEN r.to_entity ELSE r.from_entity END as neighbor_id,
                   SUM(r.weight) as weight
            FROM relationships r
            WHERE (r.from_entity = ? OR r.to_entity = ?) AND r.type = 'co_occurs'
              AND r.invalid_at IS NULL
            GROUP BY neighbor_id
        """, (current, current, current)).fetchall()

        for row in neighbors:
            nid = row["neighbor_id"]
            if nid == eb.id:
                full_path = path + [nid]
                found_paths.append(full_path)
                found_depth = len(full_path)
                continue
            if nid not in visited and len(path) < max_depth:
                visited.add(nid)
                queue.append((nid, path + [nid]))

    # Resolve entity names for paths
    all_ids = set()
    for p in found_paths:
        all_ids.update(p)
    entity_map = {}
    if all_ids:
        ph = ",".join("?" * len(all_ids))
        rows = conn.execute(f"SELECT id, canonical_name, type FROM entities WHERE id IN ({ph}) AND invalid_at IS NULL", list(all_ids)).fetchall()
        entity_map = {r["id"]: {"id": r["id"], "name": r["canonical_name"], "type": r["type"]} for r in rows}

    resolved_paths = []
    for p in found_paths[:5]:  # Return at most 5 paths
        resolved_paths.append({
            "length": len(p) - 1,
            "nodes": [entity_map.get(eid, {"id": eid, "name": "?", "type": "?"}) for eid in p],
        })

    store.close()

    result = {
        "query_id": qid,
        "entity_a": {"id": ea.id, "name": ea.canonical_name, "type": ea.type},
        "entity_b": {"id": eb.id, "name": eb.canonical_name, "type": eb.type},
        "paths": resolved_paths,
        "path_count": len(resolved_paths),
        "searched_depth": max_depth,
    }

    # Broadcast to viz — light up all entities along the paths
    path_entities = set()
    for p in resolved_paths:
        for node in p["nodes"]:
            path_entities.add(node["name"])
    if path_entities:
        await broadcast_search(f"path:{ea.canonical_name}→{eb.canonical_name}", list(path_entities))

    return result


@router.post("/subgraph")
async def get_subgraph(
    body: dict,
    auth: AuthStore = Depends(get_auth_store),
    qid: str = Depends(query_id),
):
    """Get the subgraph connecting a set of entities. Pass {"entity_names": [...], "max_hops": 1}."""
    store = auth.store
    conn = store._conn if hasattr(store, '_conn') else store.entities._conn
    entity_names = body.get("entity_names", [])
    max_hops = min(body.get("max_hops", 1), 2)

    if not entity_names:
        store.close()
        raise HTTPException(status_code=400, detail="entity_names required")

    # Resolve all seed entities
    seeds = {}
    for name in entity_names:
        try:
            e = _resolve_entity(store, name)
            seeds[e.id] = {"id": e.id, "name": e.canonical_name, "type": e.type, "is_seed": True}
        except HTTPException:
            continue

    if not seeds:
        store.close()
        raise HTTPException(status_code=404, detail="No matching entities found")

    # Expand neighborhood
    all_nodes = dict(seeds)
    frontier = list(seeds.keys())
    edge_set: set[tuple] = set()
    edges = []

    for _ in range(max_hops):
        adj = _get_adjacency(conn, frontier)
        next_frontier = []
        for src_id in frontier:
            for neighbor in adj.get(src_id, []):
                nid = neighbor["id"]
                edge_key = tuple(sorted([src_id, nid]))
                if edge_key not in edge_set:
                    edge_set.add(edge_key)
                    edges.append({"source": src_id, "target": nid, "weight": neighbor["weight"]})
                if nid not in all_nodes:
                    all_nodes[nid] = {
                        "id": nid, "name": neighbor["name"],
                        "type": neighbor["type"], "is_seed": False,
                    }
                    next_frontier.append(nid)
        frontier = next_frontier

    # Filter edges to only those between nodes in our set
    final_edges = [e for e in edges if e["source"] in all_nodes and e["target"] in all_nodes]

    # Silo + kind, resolved LIVE via the silo_kind view join (task 11a), for every
    # node in the subgraph — seeds included.
    silo_info = entity_silos(conn, list(all_nodes.keys()))
    for nid, node in all_nodes.items():
        sinfo = silo_info.get(nid, {})
        node["silo_id"] = sinfo.get("silo_id")
        node["kind"] = sinfo.get("kind")
    for nid, seed in seeds.items():
        sinfo = silo_info.get(nid, {})
        seed["silo_id"] = sinfo.get("silo_id")
        seed["kind"] = sinfo.get("kind")

    store.close()

    result = {
        "query_id": qid,
        "seeds": list(seeds.values()),
        "nodes": list(all_nodes.values()),
        "edges": final_edges,
        "node_count": len(all_nodes),
        "edge_count": len(final_edges),
    }

    # Broadcast to viz — light up all nodes in the subgraph
    entity_names_out = [n["name"] for n in result["nodes"][:30]]
    seed_names = [s["name"] for s in result["seeds"]]
    await broadcast_search(f"subgraph:{','.join(seed_names)}", entity_names_out)

    return result


@router.get("/domain-overview/{domain_path:path}")
async def explore_domain(
    domain_path: str,
    auth: AuthStore = Depends(get_auth_store),
    qid: str = Depends(query_id),
):
    """Overview of a domain: top entities, entity type distribution, document list, related domains."""
    store = auth.store
    conn = store._conn if hasattr(store, '_conn') else store.entities._conn

    domain = store.domains.get(domain_path)
    if not domain:
        store.close()
        raise HTTPException(status_code=404, detail=f"Domain not found: {domain_path}")

    # Documents in this domain
    doc_rows = conn.execute("""
        SELECT d.id, d.title, d.content_type FROM documents d
        JOIN document_domains dd ON d.id = dd.document_id
        WHERE dd.domain_path = ? AND d.invalid_at IS NULL ORDER BY d.created_at DESC
    """, (domain_path,)).fetchall()
    documents = [{"id": r["id"], "title": r["title"], "content_type": r["content_type"] or "text"} for r in doc_rows]

    # Top entities in this domain, by how many of its documents mention them.
    # Was an IN clause over EVERY document id in the domain, which raises past
    # SQLite's bound-parameter limit — so this failed exactly on the large domains
    # it is most useful for. The shared read does the same work, scoped and bounded.
    top_entities = []
    type_counts: dict[str, int] = defaultdict(int)
    for e in entities_in_domain(conn, domain_path, limit=30):
        top_entities.append({
            "id": e["id"], "name": e["canonical_name"],
            "type": e["type"], "doc_count": e["degree"],
        })
        type_counts[e["type"]] += 1

    # Related domains (share entities via trade routes)
    related_rows = conn.execute("""
        SELECT dd2.domain_path, COUNT(DISTINCT es1.entity_id) as shared_entities
        FROM document_domains dd1
        JOIN entity_sources es1 ON dd1.document_id = es1.document_id
        JOIN entities e ON e.id = es1.entity_id AND e.invalid_at IS NULL
        JOIN entity_sources es2 ON es1.entity_id = es2.entity_id AND es1.document_id != es2.document_id
        JOIN document_domains dd2 ON es2.document_id = dd2.document_id
        WHERE dd1.domain_path = ? AND dd2.domain_path != ?
        GROUP BY dd2.domain_path ORDER BY shared_entities DESC LIMIT 10
    """, (domain_path, domain_path)).fetchall()
    related_domains = [{"path": r["domain_path"], "shared_entities": r["shared_entities"]} for r in related_rows]

    store.close()

    result = {
        "query_id": qid,
        "domain": {"path": domain_path, "document_count": len(documents), "spec_version": domain.spec_version},
        "documents": documents,
        "top_entities": top_entities,
        "entity_type_distribution": dict(type_counts),
        "related_domains": related_domains,
    }

    # Broadcast to viz — light up top entities in this domain
    entity_names = [e["name"] for e in top_entities[:15]]
    if entity_names:
        await broadcast_search(f"domain:{domain_path}", entity_names)

    return result


@router.get("/domain/{domain_path:path}/neighbours")
def domain_neighbours_route(
    domain_path: str,
    limit: int = 10,
    auth: AuthStore = Depends(get_auth_store),
    qid: str = Depends(query_id),
):
    """Domains sharing entities with `domain_path`, strongest first.

    The domain side panel was fetching the WHOLE graph payload and filtering it in the
    browser to show six neighbours — on the large graph that is a 57k-node index plus
    9.4k edges, several MB and seconds, to render one short list. It called this path
    already; the route simply did not exist, so the panel's neighbours section had been
    silently failing on a 404 (the fetch is inside a try/catch that treats trade routes
    as optional, which is why nothing surfaced).

    `{domain_path:path}` because domain paths are hierarchical and contain `/`. The
    trailing literal segment still binds: the greedy match backtracks to leave
    `/neighbours` for the suffix.
    """
    store = auth.store
    try:
        return {"domain": domain_path,
                "neighbours": domain_neighbours(store.conn, domain_path, limit=limit),
                "query_id": qid}
    finally:
        store.close()
