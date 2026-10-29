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
    relay = build_summarization_relay(_settings(_ENV))
    assert isinstance(relay, Relay)
    assert relay._backend == "gateway"
    assert relay._gateway_url == "https://gw.lunaroute.com"


def test_config_load_set_and_unset():
    s = _settings(_ENV)
    assert s.summarization_backend == "gateway"
    assert s.summarization_gateway_url == "https://gw.lunaroute.com"
    assert s.summarization_gateway_api_key == "k"
    s = _settings({})
    assert (s.summarization_backend, s.summarization_gateway_url, s.summarization_gateway_api_key) == ("", "", "")
