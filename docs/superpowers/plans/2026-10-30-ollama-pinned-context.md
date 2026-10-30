# Ollama Pinned Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop Ollama model reloads by pinning one `num_ctx` for every client of an Ollama server and sending `keep_alive`, with the Ollama request body built in one shared helper.

**Architecture:**
- A pure `_ollama_request_body(...)` in `orrery_relay/relay.py`, used by `_complete_ollama` and `_complete_ollama_sync`.
- `Relay.__init__` gains `ollama_num_ctx` / `ollama_keep_alive`.
- The Settings and env plumbing passes both through.

**Tech Stack:** Python, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-ollama-pinned-context-design.md`

## Global Constraints
- `options.num_ctx = max(ollama_num_ctx, caller_num_ctx)` when pinning is on.
- The default `ollama_num_ctx` is **32768**. Empty = default; `OLLAMA_NUM_CTX=0` disables pinning, and the caller's value or Ollama's default applies.
- If the caller's value exceeds the pin, it is honoured and a WARNING is logged once per distinct value.
- `keep_alive` comes from `ollama_keep_alive`, default **`"30m"`**. An empty value means the default; numeric values (`-1`, `3600`, `0`) are sent as JSON numbers.
- Env names: `OLLAMA_NUM_CTX`, `OLLAMA_KEEP_ALIVE`. Settings fields: `ollama_num_ctx: int = 32768`, `ollama_keep_alive: str = "30m"`. Add them to BOTH worker and orchestrator config, plus the worker `_ENV_MAP`.
- Existing Ollama request bodies are byte-identical when a relay built with pinning disabled (ctor None/0 and keep_alive None). The image path (no `num_predict`), temperature, and the `format`/structured passthrough are unchanged.
- No change to the gateway/bedrock/openai request shapes.
- Branch: `feat/ollama-pinned-context`, off `main`.

## Review Focus
- With defaults, the classifier call (which passes `num_ctx` 16384) goes out with 32768. → test
- A caller passing `num_ctx` 65536 goes out with 65536 and logs one warning. → test
- `OLLAMA_NUM_CTX=0` produces today's exact body. → test
- An image call has no `num_predict` but does get `num_ctx` and `keep_alive`. → test
- Sync and async bodies are identical for the same inputs. → test

---

### Task 1: Shared Ollama body builder + pinned context + keep-alive + config

**Files:**
- `packages/orrery-relay/src/orrery_relay/relay.py`
- `worker/src/config.py`, `orchestrator/src/config.py`
- `CLAUDE.md`
- Tests: `packages/orrery-relay/tests/test_ollama_pinning.py`, plus the worker/orchestrator config tests

**Interfaces:**
- Produces `_ollama_request_body(model, messages, max_tokens, system, temperature, *, ollama_options, response_format, num_ctx_pin, keep_alive) -> dict`.
- `Relay(..., ollama_num_ctx: int | None = None, ollama_keep_alive: str | None = None)`. In the ctor, `0`/`""` normalize to None.

- [ ] Write failing tests that capture the request through a MockTransport, or the existing Ollama test pattern in `tests/test_ollama_kwargs.py`. Cover each Review Focus line, plus:
  - from_settings/from_env plumbing;
  - config env load in both services.
- [ ] Run; see them fail.
- [ ] Implement:
  - extract `_ollama_request_body` from the two Ollama paths with no behaviour change, then add the pin/keep-alive;
  - the ctor fields;
  - from_settings/from_env;
  - Settings fields;
  - the CLAUDE.md note in the "`num_ctx` is a CAP" section.
- [ ] Run:
  - the relay suite (`PYTHONPATH=src`, key unset): 0 failures;
  - the worker config + classifier-adjacent tests: green;
  - the orchestrator config tests: green.
- [ ] Commit `perf(relay): pin one Ollama num_ctx per process + keep_alive (stops model reloads)`.

## After merge (ops)
Deploy to the Spark (`update-code.md`, with the `/.env*` exclude). The defaults apply with no `.env` change. Re-run the upsert profile on a DB copy: expect ~5–7s per chunk with no 12s calls, and `ollama ps` showing a context of 32768. Then re-measure freshness during a workday.
