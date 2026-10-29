# ABOUTME: Per-workspace decision: summarize on the local relay or the remote (LunaRoute) one.
# ABOUTME: Reads summarization_target/summarization_model from workspaces/registry.json; fails silent to local.

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("summarization_route")

_DEFAULT = {"target": "local", "model": None}


def workspace_summarization_config(base_db_path: str, db_path: str) -> dict:
    path = os.path.join(os.path.dirname(base_db_path), "workspaces", "registry.json")
    try:
        with open(path) as f:
            registry = json.load(f)
    except (OSError, ValueError):
        return dict(_DEFAULT)
    if not isinstance(registry, list):
        return dict(_DEFAULT)
    ws_id = Path(db_path).parent.name
    for entry in registry:
        if isinstance(entry, dict) and entry.get("id") == ws_id:
            model = entry.get("summarization_model")
            return {
                "target": entry.get("summarization_target") or "local",
                "model": model if isinstance(model, str) and model else None,
            }
    return dict(_DEFAULT)


def resolve_summarization(settings, db_path: str, *, local_relay, summ_relay):
    cfg = workspace_summarization_config(settings.db_path, db_path)
    relay = local_relay
    if cfg["target"] == "remote":
        if summ_relay is not None:
            relay = summ_relay
        else:
            logger.warning(
                "workspace %s wants remote summarization but no summarization relay is "
                "configured; falling back to local", Path(db_path).parent.name)
    return relay, cfg["model"] or settings.summarization_model
