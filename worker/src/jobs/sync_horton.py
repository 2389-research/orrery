# ABOUTME: Horton/Clover watched-source syncer — drains the pull feed on each scan-due tick.
# ABOUTME: Thin wrapper over ingest_horton_chunks; the watched source OWNS its documents.
"""Transcript chunks are append/revise-only (they never vanish), so there is no deletion
pass: unlike the vault/repo syncers this never calls apply_deletions. Cursor lives in the
shared `horton_cursor` table, keyed on the source URI."""

from .ingest_horton import ingest_horton_chunks


async def sync_horton(conn, relay, settings, ws, source_config, source_id) -> dict:
    out = await ingest_horton_chunks(
        conn, relay, settings, server_url=ws["uri"], cursor_key=ws["uri"], source_id=source_id)
    return {"actions": out["actions"], "deleted": 0}
