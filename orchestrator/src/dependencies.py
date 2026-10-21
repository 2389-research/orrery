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


def _virtual_entry(workspace_id: str) -> dict | None:
    """The active virtual-noosphere registry entry for workspace_id, else None.
    Reads the registry only if it already exists (never creates it as a side effect)."""
    import json, os
    from .routes import workspace_routes  # lazy: avoids an import cycle
    if not os.path.exists(workspace_routes._registry_path()):
        return None
    for ws in workspace_routes._load_registry():
        if ws.get("id") == workspace_id:
            if ws.get("kind") == "virtual" and ws.get("status") != "archived":
                return ws
            return None
    return None


async def get_auth_store(user: AuthUser = Depends(validate_workspace)) -> AuthStore:
    """Get a DataStore scoped to the authenticated user's workspace.

    A virtual noosphere resolves to its PARENT's store with the slice attached."""
    entry = _virtual_entry(user.workspace_id)
    if entry is not None:
        from .pipeline.slice import parse_slice
        store = get_store(workspace_id=entry["parent"])
        return AuthStore(user=user, store=store, slice=parse_slice(entry["slice"]),
                         parent_id=entry["parent"])
    store = get_store(workspace_id=user.workspace_id)
    return AuthStore(user=user, store=store)


async def require_writable(auth: AuthStore = Depends(get_auth_store)) -> AuthStore:
    """get_auth_store for WRITE routes: virtual noospheres are read-only."""
    if auth.slice is not None:
        auth.store.close()
        raise HTTPException(status_code=400,
                            detail=f"read-only view; write to parent '{auth.parent_id}'")
    return auth
