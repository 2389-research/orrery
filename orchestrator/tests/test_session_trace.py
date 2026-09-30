"""Parse → persist → read round-trip for the session-trace replay backend.

File-backed SQLite via a tmp_path (never :memory: — the store opens two connections
and WAL is unavailable in memory; see the note in this repo's testing docs)."""
import json
import uuid

from src.db import init_db, get_connection
from src.pipeline.session_trace import parse_session_trace, persist_trace, read_trace


def _write_session(path):
    """A minimal Claude Code session: one user turn, three tool calls (read/edit/bash),
    each paired with its result."""
    lines = [
        {"type": "user", "sessionId": "s1", "cwd": "/repo",
         "message": {"role": "user", "content": "fix the loader"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/repo/src/loader.py"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "...", "is_error": False}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Edit",
             "input": {"file_path": "/repo/src/loader.py", "old_string": "a", "new_string": "b"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok", "is_error": False}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": "1 passed", "is_error": False}]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines))


def test_parse_maps_actions_and_relative_paths(tmp_path):
    src = tmp_path / "session.jsonl"
    _write_session(src)
    parsed = parse_session_trace(str(src), cwd="/repo")

    assert [e["action"] for e in parsed["events"]] == ["read", "edit", "verify"]
    # structured tools give a collection-relative path; a `pytest` Bash step is `verify`
    # with no file (command sniffing is deliberately dropped).
    assert [e["file_path"] for e in parsed["events"]] == ["src/loader.py", "src/loader.py", None]
    assert parsed["files"] == ["src/loader.py"]
    assert len(parsed["segments"]) == 1
    seg = parsed["segments"][0]
    assert seg["start_seq"] == 0 and seg["end_seq"] == 3
    assert "fix the loader" in seg["request"]


def test_bash_command_is_verify_not_a_file(tmp_path):
    src = tmp_path / "s.jsonl"
    _write_session(src)
    parsed = parse_session_trace(str(src), cwd="/repo")
    verify = [e for e in parsed["events"] if e["action"] == "verify"]
    assert len(verify) == 1 and verify[0]["file_path"] is None


def test_persist_and_read_round_trip(tmp_path):
    src = tmp_path / "session.jsonl"
    _write_session(src)
    dbp = str(tmp_path / "orrery.db")
    init_db(dbp)
    conn = get_connection(dbp)
    cid = uuid.uuid4().hex
    conn.execute("INSERT INTO collections (id, name, path, kind) VALUES (?,?,?,?)",
                 (cid, "repo", "/repos/repo", "git_repo"))
    conn.commit()

    parsed = parse_session_trace(str(src), cwd="/repo")
    tid = persist_trace(conn, cid, parsed, source="claude_code", title="t")
    conn.commit()

    out = read_trace(conn, cid)
    assert out["trace"]["id"] == tid
    assert out["trace"]["source"] == "claude_code"
    assert len(out["events"]) == len(parsed["events"])
    assert [e["file_path"] for e in out["events"]] == ["src/loader.py", "src/loader.py", None]
    assert len(out["segments"]) == 1
    # the segment carries the files it touched (for the timeline → constellation)
    assert out["segments"][0]["files"] == ["src/loader.py"]

    # a specific trace_id resolves; an unknown one is None (endpoint returns empty)
    assert read_trace(conn, cid, tid)["trace"]["id"] == tid
    assert read_trace(conn, cid, "nope") is None
