import importlib
import os

import pytest

from src import config as config_mod

def _get(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config_mod.get_settings()

def test_judge_defaults_reproduce_today(monkeypatch):
    for k in ("JUDGE_COUNT", "JUDGE_SAMPLES", "JUDGE_PANEL", "JUDGE_DELIBERATE"):
        monkeypatch.delenv(k, raising=False)
    s = config_mod.get_settings()
    assert s.judge_count == 1 and s.judge_samples == 1
    assert s.judge_panel == "auto" and s.judge_deliberate is True

def test_judge_env_overrides(monkeypatch):
    s = _get(monkeypatch, JUDGE_COUNT="2", JUDGE_SAMPLES="3",
             JUDGE_PANEL="coverage_hawk,precision_hawk", JUDGE_DELIBERATE="false")
    assert s.judge_count == 2 and s.judge_samples == 3
    assert s.judge_panel == "coverage_hawk,precision_hawk"
    assert s.judge_deliberate is False   # MUST parse the string, not be truthy "false"


# --- the judge's own settings ------------------------------------------------

def test_judge_settings_default_to_advise_not_apply(monkeypatch):
    """`advise` by default: the judge writes verdicts, a human still resolves.

    Defaulting to `apply` would let a model auto-resolve entries in a graph the moment
    this ships, with no one having opted in.
    """
    for k in list(os.environ):
        if k.startswith("NORMALIZATION_JUDGE"):
            monkeypatch.delenv(k, raising=False)
    s = config_mod.get_settings()
    assert s.normalization_judge_mode == "advise"
    # prefer_local defaults OFF: the judge uses the configured small/extraction tier like
    # every other task, so a remote-model deployment loads no local LLM and does no Ollama
    # probe here. Running it on a local model is opt-in (NORMALIZATION_JUDGE_PREFER_LOCAL=1).
    assert s.normalization_judge_prefer_local is False
    assert s.normalization_judge_model == ""          # empty -> extraction_model
    assert s.normalization_judge_max_attempts == 3


def test_judge_default_resolves_to_small_tier_without_probing(monkeypatch):
    """With prefer_local off (the default), the judge resolves to the small/extraction
    tier on the primary relay and NEVER probes Ollama."""
    from src.jobs.normalization_judge import resolve_judge_relay
    for k in list(os.environ):
        if k.startswith("NORMALIZATION_JUDGE"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EXTRACTION_MODEL", "small-model-x")
    s = config_mod.get_settings()
    sentinel = object()

    def _probe_must_not_run(*a, **k):
        raise AssertionError("Ollama must not be probed when prefer_local is off")

    relay, model, source = resolve_judge_relay(s, sentinel, probe=_probe_must_not_run)
    assert relay is sentinel                       # primary relay, not a new ollama one
    assert model == "small-model-x"                # the configured small tier
    assert source == s.anthropic_backend


def test_the_judge_temperature_is_not_zero(monkeypatch):
    """Greedy decoding loops on a bad generation and never terminates.

    Pinned as a test because 0.0 is the obvious-looking value for a deterministic task,
    and the failure it causes (a hung sweep) looks nothing like a temperature problem.
    """
    monkeypatch.delenv("NORMALIZATION_JUDGE_TEMPERATURE", raising=False)
    assert config_mod.get_settings().normalization_judge_temperature > 0


def test_judge_settings_are_env_overridable(monkeypatch):
    monkeypatch.setenv("NORMALIZATION_JUDGE_MODE", "off")
    monkeypatch.setenv("NORMALIZATION_JUDGE_BATCH", "3")
    monkeypatch.setenv("NORMALIZATION_JUDGE_MIN_CONFIDENCE", "0.9")
    monkeypatch.setenv("NORMALIZATION_JUDGE_PREFER_LOCAL", "false")
    s = config_mod.get_settings()
    assert s.normalization_judge_mode == "off"
    assert s.normalization_judge_batch == 3
    assert s.normalization_judge_min_confidence == 0.9
    assert s.normalization_judge_prefer_local is False


def test_a_negative_batch_cannot_disable_the_limit(monkeypatch):
    """`LIMIT -1` means NO LIMIT in SQLite.

    So a batch of -1 makes one "idle" pass judge the entire backlog — contending with
    real work, which is the single thing the idle gate exists to prevent. The failure is
    silent: the query succeeds and does far more than asked.
    """
    monkeypatch.setenv("NORMALIZATION_JUDGE_BATCH", "-1")
    assert config_mod.get_settings().normalization_judge_batch >= 1
    monkeypatch.setenv("NORMALIZATION_JUDGE_BATCH", "0")
    assert config_mod.get_settings().normalization_judge_batch >= 1


def test_an_attempt_cap_below_one_would_retry_forever(monkeypatch):
    monkeypatch.setenv("NORMALIZATION_JUDGE_MAX_ATTEMPTS", "0")
    assert config_mod.get_settings().normalization_judge_max_attempts >= 1


@pytest.mark.parametrize("value,expected", [("-0.5", 0.0), ("1.5", 1.0), ("0.9", 0.9)])
def test_the_confidence_threshold_stays_within_zero_and_one(monkeypatch, value, expected):
    """Outside 0..1 it either auto-resolves everything or nothing."""
    monkeypatch.setenv("NORMALIZATION_JUDGE_MIN_CONFIDENCE", value)
    assert config_mod.get_settings().normalization_judge_min_confidence == expected


def test_an_unknown_mode_falls_back_to_advise(monkeypatch):
    """An unrecognised mode must not be treated as `apply` by accident."""
    monkeypatch.setenv("NORMALIZATION_JUDGE_MODE", "aply")   # typo
    assert config_mod.get_settings().normalization_judge_mode == "advise"
