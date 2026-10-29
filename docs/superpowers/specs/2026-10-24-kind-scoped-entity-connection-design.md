# Kind-Scoped Entity Connection — Design

**Evolves:** `docs/superpowers/specs/2026-08-18-source-silos-provenance-design.md` (#50/#79).
**Date:** 2026-10-24
**Status:** approved (conversation + read-only simulation), building.

## 1. Problem

Per-source silos made normalization scope **per-silo**, and a one-off `/ingest/repo` makes each repo its own silo. Result: the same concept fragments into a separate entity per repo, with **zero cross-repo connectivity**. You cannot "click `openai` and see every repo that uses it" — the graph's core value. Measured on the Code Repos noosphere (6 repos): 4,734 entities, 21k within-repo co-occurrence edges, **0 entities span ≥2 repos**, **0 collection↔collection edges** → six disconnected islands. The graph is unusable for cross-repo questions.

The 2026-08-18 design was right about the *expensive* error (false merge across genuinely different contexts — "Mercury" god vs element) but drew the boundary at the wrong granularity (per-repo) and left the only cross-silo bridge as a heavyweight human-reviewed destructive merge. Entity-resolution practice (see research notes in the plan) says: a deterministic **exact key auto-resolves the easy majority within a scope**, and only *fuzzy* or *cross-context* matches need review — because false **splits** are cheap and false **merges** are expensive, so automate only the near-zero-false-positive tier.

## 2. The decision: the boundary that matters is `kind`, not the individual silo

Re-scope **exact-name** auto-connection from **silo** to **`kind`** (within a noosphere). Same provenance kind = same context (all `neutral_summary` code is "the same kind of thing"); different kind = the real disambiguation boundary (`openai` the API in code vs `openai` the company in a human vault vs an agent's claim).

**The resolution rule (replaces §3 of the silos design for the exact tier):**
- **Exact canonical match** — same `canonical_name` **and** `type` **and** `kind` → **auto-connect (merge)**, across silos. This is the near-zero-false-positive tier; it is what makes `openai` one node spanning every code repo.
- **Fuzzy / near match within a kind** (embedding similarity, review band) → **human review queue** (`normalization_review_queue`), as today. Not auto.
- **Any cross-`kind` match** (exact or fuzzy) → **propose to `graph_issues`** (human-gated; snapshot-reversible via `apply_merge`/`rollback_merge`), never auto. This is the surviving strict boundary — the one the silos design was really protecting.
- **Loose / null-kind docs** (`/ingest`, `/ingest/directory`, `silo_id IS NULL`) → unchanged (normalize among themselves).

`kind` is read via the existing `silo_kind` view (`documents.silo_id → {collections,watched_sources}.provenance_kind`). An entity's kind-set is the kinds of its source docs' silos (parallel to the existing silo-set logic in `worker/src/normalizer.py`).

## Why this is safe (simulation, read-only, Code Repos)
Exact-name-same-kind connect would collapse only **194 / 4,734 entities** (surgical), create **164 cross-repo concept bridges** (25 spanning ≥3 repos), and connect **all 6 repos into one component**. Provenance is preserved (merge re-points `entity_sources`, never deletes source docs → the one `openai` still knows every repo). The auto/backfill merges are **logged** (`normalization_log`) and re-attach on re-ingest via `merge_map` (same machinery as the existing batch auto-merge); they are **not snapshot-rollback** (`rollback_merge`) reversible. Snapshot-reversal applies only to human-gated proposed merges.

## 3. Scope of change — the auto-merge paths (from the silos design §3.1)

The silos design silo-scoped FIVE paths. This change re-scopes the **exact-name** sub-check in each from *silo* to *kind*, and routes cross-kind to proposal. Fuzzy/embedding auto-merge stays **silo-scoped** (conservative — only exact widens to kind); same-kind-cross-silo fuzzy goes to review, cross-kind fuzzy proposes.

1. **`worker/src/jobs/upsert_document.py::extract_document_entities`** (primary inline) — the exact `canonical_name = ? AND type = ?` lookup + the `merge_map` short-circuit: match within **kind** (any silo of the same kind), not just the doc's own silo.
2. **`orchestrator/src/pipeline/normalizer.py::normalize_entity`** (loose/null path) — unchanged in effect (null kind pool).
3. **`worker/src/normalizer.py::run_batch_normalization`** (faiss) — Tier-1 plural collapse + the **exact-name** collapse: scope by kind. The faiss **fuzzy** candidate search stays silo-auto; same-kind-cross-silo fuzzy → review; cross-kind → propose (existing `_propose_cross_silo_merge`, extended to "no kind in common" as the propose trigger instead of "no silo in common").
4. **`orchestrator/src/pipeline/embedding_normalizer.py::run_batch_normalization`** (O(n²)) — same re-scoping, independently (separate algorithm; its own test).
5. **`worker/src/jobs/extract_batch_image.py`** inline fuse — same exact-name re-scoping (latent for images; keep consistent).

**Helper:** add a `kind_match(entity_col)` SQL fragment alongside the existing `silo_match` in the **mirrored** `silo.py` (worker + orchestrator, byte-identical; `test_schema_mirror` enforces). It asserts the entity has a source in a silo whose `silo_kind.kind` equals a bound kind. The exact-name paths use `kind_match`; the propose trigger uses "kinds disjoint."

## 4. Backfill (existing data)

A one-time worker job `connect_exact_within_kind` (config `{}` or scoped to a collection set): group active entities by `(canonical_name, type, kind)`; for each group with ≥2 distinct entities, **auto-merge** them into one survivor using the worker `_merge_entities` (logged to `normalization_log`, merge_map re-attach; not snapshot-reversible), re-pointing `entity_sources`; recompute co-occurrence for affected entities; recount; mark graph dirty. Exact-name only (no embeddings → cheap, deterministic). Idempotent (a re-run finds nothing new). Triggered by `POST /normalize/connect-within-kind` (or fold into `POST /normalize`). Cross-kind and fuzzy are untouched by the backfill.

## 5. Reads / graph

No read-surface change needed: once entities span repos, the existing `get_collection_routes` (collection-scope co-occurrence) and `get_trade_routes` auto-derive the cross-repo edges, and the graph snapshot rebuild surfaces them. (Note the known secondary-workspace snapshot-rebuild staleness — force a rebuild after backfill; file the bg-task-per-workspace gap separately.)

## 6. Non-goals
- No fuzzy auto-merge across silos (stays review). No cross-kind auto (stays propose). No per-noosphere policy knob. No stop-list/weighting of generic concepts (separate quality pass). No canonical/`same_as` non-collapsing link layer (merge reuses existing machinery; the link layer is a documented future alternative if exact-same-kind merges prove regrettable).

## 7. Testing
- Exact-name same-kind across two silos → ONE entity (the core new behavior); within one silo unchanged.
- Cross-**kind** exact name (e.g. a `neutral_summary` repo and a `human_vault` with the same name) → NOT merged, a `graph_issues` proposal filed.
- Fuzzy same-kind cross-silo → review queue, not auto.
- `merge_map` + plural-collapse leaks re-scoped to kind (parallel to the silos-design tests, now kind-boundary).
- Both batch normalizers, separately.
- Backfill: seed exact-name dups across silos of one kind → run → one survivor spanning both, `entity_sources` preserved, logged; re-run = no-op.
- Mirror: `kind_match` byte-identical in both `silo.py` (test_schema_mirror).
