# Transcript Stretch Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split each Horton transcript document into deterministic stretches and LLM-enrich each into a structured facet (summary / concepts / ideas / nouns), stored in a `stretches` table and read via an event-time `GET /stretches` endpoint.

**Architecture:** Mirror the meridian pattern — deterministic split (`stretch_splitter`) → batched per-stretch LLM facet (`stretch_featurizer`, gemma4 via the relay) → idempotent upsert into a `stretches` table, driven by an `enrich_stretches` worker job (scoped backfill + enrich-on-arrival). A thin POST enqueues it; a GET + MCP tool reads stretches by event-time window. Facets are a parallel layer and never touch co-occurrence/entities.

**Tech Stack:** Python (worker + FastAPI orchestrator), SQLite/WAL, `orrery-relay` (ollama/gemma4), pytest with `tmp_path` file-backed DBs.

**Spec:** `docs/superpowers/specs/2026-10-28-transcript-stretch-enrichment-design.md` (facet contract source of truth: `DS-scratch/ikebana/docs/superpowers/notes/2026-10-28-orrery-enrichment-issue.md`)

## Global Constraints

- Build on the current branch tip `feat/kind-scoped-entity-connection` (carries the Horton work / PR #128; stretch enrichment consumes Horton transcript documents).
- **Mirror rule:** any new table goes into `worker/src/db.py` AND `orchestrator/src/db.py` identically (enforced by `orchestrator/tests/test_schema_mirror.py`, run natively).
- Model: `settings.extraction_model` (gemma4:e4b) through `orrery-relay`; ollama path uses `think:false` + JSON (reuse the `extract_document_entities` relay pattern in `worker/src/jobs/upsert_document.py`).
- Facets **never** mutate entities or co-occurrence — enrichment writes only the `stretches` table.
- Split rules (verbatim from spec §4): break at pause **> 2 min**; cap **~4 min or ~120 words**; **drop < ~15 content words**.
- Facet fields (stored): `summary` (2–4 sentences, exactly `"none"` for chatter → empty lists); `concepts` 1–3 × `{tag, statement}`; `ideas` 0–3 × `{kind, text, quote, quote_ok}`, kind ∈ `proposal|question|decision|problem`, quote verbatim ≤ 20 words; `nouns` list. No people names in summary/concepts/ideas.
- Prompt reconciliation (code normalizes): prompt emits `topics` → store as `concepts`; prompt `leaf_id` = our `stretch_id`; `quote_ok` computed by code (verbatim substring, whitespace/case-normalized).
- Batch ~10 stretches/model call; one strict-JSON `{"facets":[...]}` per batch; **one retry** for skipped/malformed; still-missing → recorded failed, never silently dropped, job not aborted.

## Review Focus

- **Split boundaries & degenerate docs** — >2min pause splits, ≤4min/120-word cap, <15-word drop, and an empty/one-line doc yields zero stretches without error. → Task 2 tests.
- **Idempotent re-enrich** — a stretch whose text hash is unchanged performs no model call on a second pass; a revised doc re-enriches only changed stretches. → Task 4 tests.
- **`quote_ok` correctness** — a verbatim quote → true; a paraphrased/hallucinated quote → false. → Task 3 tests.
- **Malformed/skipped batch isolation** — a batch that drops or malforms entries triggers exactly one retry; still-missing stretches are recorded failed and the job continues. → Task 3/Task 4 tests.
- **Event-time read** — `GET /stretches?since=&until=` filters on the stretch `start`/`end` (event time, not ingest time) and orders by `start`. → Task 6 tests.

---

### Task 1: `stretches` table (mirrored)

**Files:**
- Modify: `worker/src/db.py` (`SCHEMA`), `orchestrator/src/db.py` (identical)
- Test: `worker/tests/test_stretches_schema.py` (new); `orchestrator/tests/test_schema_mirror.py` (existing)

**Interfaces:**
- Produces: table `stretches` created by `init_db`.

- [ ] **Step 1: Write the failing test** — `init_db` then assert `PRAGMA table_info(stretches)` columns == `{stretch_id, doc_id, mic_id, silo_id, start, end, content_hash, summary, concepts, ideas, nouns, enriched_at}`.
- [ ] **Step 2: Run it, verify it fails** (`cd worker && pytest tests/test_stretches_schema.py -v`).
- [ ] **Step 3: Add DDL** to both `db.py` `SCHEMA` blocks (identical), near the other feature tables:
```sql
CREATE TABLE IF NOT EXISTS stretches (
    stretch_id TEXT PRIMARY KEY,
    doc_id TEXT,
    mic_id TEXT,
    silo_id TEXT,
    start TEXT,
    end TEXT,
    content_hash TEXT,
    summary TEXT,
    concepts TEXT,   -- JSON [{tag,statement}]
    ideas TEXT,      -- JSON [{kind,text,quote,quote_ok}]
    nouns TEXT,      -- JSON [str]
    enriched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stretches_start ON stretches(start);
CREATE INDEX IF NOT EXISTS idx_stretches_doc ON stretches(doc_id);
```
- [ ] **Step 4: Run** the new test (PASS) and `cd orchestrator && pytest tests/test_schema_mirror.py -v` (PASS).
- [ ] **Step 5: Commit** — `feat(stretches): stretches table (mirrored db.py)`

---

### Task 2: Stretch splitter

**Files:**
- Create: `worker/src/jobs/stretch_splitter.py`
- Test: `worker/tests/test_stretch_splitter.py`

**Interfaces:**
- Produces: `split_document(doc_id: str, mic_id: str, content: str) -> list[dict]` → each `{stretch_id, doc_id, mic_id, start, end, text, content_hash}`. `content` is the transcript body (`[HH:MM:SS] text` lines, office-local). `stretch_id = f"{doc_id}:{index}"`. `start`/`end` = first/last line `HH:MM:SS` in the stretch. `content_hash` = sha256 of the stretch text.

- [ ] **Step 1: Write failing tests:**
  - a >2-minute gap between consecutive lines starts a new stretch;
  - a run exceeding ~120 words (or ~4 min) is cut into multiple stretches;
  - a stretch with <15 content words is dropped from the result;
  - an empty or single-short-line document returns `[]`;
  - `stretch_id` is `f"{doc_id}:0"`, `f"{doc_id}:1"`, … and `start`/`end` match the first/last line times.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `split_document`** — parse `[HH:MM:SS]` prefixes; accumulate lines into a stretch, breaking on >120s inter-line gap or when the stretch would exceed ~120 words / ~4 min; emit stretches with ≥15 content words; stable ids by index. (Analog of `iter_agent_segments`, single-mic + pause-based.)
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(stretches): deterministic stretch splitter`

---

### Task 3: Stretch featurizer (batched LLM facets)

**Files:**
- Create: `worker/src/jobs/stretch_featurizer.py`
- Test: `worker/tests/test_stretch_featurizer.py`

**Interfaces:**
- Consumes: stretch dicts from Task 2 (`stretch_id`, `mic_id`, `start`, `text`).
- Produces:
  - `build_prompt(stretches: list[dict]) -> str` — the tested reference system prompt (spec §4) + the batch rendered as `[stretch_id] (mic HH:MM)` + text.
  - `normalize_facet(raw: dict, stretch_text: str) -> dict` — map `topics→concepts`, coerce `ideas`/`nouns`, compute `quote_ok` (verbatim normalized substring of `stretch_text`), ensure `"none"` summary → empty `concepts`/`ideas`/`nouns`.
  - `async enrich_batch(relay, model, stretches: list[dict]) -> dict[str, dict]` → `{stretch_id: facet}` for ~10 stretches; one relay call (ollama JSON, `think:false`); parse `{"facets":[...]}` (strip fences, `json.loads` with fallback); **one retry** over stretch_ids missing/malformed; still-missing omitted from the result (caller records them failed).

- [ ] **Step 1: Write failing tests** (FakeRelay returning canned `{"facets":[...]}`; no real LLM):
  - `normalize_facet`: a verbatim quote → `quote_ok True`; an absent/paraphrased quote → `False`; `topics` key becomes `concepts`; `summary=="none"` ⇒ empty lists.
  - `enrich_batch`: a fake whose first response omits one stretch → the retry fills it (assert both present); a fake that omits it twice → that stretch_id is absent from the result (caller handles) and no exception.
  - `build_prompt` includes each `stretch_id` and the reference-prompt system text.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the three functions (reuse the JSON-from-relay handling in `upsert_document.extract_document_entities`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(stretches): batched gemma4 facet featurizer + quote_ok`

---

### Task 4: `enrich_stretches` job + worker dispatch

**Files:**
- Create: `worker/src/jobs/enrich_stretches.py`
- Modify: `worker/src/main.py` (dispatch branch for `enrich_stretches`)
- Test: `worker/tests/test_enrich_stretches.py`

**Interfaces:**
- Consumes: `split_document` (T2), `enrich_batch` (T3), the `stretches` table (T1); `get_connection`; `settings.extraction_model`; `Relay.from_settings`.
- Produces: `async run_enrich_stretches(job: dict, db_path: str) -> None`. Config `{collection?: str, since?: str, until?: str, limit?: int}` scopes which **horton transcript documents** (content_type `transcript`) to enrich (default: all in workspace; `limit`/`collection` for the test backfill). Per doc: `split_document` (mic_id from the doc's `source_path` `horton:chunk/<micID>/<...>`), skip stretches whose `content_hash` already matches a stored row (idempotent, no model call), `enrich_batch` the rest (~10/call), upsert `stretches` rows; stretches the featurizer could not produce are logged and counted failed (not written), job continues. Write job result counts; **do not** call `recompute_cooccurrence` / touch entities.

- [ ] **Step 1: Write failing tests** (tmp_path DB seeded with a couple of `content_type='transcript'` documents whose `content` has timestamped lines + `source_path=horton:chunk/MIC/...`; monkeypatch `Relay.from_settings`→FakeRelay and inject the feed; use the REAL `stretches` table and `split_document`):
  - after a run, `stretches` rows exist with correct `doc_id`/`mic_id`/`start`/`end` and parsed facets;
  - second run with unchanged docs makes **zero** model calls (assert via a call-counting FakeRelay) and rewrites nothing new;
  - entities/co-occurrence counts are unchanged by the run (assert before==after);
  - a `limit` in config caps the number of documents enriched.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `run_enrich_stretches` + add the `elif job["type"] == "enrich_stretches":` dispatch branch in `worker/src/main.py`.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Loose quality check (manual, recorded in report):** run `enrich_batch` on ~20 real seed stretches via the configured relay; eyeball summaries/concepts/ideas; note results and whether gemma4:e4b is adequate (else flag swap to gemma4:26b). Not a committed test.
- [ ] **Step 6: Commit** — `feat(stretches): enrich_stretches job (scoped backfill + idempotent, no graph mutation)`

---

### Task 5: `POST /enrich/stretches` route

**Files:**
- Modify: `orchestrator/src/routes/ingest.py` (or the closest existing enqueue-route module), `orchestrator/src/models.py`
- Test: `orchestrator/tests/test_enrich_stretches_route.py`

**Interfaces:**
- Consumes: the `enrich_stretches` job type (T4); `require_writable`, `store.jobs.create`.
- Produces: `class EnrichStretchesRequest(BaseModel)` with `collection: str | None = None`, `since: str | None = None`, `until: str | None = None`, `limit: int | None = None`; route returns `{"job_id": ...}`, 202, workspace via `X-Workspace-Id`.

- [ ] **Step 1: Write failing test** — POST with `{"limit": 50}` + header → 202, `job_id` present, a queued `enrich_stretches` job exists with the config.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the model + route (mirror the `/ingest/horton` enqueue shape).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(stretches): POST /enrich/stretches route`

---

### Task 6: `GET /stretches` read + MCP tool

**Files:**
- Create/Modify: `orchestrator/src/routes/stretches.py` (new router) + register in `main.py`; add repository read in `orchestrator/src/repositories/` (interface + sqlite impl)
- Modify: `orchestrator/src/mcp_server.py` (a `get_stretches` tool)
- Test: `orchestrator/tests/test_stretches_read.py`

**Interfaces:**
- Consumes: the `stretches` table (T1).
- Produces: `GET /stretches?since=&until=&mic=&collection=&limit=&offset=` → list ordered by `start`, each `{stretch_id, doc_id, mic_id, start, end, summary, concepts, ideas, nouns}` (JSON columns parsed). `since`/`until` filter on `start`/`end` (event time). MCP `get_stretches` with the same params.

- [ ] **Step 1: Write failing tests** — seed `stretches` rows across two days; `GET /stretches?since=&until=` returns only the in-window rows, ordered by `start`, with `concepts`/`ideas`/`nouns` as parsed JSON; `mic=` filters by mic.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the repository read (`list_stretches(since, until, mic, collection, limit, offset)`), the route, and the MCP tool.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(stretches): GET /stretches event-time read + MCP tool`

---

## Notes for the executor
- The first backfill run (for validation) is scoped — invoke `POST /enrich/stretches` with a `limit` or one `collection` after the branch is up; do not enrich all 1,528 in CI.
- Do not modify `upsert_document`, `recompute_cooccurrence`, the co-occurrence gate, or `silo.py` — this feature adds a parallel layer only.
