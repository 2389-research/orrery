# ABOUTME: Read-only client for meridian's pull API — sessions, agent tree, immutable records.
# ABOUTME: stdlib only; `fetch` is injectable so every function is testable without a network.

import hashlib
import json
import urllib.parse
import urllib.request
from typing import Callable


def make_fetch(base_url: str, timeout: float = 30.0) -> Callable:
    """GET `base_url + path` with query params; returns parsed JSON (dict)."""
    if urllib.parse.urlparse(base_url).scheme.lower() not in ("http", "https"):
        # urlopen also serves file:/ftp: — a request-supplied URL must not read local files.
        raise ValueError(f"meridian server_url must be http or https: {base_url!r}")
    base = base_url.rstrip("/")

    def fetch(path: str, params: dict | None = None) -> dict:
        qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{base}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return fetch


def _sk(session_key: str) -> str:
    # session_key contains a slash; it must be a single path segment.
    return urllib.parse.quote(session_key, safe="")


def list_changed_sessions(fetch, cursor: str | None, limit: int = 100, max_pages: int = 10000):
    """Page /v1/sessions?updated_after= until has_more is false.
    Returns (items, next_cursor, has_more); next_cursor is the one to persist."""
    items: list[dict] = []
    for _ in range(max_pages):
        page = fetch("/v1/sessions", {"updated_after": cursor, "limit": limit})
        items.extend(page.get("items") or [])
        nxt = page.get("next_cursor") or cursor
        if not page.get("has_more"):
            return items, nxt, False
        if nxt == cursor:
            # Buggy server: more pages claimed but the cursor never advances. Stop, don't spin.
            return items, nxt, True
        cursor = nxt
    return items, cursor, True


def get_session(fetch, session_key: str) -> dict:
    return fetch(f"/v1/sessions/{_sk(session_key)}", None)


def get_agents(fetch, session_key: str) -> list[dict]:
    """Agent tree, main first."""
    return list(fetch(f"/v1/sessions/{_sk(session_key)}/agents", None).get("items") or [])


def get_records(fetch, session_key: str) -> list[dict]:
    """All records of a session from the start, deduped by uuid, inherited dropped."""
    path = f"/v1/sessions/{_sk(session_key)}/records"
    out, seen, token, seen_tokens = [], set(), None, set()
    while True:
        page = fetch(path, {"page_token": token} if token else None)
        for r in page.get("items") or []:
            if r.get("inherited"):
                continue
            u = r.get("uuid")
            if u is not None:
                if u in seen:
                    continue
                seen.add(u)
            out.append(r)
        token = page.get("next_page_token")
        if not token or token in seen_tokens:
            # Missing token ends the walk; a repeated one is a buggy server — stop, don't spin.
            return out
        seen_tokens.add(token)


def _blocks_text(role: str, content) -> list[str]:
    if isinstance(content, str):
        return [f"[{role}] {content.strip()}"] if content.strip() else []
    parts = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and (b.get("text") or "").strip():
            parts.append(f"[{role}] {b['text'].strip()}")
        elif t == "tool_use":
            nm = (b.get("name") or "").split("__")[-1]
            inp = b.get("input")
            parts.append(f"[tool:{nm}] {json.dumps(inp)[:300] if inp is not None else ''}")
        elif t == "tool_result":
            c = b.get("content")
            txt = c if isinstance(c, str) else "".join(
                x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else ""
            parts.append(f"[result] {(txt or '')[:400]}")
    return parts


def _record_text(rec: dict) -> str:
    """Readable text of one record from its `raw` transcript line (fallback: `content`)."""
    raw = rec.get("raw")
    if isinstance(raw, dict):
        msg = raw.get("message")
        if isinstance(msg, dict):
            role = msg.get("role") or raw.get("type") or rec.get("type") or "msg"
            parts = _blocks_text(role, msg.get("content"))
            if parts:
                return "\n".join(parts)
    elif isinstance(raw, str) and raw.strip():
        return raw.strip()
    c = rec.get("content")
    return f"[{rec.get('type') or 'msg'}] {c.strip()}" if isinstance(c, str) and c.strip() else ""


def _depth(aid: str, by_agent: dict) -> int:
    """spawn_depth if meridian supplies it; the contract currently doesn't, so derive it
    from the parent_agent_id chain (main = 0)."""
    meta = by_agent.get(aid) or {}
    if meta.get("spawn_depth") is not None:
        return meta["spawn_depth"]
    depth, seen = 0, {aid}
    parent = meta.get("parent_agent_id")
    while parent and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = (by_agent.get(parent) or {}).get("parent_agent_id")
    return depth


def iter_agent_segments(records: list[dict], agents: list[dict], target_chars: int = 3000) -> list[dict]:
    """Per-agent ordered segments: group records by agent, order chronologically, then cut
    contiguous runs of ~target_chars (as ccvault_reader.iter_segments does). Boundaries may
    shift between runs; memoization keys on content_hash, so that is cheap."""
    by_agent = {a["agent_id"]: a for a in agents}
    streams: dict[str, list[dict]] = {}
    for r in records:
        streams.setdefault(r.get("agent_id") or "main", []).append(r)

    order = [a["agent_id"] for a in agents] + [k for k in streams if k not in by_agent]
    segments = []
    for aid in order:
        recs = streams.get(aid)
        if not recs:
            continue
        recs = sorted(recs, key=lambda r: (r.get("sort_ts") or r.get("timestamp") or "", r.get("seq") or 0))
        meta = by_agent.get(aid, {})
        texts = [t for t in (_record_text(r) for r in recs) if t]
        runs, cur, size = [], [], 0
        for t in texts:
            cur.append(t)
            size += len(t)
            if size >= target_chars:
                runs.append(cur)
                cur, size = [], 0
        if cur:
            runs.append(cur)
        for seq, run in enumerate(runs):
            text = "\n\n".join(run)
            segments.append({
                "agent_id": aid,
                "agent_type": meta.get("agent_type"),
                "parent_tool_use_id": meta.get("parent_tool_use_id"),
                "spawn_depth": _depth(aid, by_agent),
                "seq": seq,
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            })
    return segments
