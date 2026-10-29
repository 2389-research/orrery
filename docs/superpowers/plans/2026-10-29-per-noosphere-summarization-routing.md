# Per-noosphere Summarization Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let each noosphere (workspace) opt summarization into a remote backend (LunaRoute DeepSeek Flash) while extraction/classification stay local — a hybrid, per-workspace, with a safe local default.

**Architecture:** Add a dedicated *summarization relay* built from new global config (gateway→LunaRoute; LunaRoute speaks Anthropic Messages so the existing `gateway` backend is reused — no new relay backend). Per-workspace opt-in lives on the shared workspace registry (`<data>/workspaces/registry.json`), mirroring the existing `commentary_sweep` flag. A resolver maps a job's `db_path` → workspace registry entry → `(relay, model)`, used only at the summarization call sites; everything else stays on the local relay.

**Tech Stack:** Python (worker + orchestrator), `orrery_relay.Relay`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-per-noosphere-summarization-routing-design.md`

## Global Constraints
- Build on `main` (PR #130 merged — `summarization_model` + its env map exist in both config.py files; the 8 summarization call sites already read `settings.summarization_model`). Branch `feat/per-noosphere-summarization`.
- **Safe default:** with no new env set AND no workspace override, summarization behaves exactly as today (local relay, `settings.summarization_model`). Nothing goes remote implicitly.
- **Only summarization is routed.** Extraction and classification always use the local/main relay, unconditionally.
- **No secrets in the registry.** LunaRoute URL + key live only in global env/config; registry entries carry only `summarization_target` (`local`|`remote`) and optional `summarization_model`.
- Registry path + read pattern: `os.path.join(os.path.dirname(base_db_path), "workspaces", "registry.json")`, read exactly as `worker/src/jobs/generate_commentary.py:289-295` does (unreadable/missing registry ⇒ treat all workspaces as `local`). Workspace id ↔ db_path mapping: reuse the same mapping `commentary_enrolled_dbs` uses (workspace id = the `workspaces/<id>/orrery.db` directory name).
- LunaRoute facts (verified): base `https://gw.lunaroute.com` (Anthropic Messages; SDK appends `/v1/messages`), model `deepseek-4.1-flash`, key env `LUMAROUTE_API_KEY`.

## Review Focus
- `SUMMARIZATION_BACKEND` unset + no workspace override → summarization uses the local relay + `settings.summarization_model` (no remote call). → Task 2 test.
- Workspace entry `summarization_target: remote` but the summarization relay is unavailable (backend unset/misconfigured) → fall back to local **with a visible warning**, never silently drop facets. → Task 2 test.
- Extraction/classification never routed remote regardless of workspace config. → Task 3 test (extraction still uses local relay when workspace is `remote`).
- Corrupt/missing `registry.json` → every workspace resolves `local` (no crash). → Task 2 test.
- Per-workspace `summarization_model` override takes precedence over the global `settings.summarization_model`; absent → global is used. → Task 2 test.

---

### Task 1: Summarization relay — config + builder (worker)
**Files:** `worker/src/config.py` (field + `_ENV_MAP`); Create `worker/src/summarization_relay.py`; Test `worker/tests/test_summarization_relay.py`.
**Interfaces:**
- Produces `settings.summarization_backend`, `settings.summarization_gateway_url`, `settings.summarization_gateway_api_key` (all `str`, default `""`).
- Produces `build_summarization_relay(settings) -> Relay | None` — a `Relay` when `summarization_backend` is set and differs from the main backend, else `None` (meaning "no dedicated relay; use local").

- [ ] Write failing tests in `test_summarization_relay.py`:
  - `summarization_backend=""` → `build_summarization_relay(settings) is None`.
  - `summarization_backend="gateway"`, `summarization_gateway_url="https://gw.lunaroute.com"`, `summarization_gateway_api_key="k"` → returns a `Relay` with `_backend=="gateway"` and `_gateway_url=="https://gw.lunaroute.com"`.
  - config: with those three env vars set, `get_settings()` returns them; unset → `""`.
- [ ] Run; see fail.
- [ ] Implement: add the three fields to `Settings` (default `""`) + `_ENV_MAP` entries (`SUMMARIZATION_BACKEND`, `SUMMARIZATION_GATEWAY_URL`, `SUMMARIZATION_GATEWAY_API_KEY`). Implement `build_summarization_relay`: if `not settings.summarization_backend` → `None`; else `Relay(backend=settings.summarization_backend, gateway_url=settings.summarization_gateway_url, gateway_api_key=settings.summarization_gateway_api_key, ollama_url=settings.ollama_url)`.
- [ ] Run tests; pass.
- [ ] Commit: `feat(config): summarization relay backend config + builder`

### Task 2: Per-workspace resolver (worker)
**Files:** Create `worker/src/summarization_route.py`; Test `worker/tests/test_summarization_route.py`.
**Interfaces:**
- Consumes `build_summarization_relay` (Task 1), `settings.summarization_model`.
- Produces `workspace_summarization_config(base_db_path: str, db_path: str) -> dict` → `{"target": "local"|"remote", "model": str|None}` (defaults `{"target":"local","model":None}` on missing/corrupt registry or missing entry).
- Produces `resolve_summarization(settings, db_path, *, local_relay, summ_relay) -> tuple[Relay, str]` → `(relay, model)`.

- [ ] Write failing tests (use a `tmp_path` registry.json + a sentinel object for each relay):
  - no registry file → config `target=="local"`, and `resolve_summarization` returns `(local_relay, settings.summarization_model)`.
  - registry entry `{"id": <wsid>, "summarization_target": "remote"}`, `summ_relay` given → returns `(summ_relay, settings.summarization_model)`.
  - entry also `"summarization_model": "deepseek-4.1-flash"` → model is `"deepseek-4.1-flash"` (override beats global).
  - entry `remote` but `summ_relay is None` → returns `(local_relay, …)` and emits a warning (assert via `caplog`).
  - corrupt registry.json (invalid JSON) → `target=="local"`, no exception.
- [ ] Run; see fail.
- [ ] Implement: read the registry with the exact path + fail-silent pattern from `generate_commentary.py:289-295`; map `db_path`→workspace id the same way `commentary_enrolled_dbs` does; look up the entry; `resolve_summarization` picks `summ_relay` iff `target=="remote" and summ_relay is not None` (else `local_relay` + warn when remote was requested), and model = entry model or `settings.summarization_model`.
- [ ] Run tests; pass.
- [ ] Commit: `feat(worker): per-workspace summarization resolver (registry-backed)`

### Task 3: Wire the per-job summarization call sites to the resolver (worker)
**Files:** Modify `worker/src/jobs/enrich_stretches.py`, `ingest_meridian.py`, `ingest_ccvault.py`, `ingest_repo.py`, `sync_repo.py`, `ingest_tracker_runs.py`, `generate_commentary.py` (`run_generate_commentary`). Test `worker/tests/test_summarization_routing_wiring.py`.
**Interfaces:** Consumes `build_summarization_relay`, `resolve_summarization` (Tasks 1–2). Each job already has `db_path`.
**Same-shape change across 7 files — read [references/batching.md] before dispatching.** At each site: build `summ_relay = build_summarization_relay(settings)` once, then replace the summarization call's `(Relay.from_settings-local, settings.summarization_model)` with `(relay_s, model_s) = resolve_summarization(settings, db_path, local_relay=<existing local relay>, summ_relay=summ_relay)` and use `relay_s`/`model_s` for the **summary** call only. Leave extraction/classification on the existing local relay.

- [ ] Write failing test: drive one representative job (`enrich_stretches`) with a `remote` workspace + a spy `summ_relay`; assert the facet/summary call used `summ_relay`, and that the extraction/entity path (if any in that job) used the local relay. Add an assertion that with a `local` workspace the summary call uses the local relay.
- [ ] Run; see fail.
- [ ] Implement across the 7 files. (`generate_commentary.run_generate_commentary` keeps its `config.get("model") or …` precedence — pass the resolved model as the fallback.)
- [ ] Run worker suite (incl. existing enrich/meridian/ccvault/codesum/tracksum suites — must stay green).
- [ ] Commit: `feat(worker): route per-job summarization via per-workspace resolver`

### Task 4: Idle commentary sweep — per-workspace resolution (worker)
**Files:** Modify `worker/src/main.py` (the `run_commentary_sweep(..., settings.summarization_model)` call ~line 366) and `worker/src/jobs/generate_commentary.py` (`run_commentary_sweep`). Test: extend `worker/tests/test_summarization_routing_wiring.py`.
**Interfaces:** Consumes Tasks 1–2. The sweep iterates `db_paths`, so resolution is per-db inside the loop.
- [ ] Write failing test: sweep over two workspaces (one `local`, one `remote`) routes each workspace's commentary generation to the correct relay.
- [ ] Run; see fail.
- [ ] Implement: pass the summarization relay (or a resolver callable) into `run_commentary_sweep`; resolve `(relay, model)` per `db_path` inside its loop instead of using the single passed-in `model`.
- [ ] Run tests; pass.
- [ ] Commit: `feat(worker): per-workspace summarization in the idle commentary sweep`

### Task 5: Orchestrator endpoint to set a workspace's summarization override
**Files:** Modify `orchestrator/src/routes/workspace_routes.py` (add route + a `_set_summarization` helper mirroring `commentary.py:_set_sweep_enrollment`); `orchestrator/src/models.py` (request model). Test `orchestrator/tests/test_summarization_route.py`.
**Interfaces:** Writes `summarization_target` + `summarization_model` onto a workspace's registry entry via `_load_registry`/`_save_registry`.
- [ ] Write failing tests: `PUT /workspaces/{id}/summarization` with `{"target":"remote","model":"deepseek-4.1-flash"}` updates that entry (verify via `_load_registry`); `GET` (or the workspaces list) reflects it; invalid `target` → 400; unknown id → 404.
- [ ] Run; see fail.
- [ ] Implement the route + helper (validate `target ∈ {local,remote}`; `model` optional string).
- [ ] Run orchestrator suite; pass.
- [ ] Commit: `feat(api): set per-workspace summarization target/model on the registry`

---

## After merge (ops, separate — orrery-office-display)
- Global Spark `.env`: `SUMMARIZATION_BACKEND=gateway`, `SUMMARIZATION_GATEWAY_URL=https://gw.lunaroute.com`, `SUMMARIZATION_GATEWAY_API_KEY=${LUMAROUTE_API_KEY}` (extraction/classification stay `ollama`/`gemma4`); `spark/.env.example` too.
- Update `seed/register_clover.sh` to set Clover's workspace `summarization_target=remote`, `summarization_model=deepseek-4.1-flash` (call the new endpoint after registration). Every other noosphere stays `local` automatically.
- Deferred (separate PRs, per design non-goals): facet-loop parallelization (asyncio.gather across lanes); facet id-matching hardening (positional/ordinal).
