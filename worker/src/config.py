# ABOUTME: Application settings loaded from environment variables.
# ABOUTME: Supports both gateway and bedrock backends via ANTHROPIC_BACKEND env var.

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
    classification_model: str = "claude-sonnet-4-6"
    extraction_model: str = "claude-haiku-4-5"
    general_spec_threshold: int = 10
    domain_spec_threshold: int = 20
    simmer_iterations: int = 3
    chunk_size: int = 2000
    ollama_url: str = "http://localhost:11434"
    worker_poll_interval: int = 5
    judge_sweep_interval_seconds: int = 900  # periodic advisory-judge sweep cadence (~15 min)
    source_scan_interval_seconds: int = 900  # how often the sweep checks watched_sources for due scans
    db_path: str = "/data/orrery.db"
    documents_dir: str = "/data/documents"
    specs_dir: str = "/data/specs"
    judge_count: int = 1
    judge_samples: int = 1
    judge_panel: str = "auto"
    judge_deliberate: bool = True
    # Tracker-run summarization. tracker's `distill` module reads its own
    # activity.jsonl format — that schema belongs to tracker and drifts on its own
    # schedule, so orrery-tracksum takes an INJECTED trace reader rather than
    # vendoring the parser. Point this at the directory containing distill.py to let
    # the worker summarize RAW runs in-process; leave it empty and only pre-made
    # summary bundles can be ingested (which need no model calls at all).
    tracker_distill_path: str = ""
    # Normalization judge — drains the ~0.76-0.84 similarity review backlog with an LLM.
    # Runs ONLY when the worker is otherwise idle, so it never competes with
    # ingest/extract/simmer for the (possibly local) model. Modes:
    #   off    — never runs
    #   advise — write verdicts only, a human still resolves
    #   apply  — also auto-resolve confident keeps
    normalization_judge_mode: str = "advise"
    # Prefer a local Ollama model when reachable, else the cloud model: local gemma4:26b
    # matched Haiku on this task in evaluation, so the queue drains for free when Ollama
    # is up. Re-checked on a TTL rather than once at startup.
    normalization_judge_prefer_local: bool = True
    normalization_judge_local_model: str = "gemma4:26b"
    normalization_judge_model: str = ""      # cloud fallback; empty -> extraction_model
    normalization_judge_batch: int = 10      # pairs per relay call (one idle chunk)
    normalization_judge_min_confidence: float = 0.75   # apply threshold
    # Not 0: greedy decoding loops on a bad generation and never terminates.
    normalization_judge_temperature: float = 0.3
    normalization_judge_max_attempts: int = 3  # skip a pair after N failed sweeps
    # Magos Lex commentary sweep — backfills node_commentary for domains + collections.
    # Like the normalization judge, it runs ONLY when the worker is otherwise idle (no
    # real job ran this pass) and in small bounded batches, so it never competes with
    # ingest/extract/simmer for the (possibly local) model. only_missing is implicit:
    # a node that already has commentary is skipped, so a caught-up graph costs one cheap
    # indexed query per idle pass, and newly-added nodes are picked up automatically.
    commentary_sweep_enabled: bool = True
    commentary_sweep_batch: int = 5          # nodes per idle pass (one small chunk)


# Env var name mapping — keys are Settings field names, values are env var names.
# If a field isn't listed, it uses UPPER_SNAKE_CASE of the field name.
_ENV_MAP = {
    "anthropic_backend": "ANTHROPIC_BACKEND",
    "gateway_url": "GATEWAY_URL",
    "gateway_api_key": "GATEWAY_API_KEY",
    "aws_access_key": "AWS_ACCESS_KEY",
    "aws_secret_key": "AWS_SECRET_KEY",
    "aws_region": "AWS_REGION",
    "classification_model": "CLASSIFICATION_MODEL",
    "extraction_model": "EXTRACTION_MODEL",
    "general_spec_threshold": "GENERAL_SPEC_THRESHOLD",
    "domain_spec_threshold": "DOMAIN_SPEC_THRESHOLD",
    "simmer_iterations": "SIMMER_ITERATIONS",
    "chunk_size": "CHUNK_SIZE",
    "ollama_url": "OLLAMA_URL",
    "worker_poll_interval": "WORKER_POLL_INTERVAL",
    "judge_sweep_interval_seconds": "JUDGE_SWEEP_INTERVAL_SECONDS",
    "source_scan_interval_seconds": "SOURCE_SCAN_INTERVAL_SECONDS",
    "db_path": "DB_PATH",
    "documents_dir": "DOCUMENTS_DIR",
    "specs_dir": "SPECS_DIR",
    "judge_count": "JUDGE_COUNT",
    "judge_samples": "JUDGE_SAMPLES",
    "judge_panel": "JUDGE_PANEL",
    "judge_deliberate": "JUDGE_DELIBERATE",
    "tracker_distill_path": "TRACKER_DISTILL_PATH",
    "normalization_judge_mode": "NORMALIZATION_JUDGE_MODE",
    "normalization_judge_prefer_local": "NORMALIZATION_JUDGE_PREFER_LOCAL",
    "normalization_judge_local_model": "NORMALIZATION_JUDGE_LOCAL_MODEL",
    "normalization_judge_model": "NORMALIZATION_JUDGE_MODEL",
    "normalization_judge_batch": "NORMALIZATION_JUDGE_BATCH",
    "normalization_judge_min_confidence": "NORMALIZATION_JUDGE_MIN_CONFIDENCE",
    "normalization_judge_temperature": "NORMALIZATION_JUDGE_TEMPERATURE",
    "normalization_judge_max_attempts": "NORMALIZATION_JUDGE_MAX_ATTEMPTS",
    "commentary_sweep_enabled": "COMMENTARY_SWEEP_ENABLED",
    "commentary_sweep_batch": "COMMENTARY_SWEEP_BATCH",
}


def _xdg_data_dir() -> str:
    """Base data directory following XDG Base Directory spec."""
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return str(Path(xdg) / "orrery")


def get_settings() -> Settings:
    """Build Settings from env vars, falling back to dataclass defaults.

    Default paths use /data/ to match Docker layout. When /data/ doesn't
    exist (native dev without Docker), fall back to XDG paths.
    """
    defaults = Settings()

    # Use /data/ (Docker default) if it exists, otherwise XDG for native dev
    if os.path.isdir("/data"):
        _base = "/data"
    else:
        _base = _xdg_data_dir()

    path_defaults = {
        "db_path": f"{_base}/orrery.db",
        "documents_dir": f"{_base}/documents",
        "specs_dir": f"{_base}/specs",
    }

    kwargs = {}
    for f in fields(Settings):
        env_var = _ENV_MAP.get(f.name, f.name.upper())
        default = path_defaults.get(f.name, getattr(defaults, f.name))
        val = os.environ.get(env_var)
        if val is not None:
            if f.type is bool:
                kwargs[f.name] = str(val).strip().lower() in ("1", "true", "yes", "on")
            elif f.type in (int, float):
                kwargs[f.name] = f.type(val)
            else:
                kwargs[f.name] = val
        else:
            kwargs[f.name] = default

    return _validated(Settings(**kwargs))


def _validated(s: "Settings") -> "Settings":
    """Clamp the judge limits to values that mean what they say.

    These reach SQL and a scheduling decision directly, and the failure is silent rather
    than loud: `LIMIT -1` means NO LIMIT in SQLite, so a batch of -1 makes one "idle"
    pass judge the ENTIRE backlog — contending with real work, which is the single thing
    the idle gate exists to prevent. A max_attempts below 1 retries a hopeless pair
    forever; a confidence threshold outside 0..1 either auto-resolves everything or
    nothing. Clamped rather than raised, because a bad env var should not stop the worker
    booting — but it is reported, so the operator sees why their value did not take.
    """
    import dataclasses

    fixes: dict = {}
    if s.normalization_judge_batch < 1:
        fixes["normalization_judge_batch"] = 1
    if s.commentary_sweep_batch < 1:
        # Same trap as the judge batch: LIMIT -1 means NO LIMIT in SQLite, so a batch of
        # -1 makes one "idle" pass generate commentary for the ENTIRE graph — the exact
        # contention the idle gate exists to prevent.
        fixes["commentary_sweep_batch"] = 1
    if s.normalization_judge_max_attempts < 1:
        fixes["normalization_judge_max_attempts"] = 1
    if not 0.0 <= s.normalization_judge_min_confidence <= 1.0:
        fixes["normalization_judge_min_confidence"] = min(
            1.0, max(0.0, s.normalization_judge_min_confidence))
    if s.normalization_judge_mode not in ("off", "advise", "apply"):
        fixes["normalization_judge_mode"] = "advise"
    if fixes:
        for name, value in fixes.items():
            print(f"config: {name}={getattr(s, name)!r} is out of range; using {value!r}",
                  flush=True)
        s = dataclasses.replace(s, **fixes)
    return s
