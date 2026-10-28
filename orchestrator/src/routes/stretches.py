from fastapi import APIRouter, Depends

from ..dependencies import get_auth_store, AuthStore
from ..pipeline.slice import ensure_slice_doc_temp_table
from .entities import _active_slice, _scope

router = APIRouter()


@router.get("/stretches")
def get_stretches(since: str | None = None, until: str | None = None,
                  mic: str | None = None, collection: str | None = None,
                  limit: int = 100, offset: int = 0,
                  auth: AuthStore = Depends(get_auth_store)):
    """Enriched transcript stretches ordered by event time (`start`). `since`/`until` are
    ISO-8601 and select stretches overlapping the window (end >= since, start <= until)."""
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)
    # Only the virtual noosphere's slice applies here (no ad-hoc slice params on this route).
    slc = _active_slice(auth, None, None, None, None, None, None)
    store = auth.store
    try:
        if slc is None:
            return store.documents.list_stretches(since=since, until=until, mic=mic,
                                                  collection=collection, limit=limit, offset=offset)
        ensure_slice_doc_temp_table(store.conn, slc)
        rows = store.documents.list_stretches(since=since, until=until, mic=mic,
                                              collection=collection, limit=limit, offset=offset,
                                              in_slice=True)
        return {"stretches": rows, "scope": _scope(slc)}
    finally:
        store.close()
