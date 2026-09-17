# ABOUTME: Parse an agent session log (Claude Code jsonl / tracker activity.jsonl)
# ABOUTME: into an ordered file-touch trace + recursive-summary segments, and
# ABOUTME: persist/read it against a repo COLLECTION. The collection's
# ABOUTME: document_collections tree is the MAP (final repo space); a trace is the
# ABOUTME: motion replayed over it (see GET /collections/{id}/structure + /trace).
#
# Pure module: sqlite + stdlib only, no FastAPI, so it is unit-testable and the
# route handlers stay thin.
import json
import re
import uuid

# Tool -> action vocabulary (mindwalk's closed set). The trajectory follows only
# STRUCTURED file targets; Bash/search command sniffing produced garbage (urls,
# ~/.zshrc, .venv) so it is deliberately dropped — a Bash step is an `exec` event
# with no file, which still advances the playhead but lights nothing.
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_READ_TOOLS = {"Read"}
_SEARCH_TOOLS = {"Grep", "Glob", "LS"}
_SUBAGENT_TOOLS = {"Task", "Agent"}
_VERIFY_RE = re.compile(r"\b(pytest|npm test|go test|make test|cargo test|vitest|jest)\b")
_READCMD_RE = re.compile(r"^(cat|ls|head|tail|grep|find|rg|git log|git diff|git status)\b")
# Harness-injected pseudo-user lines — NOT real user turns, so they never open a segment.
_INJECTED_RE = re.compile(
    r"^(Caveat:|<command-|<local-command|<system-reminder|<task-notification|<user-|"
    r"\[Request interrupted|This session is being continued|Base directory for this skill|"
    r"The following|## |ARGUMENTS:)")
_TERMINAL_RE = re.compile(r"[⇡⚡~]|^\s*\$|·{3,}|^\s*uv run|^\s*python |^\s*git ")

_RANK = {"search": 1, "read": 2, "verify": 1, "exec": 1, "other": 0, "subagent": 0, "edit": 3}


def _action_for(tool, cmd):
    if tool in _EDIT_TOOLS:
        return "edit"
    if tool in _READ_TOOLS:
        return "read"
    if tool in _SEARCH_TOOLS:
        return "search"
    if tool in _SUBAGENT_TOOLS:
        return "subagent"
    if tool == "Bash":
        c = (cmd or "").strip().lower()
        if _VERIFY_RE.search(c):
            return "verify"
        if _READCMD_RE.search(c):
            return "read"
        return "exec"
    return "other"


def _file_for(tool, inp):
    if not isinstance(inp, dict):
        return None
    for k in ("file_path", "notebook_path"):
        if inp.get(k):
            return inp[k]
    return None


def _rel(path, cwd):
    """Collection-relative path so it joins the structure endpoint's leaf titles."""
    if cwd and path.startswith(cwd + "/"):
        return path[len(cwd) + 1:]
    if not path.startswith("/"):
        return path
    return "(external)/" + path.rsplit("/", 1)[-1]


def _clean_req(text):
    text = (text or "").strip()
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _seg_title(request):
    r = _clean_req(request)
    if not r:
        return "Discussion"
    words = r.split()
    t = " ".join(words[:7])
    return (t + "…") if len(words) > 7 else t[:60]


def _seg_summary(request, actions, n_files):
    req = _clean_req(request)
    parts = []
    for k, verb in (("edit", "edited"), ("read", "read"), ("search", "searched"),
                    ("verify", "ran tests"), ("exec", "ran commands"),
                    ("subagent", "dispatched a subagent")):
        if actions.get(k):
            parts.append(f"{verb} ({actions[k]})")
    work = "; ".join(parts) if parts else "no tool activity (discussion)"
    fclause = f" across {n_files} file{'s' if n_files != 1 else ''}" if n_files else ""
    lead = req if req and not _TERMINAL_RE.search(req) else "Continued work"
    return f"{lead}. {work.capitalize()}{fclause}."


def _user_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(it.get("text", "") for it in content
                         if isinstance(it, dict) and it.get("type") == "text").strip()
    return ""


def _is_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(it, dict) and it.get("type") == "tool_result" for it in content)


def parse_session_trace(jsonl_path, cwd=None):
    """Read a Claude Code session jsonl into an ordered file-touch trace.

    Returns {session, events, segments, files}:
      events   = [{seq, action, file_path(rel|None), is_error, segment_idx}]
      segments = [{idx, title, summary, request, start_seq, end_seq, files:[rel]}]
      files    = sorted distinct collection-relative paths touched
    Segment boundaries are genuine user turns (harness-injected lines excluded);
    every real turn is a segment, even a zero-tool discussion turn.
    """
    events, segments, pending = [], [], {}
    cur = None
    session = {"cwd": cwd, "model": None, "id": None, "started": None, "ended": None}

    def close(end):
        nonlocal cur
        if cur is None:
            return
        cur["end_event"] = end
        cur["files"] = sorted(cur["files"])
        cur["has_work"] = end > cur["start_event"]
        segments.append(cur)
        cur = None

    with open(jsonl_path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except Exception:
                continue
            t = line.get("type")
            if not session["id"] and line.get("sessionId"):
                session["id"] = line["sessionId"]
            if not session["cwd"] and line.get("cwd"):
                session["cwd"] = cwd = line["cwd"]
            ts = line.get("timestamp")
            if ts:
                session["started"] = session["started"] or ts
                session["ended"] = ts
            msg = line.get("message")
            if isinstance(msg, dict) and msg.get("model") and not session["model"]:
                session["model"] = msg["model"]
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")

            if t == "user" and not _is_tool_result(content):
                txt = _user_text(content)
                if txt and not _INJECTED_RE.match(txt):
                    close(len(events))
                    cur = {"idx": len(segments), "request": txt[:600],
                           "start_event": len(events), "files": set(), "actions": {}}
                continue
            if not isinstance(content, list):
                continue
            for it in content:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "tool_use":
                    inp = it.get("input") or {}
                    pending[it.get("id")] = {"name": it.get("name"),
                                             "cmd": inp.get("command"),
                                             "file": _file_for(it.get("name"), inp)}
                elif it.get("type") == "tool_result":
                    call = pending.pop(it.get("tool_use_id"), None)
                    if not call:
                        continue
                    act = _action_for(call["name"], call["cmd"])
                    rel = _rel(call["file"], session["cwd"]) if call["file"] else None
                    seg_idx = cur["idx"] if cur is not None else None
                    events.append({"seq": len(events), "action": act, "file_path": rel,
                                   "is_error": bool(it.get("is_error")), "segment_idx": seg_idx})
                    if cur is not None:
                        if rel:
                            cur["files"].add(rel)
                        cur["actions"][act] = cur["actions"].get(act, 0) + 1
    close(len(events))
    for i, s in enumerate(segments):
        s["idx"] = i

    seg_out = []
    for s in segments:
        seg_out.append({"idx": s["idx"], "title": _seg_title(s["request"]),
                        "summary": _seg_summary(s["request"], s["actions"], len(s["files"])),
                        "request": _clean_req(s["request"]),
                        "start_seq": s["start_event"], "end_seq": s["end_event"],
                        "files": s["files"], "has_work": s["has_work"],
                        "actions": s["actions"]})
    files = sorted({e["file_path"] for e in events if e["file_path"]})
    return {"session": session, "events": events, "segments": seg_out, "files": files}


def persist_trace(conn, collection_id, parsed, source="claude_code", title=None):
    """Write a parsed trace against a repo collection. Returns the new trace id.

    One transaction: traces + trace_events + trace_segments, so a crash never
    leaves half a trace. Replaces any prior trace with the same session id for the
    collection (re-ingest is idempotent, like the watched-source sync)."""
    tid = uuid.uuid4().hex
    sess = parsed["session"]
    title = title or sess.get("id") or "session"
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO traces (id, collection_id, source, title, model, n_events, n_segments, n_files) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, collection_id, source, title, sess.get("model"),
             len(parsed["events"]), len(parsed["segments"]), len(parsed["files"])))
        conn.executemany(
            "INSERT INTO trace_events (trace_id, seq, action, file_path, is_error, segment_idx) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(tid, e["seq"], e["action"], e["file_path"], int(e["is_error"]), e["segment_idx"])
             for e in parsed["events"]])
        conn.executemany(
            "INSERT INTO trace_segments (trace_id, idx, title, summary, request, start_seq, end_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(tid, s["idx"], s["title"], s["summary"], s["request"], s["start_seq"], s["end_seq"])
             for s in parsed["segments"]])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tid


def read_trace(conn, collection_id, trace_id=None):
    """Payload for GET /collections/{id}/trace — the latest trace for the collection
    (or a specific trace_id): ordered events + segments, for replay over the map."""
    if trace_id:
        tr = conn.execute("SELECT * FROM traces WHERE id = ? AND collection_id = ?",
                          (trace_id, collection_id)).fetchone()
    else:
        tr = conn.execute("SELECT * FROM traces WHERE collection_id = ? "
                          "ORDER BY created_at DESC LIMIT 1", (collection_id,)).fetchone()
    if not tr:
        return None
    tid = tr["id"]
    events = [{"seq": r["seq"], "action": r["action"], "file_path": r["file_path"],
               "is_error": bool(r["is_error"]), "segment_idx": r["segment_idx"]}
              for r in conn.execute(
                  "SELECT seq, action, file_path, is_error, segment_idx FROM trace_events "
                  "WHERE trace_id = ? ORDER BY seq", (tid,)).fetchall()]
    # each segment carries the files it touched (for the timeline click -> constellation)
    files_by_seg = {}
    for e in events:
        if e["file_path"] and e["segment_idx"] is not None:
            files_by_seg.setdefault(e["segment_idx"], set()).add(e["file_path"])
    segments = [{"idx": r["idx"], "title": r["title"], "summary": r["summary"],
                 "request": r["request"], "start_seq": r["start_seq"], "end_seq": r["end_seq"],
                 "files": sorted(files_by_seg.get(r["idx"], ()))}
                for r in conn.execute(
                    "SELECT idx, title, summary, request, start_seq, end_seq FROM trace_segments "
                    "WHERE trace_id = ? ORDER BY idx", (tid,)).fetchall()]
    return {"trace": {"id": tid, "source": tr["source"], "title": tr["title"],
                      "model": tr["model"], "n_events": tr["n_events"],
                      "n_segments": tr["n_segments"], "n_files": tr["n_files"]},
            "events": events, "segments": segments}
