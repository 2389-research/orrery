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


@pytest.mark.asyncio
async def test_cache_control_on_image_and_message_level_absent():
    seen, handler = _capture()
    msgs = [{"role": "user", "cache_control": {"type": "ephemeral"}, "content": [
        {"type": "image", "cache_control": {"type": "ephemeral"},
         "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}},
    ]}]
    await _relay(handler).complete("m", msgs, 5)
    assert "cache_control" not in seen[0].content.decode()


@pytest.mark.asyncio
async def test_503_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=_ok("up"))

    resp = await _relay(handler).complete("m", MSGS, 5)
    assert len(calls) == 2 and resp.text == "up"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"error": {"message": "quota"}}])
async def test_malformed_200_raises(payload):
    seen, handler = _capture(payload)
    with pytest.raises(ValueError, match="my-model"):
        await _relay(handler).complete("my-model", MSGS, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,expected", [(None, 120.0), (7.0, 7.0), (0, 120.0)])
async def test_timeout_reaches_httpx(timeout, expected):
    seen, handler = _capture()
    await _relay(handler, timeout=timeout).complete("m", MSGS, 5)
    assert seen[0].extensions["timeout"]["read"] == expected


def test_dropped_block_warns_and_empty_message_raises(caplog):
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "keep"}, {"type": "tool_result", "content": "x"}]}]
    with caplog.at_level(logging.WARNING, logger="orrery_relay"):
        out = _anthropic_to_openai_messages(msgs, None)
    assert out[0]["content"] == [{"type": "text", "text": "keep"}]
    assert any("tool_result" in r.getMessage() for r in caplog.records)
    with pytest.raises(ValueError, match="dropped"):
        _anthropic_to_openai_messages(
            [{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}], None)


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


# ---- Task 2: sync + structured -------------------------------------------

def test_sync_request_shape_and_mapping():
    seen, handler = _capture(_ok("sync hi", 3, 4))
    r = _relay(handler).complete_sync("some/model", MSGS, 50, system="sys", temperature=0.2)
    req = seen[0]
    assert str(req.url) == "https://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["max_tokens"] == 50 and body["temperature"] == 0.2
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert (r.text, r.input_tokens, r.output_tokens, r.backend) == ("sync hi", 3, 4, "openai")


def test_sync_retries_429_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow"})
        return httpx.Response(200, json=_ok("ok"))

    assert _relay(handler).complete_sync("m", MSGS, 5).text == "ok"
    assert len(calls) == 2


def test_sync_malformed_200_raises():
    _, handler = _capture({"choices": []})
    with pytest.raises(ValueError):
        _relay(handler).complete_sync("m", MSGS, 5)


SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


async def _structured(content, msgs=MSGS):
    seen, handler = _capture(_ok(content))
    out = await _relay(handler).complete_structured("m", msgs, 50, SCHEMA, tool_name="my_tool")
    return out, json.loads(seen[0].content)


@pytest.mark.asyncio
async def test_structured_sends_response_format():
    _, body = await _structured('{"a": 1}')
    assert body["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "my_tool", "strict": False, "schema": SCHEMA}}
    assert "tools" not in body


@pytest.mark.asyncio
async def test_structured_nudge_str_and_list():
    _, body = await _structured('{"a": 1}')
    assert body["messages"][-1]["content"].startswith("Hi\n\nReturn ONLY a JSON value")
    _, body = await _structured('{"a": 1}', [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}])
    assert "Return ONLY a JSON value" in json.dumps(body["messages"][-1]["content"])


@pytest.mark.asyncio
async def test_structured_parses_plain_fenced_and_garbage():
    assert (await _structured('{"a": 1}'))[0] == {"a": 1}
    assert (await _structured('```json\n{"a": 2}\n```'))[0] == {"a": 2}
    assert (await _structured("not json at all"))[0] == {}


@pytest.mark.asyncio
async def test_complete_with_tools_raises():
    _, handler = _capture()
    with pytest.raises(NotImplementedError):
        await _relay(handler).complete("m", MSGS, 5, tools=[{"name": "x"}])


# ---- Task 2 fix round ------------------------------------------------------

@pytest.mark.asyncio
async def test_structured_list_json_returns_empty():
    assert (await _structured("[1]"))[0] == {}


@pytest.mark.asyncio
async def test_structured_null_content_returns_empty():
    seen, handler = _capture({"choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}], "usage": {}})
    assert await _relay(handler).complete_structured("m", MSGS, 5, SCHEMA) == {}


@pytest.mark.asyncio
async def test_structured_4xx_raises():
    _, handler = _capture({"error": "bad"}, status=400)
    with pytest.raises(httpx.HTTPStatusError):
        await _relay(handler).complete_structured("m", MSGS, 5, SCHEMA)


@pytest.mark.asyncio
async def test_structured_caller_response_format_ignored():
    seen, handler = _capture(_ok('{"a": 1}'))
    out = await _relay(handler).complete_structured("m", MSGS, 5, SCHEMA, response_format={"type": "text"})
    assert out == {"a": 1}
    assert json.loads(seen[0].content)["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_complete_with_tool_choice_raises():
    _, handler = _capture()
    with pytest.raises(NotImplementedError):
        await _relay(handler).complete("m", MSGS, 5, tool_choice={"type": "tool", "name": "x"})


def test_json_nudge_does_not_mutate_input():
    from orrery_relay.relay import _with_json_nudge
    s_msgs = [{"role": "user", "content": "Hi"}]
    l_msgs = [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]
    _with_json_nudge(s_msgs)
    _with_json_nudge(l_msgs)
    assert s_msgs == [{"role": "user", "content": "Hi"}]
    assert l_msgs == [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]


def test_sync_truncation_warning_names_max_tokens(caplog):
    _, handler = _capture(_ok("cut", finish="length"))
    with caplog.at_level(logging.WARNING):
        _relay(handler).complete_sync("m", MSGS, 77)
    assert any("truncated" in r.message and "77" in r.message for r in caplog.records)
