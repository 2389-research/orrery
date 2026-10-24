import uuid
from fastapi import APIRouter, HTTPException, Depends
from ..db import mark_graph_dirty
from ..dependencies import get_auth_store, require_writable, AuthStore
from ..repositories.factory import get_store
from ..pipeline.embedding_normalizer import (
    run_batch_normalization,
    get_normalization_summary,
    get_review_queue,
    resolve_review,
)

router = APIRouter()


@router.post("/normalize")
def trigger_normalization(auth: AuthStore = Depends(require_writable)):
    store = auth.store
    try:
        results = run_batch_normalization(store)
        # Merges may have changed the graph — flag the snapshot for rebuild.
        mark_graph_dirty(store.conn)
        store.conn.commit()
    finally:
        store.close()
    return results


@router.get("/normalize/summary")
def normalization_summary(auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    try:
        return get_normalization_summary(store)
    finally:
        store.close()


@router.get("/normalize/review")
def review_queue(auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    try:
        return get_review_queue(store)
    finally:
        store.close()


@router.post("/normalize/review/{review_id}")
def resolve_review_item(review_id: str, action: str = "merge", auth: AuthStore = Depends(require_writable)):
    if action not in ("merge", "keep_separate"):
        raise HTTPException(status_code=400, detail="action must be 'merge' or 'keep_separate'")
    store = auth.store
    try:
        resolve_review(store, review_id, action)
        # A merge edits the active graph; keep_separate does not.
        if action == "merge":
            mark_graph_dirty(store.conn)
            store.conn.commit()
    finally:
        store.close()
    return {"status": "resolved", "action": action}


@router.post("/normalize/connect-within-kind", status_code=202)
def connect_within_kind(auth: AuthStore = Depends(require_writable)):
    """Enqueue the one-time backfill that merges exact-name+type duplicates within a
    provenance kind across the whole workspace (pre-feature per-silo fragments)."""
    store = auth.store
    try:
        job_id = str(uuid.uuid4())
        store.jobs.create(job_id, "connect_within_kind", None, {})
    finally:
        store.close()
    return {"job_id": job_id}
