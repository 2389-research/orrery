# ABOUTME: Read-only client for horton-orrery's pull API — mic transcript chunks.
# ABOUTME: stdlib only; `fetch` is injectable so every function is testable without a network.

import re
import urllib.parse

from src.jobs.meridian_reader import make_fetch  # noqa: F401  (same stdlib GET/JSON fetch)

_BODY_CLOCK = re.compile(r"^\s*\[(\d{2}:\d{2}):\d{2}\]")


def list_chunks_page(fetch, after: str | None, limit: int = 100):
    """One GET /v1/chunks page. Returns (items, next_cursor, has_more)."""
    page = fetch("/v1/chunks", {"after": after, "limit": limit})
    return list(page.get("items") or []), page.get("next_cursor"), bool(page.get("has_more"))


def drain_chunks(fetch, after: str | None, limit: int = 100, max_pages: int = 10000):
    """Page /v1/chunks until has_more is false. Returns (items, next_cursor, has_more);
    next_cursor is the one to persist."""
    items: list[dict] = []
    for _ in range(max_pages):
        page_items, nxt, more = list_chunks_page(fetch, after, limit)
        items.extend(page_items)
        nxt = nxt or after
        if not more:
            return items, nxt, False
        if nxt == after:
            # Buggy server: more pages claimed but the cursor never advances. Stop, don't spin.
            return items, nxt, True
        after = nxt
    return items, after, True


def get_chunk(fetch, chunk_id: str) -> dict:
    # chunk ids contain ':' and '/'; they must be a single path segment.
    return fetch(f"/v1/chunks/{urllib.parse.quote(chunk_id, safe='')}", None)


def chunk_to_doc(chunk: dict) -> dict | None:
    """Map a chunk to an ingestable doc dict; None when the body is empty."""
    body = chunk.get("body")
    if not body or not body.strip():
        return None
    mic = chunk.get("mic") or {}
    window = chunk.get("window") or {}
    mic_name = mic.get("name") or mic.get("id")

    m = _BODY_CLOCK.match(body)
    if m:
        hhmm = m.group(1)  # body clock is already office-local
    else:
        start = window.get("start") or ""
        hhmm = start[11:16] if len(start) >= 16 else ""

    return {
        "source_path": chunk["id"],
        "title": f"{mic_name} · {hhmm}",
        "content": body,
        "parent_path": chunk.get("folder"),
        "mic_id": mic.get("id"),
        "day": chunk.get("day"),
        "metadata": {
            "mic": {"id": mic.get("id"), "name": mic.get("name"),
                    "names_in_window": mic.get("names_in_window"),
                    "mixed_mics": mic.get("mixed_mics")},
            "window_start": window.get("start"),
            "window_end": window.get("end"),
            "day": chunk.get("day"),
            "complete": chunk.get("complete"),
            "gaps": chunk.get("gaps"),
            "source": chunk.get("source"),
            "line_count": chunk.get("line_count"),
            "revision": chunk.get("revision"),
            "content_hash": chunk.get("content_hash"),
        },
    }
