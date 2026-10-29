# What Orrery Needs From Horton

**For the `horton-orrery` service author.** This is the reverse of `ORRERY_HANDOFF.md`: it states the shape and quality of chunk data Orrery's ingest **and** the new transcript **stretch-enrichment** pipeline depend on. Deliver chunks that meet this and both work; the handoff doc still defines the pull-API mechanics.

Status: 2026-10-28, after Orrery built per-chunk ingest + stretch enrichment (summary/concepts/ideas/nouns) on top of the chunks.

## The pull API (unchanged from the handoff — restated as the contract Orrery relies on)
- `GET /v1/chunks?after=&limit=` → `{items, next_cursor, has_more}`, ordered by `change_seq`, `after` exclusive, opaque cursors, at-least-once. Orrery drains all pages, processes, then commits the cursor once.
- `GET /v1/chunks/{id}` with the id **percent-encoded** (it contains `:` and `/`).
- A chunk gets a **higher `revision` and new `change_seq`** whenever its content changes (late line, gap change), so it resurfaces past the cursor. Orrery upserts by `id` and re-processes when the body changed.

## Per-chunk fields Orrery consumes (and how)
| Field | How Orrery uses it | Requirement |
|---|---|---|
| `id` = `horton:chunk/<micID>/<windowStartUTC-RFC3339-Z>` | document identity (upsert key) **and** parsed for `mic.id` + window start | **Stable & unique** per `(mic, window)`; keep the exact `horton:chunk/<micID>/<Z>` shape — Orrery splits on `/` to get the mic id, so the mic id must be the 2nd segment even when it's a MAC with colons |
| `body` = `[HH:MM:SS] text` lines | the raw document text **and** the input to the stretch splitter | see "Body line timestamps" below — this is now load-bearing |
| `day` (office-local, America/Chicago) | **the event-date of every stretch** — stretch `start`/`end` are composed as `{day}T{HH:MM:SS}` | **Required, correct, office-local on every chunk.** A missing/empty `day` makes Orrery skip the whole chunk (doc-error). This is the single source of the stretch date; do not omit it. |
| `mic.id` | per-mic collection key (`horton/<micID>/<day>`), mic grouping | **Stable across renames.** Prefer a real MAC so seed and live are one continuous feed; `seed:<name>` works but fragments history when a name later gains a MAC. |
| `mic.name`, `mic.names_in_window`, `mic.mixed_mics` | document title + metadata | nice-to-have; `name` falls back to `mic.id` if absent |
| `window.start`, `window.end` (UTC) | stored in metadata | present; UTC is fine (Orrery does not use these for the stretch date — it uses `day`) |
| `revision`, `content_hash` | change detection → idempotent re-ingest/re-enrich | **Accurate.** `content_hash` must change iff the body changes; otherwise Orrery needlessly re-work or (worse) misses a late-line update |
| `complete`, `gaps`, `source`, `line_count` | metadata | present; informational |

## Body line timestamps — the one thing to get right for enrichment
Orrery splits each chunk's `body` into "stretches" (contiguous runs broken at a **pause > 2 minutes**, capped ~4 min / ~120 words) and LLM-summarizes each. The splitter **parses the `[HH:MM:SS]` prefix of every line**:

- **Every utterance line must start with `[HH:MM:SS]`** (office-local clock). Lines without a parseable `[HH:MM:SS]` prefix are **ignored** (dropped from enrichment) — so an unprefixed line is lost signal.
- Times should be **office-local** (America/Chicago) and consistent with `day` — the stretch event datetime is literally `day` + the line's `HH:MM:SS`. (Keep `window.start`/`lines[].ts` in UTC in the structured fields as today; just keep the `body` clock office-local, as the handoff already specifies.)
- Times should be **monotonic within a chunk** (used to compute inter-line pauses). Out-of-order/duplicate times cause a conservative extra split — not fatal, but cleaner is better.
- `date_confidence` on seed lines (`exact`/`aligned`/`inferred`) is fine — Orrery doesn't branch on it — but the `[HH:MM:SS]` must still be present.

## What Orrery does NOT need from Horton (keep out / downstream)
- **No summaries, topics, concepts, ideas, or threads.** Orrery now produces the per-stretch enrichment itself (and threads/topic-composition are a downstream consumer's job). Horton should ship **clean raw chunks only**.
- No speaker identity / diarization (mic/channel is enough; Orrery deliberately keeps people's names out of derived facets).
- No de-duplication of co-located mics (conference-table pair, kitchen) — Orrery ingests each mic separately by design; dedup is downstream.

## Live vs. seed (operational note)
- The current seed (`dataset/seed-7d.jsonl`, Oct 15–21) is a **backfill to prove the pipeline**. For it, Orrery slices by the chunk's `day`/event time (its own ingest time is "today").
- For the **live feed**, Orrery's ingest-time ≈ event-time, so its time filters track reality naturally. Keep `day` and the body clock correct and the live path "just works."
- **Re-delivery of the seed:** if mic ids change (a `seed:<name>` gains a MAC), Orrery must wipe `source:"seed"` chunks and re-ingest (chunk ids change). A `force` re-enrich path exists on the Orrery side to regenerate facets after a prompt change.

## Quick self-check before handing Orrery a feed
1. Every chunk has a non-empty office-local `day`.
2. Every `body` line starts with `[HH:MM:SS]` (office-local), times monotonic.
3. `id` is `horton:chunk/<micID>/<UTC-Z>`; `mic.id` is stable (MAC preferred).
4. `content_hash`/`revision` change exactly when the body changes.
5. `GET /v1/chunks` paginates by `change_seq` with opaque cursors; `{id}` accepts a percent-encoded id.
