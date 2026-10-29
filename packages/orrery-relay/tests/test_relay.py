# ABOUTME: Tests for the Relay class — the main orrery-relay interface.
# ABOUTME: Verifies complete(), complete_sync(), from_env(), usage callbacks.

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_anthropic_message(text="Hello", input_tokens=10, output_tokens=5):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return msg


@pytest.mark.asyncio
async def test_relay_complete_gateway():
    from orrery_relay import Relay
    mock_msg = _mock_anthropic_message("response text", 15, 8)
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    with patch("orrery_relay.relay.create_async_client", return_value=mock_client):
        relay = Relay(backend="gateway", gateway_url="https://gw.test", gateway_api_key="key")
        resp = await relay.complete(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert resp.text == "response text"
    assert resp.input_tokens == 15
    assert resp.output_tokens == 8
    assert resp.model == "claude-sonnet-4-6"
    assert resp.backend == "gateway"
    assert resp.latency_ms >= 0
    mock_client.messages.create.assert_called_once()
    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_relay_complete_bedrock_maps_model():
    from orrery_relay import Relay
    mock_msg = _mock_anthropic_message()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    with patch("orrery_relay.relay.create_async_client", return_value=mock_client):
        relay = Relay(backend="bedrock", aws_access_key="ak", aws_secret_key="sk")
        await relay.complete(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["model"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"


@pytest.mark.asyncio
async def test_relay_complete_passes_system_and_temperature():
    from orrery_relay import Relay
    mock_msg = _mock_anthropic_message()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    with patch("orrery_relay.relay.create_async_client", return_value=mock_client):
        relay = Relay(backend="gateway", gateway_url="https://gw.test", gateway_api_key="key")
        await relay.complete(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
            system="You are helpful.",
            temperature=0.5,
        )
    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["system"] == "You are helpful."
    assert call_kwargs["temperature"] == 0.5


@pytest.mark.asyncio
async def test_relay_complete_fires_usage_callback():
    from orrery_relay import Relay
    mock_msg = _mock_anthropic_message(input_tokens=20, output_tokens=30)
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    usage_events = []
    async def capture_usage(event):
        usage_events.append(event)
    with patch("orrery_relay.relay.create_async_client", return_value=mock_client):
        relay = Relay(
            backend="gateway", gateway_url="https://gw.test", gateway_api_key="key",
            on_usage=capture_usage,
        )
        await relay.complete(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
    assert len(usage_events) == 1
    assert usage_events[0].model == "claude-sonnet-4-6"
    assert usage_events[0].input_tokens == 20
    assert usage_events[0].output_tokens == 30
    assert usage_events[0].backend == "gateway"
    assert usage_events[0].retries == 0


def test_relay_complete_sync():
    from orrery_relay import Relay
    mock_msg = _mock_anthropic_message("sync response")
    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(return_value=mock_msg)
    with patch("orrery_relay.relay.create_sync_client", return_value=mock_client):
        relay = Relay(backend="gateway", gateway_url="https://gw.test", gateway_api_key="key")
        resp = relay.complete_sync(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=50,
        )
    assert resp.text == "sync response"
    assert resp.backend == "gateway"


def test_relay_from_env_gateway(monkeypatch):
    from orrery_relay import Relay
    monkeypatch.setenv("ANTHROPIC_BACKEND", "gateway")
    monkeypatch.setenv("GATEWAY_URL", "https://gw.env.test")
    monkeypatch.setenv("GATEWAY_API_KEY", "env-key")
    with patch("orrery_relay.relay.create_async_client") as mock_create:
        relay = Relay.from_env()
        assert relay._backend == "gateway"
        assert relay._gateway_url == "https://gw.env.test"


def test_relay_from_env_bedrock(monkeypatch):
    from orrery_relay import Relay
    monkeypatch.setenv("ANTHROPIC_BACKEND", "bedrock")
    monkeypatch.setenv("AWS_ACCESS_KEY", "env-ak")
    monkeypatch.setenv("AWS_SECRET_KEY", "env-sk")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    with patch("orrery_relay.relay.create_async_client") as mock_create:
        relay = Relay.from_env()
        assert relay._backend == "bedrock"
        assert relay._aws_region == "us-west-2"


@pytest.mark.asyncio
async def test_relay_complete_passes_kwargs_through():
    from orrery_relay import Relay
    mock_msg = _mock_anthropic_message()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)
    with patch("orrery_relay.relay.create_async_client", return_value=mock_client):
        relay = Relay(backend="gateway", gateway_url="https://gw.test", gateway_api_key="key")
        await relay.complete(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
            tools=[{"name": "my_tool", "description": "does stuff", "input_schema": {}}],
        )
    call_kwargs = mock_client.messages.create.call_args[1]
    assert len(call_kwargs["tools"]) == 1


# --- reasoning_effort / timeout config plumbing ---

class _S:
    anthropic_backend = "openai"
    gateway_url = "https://x/v1"
    gateway_api_key = "k"


def test_from_settings_passes_reasoning_and_timeout():
    from orrery_relay import Relay
    s = _S()
    s.reasoning_effort = "none"
    s.relay_timeout_seconds = 45.0
    r = Relay.from_settings(s)
    assert r._reasoning_effort == "none"
    assert r._timeout == 45.0


def test_from_settings_unset_maps_to_none():
    from orrery_relay import Relay
    s = _S()
    s.reasoning_effort = ""
    s.relay_timeout_seconds = 0.0
    r = Relay.from_settings(s)
    assert r._reasoning_effort is None
    assert r._timeout is None


def test_from_settings_without_new_attrs_still_builds():
    from orrery_relay import Relay
    r = Relay.from_settings(_S())
    assert r._reasoning_effort is None and r._timeout is None


def test_from_settings_overrides_win():
    from orrery_relay import Relay
    s = _S()
    s.reasoning_effort = "none"
    s.relay_timeout_seconds = 45.0
    r = Relay.from_settings(s, reasoning_effort="high", timeout=5.0)
    assert r._reasoning_effort == "high" and r._timeout == 5.0


def test_from_env_reads_reasoning_and_timeout(monkeypatch):
    from orrery_relay import Relay
    monkeypatch.setenv("ANTHROPIC_BACKEND", "openai")
    monkeypatch.setenv("GATEWAY_URL", "https://x/v1")
    monkeypatch.setenv("REASONING_EFFORT", "low")
    monkeypatch.setenv("RELAY_TIMEOUT_SECONDS", "30")
    r = Relay.from_env()
    assert r._reasoning_effort == "low" and r._timeout == 30.0
    assert Relay.from_env(timeout=9.0)._timeout == 9.0


def test_from_env_unset_is_none(monkeypatch):
    from orrery_relay import Relay
    monkeypatch.setenv("ANTHROPIC_BACKEND", "openai")
    monkeypatch.setenv("GATEWAY_URL", "https://x/v1")
    monkeypatch.delenv("REASONING_EFFORT", raising=False)
    monkeypatch.delenv("RELAY_TIMEOUT_SECONDS", raising=False)
    r = Relay.from_env()
    assert r._reasoning_effort is None and r._timeout is None
