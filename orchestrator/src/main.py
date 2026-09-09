import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from .config import get_settings
from .db import init_db
from .repositories.factory import _sqlite_workspace_db_path

logger = logging.getLogger(__name__)


def _active_workspace_ids() -> list[str]:
    """The 'default' noosphere plus every non-archived one in the registry."""
    ids = ["default"]
    registry_path = os.path.join(
        os.path.dirname(get_settings().db_path), "workspaces", "registry.json"
    )
    registry: list[dict] = []
    try:
        import json
        if os.path.exists(registry_path):
            with open(registry_path) as f:
                registry = json.load(f)
    except Exception as e:
        # File missing or unparseable — skip warmup; lazy init still works.
        logger.warning("Noosphere registry unreadable (%s): %s", registry_path, e)
        return ids
    for ws in registry:
        if ws.get("status") == "archived":
            continue
        ws_id = ws.get("id")
        if not ws_id:
            logger.warning("Skipping registry entry without id: %r", ws)
            continue
        if ws_id not in ids:
            ids.append(ws_id)
    return ids


async def _rebuild_dirty_snapshots() -> None:
    """One sweep: rebuild the graph snapshot for every workspace flagged dirty.

    The CPU-bound build (embeddings/UMAP + aggregation) runs in a worker thread
    so it never blocks the event loop. A burst of writes between sweeps
    collapses into a single rebuild (debounce via the dirty bit).
    """
    from .pipeline.graph_snapshot import is_dirty, rebuild_snapshot
    from .repositories.factory import get_store
    settings = get_settings()

    def _rebuild_one(ws_id: str):
        store = get_store(workspace_id=ws_id)
        try:
            if not is_dirty(store):
                return None
            payload = rebuild_snapshot(
                store, max_render_nodes=settings.graph_render_max_nodes
            )
            return payload.get("meta", {}).get("counts", {}).get("nodes_total", 0)
        finally:
            store.close()

    for ws_id in _active_workspace_ids():
        try:
            n = await asyncio.to_thread(_rebuild_one, ws_id)
            if n is not None:
                logger.info("Graph snapshot rebuilt for %s (%d nodes)", ws_id, n)
        except Exception:
            logger.warning("Graph snapshot rebuild failed for %s", ws_id, exc_info=True)


async def _snapshot_rebuild_loop(interval: int) -> None:
    while True:
        try:
            await _rebuild_dirty_snapshots()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Snapshot rebuild loop iteration failed", exc_info=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run schema + migrations for the default noosphere up front so the first
    # request doesn't pay the cost (and doesn't collide with the worker).
    init_db(_sqlite_workspace_db_path("default"))
    # Also init any noospheres already in the registry so their first request
    # is a fast read instead of a write-locked migration.
    for ws_id in _active_workspace_ids():
        try:
            init_db(_sqlite_workspace_db_path(ws_id))
        except Exception:
            # One bad noosphere shouldn't stop the others from being warmed.
            logger.warning("Noosphere warmup failed for %s", ws_id, exc_info=True)
    # Pre-warm SentenceTransformer model so first /graph request isn't slow
    try:
        from .pipeline.domain_layout import _embed_texts
        _embed_texts(["warmup"])
    except Exception:
        pass  # No embedding available — circular layout fallback

    # Background task: rebuild dirty graph snapshots so /graph serves an O(1) cache.
    # Skipped under tests — a shared test store must not be rebuilt/closed from a
    # background thread (tests drive /graph's inline build directly).
    from .repositories import factory
    snapshot_task = None
    interval = get_settings().graph_snapshot_rebuild_interval
    if interval > 0 and getattr(factory, "_test_store", None) is None:
        snapshot_task = asyncio.create_task(_snapshot_rebuild_loop(interval))

    yield

    if snapshot_task is not None:
        snapshot_task.cancel()
        try:
            await snapshot_task
        except (asyncio.CancelledError, Exception):
            pass

app = FastAPI(title="Orrery", lifespan=lifespan)


# ── Query correlation id (issue #93; owned by the API, not any one client) ─────
# Every request mints a `query_id`, stashed on request.state and returned in the `X-Query-Id`
# header. The capture-relevant READ routes (search / entity / graph reads / document reads)
# ALSO put it in their JSON body via the `query_id` dependency — so a bare `curl` (which won't
# see a header without -i) still logs it beside the entity ids already in the body. The
# middleware itself never touches a response body: it only sets the header. This keeps file
# downloads, lists, streams, and error responses untouched — nothing to buffer or re-serialize.
import uuid as _uuid
from starlette.middleware.base import BaseHTTPMiddleware


class QueryIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        qid = f"qry_{_uuid.uuid4().hex}"
        request.state.query_id = qid
        response = await call_next(request)
        response.headers["X-Query-Id"] = qid
        return response


app.add_middleware(QueryIdMiddleware)

# The graph payload is large and highly repetitive, and nothing was compressing it:
# /graph served ~31 MB raw on the large graph with no content-encoding at all. It
# gzips ~9x (to ~3.4 MB), because the bulk is repeated keys and ids. The 1 KB floor
# keeps small JSON responses uncompressed, where the CPU is not worth it.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from .routes.ingest import router as ingest_router
from .routes.documents import router as documents_router
from .routes.domains import router as domains_router
from .routes.entities import router as entities_router
from .routes.jobs import router as jobs_router
from .routes.simmer import router as simmer_router
from .routes.stats import router as stats_router
from .routes.normalize import router as normalize_router
from .routes.subdomains import router as subdomains_router
from .routes.graph import router as graph_router
from .routes.reader import router as reader_router
from .routes.search import router as search_router
from .routes.reclassify import router as reclassify_router
from .routes.auth_routes import router as auth_router
from .routes.workspace_routes import router as workspace_router
from .routes.image_files import router as image_files_router
from .routes.graph_ops import router as graph_ops_router
from .routes.corrections import router as corrections_router
from .routes.commentary import router as commentary_router
from .routes.watched_sources import router as watched_sources_router
from .routes.export import router as export_router

app.include_router(ingest_router)
app.include_router(documents_router)
app.include_router(domains_router)
app.include_router(entities_router)
app.include_router(jobs_router)
app.include_router(simmer_router)
app.include_router(stats_router)
app.include_router(normalize_router)
app.include_router(subdomains_router)
app.include_router(graph_router)
app.include_router(reader_router)
app.include_router(search_router)
app.include_router(reclassify_router)
app.include_router(image_files_router)
app.include_router(auth_router)
app.include_router(workspace_router)
app.include_router(graph_ops_router)
app.include_router(corrections_router)
app.include_router(commentary_router)
app.include_router(watched_sources_router)
app.include_router(export_router)

from fastapi import WebSocket as WS
from .broadcast import ws_endpoint

@app.websocket("/ws")
async def websocket_route(websocket: WS):
    await ws_endpoint(websocket)

@app.get("/health")
def health():
    return {"status": "ok"}
