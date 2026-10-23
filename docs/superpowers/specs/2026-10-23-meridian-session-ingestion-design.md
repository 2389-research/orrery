# Meridian session ingestion — design

Status: **design** (brainstormed 2026-10-23). Branch `feat/meridian-session-ingestion`.

## Intent & why

Ingest Claude Code sessions from **meridian** (a new service that collects CC transcripts from
opted-in machines and serves them via a cursor-paginated pull API) into a dedicated, persistent
**"Code Sessions" noosphere**, and keep each session's record growing as the session continues — on
a regular cadence. Orrery stays focused on being good at ingesting docs; meridian is the source,
and **meadow** (a separate Root-board-game-themed front end) is a downstream consumer that reads
across this noosphere and the code-repos noosphere to show "which agents are working where."

This is a **re-home of the existing ccvault session-ingestion flow onto a new source**, not new
featurizer theory. Meridian's `ORRERY_HANDOFF.md` even ships a ccvault→meridian mapping table.

## What exists today (context)

- **ccvault flow** (`worker/src/jobs/ingest_ccvault.py` + `worker/src/ccvault_reader.py`, route
  `POST /ingest/ccvault`): a session is recursively summarized like a repo (codesum) into one
  `agent_report` silo/collection — `leaf` = segment summary, `group` = session rollup; classified
  once; `extract_batch` mines the neutral leaves. An **active-work** branch (per-segment) links
  graph-using segments to real entity ids. Dedup is **write-once per session**
  (`ccvault_sessions_seen`) + per-`query_id` (`ccvault_processed`). It writes into a **clone** of
  the queried noosphere *solely* so active-work entity ids resolve.
- **meridian** (`~/Documents/GitHub/meridian`, `docs/ORRERY_HANDOFF.md`): pull API on loopback
  (Spark: `localhost:8080`). Routes: `GET /v1/sessions?updated_after=&limit=&contributor=&cwd_prefix=&lifecycle=`,
  `/v1/sessions/{session_key}` (+`/agents`, +`/records`), `GET /v1/records`. `session_key =
  {collector_id}/{session_id}` (contains a slash — percent-encode). Records carry `raw` (the full
  parsed transcript line, == ccvault's `raw_json`) + `tool_uses`/`tool_results`. Sessions carry
  `revision` (bumps on any change), `cwd`, `git_branch`, `title`, `started_at`,
  `last_activity_at`, `lifecycle` (active/idle/inactive/ended), `resumed_from*`, and deterministic
  `rollup`s (tokens, tools, files, subagents, PR links). Multi-machine; at-least-once delivery.
- **orchestration** lives in **`orrery-office-display`** (launchd): `delivery/orrery-sync.sh` runs
  every 4h and already nudges orrery over HTTP (`curl -X POST .../watched-sources/scan-due`). The
  meridian cadence is one more curl there.

## Goals

- A dedicated, persistent **Code Sessions** noosphere (an ordinary workspace DB) that accumulates
  meridian CC sessions as `agent_report` session docs.
- **Incremental growth per session:** backfill on first sight; on a `revision` bump, append only
  the **new tail** (new segment leaves + a refreshed group rollup). **Never full re-summarize.**
- **Preserve meridian's structured metadata as queryable fields** — contributor, agent(s), project
  `cwd` (the repo **join key**), `git_branch`, recency (`started_at`/`last_activity_at`),
  `lifecycle`, PR links, `session_key` — so meadow can answer "which agent is working in which
  repo" and correlate to the code-repos noosphere.
- An **idempotent `POST /ingest/meridian`** route: repeated calls are safe and incremental, so an
  external scheduler drives cadence.

## Non-goals / anti-goals

- **No active-work entity linking (Flow B).** It only pays off in the noosphere the sessions
  queried; this standalone sessions noosphere isn't that graph. The branch is **flag-gated OFF**.
  With it off there is **no clone, no clone hygiene, no retrieval-isolation** requirement.
- **No in-orrery scheduler.** Cadence is external (office-display launchd), exactly like
  `/watched-sources/scan-due`.
- **Not the code-repos noosphere.** That is the *existing* repo/codesum ingestion, pointed at the
  dirs meridian reports as worked-on (`cwd`s) plus a few extra repos — config/orchestration, not
  design work, handled separately.
- **Not meadow's placement/matching** (meadow matches agents to known repos and invents its own
  fallback for unknown ones).
- **Not** multi-noosphere federation / a meta-index (future direction; keep noospheres independent
  so they remain the units a later index would route between).
- **Not** Codex-shaped sessions yet (CC-shaped first, like ccvault).

## Constraints

- **meridian:** loopback/tailnet only, **no auth** v1; **at-least-once** delivery → dedupe by
  record `id`; `/v1/sessions/{k}/records` is a **snapshot, not a resume point** → re-pull a
  session's records from the start each time and dedupe by id, never persist its `next_page_token`;
  **inheritance** (resume/fork) copies earlier records into a new session flagged `inherited:true`
  → dedupe knowledge by `uuid`/`id` so shared history isn't double-ingested; **retention**
  (`truncated_before`) can drop records; records without `uuid` can reappear under a new
  generation; keep `limit` modest for multi-MB records.
- **orrery:** featurizer stages are **bounded non-agentic `relay.complete`** (see ccvault);
  `agent_report` silo is **gated out of co-occurrence** (`recompute_cooccurrence`); new schema
  tables go in **both** `db.py` files **and** `_MIRRORED_TABLES` (`test_schema_mirror.py`);
  `FLOW_DEFAULT_KIND` is mirrored byte-identical in both `silo.py`; tests use **file-backed**
  sqlite; native macOS segfaults on faiss/libomp so **ingest/tests run in Docker**; the local venv
  `orrery_relay` may be stale (`ollama_options` strip) — use `packages/orrery-relay`.

## Approaches considered

1. **Meridian as a `watched_sources` type** (reuse the worker's due-scan cadence), featurizer
   extended in place. — Smallest new scheduling surface, but bends the filesystem/commit-sha scan
   model onto an HTTP cursor pull.
2. **Standalone scheduled `ingest_meridian` job + a shared featurizer.** — Cleanest long-term reuse
   (both ccvault and meridian share one featurizer; incremental append benefits both), but adds a
   second in-orrery scheduler, a new state table, and an up-front refactor.
3. **CHOSEN — Manual, idempotent `POST /ingest/meridian` + worker job; cadence external.** Orrery
   exposes an idempotent, incremental ingest; **office-display** calls it on a schedule (the same
   shape it already uses to nudge `/watched-sources/scan-due`). Fastest to validate the one new/hard
   piece (incremental append) on real data, no in-orrery scheduler, and it honors the established
   orrery↔office-display boundary. Matches ccvault's own "phase-1 manual → phase-2 cadence" path.

## Decided how

- **Route** — `POST /ingest/meridian` (`orchestrator/routes/ingest.py`), two-phase like
  `/ingest/ccvault`: validate target workspace (the Code Sessions noosphere) + meridian base URL;
  get-or-create the **one persistent meridian collection** (idempotent; `try/except IntegrityError
  → re-get_by_path` for the UNIQUE-path TOCTOU); enqueue `ingest_meridian`; return
  `{job_id, collection_id}`. Request model beside `RepoIngestRequest` in `models.py`.
- **Source adapter** — a new `meridian_reader` that pulls via the API: page
  `GET /v1/sessions?updated_after=<cursor>` while `has_more`; per changed session fetch
  `/v1/sessions/{k}` + `/agents` and page `/records` from the start; expose the per-session shape
  the featurizer consumes (title/metadata; ordered records with `raw` + `tool_uses`/`tool_results`;
  contributor/agents/cwd/lifecycle/revision). Percent-encode `session_key`. Dedupe records by
  `id`/`uuid`; skip/flag `inherited:true`.
- **Featurizer** — extract the **minimal shared core** of ccvault's recursive summary (segment →
  `leaf`, rollup → `group`) into a module both ccvault and meridian call. Add **incremental
  append**: a per-session watermark; backfill on first sight; on higher `revision`, segment only
  the new tail, append new leaves, regenerate the one group rollup; **buffer the partial trailing
  segment** until it fills or `lifecycle` reaches a flush state. Keep the **active-work branch
  flag-gated OFF** (`link_active_work=False`) — when off, every segment is a neutral session doc,
  and the `ccvault_processed` query-id ledger is skipped.
- **Watermark / state** — a per-session table (e.g. `meridian_sessions`: `session_key` PK,
  `last_revision`, last-consumed record position, doc/collection refs) + the `updated_after` cursor
  per source. Replaces ccvault's content-hash/`sessions_seen`. In both `db.py` + `_MIRRORED_TABLES`.
- **Metadata preservation** — persist contributor, agent(s), `cwd` (normalized repo **join key**),
  `git_branch`, `started_at`/`last_activity_at`, `lifecycle`, PR links, `session_key` as
  **queryable fields** on the session's group doc / collection. The join key must be queryable and
  must match the code-repos noosphere's repo identity so meadow can correlate.
- **Target** — a dedicated **Code Sessions** workspace (real noosphere); its workspace id is a
  **route parameter** (operator creates/designates it). **No clone.**
- **Provenance** — `agent_report` silo (co-occurrence gated), like ccvault.
  `FLOW_DEFAULT_KIND["meridian"] = agent_report` in **both** `silo.py`.
- **Cadence (external)** — office-display adds a launchd/curl to `POST /ingest/meridian` on a
  cadence (its own repo, its own change). Idempotency + incrementality make repeated calls safe.
- **Atomicity** — summarize a session, then commit its docs + watermark in one transaction;
  never watermark-before-doc; never one giant transaction over the whole pull.

## What's left to the builder

- Exact **flush rule** for the buffered partial tail (ended only, or idle too; the char budget).
- Exact **storage location** of the metadata fields (doc `metadata` JSON vs columns vs collection
  fields) and the **normalized repo join-key format** (must align with the code-repos noosphere).
- How much featurizer to **extract vs copy** now (recommend: minimal extraction of the
  summary+append core; leave ccvault's route/ledgers in place).
- **Inheritance** specifics (process a record as inherited in one session, original in the other;
  dedupe by `uuid`).
- **Test strategy** — mirror ccvault's (worker + route tests, run in Docker); a local meridian via
  `docker compose` for an integration check; a spike puller to validate append on real data.
