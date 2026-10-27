# Horton Transcript Ingestion — Design

**Status:** proposed (2026-10-26)
**Author:** Nomzor + Claude
**Related:** `docs/superpowers/specs/2026-08-18-source-silos-provenance-design.md` (silos/kinds),
`docs/superpowers/specs/2026-10-24-kind-scoped-entity-connection-design.md` (kind-scope),
the meridian ingestion feature (#127) — this is its simpler sibling.
**Source contract:** `~/Documents/GitHub/DS-scratch/horton-orrery/docs/ORRERY_HANDOFF.md` (+ `api/openapi.yaml`).

## 1. Intent / Why

Horton is the office's always-on microphone transcription system. `horton-orrery` (a prototype
service, structurally cloned from meridian) listens to Horton's MQTT feed and seals utterances
into per-`(mic, 15-minute window)` **chunk** records, served over a meridian-shaped cursor-paginated
pull API.

We want those transcripts in the knowledge graph so the office's spoken context becomes
graph-walkable alongside code and vault notes: *"what has the conference table been discussing,"*
*"which mic-days mention `deploy`,"* and (later, downstream) higher-level rollups. The search /
time-slice work done recently was the **preparation** for this ingestion flow — it is not a
separate deliverable here.

This is the ingestion half only. Summarization is explicitly **downstream** (see §3).

## 2. Goals / Non-Goals / Anti-Goals

**Goals**
- Pull Horton chunks via the meridian-shaped API and ingest each chunk as one raw-text document.
- Entities extracted + classified per chunk so the transcript graph is queryable and connected.
- Time-sliceable by day (and by mic) without new indexed machinery.
- Idempotent + resumable: re-pull is cheap no-ops; late-revised chunks update in place.
- Bounded collections — no infinitely-growing collection despite always-on mics.

**Non-Goals**
- Summarizing transcripts inside orrery (downstream; re-ingested later as its own kind).
- An agent/segment tree (meridian's structure) — transcripts are flat.
- A new indexed event-time column / recency ranking (deferred; day-slicing is free via collection path).
- De-duplicating near-identical chunks from co-located mics (accepted as distinct; see §7).
- Live MQTT subscription inside orrery — orrery only reads `horton-orrery`'s pull API.

**Anti-Goals (would make this a failure even if it "works")**
- Transcript "summaries" silently emitting co-occurrence as if they were neutral evidence.
- A late-arriving 2-week-old revised chunk landing in *today's* collection.
- The same concept fragmenting per mic-day so the transcript graph has no cross-mic connectivity.

## 3. Why summarization lives downstream (settled)

Orrery summarizes a *repo* only because `codesum` is the sole way to turn code into text. A
transcript is **already text** (`body`, "ready to index"). Orrery's native value-add is the
**entity graph**, and *"what is this collection discussing"* is answerable from the time-sliced
subgraph — lossless and re-sliceable, unlike a frozen prose blob.

So: **ingest raw `body`.** If a prose rollup is ever wanted, a downstream service pulls a
time-sliced collection (the primitive we built) → summarizes → **re-ingests the summary as a
document with its own kind** (`agent_report`, which is *excluded* from co-occurrence emission — an
LLM's account of a conversation is not neutral evidence that two concepts co-occur). No hourly
in-orrery resummarization job (it would worsen the Mac GPU contention for zero graph benefit).

## 4. Constraints

- **Mirror rule:** `worker/src/silo.py` and `orchestrator/src/pipeline/silo.py` must stay
  byte-identical below their ABOUTME header (enforced by `orchestrator/tests/test_schema_mirror.py`,
  run natively). Adding the new kind touches both.
- **Schema mirror:** any new table (the cursor) is added identically to `worker/src/db.py` and
  `orchestrator/src/db.py`.
- **Builds on branch `feat/kind-scoped-entity-connection`** (tip of the stacked
  meridian → work-unit → kind-scope line), where `meridian_reader.py` / `ingest_meridian.py` /
  `upsert_document.py` already exist to pattern against.
- **Reader is stdlib-only with an injectable `fetch`** (mirrors `meridian_reader.make_fetch`), so
  every function is unit-testable without a network.
- **WAL + `get_connection()`** everywhere; worker fail-fast timeout unchanged.
- **Local-model reality:** classification/extraction run through the relay on gemma4 via ollama;
  cost scales with chunk count, so change-detection must prevent re-work on re-pull.

## 5. Approaches considered

**A. Reuse the meridian ingest shape wholesale (tree + summarization).** Rejected — transcripts have
no agent tree and no need for segment/rollup summarization; it would import complexity the source
doesn't have and bake summaries into ingest (an anti-goal).

**B. One collection per mic, forever.** Rejected — mics are always on, so the collection grows
without bound (the user's explicit concern).

**C. (chosen) Flat per-chunk ingest + per-`(mic, day)` collections.** Each chunk → one raw-text
document via the existing `upsert_document` primitive; collection keyed by the chunk's own
`(mic.id, day)`. Bounded by construction (~96 windows/mic-day), day-sliceable via collection path,
and — because every transcript collection shares one kind — entities auto-connect across mics and
days under kind-scope. Smallest build that satisfies every goal.

## 6. The design

### 6.1 Provenance kind: new `horton_transcript`

Add `horton_transcript` to the closed `KINDS` set in **both** `silo.py` files, and
`FLOW_DEFAULT_KIND["horton"] = "horton_transcript"`. It is a bucket that **emits co-occurrence**
(only `agent_report` is excluded by the emission gate — no gate change needed; a transcript *is*
neutral evidence of what co-occurs in speech).

Rationale for a distinct bucket rather than mapping to `neutral_summary`: provenance hygiene under
kind-scope. In the dedicated noosphere (§6.6) it is moot for connectivity (one DB, one kind), but if
transcripts are ever ingested into a shared noosphere alongside code/vault, transcript-`openai` must
**propose** rather than silently auto-merge with code-`openai`. Cheap, principled, consistent with
the kind-scope model. (Zero-churn fallback if rejected: map to `neutral_summary` — no mirror edit.)

- `collections.kind` (the collection TYPE / featurizer) = `'horton'`.
- `collections.provenance_kind` (the bucket, must be in `KINDS`) = `'horton_transcript'`, set via
  `resolve_kind(flow_default_kind("horton"), override)`.

### 6.2 Reader — `worker/src/jobs/horton_reader.py`

Mirror `meridian_reader.py`'s shape (stdlib, injectable `fetch` via `make_fetch`). Functions:

- `list_chunks_page(fetch, after, limit=100)` → `(items, next_cursor, has_more)` for one page of
  `GET /v1/chunks?after=&limit=`. Envelope `{items, next_cursor, has_more}`; `after` exclusive;
  opaque cursors (never parsed).
- `drain_chunks(fetch, after, limit=100, max_pages=10000)` → `(items, next_cursor, has_more)` —
  pages until `has_more` is false, accumulating items. **Spin guard:** if a page claims `has_more`
  but `next_cursor == after`, stop and return `has_more=True` (buggy-server guard, copied from
  `list_changed_sessions`).
- `get_chunk(fetch, chunk_id)` → `GET /v1/chunks/{id}` with the id **percent-encoded**
  (`urllib.parse.quote(chunk_id, safe="")` — ids contain `:` and `/`). Used only where a single
  re-fetch is needed; the page items already carry the full record, so the main path does not call it.
- Pure helper `chunk_to_doc(chunk)` → `{source_path, title, content, parent_path, metadata,
  mic_id, day}` applying the §6.5 mapping. No I/O — unit-tested directly against `fixtures/api/`.

### 6.3 Cursor — `horton_cursor` table

One pull feed per workspace, many collections, so the cursor is **per feed**, not per collection
(unlike `meridian_cursor`). Added identically to both `db.py` files:

```sql
CREATE TABLE IF NOT EXISTS horton_cursor (
    server_url TEXT PRIMARY KEY,
    after TEXT
);
```

Keyed by `server_url` so pointing the same workspace at the seed export vs. the live listener keeps
separate cursors. Whole-drain / process-all / commit-cursor-once, holding the cursor if any chunk
failed (same discipline as `run_ingest_meridian`).

### 6.4 Ingest job — `worker/src/jobs/ingest_horton.py` (`run_ingest_horton`)

```
config = {server_url, spec_id?}
conn = get_connection(db_path)
after = SELECT after FROM horton_cursor WHERE server_url = ?
items, next_cursor, _ = drain_chunks(fetch, after)
coll_cache = {}                         # (mic_id, day) -> collection_id, within this pass
counts = {created, updated, skipped, failed}
for chunk in items:
    doc = chunk_to_doc(chunk)
    collection_id = get_or_create_collection(conn, doc.mic_id, doc.day, server_url, coll_cache)
    try:
        res = await upsert_document(conn, relay, settings,
                 source_path=doc.source_path, title=doc.title, content=doc.content,
                 collection_id=collection_id, role="leaf", parent_path=doc.parent_path,
                 content_type="transcript", metadata=doc.metadata,
                 classify=True, emits_cooccurrence=True)
        counts[res.action] += 1
    except Exception: counts.failed += 1; log      # never abort the whole drain
if counts.failed == 0 and next_cursor and next_cursor != after:
    upsert horton_cursor(server_url, next_cursor)   # advance ONCE; hold on any failure
if counts.created or counts.updated: mark_graph_dirty(conn)
write job result; commit
```

- `get_or_create_collection(conn, mic_id, day, server_url, cache)`: path = `horton/<mic_id>/<day>`
  (= the chunk's `folder`). On miss, `SELECT id FROM collections WHERE path = ?`; if absent
  `INSERT INTO collections (id, name, path, root_path, remote_url, document_count, kind,
  provenance_kind) VALUES (?, ?, ?, ?, ?, 0, 'horton', ?)` with
  `provenance_kind = resolve_kind(flow_default_kind("horton"))`. Catch `IntegrityError` on the
  `UNIQUE(path)` race and re-select (same guard as the meridian route). Cache the id for the pass.
- **Late-revision correctness:** `day` is derived from `window.start` (event time), carried on the
  chunk, so a revised old chunk routes to its **original** day collection, never today's.
- **Change detection is free:** `upsert_document` skips when `content_hash` is unchanged (so a
  re-pulled chunk costs no LLM calls), re-extracts when `body` changed on a revision bump, and
  upserts metadata-only when just the metadata moved.
- Optional one-time `extract_batch` enqueue mirroring the meridian tail (only if a `spec_id` is
  configured and transcript docs are stranded) — include only if the general-spec path leaves docs
  under-extracted; otherwise omit (YAGNI; `upsert_document` already extracts with the general spec).

### 6.5 Chunk → document mapping

| document field | source |
|---|---|
| `source_path` | chunk `id` (`horton:chunk/<micID>/<window start Z>`) — stable identity, drives upsert |
| `title` | `f"{mic.name} · {local HH:MM}"` derived from `window.start` + `mic.name` (fallback `mic.id`) |
| `content` | `body` (raw `[HH:MM:SS] text` lines) |
| `content_type` | `'transcript'` |
| `parent_path` | `folder` (`horton/<micID>/<day>`) |
| `collection_id` / silo | the `(mic.id, day)` collection → silo = collection_id, kind = `horton_transcript` |
| `metadata` | `{mic: {id,name,names_in_window,mixed_mics}, window_start, window_end, day, complete, gaps, source, line_count, revision, content_hash}` |
| date | **event-time in metadata** (`window_start`/`window_end`); `created_at` stays ingest time |
| emits co-occurrence | `True` |

**Empty body:** skip (no document) — a window with no utterances carries no graph signal.

### 6.6 Noosphere / workspace

A **dedicated "Horton Transcripts" noosphere** (its own workspace / SQLite file), created by the
user via the MCP `create_noosphere` tool. The route selects it by the `X-Workspace-Id` header, like
every ingest route. Keeping transcripts in their own DB keeps them off Code Repos / Code Sessions;
the distinct kind (§6.1) is the belt-and-suspenders that also keeps entities provenance-separate
*within* any DB.

### 6.7 Route — `POST /ingest/horton`

In `orchestrator/src/routes/ingest.py`, mirroring `/ingest/meridian` but simpler (no collection
get-or-create here — collections are derived per chunk in the worker):

- Request: `{server_url: str, spec_id?: str}` (`HortonIngestRequest` in `models.py`); workspace from
  `X-Workspace-Id`.
- Reuse the workspace general spec if present, else seed `GENERAL_TEXT_SPEC` (as meridian does).
- Enqueue a `run_ingest_horton` job with `{server_url, spec_id}`; return `{job_id}`.
- Idempotent: a repeat POST resumes from `horton_cursor`.
- Register `run_ingest_horton` in the worker dispatch (`worker/src/main.py`).

## 7. Known limitations (accepted)

- **Near-duplicate chunks** from co-located mics (conference-table pair, kitchen) repeat the same
  speech as separate documents, by design. Entity-level normalization already collapses the shared
  *entities* across those collections under kind-scope, so the graph is not double-counting concepts
  (co-occurrence weights may still reflect both mics — acceptable). **Ruling (2026-10-27):** orrery
  does *no* near-duplicate dedup, not even the cheap emission-gate-by-colocation lever. Nuanced
  dedup belongs downstream (co-location is physical-world knowledge orrery can't derive); if a
  downstream system settles on an opinionated approach, we revisit pulling it into orrery then.
  This is the general-layer philosophy: expose the raw per-mic docs, let consumers compose.
- **Seed re-delivery:** the seed dataset is PROVISIONAL (most mics are `seed:<name>`; mic ids change
  when a name gains a MAC). On re-delivery the handoff says wipe and reload `seed:*` mics and all
  `source:"seed"` chunks. Provide this as a maintenance step: a scoped delete of collections whose
  `path LIKE 'horton/seed:%'` plus their documents, then re-pull. A dedicated reset route is
  out-of-scope for v1 (a documented manual SQL step suffices); add one only if reload becomes routine.
- **No exact indexed event-time slicing** — day-level slicing is via collection path; finer slicing
  reads `metadata.window_start`. An indexed `occurred_at` column is a separate, deferred change.

## 8. Review focus (failure modes to pin with tests)

1. **Id percent-encoding** — `get_chunk` with an id containing `:` and `/` must round-trip
   (`quote(..., safe="")`); a raw id would 404.
2. **Cursor spin guard** — `has_more=True` with a non-advancing `next_cursor` must stop, not loop.
3. **Late-revision day routing** — a revised chunk with an old `window.start` updates its original
   `(mic, day)` collection, not the current day's.
4. **`content_hash` skip** — an unchanged re-pulled chunk performs zero LLM calls (assert via a
   `fetch`/relay spy or the `skipped` action).
5. **Cursor held on failure** — if any chunk raises, `horton_cursor` is not advanced, so the page
   is re-pulled next pass (done chunks are cheap skips).
6. **Collection path uniqueness / race** — concurrent first-chunk creates for the same `(mic, day)`
   resolve to one collection via the `IntegrityError` re-select.
7. **Mirror integrity** — `test_schema_mirror` still passes after the `KINDS` /
   `FLOW_DEFAULT_KIND` edit and the `horton_cursor` DDL (both files edited identically).
8. **Empty / missing fields** — empty `body` skipped; missing `mic.name` falls back to `mic.id`;
   `gaps`/`mixed_mics` absent → tolerated in metadata.

## 9. Left to the builder

- Idiomatic test placement under `worker/tests/` (reader unit tests against `fixtures/api/`;
  an ingest test with an injected `fetch` + a tmp-path DB, per `conftest.py`).
- Exact `title` formatting and local-time conversion from `window.start` (office-local is already
  encoded in `day`; derive `HH:MM` from the RFC3339 `window.start`).
- Whether to include the optional `extract_batch` tail (§6.4) — decide from whether the general-spec
  extraction in `upsert_document` leaves transcript docs adequately covered.
- Metadata key names (follow existing JSON-metadata conventions in neighboring ingest jobs).
```
