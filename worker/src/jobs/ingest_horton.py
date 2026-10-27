# ABOUTME: Ingest horton office-mic transcript chunks (pull API) as raw-text documents.
# ABOUTME: One collection per (mic, day), created lazily; each chunk is one flat upsert_document.
# ABOUTME: Cursor discipline mirrors ingest_meridian: whole-drain, process-all, advance once, hold on failure.

import json
import sqlite3
import traceback
import uuid

from orrery_relay import Relay
from ..config import get_settings
from ..db import get_connection, mark_graph_dirty
from ..silo import resolve_kind, flow_default_kind
from .horton_reader import make_fetch, drain_chunks, chunk_to_doc
from .upsert_document import upsert_document


def get_or_create_collection(conn, mic_id: str, day: str, server_url: str, cache: dict) -> str:
    """Find-or-create the collection for (mic, day); memoized in `cache`. A late-revised chunk
    carries its original day, so it lands in that day's collection, not today's."""
    key = (mic_id, day)
    if key in cache:
        return cache[key]
    path = f"horton/{mic_id}/{day}"
    row = conn.execute("SELECT id FROM collections WHERE path = ?", (path,)).fetchone()
    if row is None:
        cid = str(uuid.uuid4())
        try:
            conn.execute(
                "INSERT INTO collections (id, name, path, root_path, remote_url, document_count, kind, "
                "provenance_kind) VALUES (?, ?, ?, ?, ?, 0, 'horton', ?)",
                (cid, f"{mic_id} · {day}", path, path, server_url,
                 resolve_kind(flow_default_kind("horton"))))
            conn.commit()
        except sqlite3.IntegrityError:  # UNIQUE(path) race
            row = conn.execute("SELECT id FROM collections WHERE path = ?", (path,)).fetchone()
            cid = row[0]
    else:
        cid = row[0]
    cache[key] = cid
    return cid


async def run_ingest_horton(job: dict, db_path: str) -> None:
    settings = get_settings()
    relay = Relay.from_settings(settings)
    config = json.loads(job["config"]) if job["config"] else {}
    server_url = config["server_url"]
    fetch = make_fetch(server_url)

    conn = get_connection(db_path)
    counts = {"created": 0, "updated": 0, "skipped": 0, "conflict": 0, "empty": 0, "failed": 0}
    try:
        row = conn.execute("SELECT after FROM horton_cursor WHERE server_url=?", (server_url,)).fetchone()
        after = row[0] if row else None

        items, next_cursor, _ = drain_chunks(fetch, after)
        cache: dict = {}
        for item in items:
            try:
                doc = chunk_to_doc(item)
                if doc is None:
                    counts["empty"] += 1
                    continue
                cid = get_or_create_collection(conn, doc["mic_id"], doc["day"], server_url, cache)
                res = await upsert_document(
                    conn, relay, settings, source_path=doc["source_path"], title=doc["title"],
                    content=doc["content"], collection_id=cid, role="leaf",
                    parent_path=doc["parent_path"], content_type="transcript",
                    metadata=doc["metadata"], classify=True, emits_cooccurrence=True)
                counts[res["action"]] += 1
            except Exception:
                counts["failed"] += 1
                print(f"[ingest_horton] chunk failed: {item.get('id') if isinstance(item, dict) else item}\n"
                      f"{traceback.format_exc()}", flush=True)

        if counts["failed"] == 0 and next_cursor and next_cursor != after:
            conn.execute("INSERT INTO horton_cursor (server_url, after) VALUES (?, ?) "
                         "ON CONFLICT(server_url) DO UPDATE SET after=excluded.after",
                         (server_url, next_cursor))
        if counts["created"] or counts["updated"]:
            mark_graph_dirty(conn)
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(counts), job["id"]))
        conn.commit()
        print(f"[ingest_horton] done: {counts}", flush=True)
    finally:
        conn.close()
