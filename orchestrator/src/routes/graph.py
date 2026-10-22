"""Serve the graph payload (contract v5) for the visualization.

`GET /graph` serves a materialized, cached snapshot (see
`pipeline/graph_snapshot.py`); it no longer recomputes the payload per request.
"""

import dataclasses
import json
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from ..dependencies import get_auth_store, AuthStore
from ..pipeline.graph_v5 import build_graph_v5
from ..pipeline.slice import parse_slice, Slice, ensure_slice_doc_temp_table
from ..pipeline.time_scope import TimeScopeError

router = APIRouter()

# TTL cache for SLICED payloads only (never the unsliced get_or_build snapshot path).
# Key: (workspace_id, raw spec json, minute). For a virtual noosphere the workspace id is the VIEW id.
# A view can be up to _SLICE_CACHE_TTL stale after a parent write (accepted for PR1).
_SLICE_CACHE_MAXSIZE = 64
_SLICE_CACHE_TTL = 60.0
_slice_cache: dict = {}  # key -> (stored_at_monotonic, payload)
_slice_cache_lock = threading.Lock()


def clear_slice_cache() -> None:
    with _slice_cache_lock:
        _slice_cache.clear()


def _slice_cache_key(workspace_id, raw_spec):
    """Cache key: (workspace, canonical RAW slice spec, wall-clock minute). A relative window
    ("1w") has the same raw spec every request, so it hits within the minute even though its
    resolved `since` moves each second; two distinct absolute as_of values have different raw
    specs, so they never collide. The payload still echoes the TRUE resolved slice."""
    return (workspace_id, json.dumps(raw_spec, sort_keys=True), int(time.time() // 60))


def _cached_slice_payload(key, build):
    now = time.monotonic()
    with _slice_cache_lock:
        hit = _slice_cache.get(key)
        if hit is not None and now - hit[0] < _SLICE_CACHE_TTL:
            return hit[1]
    payload = build()
    with _slice_cache_lock:
        _slice_cache[key] = (time.monotonic(), payload)
        if len(_slice_cache) > _SLICE_CACHE_MAXSIZE:
            # drop expired first, then oldest, until under the cap
            t = time.monotonic()
            for k in [k for k, (ts, _) in _slice_cache.items() if t - ts >= _SLICE_CACHE_TTL]:
                _slice_cache.pop(k, None)
            while len(_slice_cache) > _SLICE_CACHE_MAXSIZE:
                _slice_cache.pop(min(_slice_cache, key=lambda k: _slice_cache[k][0]))
    return payload


def _chunked(seq, size=900):
    """Yield slices of `seq` — keeps `IN (?, ?, …)` clauses under SQLite's
    bound-parameter limit (SQLITE_MAX_VARIABLE_NUMBER, historically 999)."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


@router.get("/graph")
def get_graph_data(silo: str | None = None, kind: str | None = None,
                    since: str | None = None, until: str | None = None,
                    as_of: str | None = None, window: str | None = None,
                    domain: str | None = None, collection: str | None = None,
                    auth: AuthStore = Depends(get_auth_store)):
    """Return the graph payload (contract v5) from the cached snapshot.

    Materialized once and served cached (O(1)); writers flip `graph_snapshot.dirty`
    and the orchestrator's background task rebuilds. Only a missing — or pre-v5 —
    snapshot triggers an inline build here. See `pipeline/graph_snapshot.py` and
    docs/superpowers/specs/2026-08-05-graph-contract-design.md.

    The `?format` parameter is gone with the v4 adapter: there is one format now.

    `?silo=<id>` (or `?silo=none` for the null-silo pool) and `?kind=<k>` are SLICE
    FACETS routed through the resolver, exactly like `since/until/as_of/window/domain/
    collection`: a bare `?silo=`/`?kind=` now builds a sliced payload (the response
    gains `meta.slice`), node sizes/mention_count are recomputed IN-SLICE, and an
    entity is included iff it has ≥1 in-slice source. `silo=none` selects the
    null-silo pool via the resolver sentinel. Only the no-param full-graph request
    takes the unsliced cached `get_or_build` snapshot path.
    """
    from ..pipeline.graph_snapshot import get_or_build
    store = auth.store
    try:
        slc = auth.slice
        raw_spec = auth.slice_spec
        if slc is None and any(v is not None for v in
                               (since, until, as_of, window, domain, collection, silo, kind)):
            # Any slice facet present (including a bare silo/kind): resolve the whole
            # thing through the slice engine. Only the no-param request stays unsliced.
            params = {"since": since, "until": until, "as_of": as_of,
                      "window": window, "domain": domain, "collection": collection,
                      "silo": silo, "kind": kind}
            raw_spec = {k: v for k, v in params.items() if v is not None}
            try:
                slc = parse_slice(params)
            except TimeScopeError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if slc is None:
                # We entered this branch because a facet param was supplied, yet it
                # resolved to no slice — the only cause is a supplied-but-EMPTY value
                # (e.g. `?silo=` / `?kind=`), which parse_slice ignores. Don't silently
                # fall through to the full graph; an empty filter is a malformed request.
                raise HTTPException(status_code=400,
                                    detail="empty facet value is not a valid filter")
        if slc is not None:
            # Sliced: built on demand + TTL-cached.
            def build():
                p = dict(build_graph_v5(store, slice=slc))
                meta = dict(p.get("meta") or {})
                meta["slice"] = {k: (list(v) if isinstance(v, tuple) else v)
                                 for k, v in dataclasses.asdict(slc).items()}
                p["meta"] = meta
                return p
            return dict(_cached_slice_payload(_slice_cache_key(auth.user.workspace_id, raw_spec), build))
        payload = get_or_build(store)
    finally:
        store.close()
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

    ensure_slice_doc_temp_table(conn, Slice(collections=(collection_id,)))
    docs = conn.execute(
        "SELECT d.id, d.title, d.content, d.source_path, dc.role, dc.parent_path "
        "FROM documents d "
        "JOIN _slice_docs sd ON sd.doc_id = d.id "
        "JOIN document_collections dc ON dc.document_id = d.id AND dc.collection_id = ?",
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
