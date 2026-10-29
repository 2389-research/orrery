"""summarization_model config: defaults to the RESOLVED classification_model."""

import os
from unittest import mock


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


def test_relay_knobs_default_unset():
    s = _settings({})
    assert s.reasoning_effort == "" and s.relay_timeout_seconds == 0.0
    assert s.summarization_reasoning_effort == "" and s.summarization_timeout_seconds == 0.0


def test_relay_knobs_load_from_env():
    s = _settings({
        "REASONING_EFFORT": "none", "RELAY_TIMEOUT_SECONDS": "60",
        "SUMMARIZATION_REASONING_EFFORT": "low", "SUMMARIZATION_TIMEOUT_SECONDS": "30",
    })
    assert s.reasoning_effort == "none" and s.relay_timeout_seconds == 60.0
    assert s.summarization_reasoning_effort == "low" and s.summarization_timeout_seconds == 30.0
