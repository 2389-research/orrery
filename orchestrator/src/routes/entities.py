from fastapi import APIRouter, HTTPException, Depends
from ..dependencies import get_auth_store, AuthStore, query_id
from ..repositories.factory import get_store

router = APIRouter()

@router.get("/entities")
def list_entities(
    limit: int = 50,
    offset: int = 0,
    type: str | None = None,
    domain: str | None = None,
    job_id: str | None = None,
    auth: AuthStore = Depends(get_auth_store),
):
    store = auth.store
    entities = store.entities.list(
        limit=limit, offset=offset,
        type_filter=type, domain_filter=domain, job_id=job_id,
    )
    store.close()
    return [{"id": e.id, "canonical_name": e.canonical_name, "type": e.type,
             "source_count": e.source_count} for e in entities]


@router.get("/entities/{entity_id}/cooccurrences")
def get_cooccurrences(entity_id: str, limit: int = 10, auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    coentities = store.relationships.get_cooccurrences(entity_id, limit=limit)
    store.close()
    return [{"id": c.id, "canonical_name": c.canonical_name, "type": c.type,
             "weight": c.weight} for c in coentities]


@router.get("/entities/{entity_id}/star-graph")
def get_star_graph(entity_id: str, co_limit: int = 150, auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    result = store.relationships.get_star_graph(entity_id, co_limit=co_limit)
    if not result:
        store.close()
        raise HTTPException(status_code=404, detail="Entity not found")
    store.close()
    return result


@router.get("/entities/{entity_id}")
def get_entity(entity_id: str, auth: AuthStore = Depends(get_auth_store), qid: str = Depends(query_id)):
    store = auth.store
    entity = store.entities.get(entity_id)
    if not entity:
        store.close()
        raise HTTPException(status_code=404, detail="Entity not found")
    sources = store.entity_sources.get_for_entity(entity_id)
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
    return {
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
