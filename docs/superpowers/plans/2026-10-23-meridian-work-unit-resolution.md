# Meridian Work-Unit Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach orrery's best-guess `work_unit` (repository / body of work) + evidence to each
meridian session-group and agent-group doc, as queryable metadata, with a backfill path.

**Architecture:** A pure resolver module extracts repo evidence from data the ingest already loads
(meridian `records` + `agents`/`session` rollups — no disk/git), produces a best-guess unit per agent
(+ a session-level set), and the ingest writes it into the existing doc `metadata` JSON. A separate
worker job + route backfills already-ingested sessions without re-summarizing.

**Tech Stack:** Python 3.12, stdlib only in the resolver; FastAPI route; file-backed sqlite tests.

**Spec:** `docs/superpowers/specs/2026-10-23-meridian-work-unit-resolution-design.md`

## Global Constraints

- **No schema change, no db.py/silo.py mirror edits.** `work_unit` lives inside the existing
  `documents.metadata` JSON. Do not add columns or touch `_MIRRORED_TABLES`/`_MIRRORED_INDEXES`.
- **No filesystem / git / subprocess access.** All evidence from `records` + rollups. The worker
  container and the Spark do not have the repos on disk.
- **Resolver is a pure module** (`worker/src/jobs/work_unit.py`), stdlib only, every function unit-testable
  with plain dicts (no network).
- **Deterministic only.** No mapping table, no ownership remap, no synonym folding, no collection join,
  no cross-noosphere merge. Record `source` (confidence) on every guess. Allow a set of units per agent.
- **Grain = agent.** Tag session-group + agent-group docs. Leaves inherit their agent; do not tag leaves.
- **Normalization:** remote → `host/owner/name` (lowercase host; strip scheme, credentials, `.git`;
  `git@host:owner/name` → `host/owner/name`). Worktree folds to main root. Subproject = first path
  segment of the **main** agent's anchor below its repo root; a subagent in the session's repo gets NO
  subproject (inherit).
- **Signal priority:** `pr` → `git_remote` → `git_name` → `folder`. Anchor = deepest dir holding ≥80%
  of an agent's records.
- Tests use file-backed sqlite (`tmp_path`), never `:memory:`.

## Review Focus

- **Agent with no cwd/records at all** (stub session) → must yield a `folder`/unresolved unit, never
  crash or emit `null` key. (Task 1 test.)
- **Misleading `git_branch`** (three live sessions all report branch `jev-plays-40k` while in unrelated
  repos) → branch must NOT drive the key; cwd/remote/pr do. (Task 1 test.)
- **Monorepo vs nested repo** (`ds-scratch :: jev-plays-40k` is a subproject, but `watcher` nested in
  ds-scratch has its own remote → its own repo, NOT `ds-scratch :: watcher`). (Task 1 test.)
- **Re-derive idempotence** — re-running ingest on an unchanged session must not flip or duplicate the
  stored `work_unit`. (Task 2 test.)
- **Backfill over a session whose docs were written pre-feature** → adds `work_unit`, leaves summaries/
  domains/counts untouched, is re-runnable. (Task 3 test.)

---

### Task 1: `work_unit.py` resolver (pure)

**Files:**
- Create: `worker/src/jobs/work_unit.py`
- Test: `worker/tests/test_work_unit.py`

**Interfaces:**
- Produces:
  - `normalize_remote(url: str | None) -> str | None`
  - `resolve_work_units(agents: list[dict], session: dict, records: list[dict]) -> dict`
    returns `{agent_id: unit_dict}` plus key `"__session__"` → session-level
    `{"key": str, "keys": list[str], "units": list[dict]}`. Each `unit_dict` matches the spec's
    `work_unit` shape (`key, source, repo_remote, repo_root, repo_name, subproject, anchor_dir,
    anchor_share, git_branch, pr_links, units`).

- [ ] **Step 1: Write failing tests** in `test_work_unit.py` covering: normalize_remote cases
  (`git@github.com:2389-research/meadow.git`, `https://github.com/2389-research/meadow`,
  credentials/scheme stripping, `None`); worktree fold; monorepo subproject (`ds-scratch` root with
  anchor `ds-scratch/jev-plays-40k` → `… :: jev-plays-40k`); nested repo with own remote → own repo,
  no subproject; PR-link ground truth wins over cwd; stub agent (no cwd) → `folder` source, non-null
  key; misleading `git_branch` ignored; subagent in session repo inherits (no subproject);
  multi-valued agent (two roots) → `units` has both.
- [ ] **Step 2: Run tests, verify they fail** (`cd worker && pytest tests/test_work_unit.py -v`).
- [ ] **Step 3: Implement `work_unit.py`.** Pure stdlib. Extract per-agent cwd histogram from
  `records` (`raw.cwd`, grouped by `agent_id`), git roots+remotes from
  `raw.serverClassifierContext.context.git_state`, pr_links from agent rollup `outputs.pr_links`
  (fallback session rollup). Anchor = deepest dir ≥80%. Build known-root set across the session; fold
  worktrees (nested root, same/unknown remote). Resolve per priority; set `source`. Subproject only for
  main's anchor below root. Session-level = distinct keys across agents (primary = main's key, else most
  common).
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit** (`feat(work-unit): pure resolver over meridian records + rollups`).

### Task 2: Wire resolver into ingest_meridian

**Files:**
- Modify: `worker/src/jobs/ingest_meridian.py`
- Test: `worker/tests/test_ingest_meridian.py` (add cases; do not weaken existing)

**Interfaces:**
- Consumes: `resolve_work_units` (Task 1).
- In both `_process_session` (first-sight) and `_rederive_session`: compute
  `units = resolve_work_units(agents, session, records)` once; merge `units["__session__"]` into
  `_session_meta(...)` as `work_unit`, and each agent's `units[aid]` into its agent `meta` as
  `work_unit` (in `_agent_meta` and the inline first-sight agent-meta block). Leaves unchanged.

- [ ] **Step 1: Write failing test** — ingest a fake session (fake `fetch`) whose records carry cwd +
  git_state + a PR link; assert the session-group doc metadata has `work_unit.key` and the main
  agent-group doc metadata has `work_unit.source == "pr"` (or `git_remote`). Add a re-derive
  idempotence assertion (second run on same revision → metadata `work_unit` unchanged, no dup docs).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — thread `resolve_work_units` into both paths; merge into the two metadata
  builders. Keep it a single resolve per session (pass the already-loaded `agents`/`records`).
- [ ] **Step 4: Run worker feature tests** (`pytest tests/test_ingest_meridian.py tests/test_work_unit.py -v`).
- [ ] **Step 5: Commit** (`feat(work-unit): tag session/agent docs during meridian ingest`).

### Task 3: Backfill job + route

**Files:**
- Create: `worker/src/jobs/work_unit_backfill.py`
- Modify: `worker/src/main.py` (register `work_unit_backfill` dispatch), `orchestrator/src/routes/ingest.py`
  (`POST /ingest/meridian/work-units`), `orchestrator/src/models.py` (request model if needed)
- Test: `worker/tests/test_work_unit_backfill.py`, `orchestrator/tests/test_ingest_meridian_route.py` (add)

**Interfaces:**
- Consumes: `resolve_work_units` (Task 1), `meridian_reader` (get_agents/get_session/get_records),
  `meridian_sessions` rows.
- Job config `{collection_id, server_url}`. For each session row: pull agents+session+records, resolve,
  `UPDATE documents SET metadata=…` on the existing session-group doc and each agent-group doc
  (`source_path='meridian:{session_key}'`, `role='group'`, `invalid_at IS NULL`), merging `work_unit`
  into existing metadata. No LLM, no re-summarize, no domain/count changes. Idempotent, re-runnable.

- [ ] **Step 1: Write failing tests** — (worker) pre-insert a meridian session + group docs WITHOUT
  `work_unit`, run `run_work_unit_backfill` with a fake fetch, assert `work_unit` now present on session
  + agent group docs and summaries/domain untouched; re-run → no change. (orchestrator) `POST
  /ingest/meridian/work-units` with `X-Workspace-Id` enqueues a `work_unit_backfill` job with config
  keys `{collection_id, server_url}`; 404/400 when no meridian collection for the workspace.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** job + dispatch registration + route + model.
- [ ] **Step 4: Run** worker + orchestrator feature tests for the touched files.
- [ ] **Step 5: Commit** (`feat(work-unit): backfill job + POST /ingest/meridian/work-units`).
