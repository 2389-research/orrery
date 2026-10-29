# ABOUTME: Application settings loaded from environment variables.
# ABOUTME: Supports gateway, bedrock, and ollama backends via ANTHROPIC_BACKEND env var.

import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    anthropic_backend: str = "gateway"
    gateway_url: str = ""
    gateway_api_key: str = ""
    aws_access_key: str = ""
    aws_secret_key: str = ""
    aws_region: str = "us-east-1"
    ollama_url: str = "http://localhost:11434"
    classification_model: str = "claude-sonnet-4-6"
    extraction_model: str = "claude-haiku-4-5"
    summarization_model: str = ""  # empty -> resolved classification_model
    general_spec_threshold: int = 10
    domain_spec_threshold: int = 20
    simmer_iterations: int = 3
    chunk_size: int = 2000
    worker_poll_interval: int = 5
    db_path: str = "/data/orrery.db"
    documents_dir: str = "/data/documents"
    specs_dir: str = "/data/specs"
    # Graph payload: the render cap is a *rendering* bound, not a data bound —
    # `meta.counts` always reports the true totals so a capped payload is visible
    # as capped rather than silently truncated.
    graph_render_max_nodes: int = 3000
    # Seconds between background sweeps that rebuild dirty graph snapshots.
    # 0 disables the loop (the inline build in /graph still covers correctness).
    graph_snapshot_rebuild_interval: int = 20
    # How often the worker sweep checks watched_sources for due scans (~15 min).
    # Read here too so both services agree on the field (the sweep runs in the worker).
    source_scan_interval_seconds: int = 900


def _xdg_data_dir() -> str:
    """Base data directory following XDG Base Directory spec."""
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return str(Path(xdg) / "orrery")


def get_settings() -> Settings:
    """Build Settings from env vars, falling back to dataclass defaults."""
    defaults = Settings()
    _base = _xdg_data_dir()

    # Override path defaults with XDG-based paths when not in Docker
    path_defaults = {
        "db_path": f"{_base}/orrery.db",
        "documents_dir": f"{_base}/documents",
        "specs_dir": f"{_base}/specs",
    }

    kwargs = {}
    for f in fields(Settings):
        env_var = f.name.upper()
        default = path_defaults.get(f.name, getattr(defaults, f.name))
        val = os.environ.get(env_var)
        if val is not None:
            kwargs[f.name] = f.type(val) if f.type in (int, float) else val
        else:
            kwargs[f.name] = default

    kwargs["summarization_model"] = kwargs["summarization_model"] or kwargs["classification_model"]

    return Settings(**kwargs)
