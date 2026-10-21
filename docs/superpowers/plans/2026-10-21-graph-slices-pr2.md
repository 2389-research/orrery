# Graph Slices PR 2 — Entity reads + graph ops respect the active slice

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make entity reads and graph-ops endpoints count/traverse **within the active slice** (virtual noosphere `auth.slice`, or ad-hoc slice params), including the entity↔entity **in-slice co-occurrence edges** deferred from PR 1.

**Architecture:** `relationships` (co_occurs) has no per-chunk provenance, so in-slice edges can't be read-filtered — they are **recomputed live** from `entity_sources ⋈ _slice_chunks` (the `recompute_cooccurrence` self-join, restricted to the slice). One new **in-slice adjacency** helper is the keystone; it replaces the 4 duplicated co_occurs reads under a slice. Entity counts/sources use `_slice_docs` (matching PR 1's `mention_count` = `COUNT(*)` of in-slice `entity_sources` rows). Unsliced paths are unchanged.

**Tech Stack:** FastAPI, SQLite (WAL), pytest (`tmp_path`/`test_store` fixtures, never `:memory:`).

Builds on PR 1 (merged): `pipeline/slice.py` (`Slice`, `parse_slice`, `ensure_slice_temp_table`→`_slice_chunks`, `ensure_slice_doc_temp_table`→`_slice_docs`), `AuthStore.slice`/`slice_spec`, sliced helpers (`entity_silos(in_slice=)`, `get_collection_weights(in_slice=)`, `documents.get_recent(in_slice=)`, `_exec_in_slice`). Design: `docs/superpowers/specs/2026-10-21-graph-slices-virtual-noospheres-design.md` §4.

**Conventions (from PR 1):** when sliced, the temp table is the ONLY doc-validity filter — drop independent `d.invalid_at IS NULL` joins, don't stack them. Keep entity-active (`e.invalid_at IS NULL`), `emits_cooccurrence`, and silo-kind (`!= 'agent_report'`) gates. `store.conn` must be the connection the temp table is created on (verify `store.conn is store.entities._conn`). Chunk `IN(...)` id lists under the SQLite variable limit.

---

## Scope / boundaries

**In:** `/entities`, `/entities/{id}`, `/entities/{id}/cooccurrences`, `/entities/{id}/star-graph`, `/shared-context`, `/neighborhood`, `/paths`, `POST /subgraph`, `/domain-overview/{path}`, `/domain/{path}/neighbours` — each respects `auth.slice` and accepts ad-hoc slice params. Response echoes the resolved slice when sliced.
**Deferred:** search + MCP parity (PR 3), fold the one-off slices onto the resolver (PR 4), `/timeline`+`/changes` (not being built). `slice_first_seen`/`slice_last_seen` still deferred.

**Count semantics (decide once):** in-slice `mention_count` = `COUNT(*)` of in-slice `entity_sources` rows (matches PR 1 `build_graph_v5`). Document it; don't mix with `COUNT(DISTINCT document_id)`.

---

## Task 1: In-slice co-occurrence adjacency engine (keystone)

**Files:** Create `orchestrator/src/pipeline/slice_edges.py` (or add to `repositories/graph_reads.py`); test `orchestrator/tests/test_slice_edges.py`.

The one reusable primitive. Precondition: caller created `_slice_chunks` (via `ensure_slice_temp_table`) on `conn`.

- [ ] **Step 1 — failing test.** Seed entities A,B,C across chunks: A&B share an in-slice chunk; A&C share only an OUT-of-slice chunk; B&C share an in-slice chunk. Assert `in_slice_adjacency(conn, [A,B,C])` yields edges A-B and B-C (in-slice) but NOT A-C; weights = shared in-slice chunk counts. Assert a human-invalidated A-B edge (`relationships` row with `invalid_at` set) is excluded. Assert an `emits_cooccurrence=0` doc contributes no edge, and an `agent_report`-silo doc contributes none.

- [ ] **Step 2 — run, FAIL.**

- [ ] **Step 3 — implement.** `in_slice_adjacency(conn, entity_ids) -> dict[str, list[tuple[str, int]]]` (entity_id → [(neighbour_id, weight)]). SQL mirrors `recompute_cooccurrence` (`db.py:945-962`) but:
  - `JOIN _slice_chunks sc ON sc.chunk_id = s1.chunk_id` (both sides share the chunk, so one join suffices);
  - DROP the `d1/d2.invalid_at IS NULL` gates (the `_slice_chunks` membership already encodes as-of validity); KEEP `e1/e2.invalid_at IS NULL`, both-side `emits_cooccurrence` gate, and the `silo_kind != 'agent_report'` gate;
  - `s1.entity_id < s2.entity_id`, `COUNT(DISTINCT s1.chunk_id) AS w`, `GROUP BY a,b`;
  - frontier filter `(s1.entity_id IN (ids) OR s2.entity_id IN (ids))`, chunked under the variable limit;
  - exclude human-invalidated pairs: `LEFT JOIN relationships r ON r.type='co_occurs' AND r.invalid_at IS NOT NULL AND ((r.from_entity=a AND r.to_entity=b) OR (r.from_entity=b AND r.to_entity=a))` → keep only where no such row (match `recompute_cooccurrence`'s skip, both orders);
  - return symmetric adjacency (both directions) so callers can look up either endpoint.
  Add a thin `in_slice_cooccurrences(conn, entity_id, limit)` → that entity's neighbours sorted by weight desc, top-`limit` (for `/cooccurrences` + star-graph).

- [ ] **Step 4 — run, PASS.**
- [ ] **Step 5 — commit** `feat(slices): in-slice co-occurrence adjacency engine`.

---

## Task 2: Entity reads sliced — `/entities`, `/entities/{id}`

**Files:** `orchestrator/src/routes/entities.py`, `orchestrator/src/repositories/sqlite_store.py` (`entities.list`, `entities.get`, `entity_sources.get_for_entity`), `interfaces.py`; test `test_entities_slice.py`.

- [ ] **Step 1 — failing tests.** With a slice active (ad-hoc params + a virtual-noosphere header): `/entities` returns only entities with an in-slice source, `source_count` = in-slice mention count, ordered by it; an entity big all-time but absent this-slice drops out. `/entities/{id}` returns in-slice `source_count` and only in-slice sources (time-ordered); an entity with no in-slice source returns 404 (or an explicit empty — pick and test). No-slice behavior byte-identical to today. Response echoes `scope` when sliced.
- [ ] **Step 2 — FAIL.**
- [ ] **Step 3 — implement.** Parse the effective slice: `auth.slice` else `parse_slice(ad-hoc params)`. When sliced, `ensure_slice_doc_temp_table(store.conn, slice)` then:
  - `entities.list(..., in_slice=False)`: when in_slice, the `source_count` subquery joins `_slice_docs` on `es.document_id`, and the outer query filters to entities with ≥1 in-slice source (INNER via EXISTS). Keep `domain_filter`/`job_id` working.
  - `entities.get` / `entity_sources.get_for_entity`: in-slice variants (join `_slice_docs`); `get_for_entity` returns only in-slice sources. 404/empty for no in-slice sources.
  - echo `{"scope": ...}` when sliced (mirror PR 1 /documents shape — bare today's response when unsliced).
- [ ] **Step 4 — PASS**, plus existing `tests/ -k entities` (minus mcp files) green.
- [ ] **Step 5 — commit** `feat(entities): in-slice counts + sources on /entities and /entities/{id}`.

---

## Task 3: `/cooccurrences`, `/star-graph`, `/shared-context` sliced

**Files:** `orchestrator/src/routes/entities.py`, `orchestrator/src/routes/graph_ops.py`, `orchestrator/src/repositories/sqlite_store.py` (`relationships.get_cooccurrences`, `get_star_graph`); test `test_cooccurrence_slice.py`.

- [ ] **Step 1 — failing tests.** Under a this-week slice where A-B co-occur only last week: `/entities/A/cooccurrences` excludes B; a this-week co-occurrence surfaces. `/star-graph` co-entities + per-doc counts + shared-docs reflect the slice. `/shared-context` returns only in-slice shared docs and in-slice shared neighbours. No-slice unchanged.
- [ ] **Step 2 — FAIL.**
- [ ] **Step 3 — implement.** When sliced, route `/cooccurrences` and the star-graph co-entities block through `in_slice_cooccurrences` (Task 1) instead of `relationships.get_cooccurrences`; slice star-graph's other `entity_sources`/`documents` reads (docs L621-628, n_entities L648-657, shared-docs L676-690) via `_slice_docs`. `/shared-context`: slice its doc SQL (L220-236) via `_slice_docs` and use in-slice adjacency for A/B neighbours (L243-244). Echo scope.
- [ ] **Step 4 — PASS**, plus `tests/ -k "star or cooccur or shared"` green.
- [ ] **Step 5 — commit** `feat(slices): in-slice cooccurrences, star-graph, shared-context`.

---

## Task 4: Traversal — `/neighborhood`, `/subgraph`, `/paths`

**Files:** `orchestrator/src/routes/graph_ops.py` (`_get_adjacency` L59-97, neighborhood BFS L133-160, subgraph L420-440, paths BFS L321-350, mention backfill L153-163, `entity_silos` calls L175/L443); test `test_traversal_slice.py`.

- [ ] **Step 1 — failing tests.** Under a slice, neighborhood/subgraph expansion only follows in-slice edges and includes only in-slice entities; paths only traverses in-slice edges (a path that exists only via an out-of-slice edge is not returned). Per-node mention counts are in-slice; `entity_silos` uses `in_slice=True`. No-slice unchanged.
- [ ] **Step 2 — FAIL.**
- [ ] **Step 3 — implement.** When sliced, `ensure_slice_temp_table` + `ensure_slice_doc_temp_table` on `store.conn`, then make `_get_adjacency` (and the `find_paths` inline BFS — refactor it to reuse the sliced adjacency so the logic lives once) use `in_slice_adjacency` (Task 1). Restrict seed/frontier entities to in-slice (an entity with no in-slice source isn't a valid node). Mention backfill joins `_slice_docs`; `entity_silos(conn, ids, in_slice=True)`. Echo scope.
- [ ] **Step 4 — PASS**, plus `tests/ -k "neighborhood or subgraph or path"` green.
- [ ] **Step 5 — commit** `feat(slices): in-slice neighborhood, subgraph, paths traversal`.

---

## Task 5: Domain ops — `/domain-overview/{path}`, `/domain/{path}/neighbours`

**Files:** `orchestrator/src/routes/graph_ops.py` (explore_domain L480-535, domain_neighbours_route L548-574), `orchestrator/src/repositories/graph_reads.py` (`entities_in_domain` L186, `domain_neighbours` L203 / `_domain_neighbours_live` L317); test `test_domain_ops_slice.py`.

- [ ] **Step 1 — failing tests.** `/domain-overview/X?window=1w` counts only in-slice docs/entities for X; related-domains self-join only over in-slice docs. `/domain/X/neighbours` under a slice computes neighbours from in-slice data (not the materialized `domain_edges`). No-slice unchanged (overview from all-time; neighbours from the materialized table).
- [ ] **Step 2 — FAIL.**
- [ ] **Step 3 — implement.** `explore_domain`: slice its doc SQL (L494-499), `entities_in_domain` (in-slice `doc_count`), the related-domains self-join (L519-527) via `_slice_docs`. `domain_neighbours`: when sliced, bypass the materialized `domain_edges` and compute live via a sliced `_domain_neighbours_live` (restrict to `_slice_docs`). Echo scope.
- [ ] **Step 4 — PASS.**
- [ ] **Step 5 — commit** `feat(slices): in-slice domain-overview and domain neighbours`.

---

## Task 6: Ad-hoc params consistency + integration

**Files:** all PR-2 routes; test `test_slice_reads_integration.py`.

- [ ] **Step 1 — tests.** Every PR-2 route accepts the same ad-hoc params (`since`/`until`/`as_of`/`window`/`domain`/`collection`) and, under a virtual noosphere, uses `auth.slice` (ad-hoc ignored when a view is active, consistent with PR 1 `/graph` — document it). Bad window → 400 on each. A single end-to-end test: create a view, hit every PR-2 route with its header, assert each respects the slice. No-param requests to every route remain byte-identical (regression).
- [ ] **Step 2–4 — implement any missing param wiring; green.**
- [ ] **Step 5 — commit** `feat(slices): ad-hoc slice params across entity/graph-op reads`.

---

## Wrap-up
- [ ] Full suite: `cd orchestrator && pytest tests/ -q` (minus the pre-existing `mcp`-import collection failures) + `cd worker && pytest tests/ -q`. Only the known pre-existing failures remain.
- [ ] Final independent pre-merge review over `main..feat/graph-slices-pr2` (as for PR 1 — it caught real blockers).
- [ ] Open PR (base `main`). Note deferred: search+MCP (PR 3), one-off-slice cleanup (PR 4).
