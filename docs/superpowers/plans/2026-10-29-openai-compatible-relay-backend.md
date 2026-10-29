# OpenAI-compatible Relay Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add an opt-in `openai` backend to `orrery-relay` (any OpenAI-compatible Chat Completions endpoint), selectable for both the main and the summarization relay, with configurable reasoning effort and timeout.

**Architecture:** A raw-`httpx` path in `Relay`, parallel to the native Ollama path. It translates Anthropic-shaped messages to OpenAI ones, posts to `{base_url}/chat/completions`, and maps the response to `RelayResponse`. `complete_structured` uses `response_format: json_schema` (non-strict) plus the text-parse fallback. Config plumbs `reasoning_effort` and `timeout` into `Relay` via settings and env.

**Tech Stack:** Python, httpx, pytest (`httpx.MockTransport` for HTTP fakes).

**Spec:** `docs/superpowers/specs/2026-10-29-openai-compatible-relay-backend-design.md`

## Global Constraints
- Backend value `"openai"`. Request: `POST {base_url}/chat/completions`, header `Authorization: Bearer <api_key>`, JSON body. The base URL includes the version segment (e.g. `https://gw.lunaroute.com/v1`); strip one trailing `/` before joining.
- `reasoning_effort` is included in the body **only when not None**; it is sent verbatim.
- Default timeout for the `openai` path is **120s** when unset.
- `finish_reason == "length"` → `logger.warning` naming the model and `max_tokens`.
- `complete_structured` (openai): `response_format = {"type": "json_schema", "json_schema": {"name": tool_name, "strict": False, "schema": schema}}`; parse with the existing `_parse_json_from_text`; return the dict or `{}`, never raise on bad output.
- Message translation: `system=` becomes a leading system message; text blocks become `{"type":"text","text":…}`; base64 image blocks become `{"type":"image_url","image_url":{"url":f"data:{media_type};base64,{data}"}}`; drop `cache_control`; ignore `ollama_options`/`format`.
- No behavior change to the `gateway`/`bedrock`/`ollama` backends. No new package dependency.
- Branch `feat/openai-compatible-backend` off `main`.

## Review Focus
- An existing backend's request is unchanged (the existing relay tests stay green and none were rewritten to accommodate the change). → all tasks
- `reasoning_effort=None` → the key is absent from the request body. → Task 1 test
- Structured output with unparseable text → `{}`, no raise. → Task 2 test
- `finish_reason="length"` → a WARNING is logged. → Task 1 test
- An image block → a correct data URI part; `cache_control` absent from the outgoing body. → Task 1 test

---

### Task 1: `openai` backend — async `complete` + message translation
**Files:** `packages/orrery-relay/src/orrery_relay/relay.py` (+ `backends.py` if a helper belongs there); test `packages/orrery-relay/tests/test_openai_backend.py`.
**Interfaces (produces):**
- `Relay.__init__(…, reasoning_effort: str | None = None, timeout: float | None = None, http_transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None)`. For `backend == "openai"`, do NOT build an Anthropic client (like ollama). `http_transport` is a test seam passed through to the `httpx` clients.
- `_anthropic_to_openai_messages(messages, system) -> list[dict]`.
- `Relay._complete_openai(model, messages, max_tokens, system, temperature, **kwargs) -> RelayResponse`, dispatched from `complete()` when `backend == "openai"`.

- [ ] Failing tests (MockTransport capturing the request):
  - (a) the request goes to `{base}/chat/completions` with a Bearer header and body `model`/`messages`/`max_tokens`;
  - (b) a system message is prepended;
  - (c) `reasoning_effort` is absent when None and present verbatim when `"none"`;
  - (d) an image block becomes a data-URI `image_url` part, and `cache_control` appears nowhere in the body;
  - (e) `.text` comes from `choices[0].message.content`, tokens come from `usage.prompt_tokens`/`completion_tokens`, and `backend=="openai"`;
  - (f) `finish_reason:"length"` logs a WARNING (caplog);
  - (g) a 429 is retried and then succeeds (the retry layer).
- [ ] Implement. Run `cd packages/orrery-relay && python -m pytest tests/ -q`, which must be all green including the existing tests.
- [ ] Commit `feat(relay): openai-compatible backend — async complete + message translation`.

### Task 2: `openai` — `complete_sync` + `complete_structured`
**Files:** `relay.py`; extend `tests/test_openai_backend.py`.
**Interfaces:** Consumes Task 1's translation + transport seam. `complete_sync` dispatches to `_complete_openai_sync` (an `httpx.Client` twin). `complete_structured` (openai) sends `response_format` per the Global Constraints and returns the parsed dict or `{}`.
- [ ] Failing tests:
  - sync request shape and response mapping (mirror Task 1's a/e);
  - structured: the body carries `response_format.json_schema.{name, strict: False, schema}`;
  - valid JSON content returns the dict;
  - fenced JSON parses;
  - garbage returns `{}` without raising.
- [ ] Implement; run the relay suite green.
- [ ] Commit `feat(relay): openai backend sync + structured output`.

### Task 3: Config plumbing — main + summarization relays
**Files:**
- `packages/orrery-relay/src/orrery_relay/relay.py` (`from_settings` / `from_env` read the new knobs);
- `worker/src/config.py` + `orchestrator/src/config.py` (Settings fields + worker `_ENV_MAP`);
- `worker/src/summarization_relay.py`;
- `CLAUDE.md` (backend list/table + the LunaRoute note);
- tests: `packages/orrery-relay/tests/` + `worker/tests/test_summarization_relay.py` + config tests.

**Interfaces:**
- Settings: main `reasoning_effort: str = ""` (env `REASONING_EFFORT`), `relay_timeout_seconds: float = 0.0` (env `RELAY_TIMEOUT_SECONDS`).
- Worker only: `summarization_reasoning_effort: str = ""` (env `SUMMARIZATION_REASONING_EFFORT`), `summarization_timeout_seconds: float = 0.0` (env `SUMMARIZATION_TIMEOUT_SECONDS`).
- Empty/0 means "unset", which maps to `reasoning_effort=None` / `timeout=None` on the Relay.

- [ ] Failing tests:
  - `Relay.from_settings` passes `reasoning_effort`/`timeout` through (empty → None);
  - `Relay.from_env` reads `REASONING_EFFORT` / `RELAY_TIMEOUT_SECONDS`;
  - `build_summarization_relay` with `SUMMARIZATION_BACKEND=openai`, a URL, a key, `SUMMARIZATION_REASONING_EFFORT=none` builds a Relay with `_backend=="openai"` and `reasoning_effort=="none"`;
  - config env load for each new field.
- [ ] Implement; update CLAUDE.md: add `openai` to the backend list, and note that LunaRoute's Anthropic-format endpoint can't disable reasoning, so use `openai` + `reasoning_effort` for reasoning models.
- [ ] Run the relay + worker (targeted) + orchestrator config suites green.
- [ ] Commit `feat(config): select openai backend + reasoning/timeout for main and summarization relays`.

### Task 4: Live LunaRoute smoke test
**Files:** `packages/orrery-relay/tests/test_openai_live.py`.
- [ ] A test marked/skipped unless `LUMAROUTE_API_KEY` (or `LUNAROUTE_API_KEY`) is set. It builds `Relay(backend="openai", gateway_url="https://gw.lunaroute.com/v1", gateway_api_key=key, reasoning_effort="none")` and checks:
  - `complete` returns non-empty text for a tiny prompt;
  - `complete_structured` with a small schema returns a dict containing the required key;
  - `complete_sync` returns non-empty text.
- [ ] Run it locally with the key; it must pass. Without the key it must skip.
- [ ] Commit `test(relay): live LunaRoute smoke test for the openai backend (skipped without key)`.

---

## After merge (ops)
Spark `.env`: change `SUMMARIZATION_BACKEND=gateway` to `openai`, set `SUMMARIZATION_GATEWAY_URL=https://gw.lunaroute.com/v1`, add `SUMMARIZATION_REASONING_EFFORT=none`. Deploy per `update-code.md`, then opt Clover back in, run a forced 5-doc `enrich_stretches`, confirm zero `no facet` lines, and re-measure freshness.
