# Meridian session ingestion — design

Status: **design, ready for approval** (brainstormed 2026-10-23). Branch `feat/meridian-session-ingestion`.

## Intent & why

Ingest Claude Code sessions from **meridian** (a new service that collects CC transcripts from
opted-in machines and serves them via a cursor-paginated pull API) into a dedicated, persistent
**"Code Sessions" noosphere**, keeping each session's record growing as the session continues, on a
regular cadence. Orrery stays focused on ingesting docs well; meridian is the source of raw detail,
and **meadow** (a separate Root-board-game-themed front end) is a downstream consumer that reads
across this noosphere and the code-repos noosphere to show "which agents are working where."

Orrery holds **summaries + structure**, not raw transcripts. Raw transcripts stay first-class in
**meridian**; an agent (or orrery, later) that needs more detail pulls it from meridian. This is a
re-home of the existing ccvault session featurizer onto a new source — meridian's `ORRERY_HANDOFF.md`
ships a ccvault→meridian mapping table — plus a **subagent-aware** extension.

## What exists today (context)

- **ccvault flow** (`worker/src/jobs/ingest_ccvault.py` + `ccvault_reader.py`, route
  `POST /ingest/ccvault`): a session is recursively summarized like a repo (codesum) into one
  `agent_report` silo/collection — `leaf` = segment summary, `group` = session rollup; classified
  once; `extract_batch` mines neutral leaves. A per-segment **active-work** branch links graph-using
  segments to entity ids. Dedup is **write-once per session** (`ccvault_sessions_seen`). It writes
  into a **clone** of the queried noosphere *only* so active-work ids resolve.
- orrery already **content-hashes documents** (`documents.content_hash` + `idx_documents_content_hash`
  + `get_by_hash`) for dedup / re-ingest — the mechanism the memoized featurizer reuses one level down.
- **meridian** (`docs/ORRERY_HANDOFF.md`): pull API on loopback (Spark `localhost:8080`). Routes:
  `GET /v1/sessions?updated_after=&contributor=&cwd_prefix=&lifecycle=`, `/v1/sessions/{k}`
  (+`/agents`, +`/records`), `GET /v1/records`. `session_key = {collector_id}/{session_id}`
  (contains a slash — percent-encode). Records carry `raw` (full parsed transcript line, == ccvault
  `raw_json`) + `tool_uses`/`tool_results`. Sessions carry `revision` (bumps on any change), `cwd`,
  `git_branch`, `title`, `started_at`, `last_activity_at`, `lifecycle`
  (active/idle/inactive/ended), `resumed_from*`, and deterministic `rollup`s (tokens by model deduped
  by `message.id`, tools, files, `subagents{count,by_type}`, PR links). **Subagents** are first-class:
  `/v1/sessions/{k}/agents` returns the agent tree (main first), each with `agent_type`,
  `description`, `parent_tool_use_id` (the exact spawning tool call), `spawn_depth`, `workflow_id`,
  `relation`, and a per-agent rollup. Multi-machine; at-least-once; inheritance (resume/fork) flags
  copied records `inherited:true`.
- **orchestration** lives in **`orrery-office-display`** (launchd): `delivery/orrery-sync.sh` runs
  every 4h and already nudges orrery over HTTP (`curl -X POST .../watched-sources/scan-due`). The
  meridian cadence is one more curl there.

## Goals

- A dedicated, persistent **Code Sessions** noosphere (an ordinary workspace DB) accumulating
  meridian CC sessions as `agent_report` session-summary docs.
- **Incremental growth per session via memoized re-derive:** on a `revision` bump, re-pull the
  session snapshot and re-run the featurizer, but **memoize each segment's summary by content hash**
  so the LLM only pays for genuinely new/changed content. A 100k-line session that grows by ~1k
  re-summarizes only the changed segment(s) — never the whole session.
- **Subagent-aware structure:** capture subagents fanning in/out within a flow (see below), with
  per-agent rollups and observed parent links.
- **Preserve meridian's structured metadata as queryable fields** so meadow can correlate
  sessions↔repos (see Metadata).
- An **idempotent `POST /ingest/meridian`** route: repeated calls are safe + incremental, so an
  external scheduler drives cadence.

## Non-goals / anti-goals

- **No raw transcripts as first-class artifacts.** Orrery stores summaries + structure; raw detail
  stays in meridian (a deliberate privacy boundary — meridian's raw store holds secrets).
- **No active-work entity linking (Flow B).** Flag-gated **OFF** (`link_active_work=False`); it only
  pays off in the noosphere the sessions queried, which this isn't. With it off there is **no clone,
  no clone hygiene, no retrieval-isolation** requirement.
- **No in-orrery scheduler** — cadence is external (office-display launchd), like
  `/watched-sources/scan-due`.
- **Not the code-repos noosphere** — that is the *existing* repo/codesum ingestion pointed at the
  dirs meridian reports (`cwd`s) + a few extra repos; config/orchestration, handled separately.
- **Not meadow's placement/matching**, and **not meadow's read endpoint** (ships with meadow, later;
  this project just stores the metadata queryably).
- **Not** multi-noosphere federation / meta-index (future direction; keep noospheres independent).
- **Not** Codex-shaped sessions yet (CC-shaped first, like ccvault).

## Constraints

- **meridian:** loopback/tailnet only, **no auth** v1; **at-least-once** → dedupe by record
  `id`/`uuid`; `/v1/sessions/{k}/records` is a **snapshot, not a resume point** → re-pull from the
  start each time, never persist its `next_page_token`; **inheritance** copies records flagged
  `inherited:true` → dedupe by `uuid`; **retention** (`truncated_before`) can drop head records;
  records without `uuid` can reappear under a new generation (so a positional watermark is unstable);
  keep `limit` modest for multi-MB records.
- **orrery:** featurizer stages are **bounded non-agentic `relay.complete`**; `agent_report` silo is
  **gated out of co-occurrence**; new schema tables go in **both** `db.py` files **and**
  `_MIRRORED_TABLES`; `FLOW_DEFAULT_KIND` mirrored byte-identical in both `silo.py`; tests use
  **file-backed** sqlite; native macOS segfaults on faiss/libomp → ingest/tests in Docker; use
  `packages/orrery-relay` (local venv copy may be stale).

## Approaches considered

1. **Meridian as a `watched_sources` type** (reuse the due-scan cadence), featurizer in place. —
   Smallest scheduling surface, but bends the filesystem/commit-sha scan model onto an HTTP cursor.
2. **Standalone scheduled `ingest_meridian` job + shared featurizer.** — Cleanest long-term reuse but
   adds an in-orrery scheduler + state table + up-front refactor.
3. **CHOSEN — Manual, idempotent `POST /ingest/meridian` + worker job; cadence external.** Orrery
   exposes an idempotent, incremental ingest; **office-display** calls it on a schedule (the shape it
   already uses to nudge `/watched-sources/scan-due`). Fastest to validate the one new/hard piece on
   real data, no in-orrery scheduler, honors the orrery↔office-display boundary, matches ccvault's
   "phase-1 manual → phase-2 cadence" path.

**Sub-decision — update model: memoized re-derive, not literal append.** Literal append fights
meridian's semantics (snapshot re-pull; `truncated_before`; records reappearing under new ids; a
total-size-based segmenter whose boundaries shift as a session grows) and runs against meridian's own
"re-derive on revision" guidance. Memoized re-derive gives the same cost profile (only new content
pays the LLM) using orrery's existing content-hash mechanism, with no fragile offset watermark, no
custom stable-segmenter, and no unbounded-leaf bookkeeping. Chosen.

## Design — informed by best practice

Agent/code-session analysis tools (OTel GenAI conventions, Langfuse, claude-devtools, HarnessScope)
converge on a **trace tree** — `session → agent → (tool call | sub-agent child)`, sub-agents linked
to the exact spawning tool call — and flag recurring pitfalls. We adopt the shape and the mitigations:

| Pitfall (field) | Our mitigation |
|---|---|
| Orphaned subagents (no stable link) | Link every subagent by `parent_tool_use_id` + `session_key`; if a parent link is missing, attach to session root and **flag it — never guess** (meridian's rule too). |
| Per-call totals hide cost; parent→child token double-count | **Carry meridian's per-agent + per-session rollups** as queryable metadata; never recompute from prose (meridian already dedupes by `message.id` and excludes inherited records). |
| Subagents dominate token cost; return summaries not transcripts | Recursive per-agent **summarize + memoize** (exactly our approach). |
| Handoff truncation / compaction silently lost | Preserve **lifecycle + compaction markers** (SubagentStart/Stop, PreCompact/PostCompact, session lifecycle) in the tree. |
| Raw transcripts hold secrets | Store **summaries, not raw**; raw stays in meridian. |

**Orrery flair:** the trace tree is the skeleton; orrery adds **semantic placement** — classify each
agent's work into the domain galaxy and anchor it to real entities — so meadow sees "who's doing
what, where, and how it connects," which a pure trace viewer can't give.

## Decided how

- **Route** — `POST /ingest/meridian` (`routes/ingest.py`), two-phase like `/ingest/ccvault`:
  validate target workspace (the Code Sessions noosphere) + meridian base URL; get-or-create the one
  persistent meridian collection (idempotent; `try/except IntegrityError → re-get_by_path`); enqueue
  `ingest_meridian`; return `{job_id, collection_id}`. Request model beside `RepoIngestRequest`.
- **Source adapter** — a new `meridian_reader`: page `GET /v1/sessions?updated_after=<cursor>` while
  `has_more`; per changed session fetch `/v1/sessions/{k}` + `/agents` + page `/records` from the
  start; percent-encode `session_key`; dedupe records by `uuid`; skip/flag `inherited:true`.
- **Featurizer (subagent-aware, memoized re-derive)** — extract the minimal shared summary core from
  ccvault into a module both sources call. Per session build the **agent tree** from `/agents`:
  `session group → per-agent groups (main + each subagent, linked by parent_tool_use_id, nested by
  spawn_depth) → per-agent segment leaves`. Summarize each agent's stream recursively; **memoize each
  segment's summary by content hash** (reuse unchanged); regenerate the per-agent + session group
  rollups each bump. Carry meridian's rollups as metadata (don't recompute). Keep the active-work
  branch flag-gated OFF. One doc-tree per meridian session; a fork/resume gets its own tree, shared
  history deduped by `uuid`.
- **Watermark / state** — per-session `last_revision` + the already-summarized segment content-hashes
  (implicit in stored leaves' `content_hash`) + the `updated_after` cursor per source. No positional
  offset. New table(s) in both `db.py` + `_MIRRORED_TABLES`.
- **Metadata (queryable)** — contributor, agent(s) with type/description/spawn_depth/workflow_id/
  parent_tool_use_id, `cwd` (normalized repo **join key**, matching the code-repos noosphere's repo
  identity), `git_branch`, `started_at`/`last_activity_at`, `lifecycle`, PR links, `session_key`, and
  the per-agent/session rollups — on the session/agent group docs.
- **Target** — a dedicated **Code Sessions** workspace (real noosphere); workspace id is a route
  parameter (operator creates/designates it). **No clone.**
- **Provenance** — `agent_report` silo (co-occurrence gated). `FLOW_DEFAULT_KIND["meridian"] =
  agent_report` in both `silo.py`.
- **Classification** — classify on first backfill (best-effort), re-classify **once** when the
  session settles (`ended`) — not every bump — to bound wrong-domain risk on active sessions and
  write churn.
- **Cadence (external)** — office-display adds a launchd/curl to `POST /ingest/meridian` (its own
  repo). Idempotency + incrementality make repeated calls safe.
- **Atomicity** — summarize a session, then commit its docs + watermark in one transaction; never
  watermark-before-doc; never one giant transaction over the whole pull.
- **Extensibility** — if orrery needs structure meridian doesn't expose, request a meridian field
  (meridian owns the deterministic rollups; the boundary is Orrery→meridian).

## What's left to the builder

- **Flush rule** for a buffered partial trailing segment (ended only, or idle too; char budget) — or
  whether memoized re-derive makes buffering unnecessary (a changing tail segment just re-hashes).
- **Exact storage** of the metadata (doc `metadata` JSON vs columns) and the normalized repo
  **join-key** format (must align with the code-repos noosphere's repo identity).
- How much featurizer to **extract vs copy** now (recommend: minimal extraction of the
  summarize+memoize core; leave ccvault's route/ledgers in place).
- **Inheritance** record-level specifics (process as inherited in one session, original in the other;
  dedupe by `uuid`).
- **Test strategy** — mirror ccvault's (worker + route tests, run in Docker); a local meridian via
  `docker compose` for an integration check; a spike puller to validate memoized re-derive on real
  data (backfill, add sessions, re-pull → only changed segments re-summarize).
</content>
