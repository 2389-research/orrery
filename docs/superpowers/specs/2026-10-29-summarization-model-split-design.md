# Decouple summarization model from extraction model — Design

**Status:** approved (2026-10-29), not yet built. Next: plan → subagent-driven build → PR.
**Author:** Nomzor + Claude

## Intent / Why
Orrery overloads a single `settings.extraction_model` for THREE jobs: entity extraction, stretch-facet summaries, and meridian session/segment summaries. On the Spark both `EXTRACTION_MODEL` and `CLASSIFICATION_MODEL` are `gemma4:26b`, so entity extraction needlessly runs on the big MoE. Per CLAUDE.md the right tiers are **`gemma4:e4b` (8B dense, structured output) for extraction** and **`gemma4:26b` (MoE, generation) for summarization/classification**. We want extraction on e4b (faster — helps the single-worker throughput budget, ~90s/chunk at peak) while summaries stay on a capable generation model — and, crucially, a **separate knob** so summarization can later point at deepseek-v4.1 via LunaRoute (see [[orrery-llm-backend-scaling]]) independent of extraction.

## Decision
Add a dedicated **`summarization_model`** setting; route all *summarization* call sites to it; leave *entity extraction* on `extraction_model`.

- **Config** (`worker/src/config.py` AND `orchestrator/src/config.py`): add `summarization_model` (dataclass default `""`) + env `SUMMARIZATION_MODEL` (worker needs the `_ENV_MAP` entry; orchestrator resolves via `f.name.upper()`). **Default: `classification_model`** when `SUMMARIZATION_MODEL` is unset/empty — summarization is generation, so the generation/judge model is the right fallback, and this prevents summaries silently landing on the (fast but weaker) extraction model once `EXTRACTION_MODEL=e4b`. **Resolve against the RESOLVED classification value** (`kwargs["summarization_model"] = kwargs["summarization_model"] or kwargs["classification_model"]` after the field loop), NOT the dataclass default — on the Spark `CLASSIFICATION_MODEL=gemma4:26b` is set via env, so binding the dataclass default would wrongly route summaries to `claude-sonnet-4-6`. This is a deliberate behavior change (today summaries use `extraction_model`); document it.
- **Summarization / generation call sites → `settings.summarization_model`** (full enumerated list — a full `grep settings.extraction_model` audit confirmed these are every summary/generation site):
  - `worker/src/jobs/enrich_stretches.py:35` — stretch facets.
  - `worker/src/jobs/ingest_meridian.py:440` (`sum_model`) — meridian segment + rollup.
  - `worker/src/jobs/ingest_ccvault.py:74` (`sum_model`) — ccvault segment + rollup.
  - `worker/src/jobs/ingest_repo.py:168` — codesum one-shot (`make_summarize_fn`).
  - `worker/src/jobs/sync_repo.py:197` — codesum incremental (`make_summarize_fn`).
  - `worker/src/jobs/ingest_tracker_runs.py:201` — tracksum run summaries (`make_summarize_fn`).
  - `worker/src/jobs/generate_commentary.py:255` + `worker/src/main.py:366` — Magos Lex commentary generation (on-demand + idle sweep). Generation, temp 0.7, persona — belongs on the generation model. Included (not deferred); update the `orchestrator/src/routes/commentary.py:19` comment to match.
- **Leave on `extraction_model`:** `upsert_document.extract_document_entities` (entity extraction), `simmer_general` extract/eval, `evaluate_spec`/`evaluate_image_spec`/`extract_batch_image`/`simmer_domain_image` (extraction), `normalization_judge` (own `normalization_judge_model` knob), orchestrator `routes/ingest.py` extraction, `pipeline/search/expansion.py` (deliberately pinned to the fast tier). Anything doing structured entity pulls stays.
- **Docs:** update orrery `CLAUDE.md` "Model Names"/tiers to document `summarization_model` (env `SUMMARIZATION_MODEL`, default = classification model).

## Spark config (ops repo, applied AFTER this ships)
In `orrery-office-display` `spark/.env.example` and the live Spark `.env`:
`EXTRACTION_MODEL=gemma4:e4b` · `SUMMARIZATION_MODEL=gemma4:26b` · `CLASSIFICATION_MODEL=gemma4:26b` (unchanged). Then restart the worker. (Until then the Spark stays on 26b extraction — acceptable while the office is quiet; or apply the 1-line `EXTRACTION_MODEL=e4b` stopgap, accepting summaries temporarily on e4b.)

## Non-goals
- No LunaRoute/deepseek wiring now (this just creates the seam). No change to classification. No per-flow backend selection beyond the model-name knob.

## Review focus
1. The **default** (summaries → classification model when `SUMMARIZATION_MODEL` unset) behaves as intended on a deploy with no new env var — and does NOT move entity extraction.
2. **All** summarization call sites switched (don't miss ccvault/meridian/codesum); entity extraction untouched.
3. Both `config.py` files get the setting + env-map entry (kept consistent).
