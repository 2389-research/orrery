# ABOUTME: Virtual noospheres (#119): a slice is a registry entry resolving to the parent store, read-only.
import pytest

from src.routes import workspace_routes


@pytest.fixture
def client(test_client, tmp_path, monkeypatch):
    # Isolate the registry from the real data dir.
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda: str(tmp_path / "registry.json"))
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
    resp = client.post("/ingest/text", json={"text": "hello", "title": "t"},
                       headers={"X-Workspace-Id": vid})
    assert resp.status_code == 400
    assert "read-only" in resp.json()["detail"]
    assert "'default'" in resp.json()["detail"]


def test_reads_on_virtual_resolve_to_parent(client):
    vid = _make_view(client).json()["id"]
    resp = client.get("/stats", headers={"X-Workspace-Id": vid})
    assert resp.status_code == 200


def test_write_on_normal_workspace_not_blocked(client):
    resp = client.post("/ingest/text", json={"text": "hello", "title": "t"},
                       headers={"X-Workspace-Id": "default"})
    assert "read-only" not in str(resp.json().get("detail", ""))


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

