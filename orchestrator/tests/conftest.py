import os
import pytest

# Set env vars before any imports
os.environ.setdefault("AWS_ACCESS_KEY", "test-key")
os.environ.setdefault("AWS_SECRET_KEY", "test-secret")
os.environ.setdefault("AWS_REGION", "us-east-1")

from src.db import init_db
from src.repositories.sqlite_store import SQLiteDataStore
from src.repositories.factory import set_test_store


@pytest.fixture(autouse=True)
def _isolate_workspace_registry(tmp_path, monkeypatch):
    """Sandbox the workspace/view registry to a per-test tmp file.

    Several route tests (POST /workspaces, /views, rename/archive) otherwise read and
    WRITE the real `~/.local/share/orrery/workspaces/registry.json`, leaking hundreds of
    `rest-hygiene-test`/`Renamed`/`wk` entries into a developer's native data dir across
    runs. Point the registry at tmp for every test; tests that need their own path may
    still monkeypatch it again (their fixture runs after this autouse one, so it wins)."""
    try:
        from src.routes import workspace_routes
    except Exception:
        return
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))


@pytest.fixture
def test_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def test_store(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    store = SQLiteDataStore(db_path)
    set_test_store(store)
    yield store
    set_test_store(None)
    store.close()


@pytest.fixture
def test_client(test_store):
    from src.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    yield client
