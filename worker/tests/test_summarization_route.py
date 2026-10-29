"""Per-workspace summarization resolver (registry-backed)."""

import json
import logging
import os
from unittest import mock

WSID = "ws-abc"


def _setup(tmp_path, entry=None, raw=None):
    ws_dir = tmp_path / "workspaces"
    (ws_dir / WSID).mkdir(parents=True)
    ws_db = str(ws_dir / WSID / "orrery.db")
    if raw is not None:
        (ws_dir / "registry.json").write_text(raw)
    elif entry is not None:
        (ws_dir / "registry.json").write_text(json.dumps([entry]))
    env = {"DB_PATH": str(tmp_path / "orrery.db"), "SUMMARIZATION_MODEL": "global-model"}
    with mock.patch.dict(os.environ, env, clear=True):
        from src.config import get_settings
        return get_settings(), ws_db


def test_no_registry_defaults_local(tmp_path):
    from src.summarization_route import resolve_summarization, workspace_summarization_config
    s, db = _setup(tmp_path)
    assert workspace_summarization_config(s.db_path, db) == {"target": "local", "model": None}
    local, summ = object(), object()
    assert resolve_summarization(s, db, local_relay=local, summ_relay=summ) == (local, "global-model")


def test_remote_uses_summ_relay(tmp_path):
    from src.summarization_route import resolve_summarization
    s, db = _setup(tmp_path, {"id": WSID, "summarization_target": "remote"})
    local, summ = object(), object()
    assert resolve_summarization(s, db, local_relay=local, summ_relay=summ) == (summ, "global-model")


def test_model_override(tmp_path):
    from src.summarization_route import resolve_summarization
    s, db = _setup(tmp_path, {"id": WSID, "summarization_target": "remote",
                              "summarization_model": "deepseek-4.1-flash"})
    local, summ = object(), object()
    assert resolve_summarization(s, db, local_relay=local, summ_relay=summ) == (summ, "deepseek-4.1-flash")


def test_remote_without_summ_relay_falls_back_with_warning(tmp_path, caplog):
    from src.summarization_route import resolve_summarization
    s, db = _setup(tmp_path, {"id": WSID, "summarization_target": "remote"})
    local = object()
    with caplog.at_level(logging.WARNING):
        relay, model = resolve_summarization(s, db, local_relay=local, summ_relay=None)
    assert relay is local and model == "global-model"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_corrupt_registry_defaults_local(tmp_path):
    from src.summarization_route import workspace_summarization_config
    s, db = _setup(tmp_path, raw="{ not json")
    assert workspace_summarization_config(s.db_path, db) == {"target": "local", "model": None}
