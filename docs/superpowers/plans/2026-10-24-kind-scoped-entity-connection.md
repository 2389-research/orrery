# Kind-Scoped Entity Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Connect the same concept across same-`kind` silos so the graph is navigable ("click `openai`, see every repo that uses it"), by re-scoping **exact-name** auto-merge from per-silo to per-`kind`, while leaving fuzzy (human-reviewed) and cross-kind (proposed) exactly as they are.

**Architecture:** Add a mirrored `kind_match` SQL helper; swap the exact-name sub-check in the auto-merge paths from `silo_match` to `kind_match`; extend the cross-silo propose trigger to fire on **cross-kind** (kinds disjoint) instead of cross-silo. Reuse the existing reversible, provenance-preserving merge (`rollback_merge`/`normalization_log`, `entity_sources` kept). Add a one-time backfill for existing data.

**Spec:** `docs/superpowers/specs/2026-10-24-kind-scoped-entity-connection-design.md`

## Global Constraints
- **`kind_match(entity_col)` goes in BOTH `silo.py` byte-identically** (worker + orchestrator), like `silo_match` — enforced by `orchestrator/tests/test_schema_mirror.py`. Do not diverge.
- **Only the EXACT-name tier changes** (silo→kind auto). Fuzzy/embedding auto-merge stays **silo-scoped**; the within-silo review band → `normalization_review_queue` is unchanged.
- **Cross-kind is the hard boundary:** any match whose two entities share **no kind** → `graph_issues` proposal (never auto). This replaces the old "no silo in common" propose trigger.
- `kind` is read via the existing `silo_kind` view (`documents.silo_id → provenance_kind`). An entity's kind-set parallels the existing silo-set logic (`_entity_silo_set` in `worker/src/normalizer.py`).
- Merge reuses existing machinery (reversible + `entity_sources` preserved). No destructive/naive merge, no transitive union-find over fuzzy edges (exact-key grouping only).
- Tests file-backed sqlite (`tmp_path`), never `:memory:`.
- Do not touch loose/null-kind behavior (`/ingest`, `/ingest/directory`) — unchanged.

## Review Focus
- Exact name in two **same-kind** silos → ONE entity (the core new behavior); same name **within** one silo still → one entity. (Tasks 2+3)
- Exact name across **different kinds** (neutral_summary repo vs human_vault) → NOT merged, a `graph_issues` proposal filed. (Task 3)
- Fuzzy same-kind cross-silo → still NOT auto (review/propose as today), never auto-merged. (Task 3)
- `merge_map` hit and plural-collapse (`agents`→`agent`) re-scoped to kind, not silo — a cross-kind alias/plural does not fuse. (Tasks 2+3)
- Backfill is reversible and idempotent (re-run = no-op); `entity_sources` preserved so the survivor spans every repo. (Task 4)

---

### Task 1: `kind_match` mirrored SQL helper

**Files:** Modify `worker/src/silo.py` + `orchestrator/src/pipeline/silo.py` (byte-identical below ABOUTME); Test: extend `orchestrator/tests/test_schema_mirror.py` + a unit test for the fragment.

**Interfaces — Produces:** `kind_match(entity_col: str) -> str` — a NULL-safe SQL fragment, positional `?`, asserting the entity has ≥1 source in a silo whose `silo_kind.kind IS ?`. Mirror of `silo_match`'s shape but joining `silo_kind`.

- [ ] Step 1: Write failing mirror + unit test (fragment matches an entity sourced in a silo of the given kind; excludes other kinds; NULL-safe).
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Add `kind_match` to both `silo.py` files byte-identically (join `entity_sources es → documents d → silo_kind sk ON sk.silo_id = d.silo_id WHERE es.entity_id = <col> AND sk.kind IS ?`).
- [ ] Step 4: Run mirror + unit test, verify pass.
- [ ] Step 5: Commit.

### Task 2: Re-scope inline exact-name paths to kind

**Files:** Modify `worker/src/jobs/upsert_document.py` (`extract_document_entities`: the exact `canonical_name=? AND type=?` lookup + the `merge_map` short-circuit) and `worker/src/jobs/extract_batch_image.py` (inline fuse); Test: `worker/tests/`.

**Interfaces — Consumes:** `kind_match` (Task 1).

- [ ] Step 1: Write failing tests — two same-kind silos, same entity name via the inline path → one entity; same name in two **different-kind** silos via inline → stays two (inline creates new; no cross-kind reuse); within-silo unchanged. `merge_map` alias created under kind A does not short-circuit a mention whose silo is kind B.
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Swap the exact lookup + merge_map target-scoping from `silo_match` (current silo) to `kind_match` (the ingesting doc's kind, resolved via `silo_kind` for its `silo_id`). Keep "else create new" for no same-kind match.
- [ ] Step 4: Run, verify pass (plus the existing upsert/extract tests still green).
- [ ] Step 5: Commit.

### Task 3: Re-scope both batch normalizers + cross-kind propose

**Files:** Modify `worker/src/normalizer.py` (faiss: Tier-1 plural collapse + exact-name collapse → kind; `_propose_cross_silo_merge` trigger → cross-**kind**; add `_entity_kind_set`/`_kinds_overlap` parallel to the silo helpers) and `orchestrator/src/pipeline/embedding_normalizer.py` (O(n²): same, independently); Tests: `worker/tests/` + `orchestrator/tests/` (each normalizer separately).

**Interfaces — Consumes:** `kind_match` (Task 1).

- [ ] Step 1: Write failing tests (BOTH normalizers, separate): exact-name across two same-kind silos → auto-merged into one; exact name across two **different kinds** → a `graph_issues` proposal, NOT a merge; fuzzy (≥0.85) same-kind cross-silo → NOT auto (review/propose as today); plural `agents`(kind A silo)/`agent`(same kind, other silo) → collapse; plural across kinds → does not.
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: In each normalizer: widen the **exact-name + plural** collapse scope from silo to kind (use the kind-set / `kind_match`); change the propose trigger from "no silo in common" to "no **kind** in common"; leave the fuzzy/embedding auto path silo-scoped and the within-silo review-band → `normalization_review_queue` untouched.
- [ ] Step 4: Run, verify pass (plus existing normalization tests green).
- [ ] Step 5: Commit.

### Task 4: Backfill job + route + run

**Files:** Create `worker/src/jobs/connect_within_kind.py`; Modify `worker/src/main.py` (dispatch `connect_within_kind`), `orchestrator/src/routes/normalize.py` (or `ingest.py`) (`POST /normalize/connect-within-kind`), `orchestrator/src/models.py` if needed; Tests: `worker/tests/` + `orchestrator/tests/`.

**Interfaces — Consumes:** the kind-aware merge (reuse existing `apply_merge`/merge helper + `rollback_merge`/`normalization_log`).

- [ ] Step 1: Write failing tests — seed exact-name duplicate entities across two **same-kind** silos (pre-feature state); run `run_connect_within_kind` → one survivor whose `entity_sources` span both silos' docs, `normalization_log` rows written, reversible; re-run → no-op; cross-kind exact names are NOT merged (left for the propose path). Route test: `POST /normalize/connect-within-kind` (X-Workspace-Id) enqueues the job.
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Implement the job (group active entities by `(canonical_name, type, kind)` via `silo_kind`; for each group with ≥2 distinct entity_ids, merge into one survivor using the existing reversible merge; `recompute_cooccurrence` for affected; recount; `mark_graph_dirty`), the dispatch, and the route. No embeddings; exact + kind only; idempotent.
- [ ] Step 4: Run focused tests, verify pass.
- [ ] Step 5: Commit.
