# Summarization Model Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a dedicated `summarization_model` setting so entity extraction runs on `extraction_model` (→ gemma4:e4b) while all summarization/generation call sites run on `summarization_model` (→ gemma4:26b, later deepseek via LunaRoute).

**Architecture:** One new config field (mirrored in both config.py files, default = `classification_model`) + re-point every summarization/generation call site from `extraction_model` to `summarization_model`; entity/structured extraction stays on `extraction_model`.

**Tech Stack:** Python (worker + orchestrator), pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-summarization-model-split-design.md`

## Global Constraints
- New field `summarization_model`, env `SUMMARIZATION_MODEL`, in BOTH `worker/src/config.py` and `orchestrator/src/config.py`. Worker also needs the `_ENV_MAP` entry; orchestrator resolves the env name via `f.name.upper()` automatically (no map).
- **Dataclass default MUST be `""`** (empty string), NOT a model name — the `""` is what lets the fallback fire.
- **Default resolution = the RESOLVED classification value, not the dataclass default.** After the field loop, before constructing `Settings`, add:
  `kwargs["summarization_model"] = kwargs["summarization_model"] or kwargs["classification_model"]`
  — in orchestrator before `return Settings(**kwargs)`; in worker before `return _validated(Settings(**kwargs))`. The `or` covers both absent (`""` default) and empty-string env in one shot. Using `getattr(defaults, "classification_model")` would wrongly bind `claude-sonnet-4-6` on the Spark, where `CLASSIFICATION_MODEL=gemma4:26b` is set via env — do NOT do that.
- Entity/structured extraction is NOT changed. Sites that STAY on `extraction_model`: `worker/src/jobs/upsert_document.py:87`, `simmer_general.py:232,596`, `evaluate_spec.py:180`, `extract_batch_image.py:75`, `evaluate_image_spec.py:127`, `simmer_domain_image.py:113`, `normalization_judge.py:64` (own knob), `orchestrator/src/routes/ingest.py:121,154,286`, `orchestrator/src/pipeline/search/expansion.py:34` (deliberately fast tier).
- Build on `main` (the horton/stretch stack is merged). Branch `feat/summarization-model-split`.

## Review Focus
- Unset `SUMMARIZATION_MODEL` with `CLASSIFICATION_MODEL` set to a non-default value → `summarization_model` equals the *resolved* classification value (not `claude-sonnet-4-6`), and entity extraction still uses `extraction_model`. → Task 1 sentinel test.
- Empty-string `SUMMARIZATION_MODEL=""` → falls back to classification_model (not left empty). → Task 1 test.
- Every summarization/generation call site switched; none left on `extraction_model`; extraction sites untouched. → Task 2 source-scan test + enumerated edits.

---

### Task 1: Add `summarization_model` config (both processes)
**Files:** `worker/src/config.py`, `orchestrator/src/config.py`; tests `worker/tests/test_config.py` (create/extend) + `orchestrator/tests/test_config.py` (mirror its existing `mock.patch.dict(os.environ, env, clear=True)` style).
**Interfaces:** Produces `settings.summarization_model`.

- [ ] Write failing tests (one per config file), mirroring the existing env-patch test style:
  - **sentinel default:** set `CLASSIFICATION_MODEL="sentinel-26b"`, leave `SUMMARIZATION_MODEL` unset → assert `get_settings().summarization_model == "sentinel-26b"`. (This case fails if the impl binds the dataclass default instead of the resolved value — the plain "== classification_model" assertion cannot distinguish them.)
  - **empty-string fallback:** `CLASSIFICATION_MODEL="sentinel-26b"`, `SUMMARIZATION_MODEL=""` → assert `== "sentinel-26b"`.
  - **explicit set:** `SUMMARIZATION_MODEL="gemma4:26b"` → assert `== "gemma4:26b"`.
  - **extraction untouched:** `EXTRACTION_MODEL="e4b-x"` → assert `get_settings().extraction_model == "e4b-x"`.
- [ ] Run; see fail.
- [ ] Implement in BOTH files: add `summarization_model: str = ""` to the dataclass; (worker only) add `"summarization_model": "SUMMARIZATION_MODEL"` to `_ENV_MAP`; add the resolution line from Global Constraints after the field loop, at the stated spot in each file.
- [ ] Run tests; pass.
- [ ] Commit: `feat(config): add summarization_model (defaults to classification_model), mirrored`

### Task 2: Route summarization/generation call sites to `summarization_model`
**Files (exact edits — change `settings.extraction_model` → `settings.summarization_model`):**
- `worker/src/jobs/enrich_stretches.py:35` — stretch facets.
- `worker/src/jobs/ingest_meridian.py:440` (`sum_model`) — meridian segment + rollup.
- `worker/src/jobs/ingest_ccvault.py:74` (`sum_model`) — ccvault segment + rollup.
- `worker/src/jobs/ingest_repo.py:168` — codesum one-shot (`make_summarize_fn`).
- `worker/src/jobs/sync_repo.py:197` — codesum incremental (`make_summarize_fn`).
- `worker/src/jobs/ingest_tracker_runs.py:201` — tracksum run summaries (`make_summarize_fn`).
- `worker/src/jobs/generate_commentary.py:255` — `config.get("model") or settings.extraction_model` → `config.get("model") or settings.summarization_model` (Magos Lex generation, persona, temp 0.7).
- `worker/src/main.py:366` — idle commentary sweep `run_commentary_sweep(..., settings.extraction_model, ...)` → `settings.summarization_model`.
- `orchestrator/src/routes/commentary.py:19` — update the comment `# None → worker's extraction_model` → `summarization_model`.
- `CLAUDE.md` (Model Names section) — document `summarization_model` (env `SUMMARIZATION_MODEL`, default = classification model).
**Interfaces:** Consumes `settings.summarization_model` (Task 1).

- [ ] Write failing tests in `worker/tests/`:
  - **source-scan guard (catches a missed site):** for each summarization file above, assert `"settings.extraction_model" not in <file>.read_text()`; and inversely assert `"settings.extraction_model" in upsert_document.py` (extraction stays). Resolve paths relative to the repo root. This is the cheap regression-proof catch for "a site got left behind."
  - **behavioral:** assert `enrich_stretches` passes `settings.summarization_model` to `enrich_batch` (spy/settings assertion, existing harness).
- [ ] Run; see fail.
- [ ] Implement all edits above. Leave every STAY-on-extraction site (Global Constraints) untouched.
- [ ] Run tests, incl. existing enrich_stretches / meridian / ccvault / codesum / tracksum suites — all must stay green.
- [ ] Commit: `feat(models): summaries + commentary use summarization_model; extraction stays on extraction_model`

---

## After merge (ops, separate — orrery-office-display)
Set on the Spark live `.env` + `spark/.env.example`: `EXTRACTION_MODEL=gemma4:e4b`, `SUMMARIZATION_MODEL=gemma4:26b` (CLASSIFICATION_MODEL stays `gemma4:26b`); restart worker. (`spark/.env.example` SOURCE_SCAN_INTERVAL=300 + register_clover cadence 0.0833 already set in the open ops branch #3.)
