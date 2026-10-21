# ABOUTME: /documents honours the active slice (window/domain/collection + virtual noosphere) (graph slices PR 3).
from datetime import datetime, timedelta, timezone

import pytest

from src.routes import workspace_routes


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def client(test_client, test_store, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    c = test_store.conn
    for did, age, dom in (("d_old", 20, "a/x"), ("d_new_a", 1, "a/x"), ("d_new_b", 2, "b/y")):
        c.execute("INSERT INTO documents (id,title,created_at) VALUES (?,?,?)", (did, did, _ts(age)))
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary) VALUES (?,?,1)",
                  (did, dom))
    c.commit()
    return test_client


def _ids(r):
    return [d["id"] for d in r["documents"]]


def test_unsliced_bare_list(client):
    r = client.get("/documents").json()
    assert isinstance(r, list) and {d["id"] for d in r} == {"d_old", "d_new_a", "d_new_b"}


def test_window(client):
    r = client.get("/documents?window=1w").json()
    assert set(r) == {"documents", "scope"}
    assert _ids(r) == ["d_new_a", "d_new_b"]
    assert r["scope"]["since"]


def test_domain_narrows(client):
    r = client.get("/documents?domain=a").json()
    assert set(_ids(r)) == {"d_old", "d_new_a"}
    r = client.get("/documents?window=1w&domain=a").json()
    assert _ids(r) == ["d_new_a"]
    assert r["scope"]["domains"] == ["a"]


def test_limit_offset_order_within_slice(client):
    r = client.get("/documents?window=1w&order=asc&limit=1&offset=1").json()
    assert _ids(r) == ["d_new_a"]


def test_virtual_noosphere(client):
    vid = client.post("/views", json={"name": "wk", "parent": "default",
                                      "slice": {"window": "1w"}}).json()["id"]
    r = client.get("/documents", headers={"X-Workspace-Id": vid}).json()
    assert set(_ids(r)) == {"d_new_a", "d_new_b"} and "scope" in r


def test_bad_window_400(client):
    assert client.get("/documents?window=bogus").status_code == 400
