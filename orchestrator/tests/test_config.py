"""Tests for config — single source of truth for defaults."""

import os
from unittest import mock


def test_defaults_come_from_dataclass():
    """When no env vars are set, get_settings() returns dataclass defaults."""
    env = {
        "AWS_ACCESS_KEY": "test",
        "AWS_SECRET_KEY": "test",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        # Need to reimport to pick up clean env
        from src.config import Settings, get_settings
        defaults = Settings()
        settings = get_settings()

        assert settings.simmer_iterations == defaults.simmer_iterations
        assert settings.chunk_size == defaults.chunk_size
        assert settings.domain_spec_threshold == defaults.domain_spec_threshold
        assert settings.general_spec_threshold == defaults.general_spec_threshold
        assert settings.classification_model == defaults.classification_model
        assert settings.extraction_model == defaults.extraction_model


def test_env_var_overrides_default():
    """Env var takes precedence over dataclass default."""
    env = {
        "AWS_ACCESS_KEY": "test",
        "AWS_SECRET_KEY": "test",
        "SIMMER_ITERATIONS": "7",
        "CHUNK_SIZE": "500",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        from src.config import get_settings
        settings = get_settings()

        assert settings.simmer_iterations == 7
        assert settings.chunk_size == 500


def test_no_duplicate_defaults():
    """The dataclass default and env var fallback should never disagree.

    This is a meta-test: it verifies the get_settings() pattern uses
    the dataclass defaults as fallbacks, not hardcoded strings.
    """
    from src.config import Settings
    defaults = Settings()

    # These are the values that matter most — if they drift, simmer runs
    # the wrong number of iterations, chunks are the wrong size, etc.
    assert defaults.simmer_iterations == 3
    assert defaults.chunk_size == 2000
    assert defaults.general_spec_threshold == 10
    assert defaults.domain_spec_threshold == 20


def _settings(env):
    with mock.patch.dict(os.environ, env, clear=True):
        from src.config import get_settings
        return get_settings()


def test_summarization_model_defaults_to_resolved_classification_model():
    s = _settings({"CLASSIFICATION_MODEL": "sentinel-26b"})
    assert s.summarization_model == "sentinel-26b"


def test_summarization_model_empty_string_falls_back():
    s = _settings({"CLASSIFICATION_MODEL": "sentinel-26b", "SUMMARIZATION_MODEL": ""})
    assert s.summarization_model == "sentinel-26b"


def test_summarization_model_explicit_set():
    s = _settings({"SUMMARIZATION_MODEL": "gemma4:26b"})
    assert s.summarization_model == "gemma4:26b"


def test_extraction_model_untouched():
    s = _settings({"EXTRACTION_MODEL": "e4b-x"})
    assert s.extraction_model == "e4b-x"


def _orch_settings(env):
    from src.config import get_settings
    with mock.patch.dict(os.environ, env, clear=True):
        return get_settings()


def test_reasoning_effort_default_and_env():
    assert _orch_settings({}).reasoning_effort == ""
    assert _orch_settings({"REASONING_EFFORT": "none"}).reasoning_effort == "none"


def test_relay_timeout_default_and_env():
    assert _orch_settings({}).relay_timeout_seconds == 0.0
    assert _orch_settings({"RELAY_TIMEOUT_SECONDS": "60"}).relay_timeout_seconds == 60.0


def test_empty_numeric_env_means_default():
    """RELAY_TIMEOUT_SECONDS= (empty) means unset; it must not crash get_settings()."""
    with mock.patch.dict(os.environ, {"RELAY_TIMEOUT_SECONDS": "", "CHUNK_SIZE": " "}, clear=True):
        from src.config import Settings, get_settings
        s = get_settings()
        assert s.relay_timeout_seconds == 0.0
        assert s.chunk_size == Settings().chunk_size


def test_non_numeric_numeric_env_still_fails_loudly():
    import pytest
    with mock.patch.dict(os.environ, {"RELAY_TIMEOUT_SECONDS": "abc"}, clear=True):
        from src.config import get_settings
        with pytest.raises(ValueError):
            get_settings()


def _settings_from(env):
    with mock.patch.dict(os.environ, env, clear=True):
        from src.config import get_settings
        return get_settings()


def test_ollama_pin_defaults():
    s = _settings_from({})
    assert s.ollama_num_ctx == 32768
    assert s.ollama_keep_alive == "30m"


def test_ollama_pin_env_overrides():
    s = _settings_from({"OLLAMA_NUM_CTX": "16384", "OLLAMA_KEEP_ALIVE": "1h"})
    assert (s.ollama_num_ctx, s.ollama_keep_alive) == (16384, "1h")


def test_ollama_num_ctx_empty_gives_default():
    assert _settings_from({"OLLAMA_NUM_CTX": ""}).ollama_num_ctx == 32768
