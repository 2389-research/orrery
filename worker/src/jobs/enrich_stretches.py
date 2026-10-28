# ABOUTME: enrich_stretches job — split horton transcript docs into stretches, LLM-enrich each into a
# ABOUTME: facet, upsert into `stretches`. Idempotent by content_hash. Writes ONLY `stretches` (no graph mutation).

import json
import logging

from orrery_relay import Relay
from ..config import get_settings
from ..db import get_connection
from .stretch_splitter import split_document
from .stretch_featurizer import enrich_batch

log = logging.getLogger(__name__)
BATCH_SIZE = 10

_UPSERT = (
    'INSERT INTO stretches (stretch_id, doc_id, mic_id, silo_id, start, "end", content_hash, summary, '
    "concepts, ideas, nouns, enriched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
    "ON CONFLICT(stretch_id) DO UPDATE SET doc_id=excluded.doc_id, mic_id=excluded.mic_id, "
    'silo_id=excluded.silo_id, start=excluded.start, "end"=excluded."end", '
    "content_hash=excluded.content_hash, summary=excluded.summary, concepts=excluded.concepts, "
    "ideas=excluded.ideas, nouns=excluded.nouns, enriched_at=CURRENT_TIMESTAMP"
)


def _mic_from_source_path(source_path: str | None) -> str:
    parts = (source_path or "").split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "horton:chunk" else ""


async def run_enrich_stretches(job: dict, db_path: str) -> None:
    config = json.loads(job["config"]) if job.get("config") else {}
    settings = get_settings()
    relay = Relay.from_settings(settings)
    model = settings.extraction_model

    conn = get_connection(db_path)
    try:
        # since/until filter on documents.created_at (simple string compare); collection = silo_id.
        sql = "SELECT id, content, source_path, silo_id, metadata FROM documents WHERE content_type = 'transcript'"
        args: list = []
        if config.get("collection"):
            sql += " AND silo_id = ?"; args.append(config["collection"])
        if config.get("since"):
            sql += " AND created_at >= ?"; args.append(config["since"])
        if config.get("until"):
            sql += " AND created_at < ?"; args.append(config["until"])
        sql += " ORDER BY created_at, id"
        if config.get("limit"):
            sql += " LIMIT ?"; args.append(int(config["limit"]))
        docs = conn.execute(sql, args).fetchall()

        counts = {"enriched": 0, "skipped": 0, "failed": 0, "doc_errors": 0, "docs": len(docs)}
        force = bool(config.get("force"))
        for doc_id, content, source_path, silo_id, metadata in docs:
            try:
                day = None
                try:
                    day = (json.loads(metadata) if metadata else {}).get("day")
                except (ValueError, AttributeError):
                    day = None
                if not day or not isinstance(day, str):
                    raise ValueError("document metadata has no office-local `day`")
                mic_id = _mic_from_source_path(source_path)
                todo = []
                current = split_document(doc_id, mic_id, content or "", day)
                for s in current:
                    row = conn.execute('SELECT content_hash, start, "end", silo_id FROM stretches '
                                       "WHERE stretch_id = ?", (s["stretch_id"],)).fetchone()
                    # content_hash covers the stretch TEXT only, so prompt/code improvements don't
                    # regenerate stored facets; `force` bypasses this skip to re-enrich everything.
                    if not force and row and row[0] == s["content_hash"]:
                        counts["skipped"] += 1
                        # Body unchanged, but day/silo may have been corrected: refresh the
                        # event-time fields cheaply (no model call).
                        if (row[1], row[2], row[3]) != (s["start"], s["end"], silo_id):
                            conn.execute('UPDATE stretches SET start = ?, "end" = ?, silo_id = ? '
                                         "WHERE stretch_id = ?",
                                         (s["start"], s["end"], silo_id, s["stretch_id"]))
                    else:
                        todo.append(s)
                for i in range(0, len(todo), BATCH_SIZE):
                    batch = todo[i:i + BATCH_SIZE]
                    facets = await enrich_batch(relay, model, batch)
                    for s in batch:
                        f = facets.get(s["stretch_id"])
                        if not f:
                            counts["failed"] += 1
                            log.warning("enrich_stretches: no facet for %s", s["stretch_id"])
                            continue
                        conn.execute(_UPSERT, (
                            s["stretch_id"], doc_id, mic_id, silo_id, s["start"], s["end"], s["content_hash"],
                            f.get("summary"), json.dumps(f.get("concepts", [])),
                            json.dumps(f.get("ideas", [])), json.dumps(f.get("nouns", []))))
                        counts["enriched"] += 1
                    conn.commit()
                # Reconcile: a revised transcript may split into fewer stretches; drop stale rows.
                keep = [s["stretch_id"] for s in current]
                marks = ",".join("?" * len(keep))
                conn.execute(f"DELETE FROM stretches WHERE doc_id = ?"
                             + (f" AND stretch_id NOT IN ({marks})" if keep else ""),
                             [doc_id, *keep])
                conn.commit()
            except Exception as e:
                conn.rollback()
                counts["doc_errors"] += 1
                log.error("enrich_stretches: doc %s failed: %s", doc_id, e)
                continue
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(counts), job["id"]))
        conn.commit()
    finally:
        conn.close()
