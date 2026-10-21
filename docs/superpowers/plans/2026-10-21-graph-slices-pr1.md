# Graph Slices PR 1 — Engine + Virtual Noospheres + /graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a graph slice a read-only *virtual noosphere* — a live, facet-filtered view of a parent noosphere, with mention counts and co-occurrence edges computed **within the slice**, served through `/graph` on demand and cached.

**Architecture:** A `Slice` + resolver turns facets (time/domain/collection/silo-kind) into an in-slice chunk set (a temp table). `build_graph_v5` gains an optional `slice` that constrains every fact query to that set. Virtual noospheres are registry entries resolving to the parent's store with a `Slice` on `AuthStore`; writes are rejected. `/graph` builds sliced payloads on demand, cached per view + data-version.

**Tech Stack:** FastAPI, SQLite (WAL), FAISS, pytest (`tmp_path` file-backed fixture — never `:memory:`).

Spec: `docs/superpowers/specs/2026-10-21-graph-slices-virtual-noospheres-design.md`. Builds on #116/#118 (`pipeline/time_scope.py`).

---

## File Structure

- `orchestrator/src/db.py` + `worker/src/db.py` — `idx_entity_sources_chunk` (mirrored).
- `orchestrator/src/pipeline/slice.py` — **new**: `Slice`, `parse_slice`, `resolve_slice_chunks` (temp table).
- `orchestrator/src/pipeline/graph_v5.py` — `build_graph_v5(store, *, slice=None, ...)`; fact queries join the slice temp table.
- `orchestrator/src/routes/workspace_routes.py` — registry `kind/parent/slice`; `POST /views`.
- `orchestrator/src/dependencies.py` — `AuthStore.slice`; virtual resolution; `require_writable` guard.
- `orchestrator/src/routes/graph.py` — sliced build-on-demand + LRU cache.
- `orchestrator/src/routes/ingest.py` (+ other write routes) — apply `require_writable`.
- Tests: `test_slice_resolver.py`, `test_graph_slice_aggregation.py`, `test_virtual_noosphere.py`, `test_graph_slice_route.py`, `test_graph_slice_benchmark.py`.

---

## Task 1: `idx_entity_sources_chunk` (both schemas)

**Files:** `orchestrator/src/db.py`, `worker/src/db.py`, test `orchestrator/tests/test_schema_mirror.py` (already enforces mirror).

- [ ] **Step 1:** In the static SCHEMA block of BOTH `db.py` files, beside the other `entity_sources`/`documents` indexes, add:
  `CREATE INDEX IF NOT EXISTS idx_entity_sources_chunk ON entity_sources(chunk_id);`
  (Place it identically in both; the programmatic index section at `db.py` ~796 is an alternative, but static-block is simplest and mirror-checked.)
- [ ] **Step 2:** Run `cd orchestrator && pytest tests/test_schema_mirror.py -v` → PASS (mirror intact).
- [ ] **Step 3:** Commit — `feat(db): index entity_sources(chunk_id) for slice co-occurrence`

---

## Task 2: `Slice` + resolver

**Files:** Create `orchestrator/src/pipeline/slice.py`; test `orchestrator/tests/test_slice_resolver.py`.

- [ ] **Step 1: Write failing tests** — facets resolve to the right in-slice chunk set, AND-combined, no-facet → None.

```python
# ABOUTME: Slice resolver turns facets into an in-slice chunk set (#119).
import pytest
from src.db import init_db
from src.repositories.sqlite_store import SQLiteDataStore
from src.pipeline.slice import Slice, parse_slice, resolve_slice_chunks

def _seed(store):
    c = store.conn
    # d_old(alpha, 2026-10-01), d_new(beta, 2026-10-19); one chunk each
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d_old','o','2026-10-01 00:00:00')")
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d_new','n','2026-10-19 00:00:00')")
    c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d_old','alpha',1,1.0)")
    c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d_new','beta',1,1.0)")
    c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES ('c_old','d_old',0,'x')")
    c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES ('c_new','d_new',0,'y')")
    c.commit()

@pytest.fixture
def store(tmp_path):
    p=str(tmp_path/"t.db"); init_db(p); s=SQLiteDataStore(p); yield s; s.close()

def test_time_facet(store):
    _seed(store)
    chunks = resolve_slice_chunks(store.conn, Slice(since="2026-10-15 00:00:00"))
    assert chunks == {"c_new"}

def test_domain_facet(store):
    _seed(store)
    assert resolve_slice_chunks(store.conn, Slice(domains=["alpha"])) == {"c_old"}

def test_facets_and_combined(store):
    _seed(store)
    # beta AND since-15 -> c_new; alpha AND since-15 -> empty
    assert resolve_slice_chunks(store.conn, Slice(domains=["beta"], since="2026-10-15 00:00:00")) == {"c_new"}
    assert resolve_slice_chunks(store.conn, Slice(domains=["alpha"], since="2026-10-15 00:00:00")) == set()

def test_parse_window_preset(monkeypatch):
    # window=today -> since set; parse_slice(None everything) -> None
    assert parse_slice({}) is None
    s = parse_slice({"domain": "alpha"})
    assert s is not None and s.domains == ["alpha"]
```

- [ ] **Step 2:** Run → FAIL (module missing).

- [ ] **Step 3: Implement `slice.py`.**

```python
# ABOUTME: A graph slice (facet filter) and its resolver to an in-slice chunk set (#119).
from __future__ import annotations
from dataclasses import dataclass, field
from .time_scope import resolve_scope, TimeScopeError

_WINDOW_PRESETS = {"today": "-1d", "2d": "-2d", "1w": "-7d", "2w": "-14d", "1m": "-30d", "all": None}

@dataclass(frozen=True)
class Slice:
    since: str | None = None      # resolved SQLite-UTC (see time_scope)
    until: str | None = None
    domains: tuple[str, ...] | None = None
    collections: tuple[str, ...] | None = None
    silo: str | None = None
    kind: str | None = None

def parse_slice(params: dict) -> Slice | None:
    """Build a Slice from request/registry params, or None if no facet is present.
    `window` is a shorthand that maps to `since`."""
    window = params.get("window")
    since, until, as_of = params.get("since"), params.get("until"), params.get("as_of")
    if window and window in _WINDOW_PRESETS and _WINDOW_PRESETS[window]:
        since = _WINDOW_PRESETS[window]
    scope = resolve_scope(since=since, until=until, as_of=as_of)  # raises TimeScopeError
    def _list(v): return tuple(v) if isinstance(v, (list, tuple)) else ((v,) if v else None)
    s = Slice(since=scope["since"], until=scope["until"],
              domains=_list(params.get("domain") or params.get("domains")),
              collections=_list(params.get("collection") or params.get("collections")),
              silo=params.get("silo"), kind=params.get("kind"))
    return s if any([s.since, s.until, s.domains, s.collections, s.silo, s.kind]) else None

def resolve_slice_chunks(conn, slc: Slice) -> set[str]:
    """Return the set of in-slice chunk ids: chunks whose document matches every facet."""
    return {r[0] for r in conn.execute(*_slice_chunk_query(slc)).fetchall()}

def ensure_slice_temp_table(conn, slc: Slice) -> str:
    """Materialise the in-slice chunk ids into a temp table `_slice_chunks(chunk_id)` and
    return its name. The aggregator joins against it (reusable, dodges the variable limit)."""
    conn.execute("DROP TABLE IF EXISTS _slice_chunks")
    conn.execute("CREATE TEMP TABLE _slice_chunks (chunk_id TEXT PRIMARY KEY)")
    sql, params = _slice_chunk_query(slc)
    conn.execute(f"INSERT OR IGNORE INTO _slice_chunks (chunk_id) {sql}", params)
    return "_slice_chunks"

def _slice_chunk_query(slc: Slice) -> tuple[str, list]:
    """SELECT ch.id FROM chunks ch JOIN documents d ... WHERE <facets>. `until` doubles as
    an as-of cutoff (invalid_at > until OR IS NULL), matching #118/documents.list."""
    clauses, params, joins = [], [], []
    # invalid_at gate
    if slc.until:
        clauses.append("(d.invalid_at > ? OR d.invalid_at IS NULL)"); params.append(slc.until)
    else:
        clauses.append("d.invalid_at IS NULL")
    if slc.since:  clauses.append("d.created_at >= ?"); params.append(slc.since)
    if slc.until:  clauses.append("d.created_at < ?"); params.append(slc.until)
    if slc.silo:   clauses.append("d.silo_id = ?"); params.append(slc.silo)
    if slc.domains:
        dom = " OR ".join("dd.domain_path LIKE ? || '%'" for _ in slc.domains)
        joins.append("JOIN document_domains dd ON dd.document_id = d.id")
        clauses.append(f"({dom})"); params.extend(slc.domains)
    if slc.collections:
        ph = ",".join("?" * len(slc.collections))
        joins.append("JOIN document_collections dc ON dc.document_id = d.id")
        clauses.append(f"dc.collection_id IN ({ph})"); params.extend(slc.collections)
    # kind filters via the silo_kind view (task 11a) — join documents.silo_id -> silo_kind.kind
    if slc.kind:
        joins.append("LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id")
        clauses.append("sk.kind = ?"); params.append(slc.kind)
    sql = ("SELECT DISTINCT ch.id FROM chunks ch JOIN documents d ON ch.document_id = d.id "
           + " ".join(joins) + " WHERE " + " AND ".join(clauses))
    return sql, params
```

- [ ] **Step 4:** Run → PASS. (Confirm `silo_kind` view name/columns against `db.py`; adjust if the join differs.)
- [ ] **Step 5:** Commit — `feat(slice): Slice spec + resolver to in-slice chunk set`

---

## Task 3: Sliced aggregator in `build_graph_v5`

**Files:** `orchestrator/src/pipeline/graph_v5.py`; test `orchestrator/tests/test_graph_slice_aggregation.py`; the v5 golden must NOT move (unsliced path unchanged).

**IMPORTANT — what `/graph` edges actually are.** The v5 payload's inline edges are
**domain↔domain** and **collection↔collection** co-occurrence *routes* (`get_trade_routes` /
`get_collection_routes`) plus asserted `collection_edges` (`uses`/`chain_next`). **Entity↔entity
co-occurrence is NOT in `/graph`** — it's `on_demand` via `/entities/{id}/cooccurrences`
(declared in `meta.edge_availability`). So PR 1 slices the *route* self-joins and the entity
*node* counts; the entity-edge in-slice rule lands in PR 2 where those edges are served.

- [ ] **Step 1: Write failing tests** — node counts + domain-route at the slice level.

```python
# ABOUTME: In-slice node counts + domain routes in /graph's build (#119). Entity-edge
# in-slice behavior is PR 2 (/cooccurrences), not here.
from src.pipeline.graph_v5 import build_graph_v5
from src.pipeline.slice import Slice

def _seed(store):
    c = store.conn
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 1)")
    c.execute("INSERT INTO domains (path, document_count) VALUES ('beta', 1)")
    # last week: d1(alpha) & d2(beta) share entity E -> an alpha<->beta route.
    # this week: d3(alpha) mentions E once, no beta doc -> no route, E count=1.
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d1','a','2026-10-05 00:00:00')")
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d2','b','2026-10-05 00:00:00')")
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d3','c','2026-10-19 00:00:00')")
    for d,dom in (("d1","alpha"),("d2","beta"),("d3","alpha")):
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES (?,?,1,1.0)",(d,dom))
    for d in ("d1","d2","d3"):
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'x')",(f"c_{d}",d))
    c.execute("INSERT INTO entities (id,canonical_name,type) VALUES ('E','E','Concept')")
    for d in ("d1","d2","d3"):
        c.execute("INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES ('E',?,?)",(d,f"c_{d}"))
    c.commit()

def test_this_week_slice_node_count(test_store):
    _seed(test_store)
    payload = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    assert payload["node_index"]["E"]["mention_count"] == 1   # only d3 in this week

def test_domain_route_is_sliced(test_store):
    _seed(test_store)
    last_week = build_graph_v5(test_store, slice=Slice(since="2026-10-01 00:00:00", until="2026-10-12 00:00:00"))
    routes_lw = {(e["source"],e["target"]) for e in last_week["edges"] if e["scope"]=="domain"}
    assert ("alpha","beta") in routes_lw          # d1<->d2 share E last week
    this_week = build_graph_v5(test_store, slice=Slice(since="2026-10-15 00:00:00"))
    routes_tw = {(e["source"],e["target"]) for e in this_week["edges"] if e["scope"]=="domain"}
    assert ("alpha","beta") not in routes_tw      # no beta doc this week

def test_unsliced_build_is_unchanged(test_store):
    _seed(test_store)
    assert build_graph_v5(test_store).get("node_index", {}).get("E") is not None
```

- [ ] **Step 2:** Run → FAIL (`slice` kwarg unknown).

- [ ] **Step 3: Implement.** `build_graph_v5(store, *, slice=None, max_render_nodes=...)`. When `slice` is not None, `ensure_slice_temp_table(conn, slice)` once, then:
  - entity query: `JOIN _slice_chunks sc ON sc.chunk_id = es.chunk_id`, `COUNT(*) AS mention_count`; drop entities with 0 in-slice sources; node size = `mention_count`.
  - **routes:** add a slice-aware variant of `get_trade_routes`/`get_collection_routes` (or pass the in-slice doc/chunk constraint) that restricts their self-joins to in-slice documents, preserving each method's existing gates (`invalid_at`, and for collection routes the `emits_cooccurrence` gate). Asserted `collection_edges` (`uses`/`chain_next`): keep only for collections with an in-slice document.
  - domains: in-slice doc counts (`_slice_chunks` → documents → document_domains).
  - reuse the global layout; cap by the in-slice measure.
  Keep the `slice is None` path identical to today (short-circuit before any temp table).

- [ ] **Step 4:** Run the new tests → PASS. Then the golden: `pytest tests/test_graph_v5_contract.py -q` → PASS **unchanged** (if it moved, the unsliced path regressed — fix, don't regenerate).
- [ ] **Step 5:** Commit — `feat(graph): in-slice counts + edges in build_graph_v5(slice=...)`

---

## Task 4: Virtual noospheres — registry, creation, resolution, read-only

**Files:** `orchestrator/src/routes/workspace_routes.py`, `orchestrator/src/dependencies.py`, write routes; test `orchestrator/tests/test_virtual_noosphere.py`.

- [ ] **Step 1: Write failing tests** — create a view; selecting it scopes reads to the parent + slice; a write is rejected.

```python
def test_create_and_resolve_virtual_noosphere(test_client, test_store):
    # parent 'default' exists; create a time view over it
    r = test_client.post("/views", json={"name":"this week","parent":"default","slice":{"window":"1w"}})
    assert r.status_code == 200
    vid = r.json()["id"]
    assert any(w["id"]==vid and w["kind"]=="virtual" for w in test_client.get("/workspaces").json())

def test_write_on_virtual_is_rejected(test_client):
    r = test_client.post("/views", json={"name":"v","parent":"default","slice":{"window":"1w"}})
    vid = r.json()["id"]
    resp = test_client.post("/ingest/text", json={"title":"t","content":"c"},
                            headers={"X-Workspace-Id": vid})
    assert resp.status_code == 400 and "read-only" in resp.json()["detail"].lower()
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement.**
  - `workspace_routes.py`: a `POST /views` endpoint: validate parent exists and `parse_slice(slice)` is not None; append `{"id": uuid, "name", "kind":"virtual", "parent", "slice", "status":"active", "createdAt"}`; `_save_registry`. `list_workspaces` already returns all entries (kind rides along).
  - `dependencies.py`: add `slice: Slice | None = None` to `AuthStore`. In `get_auth_store`, load the registry, find the active id; if `kind=="virtual"`, `store = get_store(workspace_id=entry["parent"])` and `slice_ = parse_slice(entry["slice"])`; else as today. Return `AuthStore(user, store, slice_)`.
  - Read-only guard: `def require_writable(auth: AuthStore = Depends(get_auth_store)) -> AuthStore:` raises `HTTPException(400, f"read-only view; write to parent '{...}'")` when `auth.slice is not None`. Apply to the write routes (`/ingest*`, `/views` writes excluded, corrections, simmer, normalize) by swapping their `get_auth_store` dep for `require_writable`. (Resolve the parent id for the message from the registry entry; stash it on `AuthStore` too, e.g. `parent_id`.)

- [ ] **Step 4:** Run → PASS; also `pytest tests/ -k "ingest or workspace" --ignore=<mcp files>` to confirm writes on real workspaces still pass.
- [ ] **Step 5:** Commit — `feat(views): read-only virtual noospheres (registry + resolution + guard)`

---

## Task 5: `/graph` sliced on demand + cache

**Files:** `orchestrator/src/routes/graph.py`; test `orchestrator/tests/test_graph_slice_route.py`.

- [ ] **Step 1: Write failing tests** — unsliced `/graph` unchanged (from cache); selecting a virtual noosphere returns the in-slice payload; repeated call hits cache; parent ingest invalidates.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement.** In `GET /graph`: if `auth.slice is None`, serve the existing cached snapshot path unchanged. Else build on demand: `build_graph_v5(store, slice=auth.slice)`, wrapped in a small module-level LRU keyed by `(active_workspace_id, data_version)` where `data_version` = the parent's `graph_snapshot` dirty counter / a bump on ingest+corrections. Echo the resolved slice in the response. Add `data_version` bump wherever the parent marks the snapshot dirty (reuse `_set_dirty`/the dirty flag).

- [ ] **Step 4:** Run → PASS; `pytest tests/ -k graph --ignore=<mcp files>` green (unsliced graph unchanged).
- [ ] **Step 5:** Commit — `feat(graph): sliced /graph on demand for virtual noospheres + cache`

---

## Task 6: Synthetic-scale benchmark

**Files:** test `orchestrator/tests/test_graph_slice_benchmark.py` (marked slow / opt-in).

- [ ] **Step 1:** Generate a plausible transcript corpus (e.g. ~3000 docs across ~30 days, N chunks/doc, M entities/chunk with realistic co-occurrence density), then time `resolve_slice_chunks` + `build_graph_v5(slice=...)` for day / week / month windows. Print resolve and aggregate ms and node/edge counts. Assert it completes (no hard budget) and record the numbers in the PR description so we know the ceiling.
- [ ] **Step 2:** Commit — `test(slice): synthetic-scale benchmark for slice resolve + aggregate`

---

## Wrap-up

- [ ] Full suites: `cd orchestrator && pytest tests/ -q` (minus the pre-existing `mcp`-import collection failures) and `cd worker && pytest tests/ -q` (schema mirror). Only the known pre-existing failures remain.
- [ ] Open PR (base `main`): engine + virtual noospheres + `/graph` + benchmark. Record benchmark numbers. Note deferred: graph-ops/entity-read slicing (PR 2), search+MCP (PR 3), one-off-slice cleanup (PR 4).
