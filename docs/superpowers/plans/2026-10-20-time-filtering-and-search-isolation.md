# Time Filtering + Per-Workspace Search Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the process-global FAISS search index so it is per-workspace (#113), and add a time-window read primitive to `/documents` and `/search` plus `first_seen`/`last_seen` recency fields on graph nodes (trimmed #116).

**Architecture:** Three independent changes. (1) Key the search index cache by `workspace_id` instead of using single module globals. (2) A shared `time_scope` helper parses `since`/`until`/`as_of` and read endpoints filter on `documents.created_at`; search over-fetches from FAISS then post-filters. (3) The graph builder emits `MIN/MAX(created_at)` per entity node. No new endpoints, no aggregation, no co-occurrence recompute.

**Tech Stack:** FastAPI, SQLite (WAL), FAISS, sentence-transformers, pytest (`tmp_path` file-backed fixture — never `:memory:`).

Spec: `docs/superpowers/specs/2026-10-20-time-filtering-and-search-isolation-design.md`

---

## File Structure

- `orchestrator/src/pipeline/search/retrieval.py` — module globals `_entity_view`/`_chunk_view` → dicts keyed by the **main DB file path** (derived from `conn` via `_ws_key(conn)`); semantic channels take the key string. `build_indexes(conn)` signature unchanged.
- `orchestrator/src/pipeline/search/pipeline.py` — `_indexes_ready` bool → `set[str]` of keys; `_ensure_indexes_built(conn)`/`rebuild_indexes_now(conn)`/`search_knowledge_graph(conn, …)` signatures unchanged (derive key internally, thread to channels); `mark_indexes_stale()` → `mark_indexes_stale(conn)`.
- `orchestrator/src/routes/search.py` — **no change** (already passes `store.conn`).
- `orchestrator/src/pipeline/graph_repair.py` — `_mark_search_index_stale()` → `_mark_search_index_stale(conn)`, threaded from the 7 `apply_*`/`rollback_*` call sites (each already has `conn`); calls `mark_indexes_stale(conn)`.
- `orchestrator/src/mcp_server.py` — **no change** (goes through `/search` over HTTP with the workspace header).
- `orchestrator/tests/test_search_invalid_at.py` — update internal-state assertions (`_entity_view` → keyed accessor; `_indexes_ready` bool → set membership) and the `_restore_index_ready_flag` fixture.

**Key choice:** index cache keyed by `conn`'s main DB file (`PRAGMA database_list`). Each workspace is its own SQLite file, so db-path ↔ workspace is 1:1 — functionally the issue's "keyed by workspace id" with zero new plumbing through the corrections/judge path.
- `orchestrator/src/pipeline/time_scope.py` — **new**. Parse/resolve a time window.
- `orchestrator/src/routes/documents.py` — `/documents` gains `since`/`until`/`as_of`/`order`.
- `orchestrator/src/repositories/sqlite_store.py` (+ `interfaces.py`) — documents `list()` gains time/order args.
- `orchestrator/src/pipeline/graph_v5.py` — entity node query gains `MIN/MAX(created_at)`; node dict gains `first_seen`/`last_seen`.
- `orchestrator/src/db.py` + `worker/src/db.py` — index on `documents(created_at)` (keep mirrored).
- Tests: `orchestrator/tests/test_search_workspace_isolation.py`, `test_time_scope.py`, `test_documents_time_filter.py`, `test_search_time_filter.py`, `test_graph_recency_fields.py`.

---

## Task 1: Per-workspace FAISS index (#113)

**Files:**
- Modify: `orchestrator/src/pipeline/search/retrieval.py`
- Modify: `orchestrator/src/pipeline/search/pipeline.py`
- Modify: `orchestrator/src/routes/search.py`
- Modify: `orchestrator/src/pipeline/graph_repair.py`
- Test: `orchestrator/tests/test_search_workspace_isolation.py`

- [ ] **Step 1: Write the failing test** — two workspaces, disjoint corpora, search B after A returns B's entity.

```python
# ABOUTME: Semantic search index is isolated per workspace (#113).
import pytest
from src.pipeline.search import search_knowledge_graph, rebuild_indexes_now, mark_indexes_stale

async def _ingest(store, name):
    # minimal: insert one entity with an embedding so FAISS has content
    ...  # use the store's entity repo + rebuild_indexes_now(store.conn, workspace_id)

@pytest.mark.asyncio
async def test_search_is_isolated_per_workspace(two_workspace_stores):
    a, b = two_workspace_stores            # each a DataStore on its own db file
    await _ingest(a, "alpha_only_entity")
    await _ingest(b, "bravo_only_entity")
    rebuild_indexes_now(a.conn, workspace_id="a")
    rebuild_indexes_now(b.conn, workspace_id="b")
    res = await search_knowledge_graph(b.conn, "bravo", expand=False, workspace_id="b")
    names = [e["name"] for e in res.entities]
    assert any("bravo" in n for n in names)
    assert not any("alpha" in n for n in names)   # A's index must not leak into B

def test_mark_stale_is_scoped(monkeypatch):
    # marking A stale must not force a rebuild of B
    ...
```

- [ ] **Step 2: Run it — expect FAIL** (`search_knowledge_graph`/`rebuild_indexes_now` reject `workspace_id`, or B returns A's hits).

Run: `cd orchestrator && pytest tests/test_search_workspace_isolation.py -v`

- [ ] **Step 3: retrieval.py — key the views by DB path.**

Replace the single-pair globals with dicts keyed by `_ws_key(conn)`. Preserve the "publish the (index, ids) tuple as one value" invariant per entry.

```python
_entity_views: dict[str, tuple] = {}
_chunk_views: dict[str, tuple] = {}

def _ws_key(conn) -> str:
    """Per-workspace cache key = the connection's main DB file path.

    Each workspace is its own SQLite file (factory maps workspace_id -> unique path),
    so the file path is a stable per-workspace identity shared by every connection to
    it — unlike a raw workspace_id, it is derivable from the `conn` every caller already
    passes, so nothing new has to be threaded through the corrections/judge path.
    Falls back to the object id for an anonymous/in-memory conn (tests use file DBs).
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = row[2] if row else ""        # (seq, name, file)
        return path or f"anon:{id(conn)}"
    except Exception:
        return f"anon:{id(conn)}"

def build_indexes(conn) -> dict:            # signature unchanged
    key = _ws_key(conn)
    ...  # build entity_index/entity_ids, chunk_index/chunk_ids as today, then:
    _entity_views[key] = (entity_index, entity_ids)
    _chunk_views[key] = (chunk_index, chunk_ids)
    return {"entities": len(entity_ids), "chunks": len(chunk_ids)}

def search_entities_semantic(query_embedding, key: str, top_k: int = 20):
    index, ids = _entity_views.get(key, (None, []))
    if index is None or index.ntotal == 0: return []
    ...  # unchanged below

def search_chunks_semantic(query_embedding, key: str, top_k: int = 20):
    index, ids = _chunk_views.get(key, (None, []))
    ...
```

- [ ] **Step 4: pipeline.py — per-workspace readiness, key derived from conn.**

```python
from .retrieval import _ws_key   # reuse the one key function

_indexes_ready: set[str] = set()     # keys whose index is built & current
_index_lock = threading.Lock()       # still one lock; serializes builds

def mark_indexes_stale(conn) -> None:            # was ()
    with _index_lock:
        _indexes_ready.discard(_ws_key(conn))

def _ensure_indexes_built(conn) -> None:         # signature unchanged
    with _index_lock:
        key = _ws_key(conn)
        if key not in _indexes_ready:
            build_indexes(conn)
            _indexes_ready.add(key)

def rebuild_indexes_now(conn):                   # signature unchanged
    with _index_lock:
        stats = build_indexes(conn)
        _indexes_ready.add(_ws_key(conn))
        return stats
```

In `search_knowledge_graph` (signature unchanged) compute `key = _ws_key(conn)` once and pass it to `_retrieve_for_subquery(conn, sq, key)` → the two semantic channels. Keep the long concurrency comments; they still apply per entry.

- [ ] **Step 5: the one external signature change.**
  - `routes/search.py`: **no change** — already passes `store.conn`.
  - `graph_repair.py`: `_mark_search_index_stale(conn)` and `mark_indexes_stale(conn)`; thread `conn` from the 7 `apply_*`/`rollback_*` call sites (each already has it).
  - `mcp_server.py`: **no change** (HTTP to `/search`).
  - Update `tests/test_search_invalid_at.py` internal-state assertions + the `_restore_index_ready_flag` fixture (`_indexes_ready` is now a set; copy it).

- [ ] **Step 6: Run tests — expect PASS.** Then full search suite: `cd orchestrator && pytest tests/ -k search -v`

- [ ] **Step 7: Commit** — `fix(search): per-workspace FAISS index cache (#113)`

---

## Task 2: `time_scope` helper

**Files:**
- Create: `orchestrator/src/pipeline/time_scope.py`
- Test: `orchestrator/tests/test_time_scope.py`

- [ ] **Step 1: failing test** — ISO, bare date, relative, `as_of`, and `until`+`as_of` conflict raises.

```python
from datetime import datetime, timezone
from src.pipeline.time_scope import resolve_scope, TimeScopeError

def test_bare_date():       assert resolve_scope(since="2026-10-19")["since"] == "2026-10-19 00:00:00"
def test_iso_z():           assert resolve_scope(until="2026-10-20T00:00:00Z")["until"] == "2026-10-20 00:00:00"
def test_relative(monkeypatch):
    now = datetime(2026,10,20,12,0,0,tzinfo=timezone.utc)
    monkeypatch.setattr("src.pipeline.time_scope._now", lambda: now)
    assert resolve_scope(since="-24h")["since"] == "2026-10-19 12:00:00"
def test_as_of_is_until():  assert resolve_scope(as_of="2026-10-19")["until"] == "2026-10-19 00:00:00"
def test_until_and_as_of_conflict():
    import pytest; pytest.raises(TimeScopeError, resolve_scope, until="2026-10-19", as_of="2026-10-20")
def test_empty_is_none():   assert resolve_scope() == {"since": None, "until": None}
```

- [ ] **Step 2: Run — FAIL** (module missing).

- [ ] **Step 3: implement.** Output format matches SQLite's stored `YYYY-MM-DD HH:MM:SS` UTC. Accept ISO (`T`/`Z`), bare date, and `-?\d+(h|m|d)` relative. Return `{"since": str|None, "until": str|None}` (resolve `as_of`→`until`). Raise `TimeScopeError` on bad input or `until`+`as_of`. `_now()` is a seam for tests.

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `feat(time): shared TimeScope parser`

---

## Task 3: `/documents` time filter

**Files:**
- Modify: `orchestrator/src/repositories/interfaces.py` (documents `list` signature), `orchestrator/src/repositories/sqlite_store.py` (impl)
- Modify: `orchestrator/src/routes/documents.py`
- Modify: `orchestrator/src/db.py` + `worker/src/db.py` (index)
- Test: `orchestrator/tests/test_documents_time_filter.py`

- [ ] **Step 1: failing test** — docs in/out of a window; `order=asc` oldest-first; no-params unchanged; response echoes `scope`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: implement.** `list(limit, offset, since=None, until=None, order="desc")` → `WHERE created_at >= ? / < ?`, `ORDER BY created_at {asc|desc}`. Route parses via `resolve_scope`, returns `{"documents": [...], "scope": {...}}` **only when a time param is given**; with no params return the bare list exactly as today (regression). Add `CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at)` in both `db.py` files (keep mirrored — `test_schema_mirror.py`).
- [ ] **Step 4: Run — PASS**, plus `pytest tests/ -k document -v`.
- [ ] **Step 5: Commit** — `feat(documents): since/until/as_of/order time filter`

---

## Task 4: `/search` time filter

**Files:**
- Modify: `orchestrator/src/routes/search.py`, `orchestrator/src/pipeline/search/pipeline.py`
- Test: `orchestrator/tests/test_search_time_filter.py`

- [ ] **Step 1: failing test** — a query whose best all-time hit is outside the window returns in-window hits instead, still up to `top_k`; no-params unchanged.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: implement.** `search_knowledge_graph(..., since=None, until=None)`. Over-fetch (`top_k * k`, k≈5) then drop chunk hits whose document is out of window and entity hits with no in-window source (one batched query: in-window doc ids → chunk/entity membership). Widen the fetch once if too few survive. Each hit carries its document `created_at`. Route parses via `resolve_scope`, echoes `scope`. No time param ⇒ today's path, untouched.
- [ ] **Step 4: Run — PASS**, plus full `pytest tests/ -k search -v`.
- [ ] **Step 5: Commit** — `feat(search): time-window post-filter`

---

## Task 5: `first_seen`/`last_seen` on graph nodes

**Files:**
- Modify: `orchestrator/src/pipeline/graph_v5.py` (entity node query ~line 130, node dict ~line 157)
- Test: `orchestrator/tests/test_graph_recency_fields.py`

- [ ] **Step 1: failing test** — an entity node carries `first_seen`/`last_seen` = min/max `created_at` of its source docs; entity with no sources → `None`, no crash; v5 contract golden still passes.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: implement.** Extend the entity query with `MIN(d.created_at) AS first_seen, MAX(d.created_at) AS last_seen`, joining `documents d ON es.document_id = d.id AND d.invalid_at IS NULL` (still `LEFT JOIN` so zero-source entities survive). Add `"first_seen"`, `"last_seen"` to the node dict. Additive, non-breaking (viz ignores unknown fields — CLAUDE.md `birthScale` note). **Viz cooling effect is a deliberate follow-up, not this task.**
- [ ] **Step 4: Run — PASS**, plus `pytest tests/test_graph_v5_contract.py -v`.
- [ ] **Step 5: Commit** — `feat(graph): first_seen/last_seen on entity nodes`

---

## Wrap-up (after all tasks)

- [ ] Full suite: `cd orchestrator && pytest tests/ -v` and `cd worker && pytest tests/ -v` (schema mirror).
- [ ] Open PR(s). Suggested: Task 1 as its own PR (#113 fix); Tasks 2–5 as a second PR (trimmed #116), or split 2–4 (time filter) from 5 (recency) if preferred.
- [ ] Update GitHub issues: close/scope-down #113; comment on #116 recording what was built vs. the dropped endpoints/co-occurrence/multi-noosphere, pointing at this spec.
