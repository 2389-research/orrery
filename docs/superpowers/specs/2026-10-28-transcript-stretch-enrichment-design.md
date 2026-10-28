# Transcript Stretch Enrichment — Design

**Status:** proposed (2026-10-28)
**Author:** Nomzor + Claude
**Upstream issue (facet contract source of truth):** `DS-scratch/ikebana/docs/superpowers/notes/2026-10-28-orrery-enrichment-issue.md`
**Related:** `docs/superpowers/specs/2026-10-26-horton-transcript-ingestion-design.md` (this **revises its §3**), the meridian ingestion feature (the architectural precedent).

## 1. Intent / Why

Horton transcripts arrive as one document per `(mic, 15-min window)` of raw, noisy open-office speech-to-text (fragments, mis-hearings, cross-talk, several subjects per window). Raw transcript is hard for any agent to use directly. A downstream consumer (ikebana, an office "magazine wall") already splits each window into short **stretches** and LLM-summarizes each into structured **facets** (summary, concepts, ideas, nouns). That denoising enrichment is **generally useful to any consumer**, so it should live in Orrery next to the transcripts; consumers then compose their own opinionated views (threads, topic lists) on top.

### Revises the Horton spec §3
The Horton design ruled summarization *out* of Orrery ("orrery's value-add is the entity graph; prose rollups live downstream"). We consciously **revise that** on new evidence:
- This is **denoising + structured extraction**, not lossy re-representation of already-usable text (the distinction the §3 ruling turned on).
- ikebana's test shows **raw entity tags are too noisy** to build topics from (1,350 scattered tags), while **topics built from per-stretch summaries are stable and specific** — so the "raw entity graph already answers what's discussed" premise does not hold for this data.
- It is the same move Orrery already makes elsewhere (codesum repo summaries, **meridian per-agent segment summaries**) — ingest-time LLM enrichment stored beside the source.

The revised boundary: **shared denoising enrichment (stretch facets) in Orrery; thread/topic-composition downstream.**

## 2. Goals / Non-Goals

**Goals**
- Split each transcript document into **stretches** and produce one structured **facet** per stretch (summary, concepts, ideas, nouns), per the ikebana contract (§4).
- Enrich **once on arrival**, keep across days; **backfill** existing docs (a scoped subset for the first test).
- Expose an **event-time read**: `GET /stretches?since=&until=` (ordered by `start`) + an MCP tool.
- Run on the configured local model (**gemma4 via ollama**), batched, strict-JSON, with a retry for skipped stretches.

**Non-Goals (explicitly, per the issue)**
- Threads, workstreams, running topic lists, write-ups — anything stringing stretches across mics/time. Consumer-owned.
- Feeding facets into the neutral co-occurrence graph or entity set (see §5).
- A new model backend — gemma4 via the existing relay; no LunaRoute/deepseek wiring (revisit only if gemma quality is inadequate).

## 3. Decisions (settled 2026-10-28)
1. **Model:** `EXTRACTION_MODEL` (gemma4:e4b — "good at structured output" per CLAUDE.md) via `orrery-relay` ollama path, `think:false`, strict-JSON prompt. Quality **loosely validated** during build on real stretches; swap to `CLASSIFICATION_MODEL` (gemma4:26b) via config if summaries read poorly. *(The ikebana prompt was tested on deepseek-flash; it is model-portable.)*
2. **Provenance / graph:** facets are their **own retrievable layer**, NOT fed into co-occurrence or entities (same reasoning that excludes `agent_report` — LLM-derived, not neutral evidence). Raw chunks keep emitting co-occurrence exactly as today. Facets never mutate the graph.
3. **Read:** `GET /stretches?since=&until=` filters on the stretch's **event-time `start`/`end`** — which also gives the clean event-time slicing raw docs lack (the gap flagged in the Office Pulse primer). Office Pulse should prefer `/stretches` for time windows.
4. **Backfill:** scoped — a subset of the 1,528 seed docs for the first test (by collection set / date / limit), then enrich-on-arrival for new docs. Mirrors meridian's backfill + incremental.

## 4. The stretch + facet contract (from the ikebana issue)

**Stretch split (deterministic, no LLM).** Each Orrery transcript document is already one mic's 15-minute window, so splitting is single-mic: operate on the document's `content` lines (`[HH:MM:SS] text`, office-local). Break a stretch at a **pause > 2 minutes** between consecutive lines; cap each stretch at **~4 minutes or ~120 words**; **drop** stretches with **< ~15 content words**.

Per stretch:
| Field | Meaning |
|---|---|
| `stretch_id` | stable id (derived from `doc_id` + stretch index; deterministic) |
| `doc_id`, `mic_id` | source document + mic (mic from the doc's `source_path`) |
| `start`, `end` | first/last line timestamps (event time — consumers slice by these) |
| `summary` | 2–4 plain sentences; exactly `"none"` for pure chatter/greetings/unintelligible (then empty lists) |
| `concepts` | 1–3 `{tag, statement}` — short lowercase tag (2–5 words) + one sentence on what was said |
| `ideas` | 0–3 `{kind, text, quote, quote_ok}` — kind ∈ proposal\|question\|decision\|problem; `text` one sentence; `quote` verbatim (≤20 words); `quote_ok` set **by code** = quote found in the stretch text |
| `nouns` | concrete tools/products/projects/systems (not people) |

**Rules:** transcript-only, nothing invented; **no people's names** in `summary`/`concepts`/`ideas` (quotes are verbatim raw text and may contain names — consumers treat quotes as raw); each stretch summarized **on its own** (no day framing — a live feed can't know the day); tag wording will vary and that's fine (consumers merge via summaries, not raw tags).

**Prompt:** the tested reference prompt in the issue (§"Reference prompt"), used verbatim as the system prompt. Note two naming reconciliations the code normalizes: the prompt emits `topics` → stored as **`concepts`**; the prompt's idea objects carry `tag` → code maps to the stored idea shape and **computes `quote_ok`** (verbatim substring check, whitespace/case-normalized). `leaf_id` in the prompt = our `stretch_id`.

**Batching:** ~10 stretches per model call, one strict-JSON `{"facets":[...]}` response per batch, **one retry** for stretches the model skipped/returned malformed.

## 5. Architecture (mirror meridian)

The meridian ingest is the template: deterministic split → per-unit LLM call → store → backfill + incremental. Reuse its shape.

- **`worker/src/jobs/stretch_splitter.py`** (new, pure, unit-tested): `split_document(doc_id, mic_id, content) -> list[Stretch]` applying the §4 rules to the `[HH:MM:SS]` lines. No I/O. (Analog of `iter_agent_segments`.)
- **`worker/src/jobs/stretch_featurizer.py`** (new): `async enrich_batch(relay, model, stretches) -> list[facet]` — builds the strict-JSON prompt (reference prompt), one relay call per ~10 stretches (`think:false`, ollama JSON), parses `{"facets":[...]}`, maps `topics→concepts`, computes `quote_ok`, retries skipped once. (Analog of `summarize_segment`, but batched + structured.)
- **`stretches` table** (new, mirrored in both `db.py`): `stretch_id PK, doc_id, mic_id, silo_id, start, end, content_hash, summary, concepts (json), ideas (json), nouns (json), enriched_at`. `content_hash` over the stretch text → idempotent re-enrich (skip unchanged, like `upsert_document`). Kept across days.
- **`worker/src/jobs/enrich_stretches.py`** (new): the job. Scope by `{workspace, collection(s)|date|limit}`. For each in-scope horton document: split → enrich new/changed stretches (batched) → upsert `stretches` rows. Runs as **backfill** (scoped subset first) and as **enrich-on-arrival** (enqueued after a horton doc is created — hook in `run_ingest_horton`, or a follow-on pass gated on new docs, mirroring meridian's tail).
- **Route `POST /enrich/stretches`** (new, orchestrator): enqueue the job with `{collection?|since?|until?|limit?}`; workspace via `X-Workspace-Id`. Idempotent.
- **Read `GET /stretches?since=&until=&mic=&collection=&limit=&offset=`** (new): rows ordered by `start`, all facet fields; event-time filter on `start`/`end`. Mirror as an MCP tool (`get_stretches` / extend `list_documents`).

## 6. Review focus (failure modes to pin with tests)
1. **Split boundaries** — >2min pause splits; ≤4min/120-word cap; <15-word stretches dropped; a single-line or empty doc yields zero stretches without error.
2. **Idempotent re-enrich** — unchanged stretch (`content_hash` match) does no model call on a second pass; a revised doc re-enriches only changed stretches.
3. **`quote_ok` correctness** — a verbatim quote → true; a hallucinated/paraphrased quote → false (normalized substring check).
4. **`"none"` handling** — chatter stretch stores `summary="none"` with empty `concepts`/`ideas`/`nouns`.
5. **Malformed/skipped batch** — a model batch that drops or malforms entries triggers exactly one retry; still-missing stretches are recorded as failed, not silently lost, and don't abort the job (cursor/scope held like the ingest job).
6. **Event-time read** — `/stretches?since=&until=` filters on `start`/`end` (event time), returns ordered by `start`.
7. **No graph mutation** — enrichment writes only `stretches`; co-occurrence/entities unchanged (assert counts before/after).

## 7. Left to the builder
- Exact `stretch_id` scheme (deterministic from `doc_id`+index), content-word counting, and timestamp parsing from the `[HH:MM:SS]` lines + the doc's `source_path` date.
- Relay JSON mechanics on ollama (reuse the `extract_document_entities` pattern: ask for JSON, strip fences, `json.loads` with fallback) + batch retry.
- Loose quality check: run `enrich_batch` on ~20 real stretches from the seed and eyeball summaries/concepts/ideas; note results in the build report; flag if gemma4:e4b underperforms (then try gemma4:26b).
- Test backfill scope for the first run (e.g. one day across all mics, or ~150–200 docs).
