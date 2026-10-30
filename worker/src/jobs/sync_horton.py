# ABOUTME: Horton/Clover watched-source syncer — drains the pull feed on each scan-due tick.
# ABOUTME: Thin wrapper over ingest_horton_chunks; the watched source OWNS its documents.
"""Transcript chunks are append/revise-only (they never vanish), so there is no deletion
pass: unlike the vault/repo syncers this never calls apply_deletions. Cursor lives in the
shared `horton_cursor` table, keyed on the source URI."""

from .ingest_horton import ingest_horton_chunks

_KNOWN_KEYS = {"min_window_start"}


async def sync_horton(conn, relay, settings, ws, source_config, source_id) -> dict:
    source_config = source_config or {}
    unknown = sorted(k for k in source_config if k not in _KNOWN_KEYS)
    if unknown:
        print(f"[sync_horton] warning: {ws['uri']} unknown config key(s) {unknown!r} "
              f"(known: {sorted(_KNOWN_KEYS)}); a misspelled min_window_start disables the cutoff",
              flush=True)
    cutoff = source_config.get("min_window_start")
    if cutoff is None or cutoff == "":  # ONLY "" / None mean unset; 0/False must reach the parser
        cutoff = None
    print(f"[sync_horton] {ws['uri']} min_window_start={cutoff!r}", flush=True)
    out = await ingest_horton_chunks(
        conn, relay, settings, server_url=ws["uri"], cursor_key=ws["uri"], source_id=source_id,
        min_window_start=cutoff)
    if out["failed"]:
        # Surface it: run_scan_source records last_status='error'/last_error and re-raises.
        raise RuntimeError(f"{out['failed']} horton chunk(s) failed; cursor held at previous position")
    return {"actions": out["actions"], "deleted": 0}
