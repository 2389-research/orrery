# Pin one Ollama context size + keep the model warm — Design

**Status:** approved (2026-10-30). **Author:** Nomzor + Claude. **Related:** #133, [[orrery-llm-backend-scaling]].

## Why
Profiling real Clover ingest on the Spark (the actual `upsert_document`, run on a copy of the workspace DB) showed **~28s per chunk, almost all of it Ollama reloading the model**. Measured:
- classify: 13.5s for 3.3k tokens in / 45 out;
- extract: **12.7s for 189 tokens in / 11 out**;
- a back-to-back second extract: 1.4s.

The cause is that orrery's Ollama calls ask for **different context sizes**:
- the classifier pins `num_ctx` 16384;
- tracksum pins 8192;
- everything else (extraction, local facets, codesum) sends nothing.

On current Ollama, sending nothing means a hardware-dependent default, which is the model maximum (262144) on the Spark's 128GB. Ollama sizes the KV cache at load, so **any change in `num_ctx` forces a full model reload**. That's documented community behaviour (see the "pin your context window" write-ups) and our measurements confirm it. Separately, Ollama unloads an idle model after **5 minutes** by default, which is the same as the source-scan cadence.

Forcing every call to one context size, in an experiment on the same DB copy, cut **27.1s → 5.0s** (252-char chunk) and **31.0s → 7.1s** (2,064 chars). Extraction dropped to 1.8–2.5s. That's roughly a **4–5× increase in local-model ingest capacity** for every local job, not just Clover.

## Decision
The relay owns **one Ollama context size per process** and a **keep-alive**:
1. **`ollama_num_ctx`** (env `OLLAMA_NUM_CTX`, default **32768**). On every Ollama request (async and sync), `options.num_ctx = max(ollama_num_ctx, caller's num_ctx)`. Callers' existing pins (classifier 16384, tracksum 8192) remain as *floors*, but every call lands on the pinned value, so nothing reloads. If a caller asks for MORE than the pinned value, honour it (never truncate below what a caller says it needs) and log a WARNING once per distinct value. That case means a reload, and the operator should raise `OLLAMA_NUM_CTX`. `0`/empty disables pinning, which keeps today's behaviour (caller's value or Ollama's default).
   - **Why 32768:** it is above every capped prompt orrery builds:
     - codesum: ≤16k chars per file, ≤24k chars at the root;
     - session segments: ≤8k chars;
     - extraction: 2k-char slices;
     - classify: ~4–5k tokens;
     - facet batches: a few thousand tokens.

     So moving the uncapped calls off 262k can't truncate anything, and it leaves headroom. That matters because Ollama truncates silently from the LEFT (see CLAUDE.md).
2. **`ollama_keep_alive`** (env `OLLAMA_KEEP_ALIVE`, default **`"30m"`**). It is sent as `keep_alive` on every Ollama request, so the model survives the 5-minute scan gaps. Empty means don't send it (Ollama's default applies).
3. **Shared Ollama request building.** The async and sync Ollama paths currently duplicate body construction. Extract one pure `_ollama_request_body(...)` used by both, so the new logic lives in one place. This is the same principle as the openai helpers in #133. The existing Ollama behaviour must stay byte-identical apart from the two new fields.
4. **Config:** the worker and orchestrator `Settings` both gain these fields. `Relay.from_settings` / `from_env` pass them through. CLAUDE.md's "`num_ctx` is a CAP" section gets a short note: the relay pins one value per process because a changing `num_ctx` forces an Ollama reload; raise `OLLAMA_NUM_CTX` rather than pinning a larger per-call value.

## Non-goals
- No change to the gateway/bedrock/openai backends.
- No change to which model handles which role.
- No Modelfile or server-side changes. A server-wide `OLLAMA_CONTEXT_LENGTH` wouldn't be enough anyway, because a per-request `num_ctx` overrides it.

## Review focus
1. With defaults, every Ollama request carries `num_ctx: 32768` and `keep_alive: "30m"`, including classifier and tracksum calls, which previously sent 16384 and 8192.
2. A caller asking for more than the pinned value gets its larger value (no truncation) and a one-time warning.
3. `OLLAMA_NUM_CTX=0` reproduces today's request bodies exactly.
4. The image path (no `num_predict`) and `format`/structured passthrough are unchanged.
5. No other backend's request changes.
