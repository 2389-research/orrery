# ABOUTME: Tests for the openai-compatible Chat Completions backend (async path).
# ABOUTME: Uses httpx.MockTransport to capture the outgoing request; no network.

import json
import logging

import httpx
import pytest

from orrery_relay import Relay
from orrery_relay.relay import _anthropic_to_openai_messages

BASE = "https://llm.test/v1"


def _ok(content="hello", prompt=11, completion=7, finish="stop"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _relay(handler, **kw):
    return Relay(
        backend="openai", gateway_url=kw.pop("gateway_url", BASE), gateway_api_key="sk-test",
        http_transport=httpx.MockTransport(handler), base_delay=0.001, max_delay=0.002, **kw,
    )


def _capture(payload=None, status=200):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else _ok())

    return seen, handler


MSGS = [{"role": "user", "content": "Hi"}]


@pytest.mark.asyncio
async def test_url_auth_and_basic_body():
    seen, handler = _capture()
    await _relay(handler).complete("some/model", MSGS, 123)
    req = seen[0]
    assert str(req.url) == "https://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "some/model"  # passes through unmapped
    assert body["max_tokens"] == 123
    assert body["messages"] == [{"role": "user", "content": "Hi"}]
    assert "temperature" not in body


@pytest.mark.asyncio
async def test_trailing_slash_base_url():
    seen, handler = _capture()
    await _relay(handler, gateway_url=BASE + "/").complete("m", MSGS, 5)
    assert str(seen[0].url) == "https://llm.test/v1/chat/completions"


@pytest.mark.asyncio
async def test_system_prepended_and_temperature():
    seen, handler = _capture()
    await _relay(handler).complete("m", MSGS, 5, system="Be terse", temperature=0.2)
    body = json.loads(seen[0].content)
    assert body["messages"][0] == {"role": "system", "content": "Be terse"}
    assert body["messages"][1]["role"] == "user"
    assert body["temperature"] == 0.2


@pytest.mark.asyncio
async def test_reasoning_effort_absent_then_present():
    seen, handler = _capture()
    await _relay(handler).complete("m", MSGS, 5)
    assert "reasoning_effort" not in json.loads(seen[0].content)

    seen2, handler2 = _capture()
    await _relay(handler2, reasoning_effort="none").complete("m", MSGS, 5)
    assert json.loads(seen2[0].content)["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_image_block_becomes_data_uri_and_no_cache_control():
    seen, handler = _capture()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what is this", "cache_control": {"type": "ephemeral"}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
    ]}]
    await _relay(handler).complete("m", msgs, 5)
    raw = seen[0].content.decode()
    assert "cache_control" not in raw
    parts = json.loads(raw)["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}


@pytest.mark.asyncio
async def test_ollama_only_kwargs_never_sent():
    seen, handler = _capture()
    await _relay(handler).complete("m", MSGS, 5, ollama_options={"num_ctx": 9}, format={"type": "object"})
    body = json.loads(seen[0].content)
    assert "ollama_options" not in body and "format" not in body and "options" not in body


@pytest.mark.asyncio
async def test_response_mapping_and_usage_event():
    events = []

    async def on_usage(e):
        events.append(e)

    seen, handler = _capture(_ok("the answer", 21, 9))
    relay = _relay(handler, on_usage=on_usage)
    resp = await relay.complete("m", MSGS, 5)
    assert resp.text == "the answer"
    assert (resp.input_tokens, resp.output_tokens) == (21, 9)
    assert resp.backend == "openai" and resp.model == "m"
    assert events[0].backend == "openai" and events[0].input_tokens == 21


@pytest.mark.asyncio
async def test_null_content_and_missing_usage_default():
    seen, handler = _capture({"choices": [{"message": {"content": None}, "finish_reason": "stop"}]})
    resp = await _relay(handler).complete("m", MSGS, 5)
    assert resp.text == ""
    assert (resp.input_tokens, resp.output_tokens) == (0, 0)


@pytest.mark.asyncio
async def test_length_finish_reason_warns(caplog):
    seen, handler = _capture(_ok(finish="length"))
    with caplog.at_level(logging.WARNING, logger="orrery_relay"):
        await _relay(handler).complete("big-model", MSGS, 77)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("big-model" in r.getMessage() and "77" in r.getMessage() for r in warns)


@pytest.mark.asyncio
async def test_429_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=_ok("recovered"))

    resp = await _relay(handler).complete("m", MSGS, 5)
    assert len(calls) == 2 and resp.text == "recovered"


@pytest.mark.asyncio
async def test_400_fails_fast():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(httpx.HTTPStatusError):
        await _relay(handler).complete("m", MSGS, 5)
    assert len(calls) == 1


def test_no_anthropic_client_built_for_openai():
    relay = Relay(backend="openai", gateway_url=BASE, gateway_api_key="k")
    assert not hasattr(relay, "_async_client")


# --- pure helper tests ---

def test_translate_string_content_and_system():
    out = _anthropic_to_openai_messages([{"role": "user", "content": "x"}], "sys")
    assert out == [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}]


def test_translate_no_system():
    assert _anthropic_to_openai_messages([{"role": "assistant", "content": "y"}], None) == [
        {"role": "assistant", "content": "y"}]


def test_translate_list_content_drops_unknown_and_non_base64():
    out = _anthropic_to_openai_messages([{"role": "user", "content": [
        {"type": "text", "text": "t"},
        {"type": "image", "source": {"type": "url", "url": "http://x"}},
        {"type": "tool_use", "id": "1"},
    ]}], None)
    assert out == [{"role": "user", "content": [{"type": "text", "text": "t"}]}]


def test_translate_does_not_mutate_input():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "t", "cache_control": {"type": "ephemeral"}}]}]
    _anthropic_to_openai_messages(msgs, None)
    assert "cache_control" in msgs[0]["content"][0]
