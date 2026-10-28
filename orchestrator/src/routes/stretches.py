from fastapi import APIRouter, Depends

from ..dependencies import get_auth_store, AuthStore

router = APIRouter()


@router.get("/stretches")
def get_stretches(since: str | None = None, until: str | None = None,
                  mic: str | None = None, collection: str | None = None,
                  limit: int = 100, offset: int = 0,
                  auth: AuthStore = Depends(get_auth_store)):
    """Enriched transcript stretches ordered by event time (`start`). `since`/`until` are
    ISO-8601 and select stretches overlapping the window (end >= since, start <= until)."""
    store = auth.store
    try:
        return store.documents.list_stretches(since=since, until=until, mic=mic,
                                              collection=collection, limit=limit, offset=offset)
    finally:
        store.close()
