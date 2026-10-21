"""Workspace CRUD endpoints.

Manages workspaces as separate .db files via a JSON registry.
"""

import os
import json
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from ..auth import AuthUser, require_role

router = APIRouter()


# --- SQLite workspace registry ---

def _registry_path(create: bool = True) -> str:
    from ..config import get_settings
    base_dir = os.path.dirname(get_settings().db_path)
    ws_dir = os.path.join(base_dir, "workspaces")
    if create:
        os.makedirs(ws_dir, exist_ok=True)
    return os.path.join(ws_dir, "registry.json")

def _load_registry() -> list[dict]:
    path = _registry_path()
    if not os.path.exists(path):
        default = [{"id": "default", "name": "Default", "description": "", "status": "active",
                     "createdAt": datetime.now(timezone.utc).isoformat()}]
        _save_registry(default)
        return default
    with open(path) as f:
        return json.load(f)

def _save_registry(workspaces: list[dict]):
    import tempfile
    path = _registry_path()
    # Atomic write — write to temp file then rename
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(workspaces, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


# --- Models ---

class CreateWorkspaceRequest(BaseModel):
    name: str
    description: str = ""

class RenameWorkspaceRequest(BaseModel):
    name: str

class CreateViewRequest(BaseModel):
    name: str
    parent: str
    slice: dict


# --- Endpoints ---

@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    req: CreateWorkspaceRequest,
    response: Response,
    user: AuthUser = Depends(require_role("admin")),
):
    ws_id = str(uuid.uuid4())[:8]
    # Create the workspace DB before recording it in the registry so a failed
    # init_db doesn't leave behind a registry entry pointing at nothing.
    from ..db import init_db
    from ..repositories.factory import _sqlite_workspace_db_path
    init_db(_sqlite_workspace_db_path(ws_id))
    registry = _load_registry()
    registry.append({
        "id": ws_id, "name": req.name, "description": req.description,
        "status": "active", "createdAt": datetime.now(timezone.utc).isoformat(),
    })
    _save_registry(registry)
    response.headers["Location"] = f"/workspaces/{ws_id}"
    return {"workspaceId": ws_id, "name": req.name}


@router.post("/views")
async def create_view(
    req: CreateViewRequest,
    user: AuthUser = Depends(require_role("editor")),
):
    """Create a read-only virtual noosphere: a slice (facet view) of a parent workspace (#119)."""
    from ..pipeline.slice import parse_slice
    from ..pipeline.time_scope import TimeScopeError
    registry = _load_registry()
    parent = next((w for w in registry
                   if w["id"] == req.parent and w.get("status") != "archived"), None)
    if parent is None:
        raise HTTPException(404, "Parent workspace not found")
    if parent.get("kind") == "virtual":
        raise HTTPException(400, "A view cannot be sliced from another view")
    try:
        parsed = parse_slice(req.slice)
    except TimeScopeError as e:
        raise HTTPException(400, str(e))
    except (TypeError, ValueError, AttributeError) as e:
        raise HTTPException(400, f"malformed slice: {e}")
    if parsed is None:
        raise HTTPException(400, "slice has no facets")
    view_id = uuid.uuid4().hex
    registry.append({
        "id": view_id, "name": req.name, "kind": "virtual", "parent": req.parent,
        "slice": req.slice, "status": "active",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    })
    _save_registry(registry)
    return {"id": view_id, "name": req.name}


@router.get("/workspaces")
async def list_workspaces(user: AuthUser = Depends(require_role("viewer"))):
    """Return all active workspaces."""
    registry = _load_registry()
    return [ws for ws in registry if ws.get("status") != "archived"]


@router.patch("/workspaces/{workspace_id}")
async def rename_workspace(
    workspace_id: str,
    req: RenameWorkspaceRequest,
    user: AuthUser = Depends(require_role("admin")),
):
    registry = _load_registry()
    for ws in registry:
        if ws["id"] == workspace_id:
            ws["name"] = req.name
            _save_registry(registry)
            return {"updated": True}
    raise HTTPException(404, "Workspace not found")


@router.delete("/workspaces/{workspace_id}")
async def archive_workspace(
    workspace_id: str,
    user: AuthUser = Depends(require_role("admin")),
):
    """Soft delete — sets status to archived."""
    registry = _load_registry()
    for ws in registry:
        if ws["id"] == workspace_id:
            ws["status"] = "archived"
            _save_registry(registry)
            return {"archived": True}
    raise HTTPException(404, "Workspace not found")
