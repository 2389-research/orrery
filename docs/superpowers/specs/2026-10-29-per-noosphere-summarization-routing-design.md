# Per-noosphere summarization routing (hybrid local/LunaRoute) — Design

**Status:** approved direction (2026-10-29); design for review → plan → build.
**Author:** Nomzor + Claude
**Builds on:** PR #130 (`summarization_model` seam) · [[orrery-llm-backend-scaling]] · [[orrery-horton-transcript-ingest]]

## Intent / Why
Benchmarks (2026-10-29, real Clover transcripts, serial) settled where each LLM role belongs:
- **Extraction → stays LOCAL.** DeepSeek Flash is ~**11× slower** than local gemma4:26b for extraction (tiny output → the LunaRoute network round-trip dwarfs the work).
- **Summarization (per-stretch facets) → LunaRoute DeepSeek Flash.** ~**2.1× faster** than local 26b (long output → generation dominates, remote decode + GPU offload win), and it frees the single Spark GPU.

So the right shape is a **hybrid, per-noosphere**: Clover (live office mics, must stay current during the workday) routes summarization to LunaRoute; code-repo noospheres stay fully local (they run overnight, no latency pressure). One LunaRoute key powers only the noosphere that needs it. The worker already carries each job's workspace (`handle_job(job, db_path)`), workspaces are separate DBs, and `resolve_judge_relay` is precedent for per-role relay selection — so the pieces exist.

## Decision

**1. A dedicated "summarization relay" target (global config).** Add settings for a remote summarization backend so it can point at LunaRoute independently of both the local ollama relay and the existing 2389 gateway:
- `SUMMARIZATION_BACKEND` (default: inherit the main backend → fully local, no behavior change).
- When it's a remote gateway: `SUMMARIZATION_GATEWAY_URL` + `SUMMARIZATION_GATEWAY_API_KEY` (env). For LunaRoute: `https://gw.lunaroute.com` + `LUMAROUTE_API_KEY`. **LunaRoute speaks the Anthropic Messages API, so the existing `gateway` backend works as-is — no new relay backend code.** (Verified: `AsyncAnthropic(base_url="https://gw.lunaroute.com")` + `deepseek-4.1-flash` returns valid responses.)
- `summarization_model` already exists (PR #130).
- Build this summarization relay once at worker startup, alongside the main relay (like the main `Relay.from_settings`). If `SUMMARIZATION_BACKEND` is unset, the summarization relay **is** the main relay (so an unconfigured deploy = today's behavior, all local).

**2. Per-workspace (per-noosphere) override — in the shared registry.** Reuse the existing workspace registry (`<data>/workspaces/registry.json`), exactly as the idle commentary sweep's `commentary_sweep` opt-in does (worker reads it at `generate_commentary.py:289`; orchestrator writes it via `workspace_routes._load_registry/_save_registry`). Add to a workspace's registry entry:
- `summarization_target`: `local` | `remote` (default `local` / absent) — `remote` means "use the summarization relay".
- `summarization_model` (optional per-workspace override of the model name).
No new per-DB table. No secrets in the registry — credentials stay in global env. (The one remote target is defined globally; arbitrary per-workspace backend *profiles* are a future generalization, YAGNI now.)

**3. Resolution at call time.** A helper `resolve_summarization(settings, ws_config, main_relay, summ_relay) -> (relay, model)` returns the relay + model for the job's workspace: workspace override → else global default → else local. The summarization call sites (the ~8 from PR #130 that already read `summarization_model`) call it. **Extraction and classification are untouched — always the local/main relay.**

**4. Management surface.** An orchestrator endpoint to read/set a workspace's summarization override on its registry entry (mirror `commentary.py:_set_sweep_enrollment`), and the ops `register_clover.sh` sets Clover's workspace to `{summarization_target: remote, summarization_model: deepseek-4.1-flash}` after deploy.

## Spark config (ops, after this ships)
Global `.env`: `SUMMARIZATION_BACKEND=gateway`, `SUMMARIZATION_GATEWAY_URL=https://gw.lunaroute.com`, `SUMMARIZATION_GATEWAY_API_KEY=${LUMAROUTE_API_KEY}` (extraction/classification stay `ollama`/`gemma4`). Then set Clover's workspace override to `remote`. Every other noosphere stays local automatically.

## Non-goals (explicitly deferred)
- **Facet-loop parallelization** — the concurrency multiplier (fan out facet batches across LunaRoute lanes with `asyncio.gather`). Separate follow-up PR; this PR delivers the ~2.1× serial win + GPU offload first.
- **Facet id-matching hardening** — the brittle `[leaf_id]` round-trip (match by position/ordinal instead). Low priority (real ids match in prod); separate optional PR.
- No change to extraction/classification routing. No arbitrary per-workspace backend profiles. No per-noosphere routing of roles other than summarization.

## Review focus
1. **Unconfigured default = no behavior change:** `SUMMARIZATION_BACKEND` unset AND no workspace override → summarization runs exactly as today (local, on `summarization_model` which defaults to classification_model). Must not accidentally send anything remote.
2. **Only summarization is routed:** extraction/classification always use the local/main relay regardless of workspace config.
3. **Secrets never land in a workspace DB** — only in global env.
4. **A workspace with `remote` but no reachable summarization relay** (misconfig) — fail safe/visible, don't silently drop facets.
5. **Privacy line (flagged, accepted):** `remote` summarization sends that noosphere's transcript text to LunaRoute. Clover already flows to ikebana via LunaRoute and the user uses it heavily, so this is an accepted, deliberate per-noosphere choice — but it must be opt-in per workspace, never global-by-default.
