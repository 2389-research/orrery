import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse, PlainTextResponse
from ..dependencies import get_auth_store, require_writable, AuthStore, query_id
from ..repositories.factory import get_store
from ..pipeline.slice import ensure_slice_doc_temp_table
from .entities import _active_slice, _scope
from ..pipeline.file_extractor import (
    TEXT_EXTENSIONS, PDF_EXTENSIONS, DOCX_EXTENSIONS, NOTEBOOK_EXTENSIONS,
)
from .image_files import MEDIA_TYPES, serve_image
from ..repositories.graph_reads import silo_kinds_of

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/documents")
def list_documents(limit: int = 50, offset: int = 0,
                   since: str | None = None, until: str | None = None,
                   as_of: str | None = None, order: str = "desc",
                   window: str | None = None, domain: str | None = None,
                   collection: str | None = None,
                   auth: AuthStore = Depends(get_auth_store)):
    """List documents, newest-first. Slice-aware like /search and /entities: the effective
    slice is the virtual noosphere's (`auth.slice`) or the ad-hoc `since`/`until`/`as_of`/
    `window`/`domain`/`collection` params (400 on bad input). `since`/`until`/`as_of` filter
    on `created_at` (ISO-8601 or relative `-<n><m|h|d>`); `order=asc|desc` sorts. Unsliced,
    the response is a bare list; sliced, `{"documents": [...], "scope": {...}}`."""
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    store = auth.store
    if slc is None:
        docs = store.documents.list(limit=limit, offset=offset, order=order)
    else:
        ensure_slice_doc_temp_table(store.conn, slc)
        docs = store.documents.list(limit=limit, offset=offset, order=order, in_slice=True)
    result = []
    for d in docs:
        entity_count = len(store.entities.get_for_document(d.id)) if d.id else 0
        result.append({
            "id": d.id, "title": d.title, "status": d.status, "created_at": d.created_at,
            "domains": d.domains, "entity_count": entity_count,
            "content_type": d.content_type, "source_path": d.source_path,
        })
    store.close()
    return {"documents": result, "scope": _scope(slc)} if slc is not None else result

@router.get("/documents/{document_id}")
def get_document(document_id: str, auth: AuthStore = Depends(get_auth_store), qid: str = Depends(query_id)):
    store = auth.store
    doc = store.documents.get(document_id)
    if not doc:
        store.close()
        raise HTTPException(status_code=404, detail="Document not found")
    domains = store.domains.get_domains_for_document(document_id)
    entities = store.entities.get_for_document(document_id)
    # For code documents: a resolvable GitHub ref so an agent can fetch the actual
    # source (the graph holds the summary, not the code). None for non-code docs.
    git_ref = store.collections.get_source_ref(document_id)
    # kind resolved LIVE via the silo_kind view — a source re-classified after ingest
    # shows up on the very next read, with no per-doc copy to go stale (task 11a).
    kind = silo_kinds_of(store.conn, [doc.silo_id]).get(doc.silo_id)
    store.close()
    return {
        "id": doc.id, "title": doc.title, "source_path": doc.source_path,
        "content": doc.content, "status": doc.status, "created_at": doc.created_at,
        "content_type": doc.content_type,
        "git_ref": git_ref,
        "silo_id": doc.silo_id, "kind": kind,
        "domains": [{"path": d.domain_path, "is_primary": d.is_primary, "confidence": d.confidence} for d in domains],
        "entities": [{"id": e.id, "canonical_name": e.canonical_name, "type": e.type} for e in entities],
        "query_id": qid,
    }


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, auth: AuthStore = Depends(require_writable)):
    store = auth.store
    doc = store.documents.get(document_id)
    if not doc:
        store.close()
        raise HTTPException(status_code=404, detail="Document not found")

    result = store.documents.delete(document_id)
    store.close()

    for path in (doc.source_path, getattr(doc, "thumbnail_path", None)):
        try:
            if path and Path(path).exists():
                os.remove(path)
        except OSError as e:
            logger.warning("Failed to remove file %s for document %s: %s", path, document_id, e)

    return {"deleted": True, "entities_removed": result.get("entities_removed", [])}


@router.get("/documents/{document_id}/file")
def serve_document_file(document_id: str, auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    doc = store.documents.get(document_id)

    if not doc:
        store.close()
        raise HTTPException(status_code=404, detail="Document not found")

    suffix = Path(doc.source_path).suffix.lower() if doc.source_path else ""

    if suffix in MEDIA_TYPES:
        # serve_image looks up the document itself and closes the store.
        return serve_image(document_id, auth=auth)

    store.close()

    if suffix in PDF_EXTENSIONS or suffix in DOCX_EXTENSIONS:
        return PlainTextResponse(doc.content or "")

    if suffix in NOTEBOOK_EXTENSIONS:
        if not doc.source_path or not Path(doc.source_path).exists():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(doc.source_path, media_type="application/json")

    if suffix in TEXT_EXTENSIONS or not suffix:
        if not doc.source_path or not Path(doc.source_path).exists():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(doc.source_path, media_type="text/plain")

    raise HTTPException(status_code=404, detail="File not found")
