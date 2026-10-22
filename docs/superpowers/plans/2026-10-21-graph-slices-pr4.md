# Graph Slices PR 4 — Fold the last one-off slices onto the shared resolver

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Retire the two remaining bespoke slice/filter code paths — `/graph`'s bare `?silo=`/`?kind=` post-filter and `/collections/{id}/structure`'s hand-written collection-membership query — by routing them through the shared slice resolver (`pipeline/slice.py`), so every read surface slices through one engine.

**Architecture:** The slice engine already treats `silo`/`kind`/`collection` as facets, and `/graph` already routes silo/kind through `parse_slice` + `build_graph_v5(slice=)` **when combined with a time/domain/collection param**. PR 4 removes the last two exceptions: (1) a *bare* `?silo=`/`?kind=` on `/graph` still falls to the legacy `_filter_by_silo_kind` post-filter over the cached snapshot — make it a real slice instead (requires teaching the resolver the `silo=none` null-pool sentinel); (2) `/collections/{id}/structure` builds its doc set with a one-off `dc.collection_id=? AND d.invalid_at IS NULL` query — fold that onto a `Slice(collections=(id,))` resolve.

**Tech Stack:** FastAPI, SQLite, pytest.

Builds on PR 1/2/3 (all merged; `main` @ `dee624a`). Branch: **`feat/graph-slices-pr4`** (already cut off `main`). Design: `docs/superpowers/specs/2026-10-21-graph-slices-virtual-noospheres-design.md`.

## Deliberate behavior change (call out in the PR)
Folding bare `?silo=`/`?kind=` onto the resolver changes `/graph?silo=X` (and `?kind=`) in two intended ways, matching how every other facet already behaves:
- **Node sizes become in-slice.** Legacy `_filter_by_silo_kind` dropped nodes but kept **all-time** `mention_count`/`degree`; the resolver recomputes size from **in-slice** source rows.
- **Membership becomes "has an in-slice source", not "dominant silo == X".** Legacy kept a node only if its *dominant* silo matched (`graph_reads._pick_dominant`); the resolver keeps any entity with ≥1 source in an in-slice (in-silo) document. Response also gains `meta.slice` (like other sliced `/graph` responses).

Everything else (`/collections/{id}/structure`) is behavior-preserving.

## Scope
**In:** `silo=none` sentinel in `_slice_facets`; `/graph` routes bare silo/kind through the slice path; delete `_filter_by_silo_kind`; `/collections/{id}/structure` doc membership via the collection-slice resolver. Update the two pinning tests + add size/membership regression coverage.
**Out (do NOT touch):** entity/document/search/graph-ops routes (already on the resolver); content-type/embedding filters in `search.py` image endpoints, `stats.py`, `domains.py` (not silo/kind/time/domain/collection subsets); the TTL slice-cache machinery; the `get_or_build` snapshot for the no-param full graph.

---

## Task 1: `silo=none` null-pool sentinel in the slice resolver
**Files:** modify `orchestrator/src/pipeline/slice.py` (`_slice_facets`, ~L73-75); test `orchestrator/tests/test_slice_resolver.py` (add cases; this file already tests the resolver).

The legacy post-filter documented `silo="none"` = "the null-silo pool" (docs with `silo_id IS NULL`), distinct from a literal silo id "none" (which never exists — ids are hex/uuid). The resolver must honor the same sentinel so a folded `?silo=none` keeps working, and so the sentinel is consistent for every slice consumer.

- [ ] **Step 1 — failing tests.** In `test_slice_resolver.py`, seed docs: one with `silo_id='sA'`, one with `silo_id=NULL`. Assert `resolve_slice_docs(conn, Slice(silo="none"))` returns exactly the NULL-silo doc id; `Slice(silo="sA")` returns exactly the `sA` doc; and (regression) `Slice(silo="none")` does NOT match a doc whose `silo_id` is the literal string `'none'` if one is inserted (it should match only true NULLs).
- [ ] **Step 2 — FAIL** (`resolve_slice_docs` currently emits `d.silo_id = 'none'`, matching nothing/literal).
- [ ] **Step 3 — implement.** In `_slice_facets`, replace the `if slc.silo:` block with sentinel handling:
  ```python
  if slc.silo:
      if slc.silo == "none":
          clauses.append("d.silo_id IS NULL")
      else:
          clauses.append("d.silo_id = ?")
          params.append(slc.silo)
  ```
  Leave `kind` unchanged (legacy had no null-kind sentinel; a NULL-silo doc already can't join `silo_kind`, so `?kind=…` excludes it, as before).
- [ ] **Step 4 — PASS** (`pytest tests/test_slice_resolver.py -q`).
- [ ] **Step 5 — commit** `feat(slice): silo=none selects the null-silo pool in the resolver`.

## Task 2: `/graph` routes bare `?silo=`/`?kind=` through the slice resolver; delete `_filter_by_silo_kind`
**Files:** modify `orchestrator/src/routes/graph.py` (`get_graph_data` L120-173; delete `_filter_by_silo_kind` L68-117); update `orchestrator/tests/test_graph_silo_filter.py` + `orchestrator/tests/test_graph_slice_route.py`.

- [ ] **Step 1 — update/author tests first.**
  - In `test_graph_slice_route.py`: **replace** `test_legacy_silo_kind_unchanged` (L94) with `test_bare_silo_is_sliced`: `GET /graph?silo=none` now returns a sliced payload — `meta.slice.silo == "none"`, `node_index` present, and only null-silo nodes appear. Keep `test_window_plus_silo_narrows_both` (already correct).
  - Add `test_bare_silo_sizes_are_in_slice`: seed an entity with 2 sources — one doc in silo `sA`, one in `sB`. `GET /graph?silo=sA` → that entity's `mention_count == 1` (in-slice), proving sizes recompute (legacy would have shown 2). Confirm `meta.slice` present.
  - In `test_graph_silo_filter.py`: the membership assertions in `test_silo_filter_returns_only_that_silos_nodes`, `test_silo_none_returns_the_null_silo_pool`, `test_kind_filter_returns_only_that_kind`, `test_silo_and_kind_combine_as_and` still hold (seed is 1 source/entity so in-slice == all-time, dominant == only silo) — **run them to confirm**, and adjust ONLY if the sliced payload shape (added `meta.slice`) breaks an assertion (the ID-set assertions should not). `test_silo_filter_does_not_mutate_the_cached_snapshot` stays valid (sliced path never touches `get_or_build`). Update the file's ABOUTME/docstring: silo/kind are now slice facets, not a post-filter.
- [ ] **Step 2 — FAIL** (`test_bare_silo_is_sliced`, `test_bare_silo_sizes_are_in_slice`).
- [ ] **Step 3 — implement.** In `get_graph_data`: add `silo`/`kind` to the slice-trigger tuple so a bare silo/kind also builds a slice:
  ```python
  if slc is None and any(v is not None for v in
                         (since, until, as_of, window, domain, collection, silo, kind)):
  ```
  (The `raw_spec`/`params` dict already includes `silo`/`kind`.) Remove the trailing `if silo is not None or kind is not None: payload = _filter_by_silo_kind(...)` block and the now-unused `_filter_by_silo_kind` function (L68-117). The no-param path still returns `get_or_build(store)`. Update the `get_graph_data` docstring (the `?silo=`/`?kind=` paragraph) to describe slice semantics.
- [ ] **Step 4 — PASS.** `pytest tests/test_graph_silo_filter.py tests/test_graph_slice_route.py -q` green; then `pytest tests/ -k "graph" -q`.
- [ ] **Step 5 — commit** `refactor(graph): bare ?silo=/?kind= go through the slice resolver; drop _filter_by_silo_kind`.

## Task 3: `/collections/{id}/structure` doc membership via the collection-slice resolver
**Files:** modify `orchestrator/src/routes/graph.py` (`_collection_structure` L220-229); tests `orchestrator/tests/test_collection_structure.py` (should stay green — it's the regression net).

Behavior-preserving fold: the initial doc query (`dc.collection_id=? AND d.invalid_at IS NULL`) is exactly `Slice(collections=(collection_id,))`. Resolve it into `_slice_docs`, then join that for membership + the `invalid_at` gate, while still joining `document_collections` for `role`/`parent_path`.

- [ ] **Step 1 — add one guard test** to `test_collection_structure.py`: `test_structure_excludes_invalidated_docs` — seed a collection with 2 leaf docs, soft-delete one (`UPDATE documents SET invalid_at=<ts>`), assert `total_files == 1` and the invalid doc isn't rendered. (Proves the resolver's `invalid_at` gate is in force after the fold.) Run the existing 7 tests to capture the green baseline.
- [ ] **Step 2 — FAIL/BASELINE.** New test passes against current code too (it's a guard); record all 8 green before refactor, then refactor must keep them green.
- [ ] **Step 3 — implement.** In `_collection_structure`, before the docs query:
  ```python
  from ..pipeline.slice import Slice, ensure_slice_doc_temp_table
  ensure_slice_doc_temp_table(conn, Slice(collections=(collection_id,)))
  ```
  Change the docs query to join `_slice_docs` for membership + validity, keeping the `document_collections` join only for `role`/`parent_path`:
  ```sql
  SELECT d.id, d.title, d.content, d.source_path, dc.role, dc.parent_path
  FROM documents d
  JOIN _slice_docs sd ON sd.doc_id = d.id
  JOIN document_collections dc ON dc.document_id = d.id AND dc.collection_id = ?
  ```
  (`_slice_docs` is already the distinct set of valid docs in the collection, so the result set is unchanged.) Leave the entity-count ranking, module grouping, and connected-collections logic exactly as-is.
- [ ] **Step 4 — PASS.** `pytest tests/test_collection_structure.py -q` (all 8) green.
- [ ] **Step 5 — commit** `refactor(graph): /collections/{id}/structure doc membership via the collection-slice resolver`.

---

## Wrap-up
- [ ] `cd orchestrator && pytest tests/ -q` + `cd worker && pytest -q`. Only the 4 pre-existing unrelated failures remain (test_document_image_columns, 2× test_image_simmer_*, test_ingest_mislabeled_pdf_returns_415) and the mcp files SKIP (no `mcp` locally).
- [ ] Final INDEPENDENT pre-merge review over `main..feat/graph-slices-pr4` (fresh reviewer, crafted context — not session history). Focus: did silo/kind semantics change exactly as intended and no further; is the collection fold truly behavior-preserving; any slice consumer surprised by the `silo=none` sentinel.
- [ ] Open PR (base `main`). Call out the deliberate `/graph?silo=`/`?kind=` size + membership change. Note remaining deferred items (not PR 4): first_seen/last_seen via MCP; multi-noosphere search (#116 §8); in-slice edge-engine perf on a real corpus; redeploy to Mac/Spark.
