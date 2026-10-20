# Time filtering + per-workspace search isolation — design

Date: 2026-10-20
Status: proposed

## Context

Two coupled needs, both much smaller than they first look:

1. **Search bug (#113):** the FAISS semantic indexes are process-global, not
   per-workspace, so in a multi-noosphere orchestrator a search after switching
   `X-Workspace-Id` runs against whichever workspace was searched *first*.
2. **Time filtering (#116):** Orrery stores `created_at` on every document but exposes
   no way to read a slice ("what happened this week / as of last Friday").

This spec deliberately **rejects most of #116**. Orrery is the general, unopinionated
layer; a consumer that wants a "morning brief" / "what's hot today" composes Orrery's
primitives or queries the DB itself. So we add the *filtering primitive* and the *raw
recency fields*, and nothing that aggregates on a consumer's behalf.

### Explicitly dropped from #116 (record of what we are NOT doing)

- **`/timeline` and `/changes` endpoints.** Pure agent-ergonomics sugar (server-side
  bucketing / window-diffing). They add no capability the time filter + recency fields
  don't already give a consumer, and `/changes`'s `new_edges` is the only thing in all
  of #116 that needs expensive in-window co-occurrence recompute. A consumer builds
  these on top of Orrery.
- **In-window co-occurrence weight recompute.** Not built. Scoped reads filter *which*
  entities/edges appear; edge weights remain all-time. This is the one accuracy
  trade-off, taken on purpose.
- **Opt-in multi-noosphere search (#116 §8).** Separate feature; not in scope. (#113,
  which it depends on, *is* in scope.)
- **`as_of` historical replay of merges/renames.** Out, as the issue already said.

## Scope (three small, independent changes)

### 1. #113 — per-workspace FAISS index cache (lands first)

The cache state in `pipeline/search/` is currently single-valued module globals
(`retrieval._entity_view` / `_chunk_view`; `pipeline._indexes_ready`). Key all of it by
`workspace_id`.

- **`retrieval.py`:** replace the two single-pair globals with dicts keyed by
  `workspace_id`; `build_indexes`, `search_entities_semantic`, `search_chunks_semantic`
  take the key. The "publish the (index, ids) tuple as one value" invariant is preserved
  per entry.
- **`pipeline.py`:** `_indexes_ready` becomes a per-workspace set/dict;
  `_ensure_indexes_built`, `mark_indexes_stale`, `rebuild_indexes_now` all take
  `workspace_id`. A single `_index_lock` still serializes builds (simplest correct
  choice; cross-workspace build concurrency is not worth the complexity for this
  deployment). `search_knowledge_graph(conn, ..., workspace_id)` threads the key down
  through `_retrieve_for_subquery` → the semantic channels.
- **Callers:** `routes/search.py` passes `auth.user.workspace_id`;
  `routes/search.py::/search/rebuild` passes it; `graph_repair.py`'s
  `mark_indexes_stale()` caller passes the workspace of the store it just corrected.
- **Invalidation = the "content version":** `mark_indexes_stale(workspace_id)` drops
  that workspace's ready flag only, so a correction or ingest in workspace A never
  forces a rebuild in B. New ingest already marks stale through the repair/normalize
  path; confirm an ingest path also invalidates (so new docs become searchable without
  a manual rebuild) — if not, add the per-workspace mark there.

**Why first:** time-filtering search post-filters whatever the index returns; filtering
the *wrong* workspace's index is meaningless.

### 2. Time filter on `/documents` and `/search`

- **`pipeline/time_scope.py` (new):** one helper. Parses `since` / `until` / `as_of`
  from ISO-8601 (`2026-10-19T00:00:00Z` or bare `2026-10-19`) or relative (`-24h`,
  `-7d`, `-30m`) against server now. Returns a resolved window normalized to the SQLite
  storage format (`YYYY-MM-DD HH:MM:SS`, UTC) so comparisons against `documents.created_at`
  are correct. `as_of` ≡ `until=as_of`; cannot combine with `until`. Every scoped
  response echoes the resolved `scope`.
- **`GET /documents`:** add `since` / `until` / `as_of` and `order=asc|desc` (default
  `desc`, unchanged). Translates to `WHERE created_at >=/< ?` in the documents repo list.
  Add an index on `documents(created_at)` (mirror into `worker/src/db.py`).
- **`GET /search`:** over-fetch (`top_k × k`) from FAISS, then drop chunk hits whose
  document is out of window and entity hits with no in-window source; widen if too few
  survive. Each surviving hit carries its document `created_at`. Response echoes `scope`.
- **Default (no params): byte-for-byte today's behavior.** Regression-tested.

### 3. `first_seen` / `last_seen` on graph nodes (optional recency; enables the viz cooling effect)

The viz already has the visual vocabulary (`birthScale` grow-in, search glow) but no
real timestamps reach it — `birthScale` is a per-session entrance animation, not tied to
`created_at`.

- **`graph_v5.py::build_graph_v5`:** for entity nodes, add `first_seen` / `last_seen`
  from one aggregate — `MIN/MAX(d.created_at)` over `entity_sources`→`documents` where
  `d.invalid_at IS NULL`. Non-breaking additive field (viz ignores unknown fields; see
  CLAUDE.md `birthScale` note).
- **`cosmic-viz.html` (optional follow-up):** decay node brightness/size by age of
  `last_seen` instead of by "appeared this session," reusing the existing glow machinery.

Build pieces 2 and 3 independently of each other; both sit on #113 only where they touch
search.

## Testing

- **#113:** two workspaces with disjoint corpora; a search in B after a search in A
  returns B's entities, not A's. Uses the `tmp_path` file-backed fixture (not `:memory:`
  — see CLAUDE.md). `mark_indexes_stale(A)` leaves B's index intact.
- **Time filter:** `since`/`until`/relative/bare-date/`as_of` parse correctly and echo;
  a `/search` whose best all-time hit is outside the window returns in-window hits
  instead, still up to `top_k`; `GET /documents?order=asc` within a window returns that
  window oldest-first; **no-params requests are unchanged** (regression test).
- **Recency fields:** entity node carries correct `first_seen`/`last_seen`; absent-source
  entity degrades gracefully.

## Risks / non-goals

- Threading `workspace_id` through the search pipeline touches the carefully-commented
  concurrency protocol in `pipeline.py`. Preserve the lock/publish invariants per entry;
  do not collapse them.
- Edge weights stay all-time under a time scope (accuracy trade-off, see dropped scope).
- No new endpoints, no aggregation, no multi-noosphere. Those are consumer concerns.
