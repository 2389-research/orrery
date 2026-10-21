# ABOUTME: Virtual noospheres (#119): a slice is a registry entry resolving to the parent store, read-only.
import pytest

from src.routes import workspace_routes


@pytest.fixture
def client(test_client, tmp_path, monkeypatch):
    # Isolate the registry from the real data dir.
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    return test_client


def _make_view(client, parent="default", slice_=None):
    return client.post("/views", json={
        "name": "last week", "parent": parent,
        "slice": {"window": "1w"} if slice_ is None else slice_})


def test_create_view_and_list(client):
    resp = _make_view(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] and body["name"] == "last week"
    entries = client.get("/workspaces").json()
    view = next(e for e in entries if e["id"] == body["id"])
    assert view["kind"] == "virtual"
    assert view["parent"] == "default"
    assert view["slice"] == {"window": "1w"}


def test_create_view_unknown_parent_404(client):
    assert _make_view(client, parent="nope").status_code == 404


def test_create_view_empty_slice_400(client):
    resp = _make_view(client, slice_={})
    assert resp.status_code == 400
    assert "no facets" in resp.json()["detail"]


def test_create_view_bad_window_400(client):
    assert _make_view(client, slice_={"window": "bogus"}).status_code == 400


def test_write_on_virtual_rejected(client):
    vid = _make_view(client).json()["id"]
    resp = client.post("/ingest/text", json={"content": "hello", "title": "t"},
                       headers={"X-Workspace-Id": vid})
    assert resp.status_code == 400
    assert "read-only" in resp.json()["detail"]
    assert "'default'" in resp.json()["detail"]


def test_reads_on_virtual_resolve_to_parent(client):
    vid = _make_view(client).json()["id"]
    resp = client.get("/stats", headers={"X-Workspace-Id": vid})
    assert resp.status_code == 200


def test_write_on_normal_workspace_not_blocked(client, tmp_path):
    from unittest.mock import AsyncMock, patch
    from src.config import Settings
    settings = Settings(aws_access_key="k", aws_secret_key="s",
                        db_path=str(tmp_path / "test.db"),
                        documents_dir=str(tmp_path / "documents"))
    cls = {"primary_domain": "a/b", "secondary_domains": [], "new_domains": [], "confidence": 0.9}
    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=cls), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=[]), \
         patch("src.routes.ingest.Relay"):
        resp = client.post("/ingest/text", json={"content": "hello", "title": "t"},
                           headers={"X-Workspace-Id": "default"})
    assert resp.status_code < 400, resp.text


@pytest.mark.asyncio
async def test_resolution_attaches_slice(client):
    from src.dependencies import get_auth_store
    from src.auth import AuthUser
    vid = _make_view(client).json()["id"]
    user = AuthUser(uid="u", email="e", workspace_id=vid)
    auth = await get_auth_store(user)
    assert auth.slice is not None and auth.parent_id == "default"
    user2 = AuthUser(uid="u", email="e", workspace_id="default")
    auth2 = await get_auth_store(user2)
    assert auth2.slice is None and auth2.parent_id is None



def _registry(client):
    return workspace_routes._load_registry()


def _set(client, view_id, **changes):
    reg = workspace_routes._load_registry()
    for w in reg:
        if w["id"] == view_id:
            w.update(changes)
    workspace_routes._save_registry(reg)


def test_archived_view_is_410(client):
    vid = _make_view(client).json()["id"]
    _set(client, vid, status="archived")
    r = client.get("/stats", headers={"X-Workspace-Id": vid})
    assert r.status_code == 410


def test_view_with_archived_parent_is_409(client):
    reg = workspace_routes._load_registry()
    reg.append({"id": "p1", "name": "P", "status": "active"})
    workspace_routes._save_registry(reg)
    vid = _make_view(client, parent="p1").json()["id"]
    _set(client, "p1", status="archived")
    assert client.get("/stats", headers={"X-Workspace-Id": vid}).status_code == 409


def test_view_with_missing_parent_is_409(client):
    vid = _make_view(client).json()["id"]
    _set(client, vid, parent="ghost")
    assert client.get("/stats", headers={"X-Workspace-Id": vid}).status_code == 409


def test_view_with_corrupt_slice_is_400(client):
    vid = _make_view(client).json()["id"]
    _set(client, vid, slice={"window": "bogus"})
    r = client.get("/stats", headers={"X-Workspace-Id": vid})
    assert r.status_code == 400 and "invalid slice" in r.json()["detail"]


def test_nested_view_rejected(client):
    vid = _make_view(client).json()["id"]
    assert _make_view(client, parent=vid).status_code == 400


def test_malformed_slice_types_400(client):
    assert _make_view(client, slice_={"window": ["x"]}).status_code == 400
    assert _make_view(client, slice_={"domains": 5, "window": 3}).status_code == 400
    # each of these has ONE problem: a facet value of the wrong type
    assert _make_view(client, slice_={"silo": ["x"]}).status_code == 400
    assert _make_view(client, slice_={"kind": {"a": 1}}).status_code == 400
    assert _make_view(client, slice_={"collection": {"a": 1}}).status_code == 400
    assert _make_view(client, slice_={"domain": [1, 2]}).status_code == 400
    assert _make_view(client, slice_={"silo": 5}).status_code == 400


def test_virtual_resolve_calls_get_store_with_parent(client, monkeypatch):
    from src import dependencies
    calls = []
    real = dependencies.get_store
    monkeypatch.setattr(dependencies, "get_store",
                        lambda **kw: (calls.append(kw), real(**kw))[1])
    vid = _make_view(client).json()["id"]
    assert client.get("/stats", headers={"X-Workspace-Id": vid}).status_code == 200
    assert calls == [{"workspace_id": "default"}]


def test_active_workspace_ids_skips_views(client, tmp_path, monkeypatch):
    from src import main
    from src.config import Settings
    vid = _make_view(client).json()["id"]
    monkeypatch.setattr(main, "get_settings", lambda: Settings(
        aws_access_key="k", aws_secret_key="s", db_path=str(tmp_path / "workspaces_parent" / "orrery.db")))
    # registry lives at <db dir>/workspaces/registry.json; mirror it there
    import os, shutil
    dst = tmp_path / "workspaces_parent" / "workspaces"
    os.makedirs(dst)
    shutil.copy(tmp_path / "registry.json", dst / "registry.json")
    ids = main._active_workspace_ids()
    assert vid not in ids and "default" in ids


# Mutating-verb routes that do NOT write the parent noosphere DB (registry/user/read-only POSTs).
READ_ONLY_ALLOWLIST = {
    ("POST", "/views"),
    ("POST", "/workspaces"),
    ("PATCH", "/workspaces/{workspace_id}"),
    ("DELETE", "/workspaces/{workspace_id}"),
    ("POST", "/graph/subgraph"),
}
READ_ONLY_PREFIXES = ("/auth", "/invites")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _calls(dep):
    yield dep.call
    for d in dep.dependencies:
        yield from _calls(d)


def _leaf_routes(routes, prefix="", extra=frozenset()):
    """Version-independent walk yielding (route, full_path, include_level_dep_callables).
    Descends into anything with its own `.routes` (APIRouter/Mount) or, on newer FastAPI,
    an `_IncludedRouter` (`original_router.routes` + `include_context` prefix/dependencies)."""
    for r in routes:
        orig = getattr(r, "original_router", None)
        ctx = getattr(r, "include_context", None)
        sub = getattr(r, "routes", None)
        if orig is not None:
            deps = {d.dependency for d in (getattr(ctx, "dependencies", None) or [])}
            yield from _leaf_routes(orig.routes, prefix + (getattr(ctx, "prefix", "") or ""),
                                    extra | deps)
        elif sub:
            yield from _leaf_routes(sub, prefix + (getattr(r, "path", "") or ""), extra)
        elif getattr(r, "methods", None):
            yield r, prefix + r.path, extra


def test_every_mutating_route_is_guarded_or_allowlisted():
    from src.main import app
    from src.dependencies import require_writable
    unguarded, checked = [], 0
    for route, path, extra in _leaf_routes(app.routes):
        dependant = getattr(route, "dependant", None)
        for m in route.methods & MUTATING:
            checked += 1
            if (m, path) in READ_ONLY_ALLOWLIST or path.startswith(READ_ONLY_PREFIXES):
                continue
            guards = set(extra) | (set(_calls(dependant)) if dependant is not None else set())
            if require_writable not in guards:
                unguarded.append((m, path))
    assert checked >= 20, f"walk only found {checked} mutating routes; enumeration is broken"
    assert not unguarded, f"mutating routes missing require_writable: {sorted(unguarded)}"


def test_corrupt_registry_does_not_break_normal_routes(client, tmp_path):
    (tmp_path / "registry.json").write_text("{not json")
    assert client.get("/stats").status_code == 200
