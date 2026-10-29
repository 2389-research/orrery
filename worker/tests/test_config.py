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
