from __future__ import annotations
"""FastAPI dependencies for authenticated data access.

Usage:
    from .dependencies import get_auth_store, AuthStore

    @router.get("/data")
    def get_data(auth: AuthStore = Depends(get_auth_store)):
        store = auth.store
        user = auth.user
        ...
        store.close()
"""

from dataclasses import dataclass
from fastapi import Depends, Header, HTTPException, Request
from .auth import get_current_user, AuthUser
from .repositories.factory import get_store
from .repositories.interfaces import DataStore
from .pipeline.slice import Slice


def query_id(request: Request) -> str:
    """The per-request correlation id minted by QueryIdMiddleware (issue #93). Capture-relevant
    READ routes depend on this and include it in their JSON body, so a session log (MCP or a bare
    curl) can correlate the call to the graph nodes it returned. Falls back to '' if the middleware
    isn't in the stack (e.g. a direct ASGI test)."""
    return getattr(request.state, "query_id", "") or ""


@dataclass
class AuthStore:
    """Authenticated data store — bundles user context with workspace-scoped store."""
    user: AuthUser
    store: DataStore
    # Set when the workspace is a virtual noosphere (a slice of `parent_id`): read-only.
    slice: Slice | None = None
    parent_id: str | None = None


async def validate_workspace(
    user: AuthUser = Depends(get_current_user),
    x_workspace_id: str | None = Header(default=None),
) -> AuthUser:
    """Validate X-Workspace-Id header. Updates user.workspace_id."""
    if x_workspace_id:
        user.workspace_id = x_workspace_id
    return user


def _view_entry(workspace_id: str) -> dict | None:
    """The registry entry for workspace_id if it is a virtual noosphere (any status), else None.
    Reads the registry only if it exists; never creates the registry or its directory."""
    import os
    from .routes import workspace_routes  # lazy: avoids an import cycle
    if not os.path.exists(workspace_routes._registry_path(create=False)):
        return None
    for ws in workspace_routes._load_registry():
        if ws.get("id") == workspace_id and ws.get("kind") == "virtual":
            return ws
    return None


async def get_auth_store(user: AuthUser = Depends(validate_workspace)) -> AuthStore:
    """Get a DataStore scoped to the authenticated user's workspace.

    A virtual noosphere resolves to its PARENT's store with the slice attached. A view id
    never falls through to get_store(view_id) (that would auto-create an orphan, writable DB)."""
    entry = _view_entry(user.workspace_id)
    if entry is None:
        store = get_store(workspace_id=user.workspace_id)
        return AuthStore(user=user, store=store)

    from .routes import workspace_routes
    from .pipeline.slice import parse_slice
    if entry.get("status") == "archived":
        raise HTTPException(status_code=410, detail="view has been archived")
    parent = next((w for w in workspace_routes._load_registry()
                   if w.get("id") == entry.get("parent")), None)
    if parent is None or parent.get("status") == "archived":
        raise HTTPException(status_code=409, detail="parent noosphere unavailable")
    try:
        slice_ = parse_slice(entry["slice"])
    except Exception:
        raise HTTPException(status_code=400, detail="view has an invalid slice spec")
    if slice_ is None:
        raise HTTPException(status_code=400, detail="view has an invalid slice spec")
    store = get_store(workspace_id=entry["parent"])
    return AuthStore(user=user, store=store, slice=slice_, parent_id=entry["parent"])


async def require_writable(auth: AuthStore = Depends(get_auth_store)) -> AuthStore:
    """get_auth_store for WRITE routes: virtual noospheres are read-only."""
    if auth.slice is not None:
        auth.store.close()
        raise HTTPException(status_code=400,
                            detail=f"read-only view; write to parent '{auth.parent_id}'")
    return auth
