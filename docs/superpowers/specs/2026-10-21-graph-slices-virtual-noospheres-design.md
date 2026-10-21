# Graph slices as read-only virtual noospheres — design

Date: 2026-10-21
Status: proposed
Issue: #119 (reshaped). Builds on #116 / #118.

## Problem

"A slice of the graph" (by time, domain, collection, source/silo) should be a first-class,
read-only *view* of an existing noosphere — counted **within the slice**, so a "today" view
shows what's active today, not last month's big nodes filtered down. #118 added time filters
on `/documents` and `/search` but deliberately left out scoped `/graph`, scoped graph ops,
and in-slice counts. This picks those up, generalised beyond time, exposed as **virtual
noospheres** rather than params on every endpoint.

## Decisions (from the #119 discussion)

- A slice is a **read-only virtual noosphere**: a registry entry `{id, name, kind:"virtual",
  parent, slice}`. It resolves to the **parent's** store/connection with the slice applied
  on every read — no data copy, parent's entity ids, parent's FAISS index (slice applied
  post-retrieval, reusing #118). Live, never frozen.
- **Reuses the noosphere surface.** `list_noospheres`/`select_noosphere`/`X-Workspace-Id`/the
  UI already address workspaces, so slicing works across every read tool with no per-endpoint
  param — this also delivers the deferred #116 §7 / #118 MCP parity for free.
- **Writes always target the parent.** A write while a virtual noosphere is active is
  **rejected** with "read-only view; write to parent `<id>`" — no silent redirect.
- **In-slice counting** is the point: mention counts and co-occurrence edges computed from the
  slice's chunks only, reusing `recompute_cooccurrence`'s shared-chunk logic and preserving
  its `emits_cooccurrence` gate.
- **Ad-hoc slice params** remain a secondary convenience for one-off queries.

## Facets (v1)

All plain document-facet filters over data we already store; AND-combined:

| Facet | Source | Match |
|---|---|---|
| time (`since`/`until`/`as_of`/`window`) | `documents.created_at` (+ `invalid_at` per TimeScope) | #118 `time_scope` + `window` presets → `since` |
| domain | `document_domains.domain_path` | prefix (`LIKE ? || '%'`) |
| collection | `document_collections.collection_id` | exact, one or more |
| silo / kind | `documents.silo_id` → `silo_kind` view | exact |

**Deferred:** entity-neighborhood facet (`entities`+`hops`) — graph traversal, not a document
filter; `slice_first_seen`/`slice_last_seen` (keep global #118 values); day-partition rollups
(Phase 2); physical freeze/fork. All out until a consumer needs them.

## Architecture

```
registry entry (kind:virtual) ──► resolve: parent store + Slice on AuthStore
Slice ──► resolver (pipeline/slice.py): in-slice chunk-id set (facets AND-combined)
in-slice chunks ──► aggregator: v5 payload with in-slice mention_count + edges + domain sizes
                                 (cached per virtual-id + data-version)
```

### 1. `Slice` + resolver — `orchestrator/src/pipeline/slice.py` (new)

- `Slice` dataclass: `since, until` (resolved UTC via `time_scope`), `domains, collections`
  (lists), `silo, kind`. `window` preset (`today/2d/1w/2w/1m/all`) maps to `since` during parse.
- `resolve_chunk_ids(conn, slice) -> set[str]` **and** `resolve_doc_ids(conn, slice) -> set[str]`:
  the in-slice document set (time+domain+collection+silo/kind, `invalid_at` per TimeScope),
  then chunks whose document is in-slice. Implemented as a reusable **temp table**
  `_slice_chunks(chunk_id)` the aggregator joins against (avoids re-deriving per query and
  dodges the SQLite variable limit for large slices).
- `parse_slice(params) -> Slice | None` shared by HTTP and the registry spec; `None` ⇒ no slice
  ⇒ today's behavior.

### 2. Aggregator — parameterise `graph_v5.build_graph_v5`

`build_graph_v5(store, *, slice=None, max_render_nodes=...)`. With a slice, every fact query
joins the `_slice_chunks` temp table:
- **entity nodes:** `COUNT(*)` of in-slice `entity_sources` rows → `mention_count`; keep an
  entity only if it has ≥1 in-slice source. **size = `mention_count`** under a slice (document
  it); degree is derived from the slice's own edges where needed.
- **edges (what `/graph` actually carries):** the v5 payload's inline edges are
  **domain↔domain** and **collection↔collection** co-occurrence *routes* (`get_trade_routes` /
  `get_collection_routes`), **not** entity↔entity edges — entity co-occurrence is `on_demand`
  via `/entities/{id}/cooccurrences` (declared in `meta.edge_availability`). So PR 1 slices
  those **route** self-joins by restricting them to in-slice documents (parameterise the two
  store methods with the in-slice doc/chunk set). Asserted structural edges (`uses`,
  `chain_next` from `collection_edges`) are not activity-derived; keep them for collections
  that have an in-slice document, otherwise drop. The **entity↔entity** in-slice edge rule
  (`recompute_cooccurrence`'s shared-chunk self-join restricted to the slice, preserving
  `emits_cooccurrence`) lands with **graph ops / `/cooccurrences` in PR 2**, where those edges
  are actually served.
- **domains:** in-slice if they have an in-slice document; size = in-slice doc count (overlap by
  design — never sum per-domain into a total; totals are distinct over in-slice docs).
- **node cap** ranked by the in-slice measure; **layout reused** from the global (positions never
  recomputed per slice — switching windows stays visually stable).
- global `first_seen`/`last_seen` (#118) retained.

Unsliced `build_graph_v5` is byte-for-byte unchanged (slice=None short-circuits).

### 3. `/graph` on demand + cache

- `GET /graph`: unsliced ⇒ the existing cached `graph_snapshot` (unchanged, O(1)). Sliced ⇒
  build on demand via the aggregator, cached in a small LRU keyed by `(virtual_id | resolved
  slice, data_version)`. `data_version` reuses the parent's `graph_snapshot` dirty signal / a
  counter bumped by ingest + corrections, so a stale slice refreshes after parent writes.

### 4. Virtual noospheres — registry + resolution + read-only

- **Registry:** entries gain optional `kind` (`"workspace"` default | `"virtual"`), `parent`,
  `slice` (the raw spec). `list_workspaces` returns them; the UI/MCP list shows both.
- **Creation:** `POST /views {name, parent, slice}` (+ MCP `create_view`) registers one;
  validates the parent exists and the slice parses.
- **Resolution:** `get_auth_store` looks the active id up in the registry; `kind:"virtual"` ⇒
  `get_store(workspace_id=entry["parent"])` and `AuthStore.slice = parse_slice(entry["slice"])`.
  `AuthStore` gains `slice: Slice | None`.
- **Read-only enforcement:** write routes/MCP tools reject when `auth.slice is not None` with
  400 "read-only view; write to parent `<parent>`". One shared guard dependency.

### 5. Index + schema

`CREATE INDEX idx_entity_sources_chunk ON entity_sources(chunk_id)` in **both** `db.py` files
(the slice co-occurrence self-join needs it; existing indexes cover entity + document only).
`test_schema_mirror.py` enforces the mirror.

### 6. Efficiency

On-demand compute + the cache (a materialized-view-with-auto-refresh). No rollups. No hard
latency budget (no real corpus yet) — instead a **synthetic-scale benchmark** (generate a
plausible transcript corpus, time resolve + aggregate for day/week/month slices) so we know the
ceiling and whether Phase 2 (day-partition rollups) is ever warranted.

## Documented behaviors (intentional, not bugs)

- **Domain facet picks documents, so per-domain sizes overlap.** `Slice(domains=["A"])` selects documents tagged `A`; a doc tagged both `A` and `B` still contributes to `B`'s in-slice size and to an `A↔B` route. The facet chooses the document set; it does not strip a doc's other domains. Never sum per-domain sizes into a total — slice totals are distinct over the in-slice documents.
- **`meta.counts` reflect the slice.** Under a slice, `counts`/`counts_by_type` are computed from the emitted sliced nodes, and the `excluded: no_domain_membership` count means "no *in-slice* domain membership" (an entity whose only in-slice source has no domain weight). Internally consistent with what's emitted.
- **As-of validity is single-sourced.** When `until`/`as_of` is set, doc validity is decided once by the resolver (`invalid_at > until OR IS NULL`) and encoded in `_slice_docs`; every sliced measure (counts, domain weights, routes) trusts that membership and does not re-filter `invalid_at`, so an as-of view never disagrees with itself.
- **Asserted `collection_edges` (`uses`/`chain_next`)** are kept only when both endpoint collections have an in-slice document. They are structural, not activity-derived, so their weights are untouched; a link to a collection outside the slice is hidden.

## Testing (acceptance)

- No slice ⇒ exactly today's responses everywhere, `/graph` still from cache. Regression tests.
- In-slice counting fixture: E mentioned 10× last week, 1× this week ⇒ this-week slice shows E
  `mention_count=1` (node count, PR 1). The entity-edge half (E–F co-occur only last week ⇒ no
  E–F edge, E's degree excludes F) is a **PR 2** assertion over `/cooccurrences`/graph ops, where
  entity edges live. PR 1's edge assertion is at the **route** level: two docs in different
  domains sharing an entity last week only ⇒ that domain↔domain route is present in the last-week
  slice, absent in the this-week slice.
- Domain facet: a 2-domain doc appears in both domain slices; slice totals count it once.
- Facets combine (`domain=X & window=1w` = intersection).
- `idx_entity_sources_chunk` in both schemas.
- Cache: repeated identical slice hits cache; parent ingest/correction invalidates it.
- Read-only: a write on a virtual noosphere is rejected naming the parent.
- A month slice on a synthetic fixture is measured and reported.
- `tmp_path` file-backed fixture (CLAUDE.md), never `:memory:`.

## PR split

1. **Engine + virtual noospheres + `/graph` + benchmark** (this plan).
2. Graph ops + entity reads respect the active slice + ad-hoc params.
3. Search + MCP: search respects the active virtual noosphere; slice echoed in output; views list/select cleanly.
4. Cleanup: fold existing one-off slices (`?silo=&kind=`, collection structure, domain overview, star graph) onto the resolver, keeping response shapes.

## Risks

- **Resolution branch is load-bearing** (`get_auth_store`/`validate_workspace` — same layer as
  the #113 index key). Virtual → parent file → parent index → slice applied in aggregation; it
  composes, but needs care.
- **Aggregator rework** touches `graph_v5.py`, the v5 golden (regenerate deliberately; sliced
  path is additive/opt-in so the unsliced golden must not move).
- **`emits_cooccurrence` gate** must be preserved in sliced edges or they diverge from the
  all-time graph's rules.
