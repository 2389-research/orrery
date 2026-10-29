from src.routes.workspace_routes import _load_registry


def _entry(wid="default"):
    return next(w for w in _load_registry() if w["id"] == wid)


def test_put_remote_with_model(test_client):
    r = test_client.put("/workspaces/default/summarization",
                        json={"target": "remote", "model": "deepseek-4.1-flash"})
    assert r.status_code == 200
    e = _entry()
    assert e["summarization_target"] == "remote"
    assert e["summarization_model"] == "deepseek-4.1-flash"
    assert r.json()["summarization_target"] == "remote"


def test_put_local_null_model_clears(test_client):
    test_client.put("/workspaces/default/summarization",
                    json={"target": "remote", "model": "m"})
    r = test_client.put("/workspaces/default/summarization",
                        json={"target": "local", "model": None})
    assert r.status_code == 200
    e = _entry()
    assert e["summarization_target"] == "local"
    assert "summarization_model" not in e


def test_invalid_target_400(test_client):
    r = test_client.put("/workspaces/default/summarization",
                        json={"target": "remote-ish"})
    assert r.status_code == 400
    assert "summarization_target" not in _entry()


def test_unknown_workspace_404(test_client):
    r = test_client.put("/workspaces/nope/summarization", json={"target": "local"})
    assert r.status_code == 404
