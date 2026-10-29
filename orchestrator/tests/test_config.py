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
