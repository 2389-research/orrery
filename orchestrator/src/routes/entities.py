import dataclasses

from fastapi import APIRouter, HTTPException, Depends
from ..dependencies import get_auth_store, AuthStore, query_id
from ..repositories.factory import get_store
from ..pipeline.slice import parse_slice, ensure_slice_doc_temp_table, ensure_slice_temp_table
from ..pipeline.slice_edges import in_slice_cooccurrences
from ..pipeline.time_scope import TimeScopeError


def _active_slice(auth, since, until, as_of, window, domain, collection):
    """auth.slice (virtual noosphere) wins; else ad-hoc params; None if unsliced. 400 on bad input."""
    if auth.slice is not None:
        return auth.slice
    try:
        return parse_slice({"since": since, "until": until, "as_of": as_of, "window": window,
                            "domain": domain, "collection": collection})
    except (TimeScopeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


def _scope(slc):
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in dataclasses.asdict(slc).items()}

router = APIRouter()

@router.get("/entities")
def list_entities(
    limit: int = 50,
    offset: int = 0,
    type: str | None = None,
    domain: str | None = None,
    job_id: str | None = None,
    since: str | None = None, until: str | None = None, as_of: str | None = None,
    window: str | None = None, collection: str | None = None,
    auth: AuthStore = Depends(get_auth_store),
):
    # `domain` is the pre-existing entity domain filter AND a slice facet, so an ad-hoc
    # `domain` both filters and slices (consistent with slice semantics).
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    store = auth.store
    if slc is not None:
        ensure_slice_doc_temp_table(store.conn, slc)
    entities = store.entities.list(
        limit=limit, offset=offset,
        type_filter=type, domain_filter=domain, job_id=job_id, in_slice=slc is not None,
    )
    store.close()
    items = [{"id": e.id, "canonical_name": e.canonical_name, "type": e.type,
              "source_count": e.source_count} for e in entities]
    return {"entities": items, "scope": _scope(slc)} if slc is not None else items


@router.get("/entities/{entity_id}/cooccurrences")
def get_cooccurrences(entity_id: str, limit: int = 10, auth: AuthStore = Depends(get_auth_store),
                      since: str | None = None, until: str | None = None, as_of: str | None = None,
                      window: str | None = None, domain: str | None = None,
                      collection: str | None = None):
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    store = auth.store
    if slc is None:
        coentities = store.relationships.get_cooccurrences(entity_id, limit=limit)
        store.close()
        return [{"id": c.id, "canonical_name": c.canonical_name, "type": c.type,
                 "weight": c.weight} for c in coentities]
    # In-slice edges are recomputed live from in-slice chunks (stored edges can't be filtered).
    ensure_slice_temp_table(store.conn, slc)
    nbrs = in_slice_cooccurrences(store.conn, entity_id, limit)
    items = []
    if nbrs:
        ph = ",".join("?" * len(nbrs))
        meta = {r["id"]: r for r in store.conn.execute(
            f"SELECT id, canonical_name, type FROM entities WHERE id IN ({ph}) AND invalid_at IS NULL",
            [n for n, _ in nbrs])}
        items = [{"id": n, "canonical_name": meta[n]["canonical_name"], "type": meta[n]["type"],
                  "weight": w} for n, w in nbrs if n in meta]
    store.close()
    return {"cooccurrences": items, "scope": _scope(slc)}


@router.get("/entities/{entity_id}/star-graph")
def get_star_graph(entity_id: str, co_limit: int = 150, auth: AuthStore = Depends(get_auth_store),
                   since: str | None = None, until: str | None = None, as_of: str | None = None,
                   window: str | None = None, domain: str | None = None,
                   collection: str | None = None):
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    store = auth.store
    if slc is not None:
        ensure_slice_temp_table(store.conn, slc)
        ensure_slice_doc_temp_table(store.conn, slc)
    result = store.relationships.get_star_graph(entity_id, co_limit=co_limit, in_slice=slc is not None)
    store.close()
    if not result:
        raise HTTPException(status_code=404,
                            detail="Entity not found in slice" if slc is not None else "Entity not found")
    if slc is not None:
        result["scope"] = _scope(slc)
    return result


@router.get("/entities/{entity_id}")
def get_entity(entity_id: str, auth: AuthStore = Depends(get_auth_store), qid: str = Depends(query_id),
               since: str | None = None, until: str | None = None, as_of: str | None = None,
               window: str | None = None, domain: str | None = None, collection: str | None = None):
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    sliced = slc is not None
    store = auth.store
    if sliced:
        ensure_slice_doc_temp_table(store.conn, slc)
    entity = store.entities.get(entity_id, in_slice=sliced)
    if not entity:
        store.close()
        raise HTTPException(status_code=404, detail="Entity not found")
    sources = store.entity_sources.get_for_entity(entity_id, in_slice=sliced)
    if sliced and not sources:
        store.close()
        raise HTTPException(status_code=404, detail="Entity not found in slice")
    merge_history = store.normalization.get_merge_history(entity_id)
    # Attach each source doc's title/content_type so the client can label them
    # directly. Previously the panel joined against the paginated /documents list
    # (default limit 50), so any source doc past the first page showed a raw
    # doc-id hash instead of its title.
    # get_titles resolves silo_id + kind LIVE (via the silo_kind view join, on this
    # call) — so a source re-classified after ingest shows up on the very next read
    # (task 11a), not just after a snapshot rebuild.
    titles = store.documents.get_titles([s.document_id for s in sources])
    store.close()
    result = {
        "id": entity.id, "canonical_name": entity.canonical_name, "type": entity.type,
        "created_at": entity.created_at,
        "sources": [{"document_id": s.document_id, "chunk_id": s.chunk_id,
                      "extraction_pass": s.extraction_pass, "spec_version": s.spec_version,
                      "job_id": s.job_id,
                      "title": titles.get(s.document_id, {}).get("title"),
                      "content_type": titles.get(s.document_id, {}).get("content_type", "text"),
                      "silo_id": titles.get(s.document_id, {}).get("silo_id"),
                      "kind": titles.get(s.document_id, {}).get("kind")}
                     for s in sources],
        "merge_history": merge_history,
        "query_id": qid,
    }
    if sliced:
        result["source_count"] = entity.source_count
        result["scope"] = _scope(slc)
    return result
