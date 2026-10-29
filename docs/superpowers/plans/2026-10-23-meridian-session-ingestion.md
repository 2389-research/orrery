# Meridian Session Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest Claude Code sessions from meridian's pull API into a dedicated "Code Sessions" noosphere as subagent-aware, incrementally-growing `agent_report` summary trees.

**Architecture:** A new `POST /ingest/meridian` route (idempotent, two-phase, mirrors `/ingest/ccvault`) enqueues a worker `ingest_meridian` job. The job pulls changed sessions (`updated_after` cursor), and per session builds `session group → per-agent groups (linked at the spawning tool call) → per-agent segment leaves`, reusing a shared featurizer extracted from the ccvault flow. On a `revision` bump it re-derives structure but **memoizes each segment's summary by content hash**, so only new/changed segments cost an LLM call. Cadence is external (office-display), not in orrery.

**Tech Stack:** FastAPI, SQLite (WAL), `orrery_relay`, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-meridian-session-ingestion-design.md`

## Global Constraints

- New schema tables go in **both** `orchestrator/src/db.py` and `worker/src/db.py`, **and** in `_MIRRORED_TABLES` in `orchestrator/tests/test_schema_mirror.py` (CI fails otherwise).
- `FLOW_DEFAULT_KIND` is **byte-identical** in `orchestrator/src/pipeline/silo.py` and `worker/src/silo.py`; add the `"meridian"` key to both.
- Meridian docs are **`agent_report`**: pass `silo_id=collection_id` on every `documents.create`, and `emits_cooccurrence=0` on every `document_collections` row (co-occurrence is gated on the silo regardless).
- Store **summaries, not raw transcripts**. `active_work` entity-linking stays **OFF** (not built here).
- Summaries are **bounded, non-agentic** `relay.complete` calls using the **extraction** model; the single per-session classify uses the **classification** model. Use `Relay.from_settings(settings)`.
- Dedupe meridian records by **`uuid`**; `session_key` contains a slash — **percent-encode** it in paths.
- Tests use **file-backed** SQLite (`tmp_path`), never `:memory:`. Worker/orchestrator tests that touch faiss/libomp run in Docker; the tasks below are pure-SQLite/HTTP-mock and run natively.

## Review Focus

- **Subagent with a missing/unknown `parent_tool_use_id`** → attach its group under the session root and set a `parent_unresolved` flag; never guess a parent, never crash. (Task 4.)
- **Same `revision` re-pulled** → full no-op: no duplicate docs, no LLM calls. (Task 5.)
- **Inherited (resume/fork) records sharing a `uuid` across sessions** → ingested once; the second session skips them. (Task 2.)
- **Empty session or empty model output** → do **not** advance the watermark (retry next pass), matching ccvault. (Task 4.)
- **A session whose tail grew** → only the changed/new segment is re-summarized; the superseded leaf is invalidated, not duplicated. (Task 5.)
- **Meridian HTTP error mid-pull** → sessions already committed stay; the `updated_after` cursor is **not** advanced past unprocessed sessions. (Task 4's job loop advances `meridian_cursor.updated_after` only after a page of sessions is fully processed.)

---

### Task 1: Schema, silo kind, request model

**Files:**
- Modify: `orchestrator/src/db.py`, `worker/src/db.py` (add tables beside the `ccvault_*` block)
- Modify: `orchestrator/src/pipeline/silo.py`, `worker/src/silo.py` (`FLOW_DEFAULT_KIND`)
- Modify: `orchestrator/src/models.py` (new request model)
- Modify: `orchestrator/tests/test_schema_mirror.py` (`_MIRRORED_TABLES`)
- Test: `orchestrator/tests/test_meridian_schema.py`

**Interfaces — Produces:**
- Tables: `meridian_sessions(session_key TEXT PRIMARY KEY, collection_id TEXT, last_revision INTEGER, group_doc_id TEXT, lifecycle TEXT, classified_final INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)`; `meridian_segments(session_key TEXT, segment_hash TEXT, agent_id TEXT, seq INTEGER, summary TEXT, leaf_doc_id TEXT, PRIMARY KEY(session_key, segment_hash))`; `meridian_cursor(collection_id TEXT PRIMARY KEY, updated_after TEXT)`.
- `MeridianIngestRequest(workspace_id: str, server_url: str = "http://localhost:8080", name: str = "Code Sessions")` in `models.py`.

- [ ] **Step 1: Write the failing test** `orchestrator/tests/test_meridian_schema.py`:
```python
def test_meridian_tables_exist(test_db):
    conn = get_connection(test_db)
    for t in ("meridian_sessions", "meridian_segments", "meridian_cursor"):
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()

def test_meridian_flow_kind_is_agent_report():
    from src.pipeline.silo import flow_default_kind
    assert flow_default_kind("meridian") == "agent_report"
```
- [ ] **Step 2: Run** `cd orchestrator && pytest tests/test_meridian_schema.py -v` → FAIL (no such table).
- [ ] **Step 3: Implement.** Add the three `CREATE TABLE IF NOT EXISTS` statements to the SCHEMA string in both `db.py` files (beside `ccvault_*`); add `"meridian_sessions","meridian_segments","meridian_cursor"` to `_MIRRORED_TABLES`; add `"meridian": "agent_report"` to `FLOW_DEFAULT_KIND` in both `silo.py`; add `MeridianIngestRequest` to `models.py`.
- [ ] **Step 4: Run** `pytest tests/test_meridian_schema.py tests/test_schema_mirror.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(meridian): schema, agent_report flow kind, ingest request model`.

### Task 2: `meridian_reader` — pull API + session/agent/segment shaping

**Files:**
- Create: `worker/src/jobs/meridian_reader.py`
- Test: `worker/tests/test_meridian_reader.py`

**Interfaces — Produces:**
- `make_fetch(base_url: str) -> Callable[[str, dict|None], dict]` — default httpx GET returning parsed JSON; tests inject their own `fetch`.
- `list_changed_sessions(fetch, cursor: str|None) -> tuple[list[dict], str|None, bool]` — pages `/v1/sessions?updated_after=`; returns `(items, next_cursor, has_more)`.
- `get_session(fetch, session_key: str) -> dict` ; `get_agents(fetch, session_key) -> list[dict]` (main first).
- `get_records(fetch, session_key) -> list[dict]` — pages `/records` from the start; **dedupes by `uuid`**; drops `inherited: true`.
- `iter_agent_segments(records: list[dict], agents: list[dict], target_chars=3000) -> list[dict]` — per-agent ordered segments; each: `{agent_id, agent_type, parent_tool_use_id, spawn_depth, seq, text, content_hash}` where `content_hash = sha256(text)`. (Segmentation logic parallels `ccvault_reader.iter_segments`; boundaries may shift freely — memoization keys on `content_hash`, so shifts are cheap.)

- [ ] **Step 1: Write failing tests** (feed canned dict responses via a fake `fetch`; no network):
```python
def test_dedupes_records_by_uuid_and_drops_inherited():
    recs = get_records(fake_fetch({...two pages, one dup uuid, one inherited...}), "c1/s1")
    assert [r["uuid"] for r in recs] == ["u1", "u2"]          # dup u1 once, inherited dropped

def test_segments_carry_stable_content_hash_and_agent_link():
    segs = iter_agent_segments(RECORDS, AGENTS)
    main = [s for s in segs if s["agent_id"] == "main"]
    sub  = [s for s in segs if s["agent_id"] == "a1"]
    assert sub and sub[0]["parent_tool_use_id"] == "tu9" and sub[0]["spawn_depth"] == 1
    assert all(len(s["content_hash"]) == 64 for s in segs)
    assert iter_agent_segments(RECORDS, AGENTS)[0]["content_hash"] == segs[0]["content_hash"]  # deterministic

def test_list_changed_sessions_pages_until_has_more_false():
    items, cur, more = list_changed_sessions(fake_fetch_two_pages(), None)
    assert len(items) == 3 and more is False
```
- [ ] **Step 2: Run** `cd worker && pytest tests/test_meridian_reader.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `meridian_reader.py` per the Interfaces. Use `httpx` in `make_fetch` only; all parsing works on dicts. `raw` field of a record is the transcript line used for segment text (fall back to `content`); build per-agent event lists keyed by the record's agent/stream, then segment each like `ccvault_reader.iter_segments`.
- [ ] **Step 4: Run** `pytest tests/test_meridian_reader.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(meridian): pull-API reader — sessions, agents, uuid-deduped records, hashed segments`.

### Task 3: Extract the shared session featurizer

**Files:**
- Create: `worker/src/jobs/session_featurizer.py`
- Modify: `worker/src/jobs/ingest_ccvault.py` (import from the new module)
- Test: `worker/tests/test_session_featurizer.py` (+ existing `worker/tests/test_ingest_ccvault*.py` must stay green)

**Interfaces — Produces (moved verbatim from `ingest_ccvault.py`, behavior unchanged):**
- `summarize_segment(relay, model, context: str, segment_text: str) -> str`
- `summarize_rollup(relay, model, context: str, segment_summaries: list[str]) -> str`
- `write_doc(conn, collection_id, content, content_type, role, parent_path, status, title, dom, metadata: dict|None=None) -> str` (adds an optional `metadata` JSON arg written to `documents.metadata`; ccvault passes `None`).
- `upsert_domain(conn, dom)` ; `parent_of(dom)`.

- [ ] **Step 1: Write the failing test**:
```python
def test_write_doc_persists_metadata_json(test_store):
    did = write_doc(test_store.conn, "c1", "body", "session_intent", "group", None, "classified", "T", "d/x", {"cwd": "/r"})
    row = test_store.conn.execute("SELECT metadata FROM documents WHERE id=?", (did,)).fetchone()
    assert json.loads(row["metadata"])["cwd"] == "/r"
```
- [ ] **Step 2: Run** `cd worker && pytest tests/test_session_featurizer.py -v` → FAIL.
- [ ] **Step 3: Implement.** Move the helpers out of `ingest_ccvault.py` into `session_featurizer.py`; have `ingest_ccvault.py` import them; add the optional `metadata` arg to `write_doc` (write it into the existing `documents.metadata` column; `NULL` when absent).
- [ ] **Step 4: Run** `pytest tests/test_session_featurizer.py tests/test_ingest_ccvault*.py -v` → PASS (ccvault unchanged).
- [ ] **Step 5: Commit** `refactor(sessions): extract shared featurizer (summary/write_doc/domain) from ccvault`.

### Task 4: `ingest_meridian` — backfill a session as a subagent-aware tree

**Files:**
- Create: `worker/src/jobs/ingest_meridian.py`
- Modify: `worker/src/main.py` (dispatch `ingest_meridian`)
- Test: `worker/tests/test_ingest_meridian.py`

**Interfaces — Consumes:** Task 2 reader, Task 3 featurizer. **Produces:** `async def run_ingest_meridian(job: dict, db_path: str) -> None`. Job `config`: `{collection_id, spec_id, server_url}`.

Per changed session (first sight → backfill): build the agent tree; write `group` doc for the session rollup (`content_type="session_intent"`, `status="classified"`), then one `group` doc per agent with `parent_path = session title` (main) or the parent agent's title (subagents), and `metadata` = `{agent_id, agent_type, description, parent_tool_use_id, spawn_depth, workflow_id, relation, rollup}`; then per-agent `leaf` docs (`parent_path = agent title`) summarizing each segment. Session-level group `metadata` = `{session_key, contributor, cwd, git_branch, lifecycle, started_at, last_activity_at, pr_links, rollup}`. Classify once on the session rollup; whole tree shares the domain. Commit the session's docs + `meridian_sessions(last_revision)` + `meridian_segments` rows in one transaction. Empty session or empty model output → skip without writing `meridian_sessions` (retry next pass).

- [ ] **Step 1: Write failing tests** (fake `fetch` + a fake relay returning deterministic text; a `tmp_path` workspace):
```python
async def test_backfill_writes_session_agent_and_leaf_docs(test_store, fake_meridian, fake_relay):
    await run_ingest_meridian(job, test_store.db_path)
    roles = test_store.conn.execute("SELECT role, COUNT(*) FROM document_collections GROUP BY role").fetchall()
    # one session group + one group per agent + one leaf per segment
    assert dict(roles)["group"] == 1 + N_AGENTS and dict(roles)["leaf"] == N_SEGMENTS

async def test_subagent_group_links_to_spawn_tool_use(test_store, fake_meridian, fake_relay):
    await run_ingest_meridian(job, test_store.db_path)
    meta = _agent_group_metadata(test_store, agent_id="a1")
    assert meta["parent_tool_use_id"] == "tu9" and meta["spawn_depth"] == 1

async def test_missing_parent_link_flags_not_guesses(test_store, fake_meridian_orphan, fake_relay):
    await run_ingest_meridian(job, test_store.db_path)
    assert _agent_group_metadata(test_store, agent_id="a1")["parent_unresolved"] is True

async def test_empty_model_output_does_not_watermark(test_store, fake_meridian, empty_relay):
    await run_ingest_meridian(job, test_store.db_path)
    assert test_store.conn.execute("SELECT COUNT(*) FROM meridian_sessions").fetchone()[0] == 0
```
- [ ] **Step 2: Run** `cd worker && pytest tests/test_ingest_meridian.py -v` → FAIL.
- [ ] **Step 3: Implement** `run_ingest_meridian` (parallels `run_ingest_ccvault`; subagent-aware tree; `active_work` branch omitted; `silo_id=collection_id`, `emits_cooccurrence=0`). The top-level loop pages `list_changed_sessions(fetch, cursor)` while `has_more`, processes each session, and advances `meridian_cursor.updated_after` **only after each fully-processed page** (a crash replays the page safely). Add the `ingest_meridian` dispatch branch in `worker/src/main.py`. On first sight write all `meridian_segments` rows `(session_key, content_hash, agent_id, seq, summary, leaf_doc_id)`.
- [ ] **Step 4: Run** `pytest tests/test_ingest_meridian.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(meridian): ingest_meridian — backfill sessions as subagent-aware agent_report trees`.

### Task 5: Memoized incremental re-derive on a `revision` bump

**Files:**
- Modify: `worker/src/jobs/ingest_meridian.py`
- Test: `worker/tests/test_ingest_meridian_incremental.py`

**Interfaces — Consumes:** `meridian_sessions`, `meridian_segments` from Task 1/4.

On a session already in `meridian_sessions`: if the pulled `revision <= last_revision`, skip entirely (no-op). If higher, recompute current segments; for each, reuse the cached summary + existing `leaf_doc_id` when `(session_key, content_hash)` is in `meridian_segments` (no LLM, leaf untouched); summarize + write a new leaf only for an unseen `content_hash`; invalidate (set `invalid_at`) leaves whose cached hash is no longer current and delete those cache rows; regenerate the session+agent group rollups (memoize the rollup by a hash of the ordered summary list); bump `last_revision`.

- [ ] **Step 1: Write failing tests** (fake relay that **counts** `complete` calls):
```python
async def test_same_revision_is_noop(test_store, fake_meridian, counting_relay):
    await run_ingest_meridian(job, db)            # backfill
    n = counting_relay.calls
    await run_ingest_meridian(job, db)            # same revision
    assert counting_relay.calls == n             # zero further LLM calls, no new docs

async def test_growth_resummarizes_only_the_new_segment(test_store, fake_meridian_growable, counting_relay):
    await run_ingest_meridian(job, db)            # 3 segments
    before = counting_relay.calls
    fake_meridian_growable.append_segment()       # revision bumps; +1 new tail segment
    await run_ingest_meridian(job, db)
    assert counting_relay.calls - before == 1 + 1 # one new segment + one rollup regen; no re-summary of the 3

async def test_changed_tail_invalidates_old_leaf_not_duplicates(test_store, fake_meridian_growable, fake_relay):
    ...  # leaf count for the session's agent increases by exactly 1; the superseded leaf has invalid_at set
```
- [ ] **Step 2: Run** `cd worker && pytest tests/test_ingest_meridian_incremental.py -v` → FAIL.
- [ ] **Step 3: Implement** the memoized re-derive branch in `run_ingest_meridian`.
- [ ] **Step 4: Run** the incremental tests + Task 4's tests → PASS.
- [ ] **Step 5: Commit** `feat(meridian): memoized re-derive — only new/changed segments re-summarize on a revision bump`.

### Task 6: `POST /ingest/meridian` route (+ cursor commit)

**Files:**
- Modify: `orchestrator/src/routes/ingest.py`
- Test: `orchestrator/tests/test_ingest_meridian_route.py`

**Interfaces — Consumes:** `MeridianIngestRequest`. **Produces:** `POST /ingest/meridian` → `{job_id, collection_id}` (202).

Mirror `/ingest/ccvault`: resolve the target workspace from `request.workspace_id`; get-or-create the one persistent meridian collection (`store.collections.get_by_path(request.name)`; create with `kind="meridian"`, provenance defaulting to `agent_report`; `try/except IntegrityError → re-get_by_path`); create/get the general-text spec like `/ingest/repo`; `store.jobs.create(job_id, "ingest_meridian", collection_id, {"collection_id": collection_id, "spec_id": spec_id, "server_url": request.server_url})`; persist the per-collection `meridian_cursor` row on first create. The job commits the `updated_after` cursor only after a fully-processed page (crash replays the page safely).

- [ ] **Step 1: Write failing tests:**
```python
def test_route_creates_collection_and_queues_job(test_client, test_store):
    r = test_client.post("/ingest/meridian", json={"workspace_id": "default", "server_url": "http://x"})
    assert r.status_code == 202 and set(r.json()) == {"job_id", "collection_id"}
    assert test_store.conn.execute("SELECT 1 FROM jobs WHERE type='ingest_meridian'").fetchone()

def test_route_is_idempotent_on_the_collection(test_client, test_store):
    a = test_client.post("/ingest/meridian", json={"workspace_id": "default"}).json()
    b = test_client.post("/ingest/meridian", json={"workspace_id": "default"}).json()
    assert a["collection_id"] == b["collection_id"]
```
- [ ] **Step 2: Run** `cd orchestrator && pytest tests/test_ingest_meridian_route.py -v` → FAIL.
- [ ] **Step 3: Implement** the route (parallel to the ccvault/repo handlers; `status_code=202`).
- [ ] **Step 4: Run** the route tests → PASS.
- [ ] **Step 5: Commit** `feat(meridian): POST /ingest/meridian route (idempotent, two-phase)`.

### Task 7: Classify on backfill, reclassify once on `ended`

**Files:**
- Modify: `worker/src/jobs/ingest_meridian.py`
- Test: `worker/tests/test_ingest_meridian_classify.py`

On backfill, classify (best-effort, often `lifecycle=active`). On a `revision` bump where the pulled `lifecycle == "ended"` and `meridian_sessions.classified_final == 0`: reclassify on the current rollup, update the tree's `document_domains` + `domains.document_count` transactionally, set `classified_final = 1`. Never reclassify again.

- [ ] **Step 1: Write the failing test:**
```python
async def test_reclassifies_once_when_session_ends(test_store, fake_meridian_endable, classify_spy):
    await run_ingest_meridian(job, db)                 # active → classify #1
    fake_meridian_endable.end()                        # revision bump, lifecycle=ended
    await run_ingest_meridian(job, db)                 # → reclassify #2
    await run_ingest_meridian(job, db)                 # still ended → no reclassify
    assert classify_spy.calls == 2
    assert test_store.conn.execute("SELECT classified_final FROM meridian_sessions").fetchone()[0] == 1
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(meridian): classify on backfill, reclassify once on session end`.

---

## Wrap-up

- [ ] `cd orchestrator && pytest tests/ -q` and `cd worker && pytest tests/ -q` (Docker if faiss/libomp bites) — only the known-unrelated failures remain.
- [ ] Integration spike (manual, optional): `docker compose` up a local meridian, `meridian backfill`, create a "Code Sessions" workspace, `POST /ingest/meridian`, run more sessions, re-POST → verify only changed segments re-summarize.
- [ ] Follow-up (separate repo, not this plan): add the cadence call to `POST /ingest/meridian` in `orrery-office-display` (alongside the existing `/watched-sources/scan-due` nudge).
</content>
