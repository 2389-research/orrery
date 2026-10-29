# ABOUTME: Backfill job: tag already-ingested meridian group docs with `work_unit` metadata.
# ABOUTME: Metadata-only (no LLM, no re-summarize, no domain/count changes); idempotent and re-runnable.
import asyncio
import json
import logging

from ..db import get_connection
from .meridian_reader import get_agents, get_records, get_session, make_fetch
from .work_unit import resolve_work_units

logger = logging.getLogger(__name__)


def _tag(conn, doc_id, unit):
    """Merge `work_unit` into a doc's metadata. Returns True if the row changed."""
    if unit is None:
        return False
    row = conn.execute("SELECT metadata FROM documents WHERE id = ? AND invalid_at IS NULL",
                       (doc_id,)).fetchone()
    if row is None:
        return False
    try:
        md = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        return False
    if not isinstance(md, dict) or md.get("work_unit") == unit:
        return False
    md["work_unit"] = unit
    conn.execute("UPDATE documents SET metadata = ? WHERE id = ?", (json.dumps(md), doc_id))
    return True


async def run_work_unit_backfill(job: dict, db_path: str) -> None:
    config = json.loads(job["config"]) if isinstance(job["config"], str) else job["config"]
    collection_id = config["collection_id"]
    fetch = make_fetch(config["server_url"])

    conn = get_connection(db_path)
    sessions = tagged = failed = 0
    try:
        rows = conn.execute("SELECT session_key, group_doc_id FROM meridian_sessions WHERE collection_id = ?",
                            (collection_id,)).fetchall()
        for session_key, group_doc_id in rows:
            try:
                session, agents, records = await asyncio.gather(
                    asyncio.to_thread(get_session, fetch, session_key),
                    asyncio.to_thread(get_agents, fetch, session_key),
                    asyncio.to_thread(get_records, fetch, session_key))
                units = resolve_work_units(agents, session, records)
                if group_doc_id:
                    tagged += _tag(conn, group_doc_id, units.get("__session__"))
                agent_docs = conn.execute(
                    "SELECT id, json_extract(metadata, '$.agent_id') FROM documents "
                    "WHERE source_path = ? AND invalid_at IS NULL "
                    "AND json_extract(metadata, '$.agent_id') IS NOT NULL "
                    "AND id IN (SELECT document_id FROM document_collections "
                    "           WHERE collection_id = ? AND role = 'group')",
                    (f"meridian:{session_key}", collection_id)).fetchall()
                for doc_id, agent_id in agent_docs:
                    if doc_id != group_doc_id:
                        tagged += _tag(conn, doc_id, units.get(agent_id))
                conn.commit()
                sessions += 1
            except Exception:
                conn.rollback()
                failed += 1
                logger.exception("work_unit backfill failed for session %s", session_key)
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?",
                     (json.dumps({"sessions": sessions, "docs_tagged": tagged, "failed": failed}), job["id"]))
        conn.commit()
        if sessions == 0 and failed > 0:
            # every session errored (e.g. meridian unreachable): don't let the job look green
            raise RuntimeError(f"work_unit backfill: all {failed} session(s) failed")
    finally:
        conn.close()
