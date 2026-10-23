# Meridian Work-Unit Resolution — Design

**Status:** approved (conversation, 2026-10-23), building.
**Line of work:** follows `2026-10-23-meridian-session-ingestion-design.md`. Fills that spec's
open item ("the session↔repo **join key** is left to the builder").
**Branch:** `feat/meridian-work-unit-resolution`, stacked on `feat/meridian-session-ingestion` (#127).

## Intent

For a given code session — and each agent/subagent within it — record orrery's **best guess of
what repository / body of work it was working on**, as queryable metadata, so a downstream service
(meadow first) can answer "which agents are working where" and decide cross-repo / cross-noosphere
matches itself.

## The layering decision (why orrery does *this much* and no more)

This matches standard practice: the **source** (the instrumented system) emits normalized repo
attributes at the finest grain; the **consumer** correlates and groups. (OpenTelemetry GenAI/VCS
semconv, LangSmith/Langfuse/Phoenix span attributes, WakaTime best-guess-plus-override,
Agent-Trace commit attribution.) Orrery is the source.

- **Orrery does:** resolve a best guess per agent, do the *deterministic* normalization
  (remote → `host/owner/name`, worktree folding, monorepo subproject split), carry the evidence and
  a confidence/`source` tag, and allow more than one unit per agent (a set, never a forced winner).
- **Orrery does NOT (now):** collapse variants with a mapping table, remap ownership
  (`ds-scratch :: jev-plays-40k` → `sugi-chan/jev-plays-40k`), fold name synonyms
  (`jev-plays-40k` ≟ `jev-plays-pokemon`), or join to `Code Repos` collections. Those are
  **judgments**, left to meadow.
- **Future:** a cross-noosphere match becomes a first-class, reversible **merge** in the
  meta-noosphere — the same machinery as today's entity merge (`normalization_log` / `invalid_at` /
  judge). meadow is where that pattern is tested first. Not in scope here.

## Goals / non-goals

- **Goal:** best-guess `work_unit` + evidence + `source` on each session-group and agent-group doc;
  multi-valued; backfillable over already-ingested sessions without re-summarizing.
- **Non-goal:** mapping table, ownership remap, synonym folding, collection join, per-leaf (sub-agent
  segment) granularity, cross-noosphere merge. (Agent grain is the unit; leaves inherit their agent.)

## Evidence & resolution (deterministic, no disk)

All evidence comes from data the ingest **already loads** (`records` + `agents`/`session` rollups) —
**no filesystem/git access** (the worker container and the Spark don't have the repos on disk).

Per agent, gather from its records + rollup:
- `cwd` histogram (`raw.cwd`) → the anchor = deepest dir holding ≥80% of the agent's records.
- git roots + remotes (`raw.serverClassifierContext.context.git_state.root` /
  `.visibility.origin`) when present.
- `pr_links` (agent rollup `outputs.pr_links`, else session) — carry the repo identity literally.
- (optional, best-effort) commit SHAs seen in tool results — recorded as evidence, not required.

Best-guess key, highest-confidence signal first (record which fired in `source`):
1. **`pr`** — PR URL → `host/owner/name` (ground truth).
2. **`git_remote`** — normalized remote from `git_state`.
3. **`git_name` / `folder`** — longest known git root containing the anchor → its basename;
   else the anchor's folder name (flagged lowest confidence).

Normalize remotes on both sides: lowercase host, strip scheme/credentials/`.git`, convert
`git@host:owner/name` → `host/owner/name`. Worktrees fold to their main root (a root nested under
another with the same/unknown remote). Subproject = first path segment of the **main** agent's anchor
below its repo root (monorepo case); subagents in the same repo as the session do **not** get a
subproject (rule-6 inherit).

## What is stored (metadata only — no schema change)

Added to each session-group and agent-group doc's existing `metadata` JSON, under `work_unit`:

```json
"work_unit": {
  "key": "github.com/2389-research/meadow",          // best guess (repo [+ ' :: ' subproject])
  "source": "git_remote",                             // pr | git_remote | git_name | folder
  "repo_remote": "github.com/2389-research/meadow",   // normalized, or null
  "repo_root": "/…/GitHub/meadow",                    // or null
  "repo_name": "meadow",
  "subproject": null,
  "anchor_dir": "/…/GitHub/meadow",
  "anchor_share": 1.0,
  "git_branch": "meadow-live-map",
  "pr_links": ["github.com/2389-research/meadow/pull/2"],
  "units": [ {…}, … ]                                 // multi-valued: all distinct units for the agent
}
```
Session-group `work_unit` carries the **set** of distinct unit keys across its agents (+ primary).
Because the raw inputs (cwd, git_state, pr_links) live in the doc rollup/metadata already,
re-resolution after rule changes is a requery, never a re-pull.

## Backfill

A worker job `work_unit_backfill` (config `{collection_id, server_url}`): for each
`meridian_sessions` row of the collection, pull `agents`+`session` and re-pull `records` (one ~30s
pass over the API; no LLM, no re-summarize), resolve, and `UPDATE documents SET metadata=…` on the
existing session-group + agent-group docs. Triggered by `POST /ingest/meridian/work-units`
(`X-Workspace-Id` selects the workspace). Going forward, new ingests tag inline (records already in
hand — no extra pull).

## Testing

Resolver is a pure module with unit tests over the hard cases proven live: worktree fold, monorepo
subproject split, nested repo (watcher) not-a-subproject, PR-link ground truth, name/folder
fallback, multi-valued agent, misleading branch ignored. Ingest wiring + backfill tested with
file-backed sqlite (tmp_path), fake `fetch`.
