# Horton Transcript Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull Horton office-mic transcript chunks from the `horton-orrery` pull API and ingest each as one raw-text document into a dedicated noosphere, so spoken office context becomes a connected, time-sliceable knowledge graph.

**Architecture:** A stdlib reader (mirror of `meridian_reader.py`) pages the cursor-paginated `/v1/chunks` API; a worker job maps each chunk to a document and calls the existing `upsert_document` primitive, lazily creating one collection per `(mic, day)`; a thin orchestrator route enqueues the job. Entity connectivity across mics/days comes for free from kind-scope because every transcript collection shares one new provenance kind, `horton_transcript`.

**Tech Stack:** Python (worker + FastAPI orchestrator), SQLite/WAL, `orrery_relay` for LLM calls, pytest with `tmp_path` file-backed DB fixtures.

**Spec:** `docs/superpowers/specs/2026-10-26-horton-transcript-ingestion-design.md`

## Global Constraints

- **Mirror byte-identical below the ABOUTME header:** `worker/src/silo.py` ⇄ `orchestrator/src/pipeline/silo.py`, and `worker/src/db.py` ⇄ `orchestrator/src/db.py`. Enforced by `orchestrator/tests/test_schema_mirror.py`, which **must run natively** (not in Docker).
- Build on branch `feat/kind-scoped-entity-connection` (tip of the meridian → work-unit → kind-scope stack).
- Reader is **stdlib-only** with an **injectable `fetch`** (mirror `meridian_reader.make_fetch`); every reader function is unit-testable without a network.
- All SQLite access via `get_connection()` (WAL + the process's existing busy_timeout); never open SQLite directly, never re-specify PRAGMAs.
- `horton_transcript` **emits** co-occurrence (only `agent_report` is gated out — no gate change).
- Collection path = the chunk's own `folder` (`horton/<micID>/<day>`); document date is **event-time in metadata** (`window_start`/`window_end`), `created_at` stays ingest-time.
- New kind value is `"horton_transcript"`; collection TYPE (`collections.kind`) is `"horton"`; `FLOW_DEFAULT_KIND["horton"] = "horton_transcript"`.
- YAGNI: no near-duplicate dedup, no indexed event-time column, no seed-reset route (manual SQL step only).

## Review Focus

- **Late-revised old chunk routes to its original `(mic, day)` collection, not today's** — `day` comes from `window.start` (event time). → Task 4 test.
- **Cursor held when any chunk fails** — a raising chunk must leave `horton_cursor.after` unadvanced so the page re-pulls next pass. → Task 4 test.
- **Chunk id with `:` and `/` round-trips** — `get_chunk` must percent-encode (`quote(id, safe="")`); a raw id 404s. → Task 3 test.
- **Unchanged re-pulled chunk does no LLM work** — identical `content_hash` → `skipped`, zero extraction. → Task 4 test (asserts `upsert_document` action `skipped` via content-hash, no relay call).
- **Degenerate fields tolerated** — empty `body` → no document; missing `mic.name` → `mic.id` fallback in the title. → Task 3 tests.

---

### Task 1: Add the `horton_transcript` provenance kind (mirrored)

**Files:**
- Modify: `worker/src/silo.py` (`KINDS`, `FLOW_DEFAULT_KIND`)
- Modify: `orchestrator/src/pipeline/silo.py` (identical edit)
- Test: `orchestrator/tests/test_schema_mirror.py` (existing — must still pass)

**Interfaces:**
- Produces: `KINDS` now contains `"horton_transcript"`; `flow_default_kind("horton") == "horton_transcript"`; `resolve_kind(flow_default_kind("horton")) == "horton_transcript"`.

- [ ] **Step 1: Write the failing test** in `worker/tests/test_silo_horton_kind.py`

```python
from src.silo import KINDS, flow_default_kind, resolve_kind

def test_horton_transcript_is_a_known_kind():
    assert "horton_transcript" in KINDS

def test_horton_flow_default():
    assert flow_default_kind("horton") == "horton_transcript"
    assert resolve_kind(flow_default_kind("horton")) == "horton_transcript"
```

- [ ] **Step 2: Run to verify it fails** — `cd worker && pytest tests/test_silo_horton_kind.py -v` → FAIL (`horton_transcript` not in `KINDS`).
- [ ] **Step 3: Implement** — add `"horton_transcript"` to the `KINDS` set and `"horton": "horton_transcript"` to `FLOW_DEFAULT_KIND` in `worker/src/silo.py`, below the ABOUTME header.
- [ ] **Step 4: Apply the identical edit** to `orchestrator/src/pipeline/silo.py` (byte-identical below the header).
- [ ] **Step 5: Run tests** — `cd worker && pytest tests/test_silo_horton_kind.py -v` (PASS) and `cd orchestrator && pytest tests/test_schema_mirror.py -v` (PASS — mirror intact).
- [ ] **Step 6: Commit** — `feat(horton): horton_transcript provenance kind (mirrored silo.py)`

---

### Task 2: Add the `horton_cursor` table (mirrored)

**Files:**
- Modify: `worker/src/db.py` (`SCHEMA`, near `meridian_cursor`)
- Modify: `orchestrator/src/db.py` (identical edit)
- Test: `worker/tests/test_horton_cursor_schema.py` (new); `orchestrator/tests/test_schema_mirror.py` (existing)

**Interfaces:**
- Produces: table `horton_cursor (server_url TEXT PRIMARY KEY, after TEXT)` present after `init_db`.

- [ ] **Step 1: Write the failing test**

```python
def test_horton_cursor_table_exists(tmp_path):
    from src.db import init_db, get_connection
    db = str(tmp_path / "t.db"); init_db(db)
    conn = get_connection(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(horton_cursor)")}
    assert cols == {"server_url", "after"}
```

- [ ] **Step 2: Run to verify it fails** — `cd worker && pytest tests/test_horton_cursor_schema.py -v` → FAIL (no such table).
- [ ] **Step 3: Implement** — add the `CREATE TABLE IF NOT EXISTS horton_cursor (server_url TEXT PRIMARY KEY, after TEXT);` block to `SCHEMA` in `worker/src/db.py`, adjacent to `meridian_cursor`.
- [ ] **Step 4: Apply the identical edit** to `orchestrator/src/db.py`.
- [ ] **Step 5: Run tests** — the new test (PASS) and `cd orchestrator && pytest tests/test_schema_mirror.py -v` (PASS).
- [ ] **Step 6: Commit** — `feat(horton): horton_cursor table (mirrored db.py)`

---

### Task 3: Reader — `horton_reader.py`

**Files:**
- Create: `worker/src/jobs/horton_reader.py`
- Test: `worker/tests/test_horton_reader.py`

**Interfaces:**
- Consumes: `make_fetch(base_url)` pattern from `meridian_reader` (re-import or re-implement the same stdlib `fetch`).
- Produces:
  - `list_chunks_page(fetch, after: str | None, limit: int = 100) -> tuple[list[dict], str | None, bool]` → `(items, next_cursor, has_more)` for one `GET /v1/chunks?after=&limit=`.
  - `drain_chunks(fetch, after: str | None, limit: int = 100, max_pages: int = 10000) -> tuple[list[dict], str | None, bool]` → accumulate pages until `has_more` is false; **spin guard**: if `has_more` but `next_cursor == after`, stop and return `has_more=True`.
  - `get_chunk(fetch, chunk_id: str) -> dict` → `GET /v1/chunks/{quote(chunk_id, safe="")}`.
  - `chunk_to_doc(chunk: dict) -> dict | None` → `{source_path, title, content, parent_path, metadata, mic_id, day}`, or `None` when `body` is empty/whitespace.

**chunk_to_doc mapping** (from spec §6.5): `source_path=chunk["id"]`; `content=chunk["body"]`; `parent_path=chunk["folder"]`; `mic_id=chunk["mic"]["id"]`; `day=chunk["day"]`; `title=f"{mic_name} · {HH:MM}"` where `mic_name = chunk["mic"].get("name") or chunk["mic"]["id"]` and `HH:MM` is the local clock from the first `body` line's `[HH:MM:SS]` prefix (fallback: the `window.start` time-of-day); `metadata={"mic": {id,name,names_in_window,mixed_mics}, "window_start": window["start"], "window_end": window["end"], "day", "complete", "gaps", "source", "line_count", "revision", "content_hash"}`.

- [ ] **Step 1: Write the failing tests** (use a `fake_fetch` returning dicts; load a real chunk from the handoff fixtures copied into the test or inlined from `chunk.json`):

```python
def test_drain_chunks_accumulates_until_has_more_false():
    pages = [ ({"items":[{"id":"a"}], "next_cursor":"c1", "has_more":True}),
              ({"items":[{"id":"b"}], "next_cursor":"c2", "has_more":False}) ]
    seq = iter(pages)
    def fake(path, params=None): return next(seq)
    items, cur, more = drain_chunks(fake, None)
    assert [i["id"] for i in items] == ["a","b"]; assert cur == "c2"; assert more is False

def test_drain_chunks_spin_guard_stops_on_nonadvancing_cursor():
    def fake(path, params=None): return {"items":[], "next_cursor": params and params.get("after"), "has_more":True}
    items, cur, more = drain_chunks(fake, "x")
    assert more is True  # returned, did not loop forever

def test_get_chunk_percent_encodes_the_id():
    seen = {}
    def fake(path, params=None): seen["path"] = path; return {"id":"ok"}
    get_chunk(fake, "horton:chunk/AA:BB/2026-10-26T19:00:00Z")
    assert "%3A" in seen["path"] and "%2F" in seen["path"]  # ':' and '/' encoded

def test_chunk_to_doc_maps_body_and_event_time(sample_chunk):
    doc = chunk_to_doc(sample_chunk)
    assert doc["source_path"] == sample_chunk["id"]
    assert doc["content"] == sample_chunk["body"]
    assert doc["parent_path"] == sample_chunk["folder"]
    assert doc["metadata"]["window_start"] == sample_chunk["window"]["start"]
    assert doc["metadata"]["revision"] == sample_chunk["revision"]

def test_chunk_to_doc_title_falls_back_to_mic_id_when_no_name():
    c = {**sample_chunk(), "mic": {"id":"AA:BB","mixed_mics":False}}  # no 'name'
    assert chunk_to_doc(c)["title"].startswith("AA:BB")

def test_chunk_to_doc_skips_empty_body():
    assert chunk_to_doc({**sample_chunk(), "body":"   "}) is None
```

- [ ] **Step 2: Run to verify they fail** — `cd worker && pytest tests/test_horton_reader.py -v` → FAIL (module/functions missing).
- [ ] **Step 3: Implement `horton_reader.py`** with the four functions above. `drain_chunks` copies the spin-guard shape from `meridian_reader.list_changed_sessions`. `get_chunk` uses `urllib.parse.quote(chunk_id, safe="")`.
- [ ] **Step 4: Run tests** — `cd worker && pytest tests/test_horton_reader.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat(horton): pull-API reader (drain, percent-encoded get, chunk_to_doc)`

---

### Task 4: Ingest job — `ingest_horton.py` + worker dispatch

**Files:**
- Create: `worker/src/jobs/ingest_horton.py`
- Modify: `worker/src/main.py:139-161` (add an `elif job["type"] == "ingest_horton":` branch)
- Test: `worker/tests/test_ingest_horton.py`

**Interfaces:**
- Consumes: `drain_chunks`, `chunk_to_doc` (Task 3); `upsert_document` (`worker/src/jobs/upsert_document.py`); `resolve_kind`, `flow_default_kind` (Task 1); `get_connection`, `mark_graph_dirty`.
- Produces:
  - `get_or_create_collection(conn, mic_id: str, day: str, server_url: str, cache: dict) -> str` → collection id for `path = f"horton/{mic_id}/{day}"`; inserts with `kind='horton'`, `provenance_kind=resolve_kind(flow_default_kind("horton"))`, `remote_url=server_url`; catches `sqlite3.IntegrityError` on the `UNIQUE(path)` race and re-selects; memoizes in `cache`.
  - `async run_ingest_horton(job: dict, db_path: str) -> None` → config `{server_url, spec_id?}`: read `horton_cursor.after`, `drain_chunks`, per chunk `chunk_to_doc` → (skip if `None`) → `get_or_create_collection` → `await upsert_document(..., collection_id=cid, role="leaf", parent_path=doc["parent_path"], content_type="transcript", metadata=doc["metadata"], classify=True, emits_cooccurrence=True)`; count by action; a per-chunk exception increments `failed` and is logged (never aborts the drain); advance `horton_cursor` **once** only if `failed == 0 and next_cursor and next_cursor != after`; `mark_graph_dirty` if any created/updated; write job result.

- [ ] **Step 1: Write the failing tests** (inject `fetch` by monkeypatching `drain_chunks`/`make_fetch` or passing a fake through config; use a `tmp_path` file-backed DB per `conftest.py`; a stub/monkeypatched `upsert_document` that records calls and returns `{"action": ...}` so no real LLM runs):

```python
async def test_late_revised_chunk_routes_to_its_original_day_collection(tmp_db, fake_two_day_feed):
    # feed: a chunk with window.start on 2026-10-15 arriving after today's chunks
    await run_ingest_horton(job, tmp_db)
    paths = {r[0] for r in conn.execute("SELECT path FROM collections")}
    assert "horton/MIC/2026-10-15" in paths  # dated by window.start, not ingest day

async def test_cursor_held_when_a_chunk_fails(tmp_db, feed_with_one_bad_chunk):
    await run_ingest_horton(job, tmp_db)
    row = conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (url,)).fetchone()
    assert row is None  # not advanced because a chunk failed

async def test_unchanged_chunk_on_second_pass_is_skipped(tmp_db, feed_one_chunk):
    await run_ingest_horton(job, tmp_db)      # creates
    calls.clear()
    await run_ingest_horton(job, tmp_db)      # same chunk, same content_hash
    assert all(c.action == "skipped" for c in calls)  # no re-extraction

async def test_collection_created_with_horton_kind_and_provenance(tmp_db, feed_one_chunk):
    await run_ingest_horton(job, tmp_db)
    row = conn.execute("SELECT kind, provenance_kind FROM collections").fetchone()
    assert row == ("horton", "horton_transcript")

def test_get_or_create_collection_is_idempotent_and_cached(tmp_db):
    cid1 = get_or_create_collection(conn, "MIC", "2026-10-15", url, {})
    cid2 = get_or_create_collection(conn, "MIC", "2026-10-15", url, {})
    assert cid1 == cid2
```

- [ ] **Step 2: Run to verify they fail** — `cd worker && pytest tests/test_ingest_horton.py -v` → FAIL.
- [ ] **Step 3: Implement `ingest_horton.py`** (`get_or_create_collection` + `run_ingest_horton`) following the §6.4 pseudocode; cursor discipline mirrors `run_ingest_meridian` (whole-drain, process-all, advance-once, hold-on-failure).
- [ ] **Step 4: Register the handler** — add to `worker/src/main.py` dispatch: `elif job["type"] == "ingest_horton": from .jobs.ingest_horton import run_ingest_horton; await run_ingest_horton(job, db_path)`.
- [ ] **Step 5: Run tests** — `cd worker && pytest tests/test_ingest_horton.py -v` → PASS.
- [ ] **Step 6: Commit** — `feat(horton): ingest job — per-chunk upsert into per-(mic,day) collections`

---

### Task 5: Route — `POST /ingest/horton`

**Files:**
- Modify: `orchestrator/src/models.py` (add `HortonIngestRequest`)
- Modify: `orchestrator/src/routes/ingest.py` (add the route, near `/ingest/meridian`)
- Test: `orchestrator/tests/test_ingest_horton_route.py`

**Interfaces:**
- Consumes: `require_writable`, `store.jobs.create`, `store.specs.get_general`/`create`, `GENERAL_TEXT_SPEC` (as `/ingest/meridian` uses them); `run_ingest_horton` job type `"ingest_horton"` (Task 4).
- Produces: `class HortonIngestRequest(BaseModel)` with `server_url: str = "http://localhost:8080"`, `spec_id: str | None = None`; route returns `{"job_id": ...}`.

- [ ] **Step 1: Write the failing test** (test client + fresh workspace, per `conftest.py`):

```python
def test_ingest_horton_enqueues_job(client):
    r = client.post("/ingest/horton", json={"server_url": "http://x:8080"},
                    headers={"X-Workspace-Id": "ws1"})
    assert r.status_code == 202
    assert "job_id" in r.json()
    # a job of type ingest_horton is queued with server_url in config
```

- [ ] **Step 2: Run to verify it fails** — `cd orchestrator && pytest tests/test_ingest_horton_route.py -v` → FAIL (404 / no route).
- [ ] **Step 3: Implement** — add `HortonIngestRequest` to `models.py`; add `@router.post("/ingest/horton", status_code=202)` to `ingest.py`: resolve/seed the general spec (reuse the meridian block), create a `"ingest_horton"` job with `{server_url, spec_id}`, return `{job_id}`. Workspace comes from the `X-Workspace-Id` header (standard dependency), not the body.
- [ ] **Step 4: Run tests** — `cd orchestrator && pytest tests/test_ingest_horton_route.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat(horton): POST /ingest/horton route (idempotent, resumes from cursor)`

---

## Notes for the executor

- **Dedicated noosphere** ("Horton Transcripts") is created by the user via the `create_noosphere` MCP tool before first ingest; it is not part of this plan's code.
- **Seed reload** (provisional dataset) is a manual maintenance step, not a route: `DELETE` collections `WHERE path LIKE 'horton/seed:%'` and their documents, clear the `horton_cursor` row, then re-POST. Documented only; do not build a reset endpoint.
- Do **not** touch `upsert_document`, `recompute_cooccurrence`, or the co-occurrence gate — `horton_transcript` emits by default (only `agent_report` is excluded).
