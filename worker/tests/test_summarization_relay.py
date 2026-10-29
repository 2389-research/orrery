"""Summarization relay: config fields + builder (None => use the local relay)."""

import os
from unittest import mock

from orrery_relay import Relay

_ENV = {
    "SUMMARIZATION_BACKEND": "gateway",
    "SUMMARIZATION_GATEWAY_URL": "https://gw.lunaroute.com",
    "SUMMARIZATION_GATEWAY_API_KEY": "k",
}


def _settings(env):
    with mock.patch.dict(os.environ, env, clear=True):
        from src.config import get_settings
        return get_settings()


def test_unset_builds_no_relay():
    from src.summarization_relay import build_summarization_relay
    assert build_summarization_relay(_settings({})) is None


def test_gateway_builds_relay():
    from src.summarization_relay import build_summarization_relay
    # Main backend differs, so this proves the builder reads summarization_backend.
    relay = build_summarization_relay(_settings({**_ENV, "ANTHROPIC_BACKEND": "ollama"}))
    assert isinstance(relay, Relay)
    assert relay._backend == "gateway"
    assert relay._gateway_url == "https://gw.lunaroute.com"
    assert relay._gateway_api_key == "k"


def test_config_load_set_and_unset():
    s = _settings(_ENV)
    assert s.summarization_backend == "gateway"
    assert s.summarization_gateway_url == "https://gw.lunaroute.com"
    assert s.summarization_gateway_api_key == "k"
    s = _settings({})
    assert (s.summarization_backend, s.summarization_gateway_url, s.summarization_gateway_api_key) == ("", "", "")


def test_unknown_backend_falls_back_to_local_with_warning(caplog):
    # A typo'd SUMMARIZATION_BACKEND must not crash summarization jobs: no relay (=> local),
    # plus a warning naming the bad backend.
    import logging
    from src.summarization_relay import build_summarization_relay
    with caplog.at_level(logging.WARNING, logger="summarization_relay"):
        relay = build_summarization_relay(_settings({**_ENV, "SUMMARIZATION_BACKEND": "gatway"}))
    assert relay is None
    assert any(r.name == "summarization_relay" and "gatway" in r.getMessage() for r in caplog.records)
