# Horton/Clover window cutoff — Design

**Status:** approved (2026-10-30). **Author:** Nomzor + Claude. **Related:** [[orrery-horton-transcript-ingest]].

## Why
Clover is switching from 15-minute to **5-minute windows**. It will rechunk its whole history at 5 minutes, which gives every chunk a new id, and re-emit that history on `/v1/chunks` with new, higher sequence numbers. Our cursor stays valid, so on its next read Orrery would see the rebuilt history.

Orrery doesn't care about window size. The only constraint is to **not ingest the same transcript lines twice**. So we **keep our existing 15-minute documents** and **skip any chunk whose `window.start` is before T**, where T is the end of the last 15-minute window we ingested. Clover has confirmed, and verified on a copy of the live DB:
- windows are cut on the UTC clock, so every quarter-hour is also a 5-minute boundary;
- with that T there is no overlap and no gap;
- the cursor stays valid.

## Decision
1. **`min_window_start`** is an optional key in a horton watched source's `config_json`. It holds an ISO-8601 timestamp; `Z` and `+00:00` are both accepted.
   - `sync_horton` passes it to `ingest_horton_chunks(..., min_window_start=...)`.
   - In the loop, a chunk whose parsed `window.start` is earlier than the cutoff is **skipped**. That means no upsert and no LLM work. It is counted under a new `before_cutoff` action, and the loop moves on.
   - Compare parsed, timezone-aware datetimes, never strings.
   - A chunk at exactly T is ingested (`>=`).
   - Skipped chunks count as processed, so the cursor still advances normally.
   - Skipped chunks never trigger enrich-on-arrival.
2. **Fail loudly on a bad cutoff.** If `min_window_start` is present but unparseable, the scan raises before ingesting anything, so the source's `last_error` shows it. A malformed cutoff must never silently ingest the duplicate history.
   - A chunk whose own `window.start` is missing or unparseable while a cutoff is set is **ingested, with a warning**. We don't drop data on a malformed record. Clover always sends it, so this is a defensive edge.
3. **`PATCH /watched-sources/{id}` accepts `config`** (a dict), shallow-merged into `config_json`. A key set to `null` is removed. This is general-purpose, and it is how the cutoff is set at switch time without hand-edited SQL. `enabled` and `cadence_hours` behave as today.
4. The one-shot `POST /ingest/horton` path passes no cutoff, so its behaviour is unchanged.

## Switch procedure (ops, documented in the PR)
1. Before Clover stops, pause the source: `PATCH {"enabled": false}`.
2. Clover switches, then confirms the first 5-minute windows are sealed.
3. Compute T = max `metadata.window_end` over the workspace's documents.
4. `PATCH {"config": {"min_window_start": T}, "cadence_hours": 0.0333, "enabled": true}`. This sets the cutoff, scans every ~2 minutes, and resumes.
5. Set the worker's `SOURCE_SCAN_INTERVAL_SECONDS` to 60 so the sweep checks often enough, then restart the worker.
6. Verify: the first scan reports a large `before_cutoff` count and creates only 5-minute chunks with `window.start >= T`.

## Non-goals
No wipe, no re-ingest, no per-mic cutoffs (Clover's grid is global UTC), no change to any other source type.

## Review focus
1. The boundary is exact: `start == T` is ingested and `start < T` is skipped, across `Z`, `+00:00` and fractional-second formats.
2. The cursor advances when every pending chunk was skipped.
3. Skipped-only scans don't enqueue `enrich_stretches`.
4. A bad cutoff fails the scan without ingesting.
5. The PATCH config merge doesn't clobber other config keys, and `null` removes a key.
