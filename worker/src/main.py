import os
import asyncio
import json
import sqlite3
import time
import traceback
import uuid
from pathlib import Path
from .config import get_settings
from .db import init_db, get_connection


def enqueue_due_source_scans(db_paths) -> int:
    """Enqueue a scan_source job for every enabled watched_source whose cadence is due.

    Jobs land in the SAME workspace DB as their source (jobs + watched_sources are both
    per-workspace, so identity is unambiguous and no cross-DB routing is needed). Skips a
    source that already has a queued/running scan so the sweep is idempotent. Factored out
    of the poll loop so it is unit-testable. Returns the number enqueued.
    """
    enqueued = 0
    for db_path in db_paths:
        conn = get_connection(db_path)
        try:
            due = conn.execute(
                "SELECT id FROM watched_sources WHERE enabled = 1 AND "
                "(last_scanned_at IS NULL OR "
                " (julianday('now') - julianday(last_scanned_at)) * 24 >= cadence_hours)"
            ).fetchall()
            for r in due:
                pending = conn.execute(
                    "SELECT 1 FROM jobs WHERE type = 'scan_source' AND target = ? "
                    "AND status IN ('queued', 'running') LIMIT 1", (r["id"],)).fetchone()
                if pending:
                    continue
                conn.execute(
                    "INSERT INTO jobs (id, type, target, status, config) "
                    "VALUES (?, 'scan_source', ?, 'queued', ?)",
                    (str(uuid.uuid4()), r["id"], json.dumps({"source_id": r["id"]})))
                enqueued += 1
            conn.commit()
        finally:
            conn.close()
    return enqueued
from .jobs.runner import (
    pick_next_job, mark_job_running, mark_job_completed, mark_job_failed,
    reset_orphaned_jobs,
)


def _record_terminal_state(db_path: str, job_id: str, *, completed: bool,
                           error: str = "", attempts: int = 5) -> bool:
    """Write a job's terminal status, retrying a locked database.

    The status write is tiny, but it lost a race once — a migration held the write
    lock — and the completion of a job that had already done its work (26 docs, 135
    entities extracted and committed) was never recorded, so the row sat 'running'
    forever. Worse, the old code recorded the outcome inside the same try that ran the
    job: when the completion write raised, the except tried to mark the job FAILED,
    that raised the same lock, and the whole thing fell through to the generic "Error
    polling" handler — the outcome of finished work vanished into a line that looked
    like an unrelated poll hiccup.

    So the recording is separated from running the job, and retried on a locked/busy
    error (each attempt on a fresh connection, since a failed commit leaves the old one
    unusable). If it still cannot be written, it is announced as CRITICAL and named —
    not swallowed — and the startup reconciler will settle the row on the next restart.
    """
    what = "completion" if completed else "failure"
    for attempt in range(attempts):
        conn = None
        try:
            conn = get_connection(db_path)
            if completed:
                mark_job_completed(conn, job_id)
            else:
                mark_job_failed(conn, job_id, error)
            return True
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                time.sleep(0.2 * (attempt + 1))
                continue
            # A non-lock write error (disk full, malformed image) will not be helped by
            # retrying, and must NOT fall through to the generic poll handler — where a
            # finished job's lost outcome reads as an unrelated hiccup, the exact thing
            # this function exists to prevent. Name it and stop; the reconciler settles
            # the row on the next restart.
            print(f"CRITICAL: recording {what} of job {job_id} hit a non-retryable "
                  f"database error: {e}; the row is left 'running' for the startup "
                  f"reconciler", flush=True)
            return False
        finally:
            if conn is not None:
                conn.close()
    print(f"CRITICAL: could not record {what} of job {job_id} after {attempts} "
          f"attempts (database stayed locked); the row is left 'running' and will be "
          f"reconciled on the next worker restart", flush=True)
    return False


def _configure_gateway_for_simmer_sdk() -> None:
    """simmer-sdk doesn't know our 'gateway' backend — it only speaks anthropic/bedrock/ollama.
    When ANTHROPIC_BACKEND=gateway, route simmer-sdk's Anthropic client through the gateway
    by setting ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY. The jobs then pass api_provider='anthropic'."""
    s = get_settings()
    if s.anthropic_backend == "gateway" and s.gateway_url and s.gateway_api_key:
        os.environ["ANTHROPIC_BASE_URL"] = s.gateway_url
        os.environ["ANTHROPIC_API_KEY"] = s.gateway_api_key


_configure_gateway_for_simmer_sdk()

async def handle_job(job: dict, db_path: str) -> None:
    if job["type"] == "simmer_general":
        from .jobs.simmer_general import run_simmer_general
        await run_simmer_general(job, db_path)
    elif job["type"] == "simmer_domain":
        from .jobs.simmer_domain import run_simmer_domain
        await run_simmer_domain(job, db_path)
    elif job["type"] == "simmer_domain_image":
        from .jobs.simmer_domain_image import run_simmer_domain_image
        await run_simmer_domain_image(job, db_path)
    elif job["type"] == "extract_batch":
        from .jobs.extract_batch import run_extract_batch
        await run_extract_batch(job, db_path)
    elif job["type"] == "extract_batch_image":
        from .jobs.extract_batch_image import run_extract_batch_image
        await run_extract_batch_image(job, db_path)
    elif job["type"] == "ingest_repo":
        from .jobs.ingest_repo import run_ingest_repo
        await run_ingest_repo(job, db_path)
    elif job["type"] == "ingest_tracker_runs":
        from .jobs.ingest_tracker_runs import run_ingest_tracker_runs
        await run_ingest_tracker_runs(job, db_path)
    elif job["type"] == "ingest_ccvault":
        from .jobs.ingest_ccvault import run_ingest_ccvault
        await run_ingest_ccvault(job, db_path)
    elif job["type"] == "ingest_pdf":
        from .jobs.ingest_pdf import run_ingest_pdf
        await run_ingest_pdf(job, db_path)
    elif job["type"] == "judge_corrections":
        from .jobs.graph_repair import run_judge_corrections
        await run_judge_corrections(job, db_path)
    elif job["type"] == "generate_commentary":
        from .jobs.generate_commentary import run_generate_commentary
        await run_generate_commentary(job, db_path)
    elif job["type"] == "scan_source":
        from .jobs.scan_source import run_scan_source
        await run_scan_source(job, db_path)
    else:
        raise ValueError(f"Unknown job type: {job['type']}")


def _find_workspace_dbs(base_db_path: str) -> list[str]:
    """Find all workspace SQLite databases.

    Checks both:
    - Multi-workspace layout: {data_dir}/workspaces/*/orrery.db
    - Legacy flat layout: {data_dir}/orrery.db
    """
    base_dir = os.path.dirname(base_db_path)
    db_paths = []

    # Multi-workspace
    ws_dir = os.path.join(base_dir, "workspaces")
    if os.path.isdir(ws_dir):
        for ws_name in os.listdir(ws_dir):
            ws_db = os.path.join(ws_dir, ws_name, "orrery.db")
            if os.path.isfile(ws_db):
                db_paths.append(ws_db)

    # Legacy flat
    if os.path.isfile(base_db_path) and base_db_path not in db_paths:
        db_paths.append(base_db_path)

    return db_paths


async def poll_loop():
    settings = get_settings()
    print(f"Worker started, polling every {settings.worker_poll_interval}s", flush=True)

    from orrery_relay import Relay
    from .jobs.graph_repair import run_judge_sweep
    from .jobs.normalization_judge import resolve_judge_relay, run_normalization_judge_sweep
    relay = Relay.from_settings(settings)
    last_sweep = 0.0  # 0 → sweep on the first iteration
    last_source_sweep = 0.0  # watched-source scan cadence, same first-iteration behaviour

    # Recover jobs orphaned by a previous worker before entering the loop. A 'running'
    # job at startup cannot be resumed (its process is gone), so it would otherwise sit
    # 'running' forever — including the one this fix's own failure mode already stranded.
    #
    # This runs ONLY here, at startup — deliberately NOT inside the loop, where
    # reset_orphaned_jobs would fail a job the worker is actively running. That makes the
    # pass a single chance per workspace, so a transient lock must not cause a permanent
    # skip: retry on a locked/busy database rather than strand the orphan until the next
    # restart. (Rare now that the migration no longer holds a startup-length lock, #67.)
    for db_path in _find_workspace_dbs(settings.db_path):
        for attempt in range(6):
            conn = None
            try:
                init_db(db_path)
                conn = get_connection(db_path)
                n = reset_orphaned_jobs(conn)
                if n:
                    print(f"reconciled {n} orphaned running job(s) in "
                          f"{Path(db_path).parent.name}", flush=True)
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    print(f"orphan reconcile skipped for {db_path}: {e}", flush=True)
                    break
                time.sleep(0.3 * (attempt + 1))
            except Exception as e:
                print(f"orphan reconcile skipped for {db_path}: {e}", flush=True)
                break
            finally:
                if conn is not None:
                    conn.close()
        else:
            # Retries exhausted with the database still locked. Say so loudly: an
            # orphaned 'running' job here stays stranded until the next restart, since
            # the reconcile cannot safely re-run once the loop may be running jobs.
            print(f"WARNING: could not reconcile orphaned jobs in "
                  f"{Path(db_path).parent.name} — database stayed locked through "
                  f"retries; a stranded 'running' job there needs another restart to "
                  f"settle", flush=True)

    while True:
        # Scan all workspace DBs for queued jobs
        db_paths = _find_workspace_dbs(settings.db_path)
        did_work = False   # did a REAL job run this pass? (this gates the idle judge)

        for db_path in db_paths:
            # Claim phase: pick a job and mark it running, in a connection that is ALWAYS
            # released — including when mark_job_running itself loses a lock race, which
            # would otherwise skip the close and leak the connection. `did_work` is set
            # only after a job is actually claimed, so a failed claim doesn't suppress the
            # idle judge for nothing.
            job = None
            try:
                init_db(db_path)
                conn = get_connection(db_path)
                try:
                    job = pick_next_job(conn)
                    if job:
                        ws_name = Path(db_path).parent.name
                        print(f"Picked up job {job['id']} ({job['type']}) in workspace {ws_name}", flush=True)
                        mark_job_running(conn, job["id"])
                finally:
                    conn.close()
            except Exception as e:
                # Could not claim here (e.g. the running-write lost a lock race). The job,
                # if one was found, stays 'queued' and is retried next pass — self-healing.
                print(f"Error polling {db_path}: {e}", flush=True)
                continue

            if job:
                did_work = True
                # Run the job, THEN record its outcome as a separate, retried step.
                # Keeping the two apart is the whole point: a lock while recording the
                # result must not be mistaken for the job failing, and must not be able
                # to leave the row 'running' silently.
                error = None
                try:
                    await handle_job(job, db_path)
                except Exception as e:
                    error = traceback.format_exc()
                    print(f"Job {job['id']} failed: {e}", flush=True)

                if error is None:
                    if _record_terminal_state(db_path, job["id"], completed=True):
                        print(f"Job {job['id']} completed", flush=True)
                else:
                    _record_terminal_state(db_path, job["id"], completed=False, error=error)

        now = time.monotonic()
        if now - last_sweep >= settings.judge_sweep_interval_seconds:
            last_sweep = now
            try:
                result = await run_judge_sweep(db_paths, relay, settings.classification_model)
                if result["judged"] or result["failed"]:
                    print(f"judge sweep: judged {result['judged']}, {result['failed']} failed/left un-judged", flush=True)
            except Exception as e:
                print(f"judge sweep error: {e}", flush=True)

        # Watched-source scan sweep: enqueue scan_source jobs for due sources. Enqueue
        # only — the scans run as ordinary jobs (retryable, visible in /jobs), so the
        # loop never blocks on a scan and the job machinery serializes the work.
        if now - last_source_sweep >= settings.source_scan_interval_seconds:
            last_source_sweep = now
            try:
                n = enqueue_due_source_scans(db_paths)
                if n:
                    print(f"source sweep: enqueued {n} scan_source job(s)", flush=True)
            except Exception as e:
                print(f"source sweep error: {e}", flush=True)

        # Low-priority background work: drain the normalization review backlog, but ONLY
        # when no real job ran this pass. The point of the gate is resource contention —
        # on a local model the judge and an extraction would fight over the same GPU, and
        # the judge is never the urgent one. One bounded batch per pass; while it is
        # draining, poll again quickly (still checking for jobs FIRST) instead of idling
        # the full interval, so a backlog clears without delaying real work.
        norm_did = False
        if settings.normalization_judge_mode != "off" and not did_work:
            try:
                # Local model if Ollama has it, else the cloud model — re-checked on a TTL
                # rather than fixed at startup, since Ollama comes and goes. The probe
                # blocks, so it runs in a thread: the poll loop must not stall on a socket
                # timeout. Inside the try on purpose — a resolve failure has to be logged
                # and retried, never crash poll_loop and take the worker down with it.
                judge_relay, judge_model, judge_src = await asyncio.to_thread(
                    resolve_judge_relay, settings, relay)
                nr = await run_normalization_judge_sweep(
                    db_paths, judge_relay, judge_model,
                    batch_size=settings.normalization_judge_batch,
                    mode=settings.normalization_judge_mode,
                    min_confidence=settings.normalization_judge_min_confidence,
                    temperature=settings.normalization_judge_temperature,
                    max_attempts=settings.normalization_judge_max_attempts,
                )
                if nr["pairs"]:
                    norm_did = True
                    ws = Path(nr.get("workspace", "")).parent.name
                    print(f"norm_judge[{settings.normalization_judge_mode}/{judge_src}:"
                          f"{judge_model}] ws={ws}: judged {nr['judged']}, "
                          f"kept-resolved {nr['kept_resolved']}, "
                          f"merge-advised {nr['merge_advised']}, unsure {nr['unsure']}, "
                          f"{nr['failed']} failed", flush=True)
            except Exception as e:
                print(f"norm_judge error: {e}", flush=True)

        await asyncio.sleep(1 if norm_did else settings.worker_poll_interval)

def main():
    asyncio.run(poll_loop())

if __name__ == "__main__":
    main()
