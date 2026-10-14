"""Serve the graph payload (contract v5) for the visualization.

`GET /graph` serves a materialized, cached snapshot (see
`pipeline/graph_snapshot.py`); it no longer recomputes the payload per request.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from ..dependencies import get_auth_store, AuthStore

router = APIRouter()


class TraceIngestRequest(BaseModel):
    jsonl_path: str          # staged session log (like /ingest/repo's staged path)
    cwd: str | None = None   # repo root, to make touched paths collection-relative
    source: str = "claude_code"
    title: str | None = None


def _chunked(seq, size=900):
    """Yield slices of `seq` — keeps `IN (?, ?, …)` clauses under SQLite's
    bound-parameter limit (SQLITE_MAX_VARIABLE_NUMBER, historically 999)."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _filter_by_silo_kind(payload: dict, *, silo: str | None, kind: str | None) -> dict:
    """Post-filter the LOADED payload by `silo`/`kind` — the cached snapshot itself
    (`get_or_build`'s return value, keyed in `graph_snapshot`) is never mutated;
    this builds a new dict from it (task 11a: kind override propagation, without
    reaching through the cache to rebuild it on every filtered request).

    `silo="none"` selects the null-silo pool (nodes with no `silo_id` at all), not
    the literal string "none" — no real silo id can collide with it since ids are
    generated hex/uuid-shaped, never the four ASCII characters "none".

    Only nodes/edges are touched — `taxonomy` and `layout` describe the domain
    backbone, which has no silo of its own and stays whole. An edge is dropped only
    if one of its endpoints WAS a filterable node (entity/collection/document) that
    got excluded; a domain-cooccurrence edge's endpoints are domain PATHS, never
    entity/collection ids, so it is untouched by this filter even though domain
    nodes aren't in the render set to begin with.

    A node's `silo_id` here is its DOMINANT silo (see `graph_reads._pick_dominant`) —
    a node built from sources in more than one silo (rare: pre-#50 data, or a
    deliberate cross-silo merge) collapses to its heaviest one, so `?silo=<a
    non-dominant silo it still has sources in>` will not surface it. `get_entity`
    is unaffected: it lists every source's own silo individually.
    """
    def matches(node: dict) -> bool:
        if silo is not None:
            want = None if silo == "none" else silo
            if node.get("silo_id") != want:
                return False
        if kind is not None and node.get("kind") != kind:
            return False
        return True

    all_nodes = payload.get("nodes", ())
    node_index = payload.get("node_index", {})

    kept_nodes = [n for n in all_nodes if matches(n)]
    kept_index = {nid: n for nid, n in node_index.items() if matches(n)}

    filterable_ids = {n["id"] for n in all_nodes} | set(node_index.keys())
    kept_ids = {n["id"] for n in kept_nodes} | set(kept_index.keys())
    dropped_ids = filterable_ids - kept_ids

    edges = [e for e in payload.get("edges", ())
             if e.get("source") not in dropped_ids and e.get("target") not in dropped_ids]

    filtered = dict(payload)
    filtered["nodes"] = kept_nodes
    filtered["node_index"] = kept_index
    filtered["edges"] = edges
    return filtered


@router.get("/graph")
def get_graph_data(silo: str | None = None, kind: str | None = None,
                    auth: AuthStore = Depends(get_auth_store)):
    """Return the graph payload (contract v5) from the cached snapshot.

    Materialized once and served cached (O(1)); writers flip `graph_snapshot.dirty`
    and the orchestrator's background task rebuilds. Only a missing — or pre-v5 —
    snapshot triggers an inline build here. See `pipeline/graph_snapshot.py` and
    docs/superpowers/specs/2026-08-05-graph-contract-design.md.

    The `?format` parameter is gone with the v4 adapter: there is one format now.

    `?silo=<id>` (or `?silo=none` for the null-silo pool) and `?kind=<k>` are a
    POST-FILTER over the loaded snapshot (task 11a) — the cached payload stays whole;
    only this response is narrowed. Because the snapshot is cached, a `provenance_kind`
    changed after the last rebuild won't be reflected here until the snapshot rebuilds
    (acceptable per the plan — live reads elsewhere resolve it immediately).
    """
    from ..pipeline.graph_snapshot import get_or_build
    store = auth.store
    try:
        payload = get_or_build(store)
    finally:
        store.close()
    if silo is not None or kind is not None:
        payload = _filter_by_silo_kind(payload, silo=silo, kind=kind)
    return payload


def _collection_with_domain(conn, collection_id: str) -> dict:
    """Fetch a collection + its dominant domain (by doc count), as
    {id, name, kind, domain, document_count}. Raises 404 if it does not exist.
    Shared by the collection-structure and collection-summary endpoints."""
    from fastapi import HTTPException
    coll = conn.execute(
        "SELECT id, name, path, document_count, kind FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    dom = conn.execute(
        "SELECT dd.domain_path, COUNT(*) c FROM document_collections dc "
        "JOIN document_domains dd ON dc.document_id = dd.document_id "
        "WHERE dc.collection_id = ? GROUP BY dd.domain_path "
        "ORDER BY c DESC, dd.domain_path ASC LIMIT 1",
        (collection_id,),
    ).fetchone()
    return {"id": coll["id"], "name": coll["name"], "kind": coll["kind"],
            "domain": dom["domain_path"] if dom else None,
            "document_count": coll["document_count"]}


@router.get("/collections/{collection_id}/structure")
def get_collection_structure(collection_id: str, max_files: int = 400, auth: AuthStore = Depends(get_auth_store)):
    """A collection's internal tree, for the collection drill-in viz:
    collection -> groups (with entity counts) -> leaves (summary, source_path, entities).
    Code itself is NOT returned; source_path stands in as a placeholder.

    Only the top `max_files` files by entity count are returned as render nodes —
    the same top-N-by-degree strategy the main graph uses (`render_node_count`).
    A 7k-leaf collection is both unrenderable and a multi-MB payload; the response
    reports `total_files`/`rendered_files` so the client can show the cap.

    Connected collections reuse the precomputed collection↔collection routes from the cached graph
    snapshot (what /graph already ships) rather than recomputing an entity_sources
    self-join per request — that join was O(seconds→minutes) on large collections.
    """
    store = auth.store
    try:
        return _collection_structure(store, collection_id, max_files)
    finally:
        store.close()


def _collection_structure(store, collection_id: str, max_files: int) -> dict:
    conn = store.conn
    collection_info = _collection_with_domain(conn, collection_id)

    docs = conn.execute(
        "SELECT d.id, d.title, d.content, d.source_path, dc.role, dc.parent_path "
        "FROM documents d JOIN document_collections dc ON d.id = dc.document_id "
        "WHERE dc.collection_id = ? AND d.invalid_at IS NULL",
        (collection_id,),
    ).fetchall()

    all_file_ids = [d["id"] for d in docs if d["role"] == "leaf"]
    total_files = len(all_file_ids)

    # Rank files by entity count (cheap per-file aggregate — one row each) and keep
    # only the top `max_files`, mirroring the main graph rendering only its top-N
    # nodes by degree. Full structure for every leaf on a huge collection is unrenderable
    # and a multi-MB payload.
    counts: dict = {}
    for chunk in _chunked(all_file_ids):
        qc = ("SELECT es.document_id AS did, COUNT(*) c FROM entity_sources es "
              "JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL "
              "WHERE es.document_id IN (%s) GROUP BY es.document_id"
              % ",".join("?" * len(chunk)))
        for r in conn.execute(qc, chunk).fetchall():
            counts[r["did"]] = r["c"]
    # Deterministic top-N: most entities first, ties broken by doc id, so the same
    # files survive the cap across requests (a bare count sort left ties unstable).
    render_ids = sorted(all_file_ids, key=lambda i: (-counts.get(i, 0), i))[:max(0, max_files)]
    render_set = set(render_ids)

    # Full entity lists only for the rendered files (bounds the fetch + payload).
    ents_by_doc: dict = {}
    for chunk in _chunked(render_ids):
        q = ("SELECT es.document_id, e.id AS eid, e.canonical_name, e.type FROM entity_sources es "
             "JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL "
             "WHERE es.document_id IN (%s)" % ",".join("?" * len(chunk)))
        for r in conn.execute(q, chunk).fetchall():
            ents_by_doc.setdefault(r["document_id"], []).append(
                {"id": r["eid"], "name": r["canonical_name"], "type": r["type"]})

    def file_dict(d):
        ents = ents_by_doc.get(d["id"], [])
        return {"id": d["id"], "title": d["title"], "path": d["title"],
                "summary": d["content"], "source_path": d["source_path"],
                "entity_count": len(ents), "entities": ents}

    # Only the rendered (top-N) files are attached to their modules.
    files_by_parent: dict = {}
    for f in (d for d in docs if d["role"] == "leaf" and d["id"] in render_set):
        files_by_parent.setdefault(f["parent_path"] or ".", []).append(f)

    modules = []
    seen = set()
    for m in (d for d in docs if d["role"] == "group"):
        mpath = m["title"]
        seen.add(mpath)
        mfiles = [file_dict(f) for f in files_by_parent.get(mpath, [])]
        # `id` is the module-level code_intent doc — lets the viz open the module's
        # grounded summary in the same doc reader it uses for files.
        modules.append({"id": m["id"], "path": mpath, "summary": m["content"],
                        "entity_count": sum(f["entity_count"] for f in mfiles),
                        "files": mfiles})

    # Leaves whose parent is no group — for a git repo that is the checkout root, since
    # codesum gives root-level files `parent_path = '.'` (its REPO_PATH sentinel) and
    # group buckets are keyed by module title, which '.' never matches.
    #
    # The collection's OWN document is the summary for that bucket. This used to
    # invent `{"id": None, "summary": ""}` while the root document sat unused in the
    # same `docs` result set — so, unlike every real group, the viz could not open it
    # in the doc reader and showed no summary.
    root_doc = next((d for d in docs if d["role"] == "root"), None)
    root_files = []
    for parent, fs in files_by_parent.items():
        if parent in seen:
            continue
        root_files.extend(file_dict(f) for f in fs)
    if root_files:
        modules.append({"id": root_doc["id"] if root_doc else None,
                        "path": "(root)",
                        "summary": root_doc["content"] if root_doc else "",
                        "entity_count": sum(f["entity_count"] for f in root_files),
                        "files": root_files})

    # Connected collections — manifest-import edges from the (cheap) collection_edges table,
    # plus shared-entity links read from the precomputed graph snapshot. Replaces the
    # old per-request entity_sources self-join, which was the dominant cost on large
    # collections.
    connected: dict = {}
    for r in conn.execute(
        "SELECT target AS cid FROM collection_edges WHERE source = ? "
        "UNION SELECT source AS cid FROM collection_edges WHERE target = ?",
        (collection_id, collection_id),
    ).fetchall():
        connected.setdefault(r["cid"], {"via": set(), "weight": 0})["via"].add("import")
    try:
        from ..pipeline.graph_snapshot import get_or_build
        # v5: what v4 exposed as a top-level `repo_routes` key is now a typed entry in
        # the single `edges` collection. Reading the old key silently yielded [] — the
        # collection tier kept working but lost every shared-entity link, which the v5 golden
        # could not catch because this endpoint is not part of the migrated contract.
        routes = [e for e in get_or_build(store).get("edges", ())
                  if e.get("type") == "cooccurrence" and e.get("scope") == "collection"]
    except Exception:
        routes = []
    for rt in routes:
        src, tgt = rt.get("source"), rt.get("target")
        other = tgt if src == collection_id else (src if tgt == collection_id else None)
        if not other:
            continue
        c = connected.setdefault(other, {"via": set(), "weight": 0})
        c["via"].add("shared_entity")
        c["weight"] = max(c["weight"], rt.get("weight", 0) or 0)

    # Strongest links first (matches the main graph's degree-ordered rendering).
    ranked = sorted(connected.items(), key=lambda kv: kv[1]["weight"], reverse=True)
    connected_collections = []
    for cid, meta in ranked:
        rr = conn.execute("SELECT id, name, kind FROM collections WHERE id = ?", (cid,)).fetchone()
        if rr:
            connected_collections.append({"id": rr["id"], "name": rr["name"],
                                          "kind": rr["kind"], "via": sorted(meta["via"])})

    return {
        "collection": collection_info,
        "modules": modules,
        "connected_collections": connected_collections,
        "total_files": total_files,
        "rendered_files": len(render_set),
    }


@router.get("/collections/{collection_id}/trace")
def get_collection_trace(collection_id: str, trace_id: str | None = None,
                         auth: AuthStore = Depends(get_auth_store)):
    """Ordered file-touch trace + recursive-summary segments to replay over the
    collection's structure MAP (see /structure). Latest trace unless `trace_id` is
    given; an empty payload (never 404) when the collection has no trace yet, so the
    viz just renders the static map."""
    from ..pipeline.session_trace import read_trace
    store = auth.store
    try:
        return (read_trace(store.conn, collection_id, trace_id)
                or {"trace": None, "events": [], "segments": []})
    finally:
        store.close()


@router.post("/collections/{collection_id}/trace")
def ingest_collection_trace(collection_id: str, req: TraceIngestRequest,
                            auth: AuthStore = Depends(get_auth_store)):
    """Parse a staged session log into an ordered trace + segments and persist it
    against this repo collection. Returns the new trace id."""
    from ..pipeline.session_trace import parse_session_trace, persist_trace
    store = auth.store
    try:
        parsed = parse_session_trace(req.jsonl_path, req.cwd)
        tid = persist_trace(store.conn, collection_id, parsed,
                            source=req.source, title=req.title)
        return {"trace_id": tid, "n_events": len(parsed["events"]),
                "n_segments": len(parsed["segments"]), "n_files": len(parsed["files"])}
    finally:
        store.close()


@router.get("/collections/{collection_id}/summary")
def get_collection_summary(collection_id: str, limit: int = 8, auth: AuthStore = Depends(get_auth_store)):
    """Side-panel payload for a collection node: its grounded root-level LLM summary
    (what the collection is / does) plus its top entities by mention. Cheap — two
    aggregate queries. See the root document produced by the ingest job
    (dc.role == 'root')."""
    store = auth.store
    try:
        return _collection_summary(store, collection_id, limit)
    finally:
        store.close()


def _collection_summary(store, collection_id: str, limit: int) -> dict:
    conn = store.conn
    collection_info = _collection_with_domain(conn, collection_id)

    # Grounded root-level summary — the LLM's description of the whole collection,
    # stored as the repo-level code_intent document by the ingest_repo job.
    summ = conn.execute(
        "SELECT d.content FROM documents d "
        "JOIN document_collections dc ON dc.document_id = d.id "
        "WHERE dc.collection_id = ? AND dc.role = 'root' "
        "AND d.content_type = 'code_intent' AND d.invalid_at IS NULL LIMIT 1",
        (collection_id,),
    ).fetchone()

    # Top entities by mention count across this collection's documents.
    top = conn.execute(
        "SELECT e.id, e.canonical_name, e.type, COUNT(*) c FROM entity_sources es "
        "JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL "
        "JOIN document_collections dc ON dc.document_id = es.document_id "
        "WHERE dc.collection_id = ? GROUP BY e.id "
        "ORDER BY c DESC, e.canonical_name ASC, e.id ASC LIMIT ?",
        (collection_id, limit),
    ).fetchall()

    return {
        "collection": collection_info,
        "summary": (summ["content"] if summ else "") or "",
        "top_entities": [
            {"id": r["id"], "name": r["canonical_name"], "type": r["type"], "count": r["c"]}
            for r in top
        ],
    }
