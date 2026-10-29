# OpenAI-compatible relay backend — Design

**Status:** approved direction (2026-10-29); design → plan → build.
**Author:** Nomzor + Claude
**Builds on:** #131 (per-noosphere summarization routing) · [[orrery-llm-backend-scaling]] · issue #132 (simmer-sdk cleanup, out of scope here)

## Intent / Why
#131 routed Clover's summarization to LunaRoute through the existing `gateway` backend (Anthropic SDK pointed at `gw.lunaroute.com`). In production that **failed**. LunaRoute's Anthropic-format endpoint always runs DeepSeek's thinking step, and it ignores every way of switching it off (`thinking: disabled`, a thinking budget, `reasoning_effort`). On a 10-stretch batch, thinking alone consumed the 8,192-token cap, so the JSON came back truncated and every facet was lost. Clover was rolled back to local.

The same batch through LunaRoute's **OpenAI-format** Chat Completions endpoint with `reasoning_effort: "none"` returned 10 of 10 facets in **11.2s** (2,032 output tokens). Local gemma4:26b on the Spark took 22–35s and was inconsistent (one run returned only 3/10). End-to-end freshness today has a median of 24.5 min but a p90 of 54 min, and unreliable local enrichment is a likely cause of the long tail.

We want this engineered as a general capability, not a LunaRoute hack. Orrery should be able to talk to **any OpenAI-compatible Chat Completions endpoint** (LunaRoute, OpenRouter, vLLM, LM Studio, OpenAI), and an operator should be able to choose that wire format per relay.

## Decision
Add an **`openai`** backend to `orrery-relay`, alongside `gateway`, `bedrock`, and `ollama`.

1. **Transport.** Plain `httpx`, mirroring the native Ollama path, so there's no new dependency. `POST {base_url}/chat/completions` with `Authorization: Bearer <key>`. By OpenAI convention the base URL includes the version segment (LunaRoute: `https://gw.lunaroute.com/v1`). Errors come from `raise_for_status()` as `httpx.HTTPStatusError`, which the existing retry layer already retries on 429/5xx and fails fast on 4xx.
2. **The full relay contract.** `complete` (async), `complete_sync` (codesum/tracksum summarize through this), and `complete_structured` (18 call sites). Each returns `RelayResponse` with `.text`, input/output tokens, latency, and `backend="openai"`, and fires `on_usage`. Nothing outside the relay reads `response.raw` or passes `tools` directly (verified), so this contract is sufficient.
3. **Message translation** (Anthropic-shaped in, OpenAI out):
   - `system=` becomes a leading `{"role":"system"}` message;
   - string content passes through;
   - text blocks become `{"type":"text"}` parts;
   - base64 image blocks become `{"type":"image_url","image_url":{"url":"data:<media_type>;base64,..."}}` (the classifier, extractor, and image jobs send images);
   - `cache_control` markers are dropped (the classifier uses them; this format has no equivalent);
   - Ollama-only kwargs (`ollama_options`, `format`) are ignored.
4. **Structured output (my call).** Send `response_format: {"type":"json_schema","json_schema":{"name":<tool_name>,"strict":false,"schema":<schema>}}` plus a short "return only JSON" nudge, then parse with the existing `_parse_json_from_text` fallback. The parsed dict is returned, or `{}` on failure, matching the other backends' contract. `strict:false` because strict mode requires `additionalProperties:false` and every property required, which our existing schemas don't satisfy. LunaRoute's json_schema support is proven by ikebana.
5. **Reasoning control.** An optional `reasoning_effort` per relay, sent verbatim when configured (`"none"`, `"low"`, `"medium"`, `"high"`, …) and **not sent at all when unset**, because some servers reject the field with a 400. `"none"` is allowed but not forced.
6. **Timeout.** An explicit per-relay timeout (default 120s) for the `openai` path, rather than an SDK default.
7. **Truncation alarm.** If `finish_reason == "length"`, log a WARNING naming the model and `max_tokens`. Today's failure was silent truncation; this would have surfaced it immediately.
8. **Config.** `openai` becomes a valid backend value for both the main relay (`ANTHROPIC_BACKEND`) and the summarization relay (`SUMMARIZATION_BACKEND`). The endpoint and key reuse the existing settings (`GATEWAY_URL`/`GATEWAY_API_KEY` for main, `SUMMARIZATION_GATEWAY_URL`/`_API_KEY` for summarization). New settings: `REASONING_EFFORT` + `RELAY_TIMEOUT_SECONDS` (main) and `SUMMARIZATION_REASONING_EFFORT` + `SUMMARIZATION_TIMEOUT_SECONDS`. `Relay.from_settings` and `Relay.from_env` read them; `build_summarization_relay` passes the summarization pair.

## Spark config (ops, after this ships)
`SUMMARIZATION_BACKEND=openai`, `SUMMARIZATION_GATEWAY_URL=https://gw.lunaroute.com/v1`, `SUMMARIZATION_REASONING_EFFORT=none` (the key is already set). Restart the worker, opt Clover back in (`PUT /workspaces/051e7d2a/summarization {"target":"remote","model":"deepseek-4.1-flash"}`), run one forced `enrich_stretches` job, and confirm zero `no facet` lines.

## Non-goals
- No change to the `gateway`/`bedrock`/`ollama` backends' behavior.
- No streaming, no tool-calling surface beyond structured output, no prompt-caching emulation.
- No simmer-sdk changes: the live simmer paths go through the relay; the abandoned image-spec simmer and dead wiring are tracked in #132.
- No facet-loop parallelization (separate follow-up).

## Review focus
1. Default unchanged: no existing backend's request shape or behavior changes; `openai` is opt-in.
2. `reasoning_effort` is sent only when configured.
3. `complete_structured` on `openai` returns the parsed dict or `{}`, never raises on unparseable output (same contract as the others), and never leaks `cache_control`/Ollama kwargs into the request.
4. Truncation (`finish_reason=length`) is visible in logs.
5. Image blocks translate correctly (data URI with the right media type).
