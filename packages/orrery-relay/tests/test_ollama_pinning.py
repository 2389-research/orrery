# ABOUTME: Ollama reloads the model whenever num_ctx changes between requests, so the
# ABOUTME: relay pins one num_ctx per process and sends keep_alive. Bodies captured via MockTransport.

import json
import logging

import httpx
import pytest

from orrery_relay import Relay
from orrery_relay import relay as relay_mod

MSGS = [{"role": "user", "content": "hello"}]


def _capture(bodies):
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1})
    return httpx.MockTransport(handler)


def _relay(bodies, **kw):
    return Relay(backend="ollama", http_transport=_capture(bodies), **kw)


@pytest.fixture(autouse=True)
def _reset_warned():
    relay_mod._WARNED_NUM_CTX.clear()
    yield
    relay_mod._WARNED_NUM_CTX.clear()


@pytest.mark.asyncio
async def test_pin_raises_smaller_caller_value_and_sends_keep_alive():
    bodies = []
    r = _relay(bodies, ollama_num_ctx=32768, ollama_keep_alive="30m")
    await r.complete("m", MSGS, 100, ollama_options={"num_ctx": 16384})
    assert bodies[0]["options"]["num_ctx"] == 32768
    assert bodies[0]["keep_alive"] == "30m"


@pytest.mark.asyncio
async def test_oversized_caller_value_wins_and_warns_once(caplog):
    bodies = []
    r = _relay(bodies, ollama_num_ctx=32768)
    with caplog.at_level(logging.WARNING, logger="orrery_relay"):
        await r.complete("m", MSGS, 100, ollama_options={"num_ctx": 65536})
        await r.complete("m", MSGS, 100, ollama_options={"num_ctx": 65536})
    assert [b["options"]["num_ctx"] for b in bodies] == [65536, 65536]
    warns = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "OLLAMA_NUM_CTX" in warns[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", [None, 0, ""])
async def test_disabled_body_is_exactly_todays_body(pin):
    bodies = []
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    r = _relay(bodies, ollama_num_ctx=pin, ollama_keep_alive=None)
    await r.complete("m", MSGS, 100, system="sys", temperature=0.2, format=schema)
    assert bodies[0] == {
        "model": "m",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}],
        "stream": False,
        "think": False,
        "options": {"num_predict": 100, "temperature": 0.2},
        "format": schema,
    }


@pytest.mark.asyncio
async def test_image_call_has_no_num_predict_but_has_pin():
    import base64
    bodies = []
    img = base64.b64encode(b"x").decode()
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
        {"type": "text", "text": "what"}]}]
    r = _relay(bodies, ollama_num_ctx=32768, ollama_keep_alive="30m")
    await r.complete("m", msgs, 100)
    assert "num_predict" not in bodies[0]["options"]
    assert bodies[0]["options"]["num_ctx"] == 32768
    assert bodies[0]["keep_alive"] == "30m"


@pytest.mark.asyncio
async def test_sync_and_async_bodies_identical():
    a, s = [], []
    kw = dict(ollama_num_ctx=32768, ollama_keep_alive="30m")
    await _relay(a, **kw).complete("m", MSGS, 50, system="sys", temperature=0.1,
                                   ollama_options={"num_ctx": 8192}, format={"type": "object"})
    _relay(s, **kw).complete_sync("m", MSGS, 50, system="sys", temperature=0.1,
                                  ollama_options={"num_ctx": 8192}, format={"type": "object"})
    assert a == s and a[0]["options"]["num_ctx"] == 32768


@pytest.mark.parametrize("val,expected", [("", None), (0, None), (None, None), ("0", None),
                                          ("16384", 16384), (16384, 16384)])
def test_ctor_num_ctx_normalization(val, expected):
    assert Relay(backend="ollama", ollama_num_ctx=val)._ollama_num_ctx == expected


@pytest.mark.parametrize("bad", ["abc", -1, "-5"])
def test_ctor_num_ctx_rejects_bad(bad):
    with pytest.raises(ValueError, match="ollama_num_ctx"):
        Relay(backend="ollama", ollama_num_ctx=bad)


@pytest.mark.parametrize("val,expected", [("-1", -1), ("3600", 3600), (-1, -1), ("30m", "30m"),
                                          ("", None), ("1.5", 1.5), (0, 0), ("0", 0), (" 30m ", "30m")])
def test_ctor_keep_alive_numeric_forms(val, expected):
    got = Relay(backend="ollama", ollama_keep_alive=val)._ollama_keep_alive
    assert got == expected and type(got) is type(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("ka,expected", [("-1", -1), (3600, 3600), ("0", 0), ("30m", "30m")])
async def test_numeric_keep_alive_is_json_number(ka, expected):
    bodies = []
    await _relay(bodies, ollama_keep_alive=ka).complete("m", MSGS, 10)
    got = bodies[0]["keep_alive"]  # parsed from the wire JSON
    assert got == expected and type(got) is type(expected)


@pytest.mark.asyncio
async def test_pin_applies_and_preserves_other_options():
    bodies = []
    await _relay(bodies, ollama_num_ctx=32768).complete("m", MSGS, 10, ollama_options={"top_k": 5})
    assert bodies[0]["options"]["num_ctx"] == 32768 and bodies[0]["options"]["top_k"] == 5


@pytest.mark.asyncio
async def test_pin_applies_without_any_ollama_options():
    bodies = []
    await _relay(bodies, ollama_num_ctx=32768).complete("m", MSGS, 10)
    assert bodies[0]["options"] == {"num_predict": 10, "num_ctx": 32768}


@pytest.mark.asyncio
async def test_pin_off_leaves_caller_num_ctx_alone():
    bodies = []
    await _relay(bodies).complete("m", MSGS, 10, ollama_options={"num_ctx": 16384})
    assert bodies[0]["options"]["num_ctx"] == 16384 and "keep_alive" not in bodies[0]


@pytest.mark.asyncio
async def test_string_caller_num_ctx_is_coerced():
    bodies = []
    await _relay(bodies, ollama_num_ctx=32768).complete("m", MSGS, 10, ollama_options={"num_ctx": "16384"})
    assert bodies[0]["options"]["num_ctx"] == 32768
    bodies.clear()
    await _relay(bodies, ollama_num_ctx=32768).complete("m", MSGS, 10, ollama_options={"num_ctx": "65536"})
    assert bodies[0]["options"]["num_ctx"] == 65536


def test_ctor_keep_alive_normalization():
    assert Relay(backend="ollama", ollama_keep_alive="  ")._ollama_keep_alive is None
    assert Relay(backend="ollama", ollama_keep_alive=" 30m ")._ollama_keep_alive == "30m"
    assert Relay(backend="ollama")._ollama_keep_alive is None
    assert Relay(backend="ollama")._ollama_num_ctx is None


def test_from_settings_plumbing_and_override():
    class S:
        anthropic_backend = "ollama"
        ollama_num_ctx = 32768
        ollama_keep_alive = "30m"
    r = Relay.from_settings(S())
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (32768, "30m")
    r = Relay.from_settings(S(), ollama_num_ctx=0, ollama_keep_alive="5m")
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (None, "5m")
    class Bare:
        anthropic_backend = "ollama"
    r = Relay.from_settings(Bare())
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (None, None)
    class Empty:
        anthropic_backend = "ollama"
        ollama_num_ctx = ""
        ollama_keep_alive = ""
    r = Relay.from_settings(Empty())  # empty -> default, mapped before the ctor
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (32768, "30m")


def test_from_env_plumbing_and_override(monkeypatch):
    for k in ("OLLAMA_NUM_CTX", "OLLAMA_KEEP_ALIVE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_BACKEND", "ollama")
    r = Relay.from_env()  # unset -> same defaults as Settings
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (32768, "30m")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "")
    r = Relay.from_env()  # empty -> default
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (32768, "30m")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "0")  # 0 disables pinning
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "0")  # legit Ollama value: unload now
    r = Relay.from_env()
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (None, 0)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "-1")
    r = Relay.from_env()
    assert (r._ollama_num_ctx, r._ollama_keep_alive) == (16384, -1)
    r = Relay.from_env(ollama_num_ctx=4096)
    assert r._ollama_num_ctx == 4096


@pytest.mark.parametrize("raw, expected", [
    ("+3600", 3600), ("1e3", 1000), ("2.5", 2.5), ("-1", -1), ("0", 0), (3600.0, 3600),
])
def test_keep_alive_any_numeric_spelling_is_a_number(raw, expected):
    # Ollama 400s on numeric STRINGS; every numeric spelling must go out as a JSON number.
    r = Relay(backend="ollama", ollama_keep_alive=raw)
    assert r._ollama_keep_alive == expected
    assert type(r._ollama_keep_alive) is type(expected)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", float("nan")])
def test_keep_alive_non_finite_rejected(raw):
    with pytest.raises(ValueError, match="finite"):
        Relay(backend="ollama", ollama_keep_alive=raw)


def test_keep_alive_duration_string_unchanged():
    assert Relay(backend="ollama", ollama_keep_alive=" 30m ")._ollama_keep_alive == "30m"
