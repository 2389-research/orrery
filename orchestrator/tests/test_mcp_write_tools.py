# ABOUTME: MCP write tools (#48) proxy the right REST endpoints and format results sanely.
import pytest

import src.mcp_server as mcp_server


@pytest.fixture(autouse=True)
def _selected_workspace(monkeypatch):
    # The mutating tools require an active noosphere; default the tests into one.
    monkeypatch.setattr(mcp_server, "_active_workspace", "ws1")


@pytest.mark.asyncio
async def test_ingest_text_posts_to_endpoint(monkeypatch):
    seen = {}

    async def fake(path, method="GET", body=None):
        seen.update(path=path, method=method, body=body)
        return {"document_id": "d1", "title": "Note", "domains": ["a/b"], "entity_count": 3}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_text("Note", "hello")
    assert seen == {"path": "/ingest/text", "method": "POST",
                    "body": {"title": "Note", "content": "hello"}}
    assert "d1" in out and "3 entities" in out


@pytest.mark.asyncio
async def test_ingest_text_requires_selected_noosphere(monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_workspace", None)
    called = False

    async def fake(*a, **k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_text("Note", "hello")
    assert "select_noosphere" in out and not called   # guarded before any write


@pytest.mark.asyncio
async def test_ingest_text_reports_failure(monkeypatch):
    async def fake(path, **k):
        return {"status": 500, "detail": "boom"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_text("x", "y")
    assert "fail" in out.lower() and "boom" in out


@pytest.mark.asyncio
async def test_ingest_repo_posts_to_endpoint(monkeypatch):
    seen = {}

    async def fake(path, method="GET", body=None):
        seen.update(path=path, method=method, body=body)
        return {"job_id": "j1", "collection_id": "c1"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_repo("/data/repos/my-repo", "my-repo")
    assert seen == {"path": "/ingest/repo", "method": "POST",
                    "body": {"path": "/data/repos/my-repo", "name": "my-repo"}}
    assert "j1" in out and "c1" in out and "get_job_status" in out


@pytest.mark.asyncio
async def test_ingest_repo_passes_provenance_when_given(monkeypatch):
    seen = {}

    async def fake(path, method="GET", body=None):
        seen.update(body=body)
        return {"job_id": "j1", "collection_id": "c1"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    await mcp_server.ingest_repo("/data/repos/r", "r", provenance_kind="neutral_summary")
    assert seen["body"]["provenance_kind"] == "neutral_summary"


@pytest.mark.asyncio
async def test_ingest_repo_requires_selected_noosphere(monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_workspace", None)
    called = False

    async def fake(*a, **k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_repo("/data/repos/r", "r")
    assert "select_noosphere" in out and not called


@pytest.mark.asyncio
async def test_ingest_repo_reports_conflict(monkeypatch):
    async def fake(path, **k):
        return {"status": 409, "detail": "Collection 'r' already exists (id c9)"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_repo("/data/repos/r", "r")
    assert "already exists" in out and "c9" in out


@pytest.mark.asyncio
async def test_ingest_repo_bad_path_gets_hint(monkeypatch):
    async def fake(path, **k):
        return {"status": 400, "detail": "Not a directory: /tmp/nope"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.ingest_repo("/tmp/nope", "r")
    assert "failed" in out.lower() and "bind-mount" in out


@pytest.mark.asyncio
async def test_create_noosphere(monkeypatch):
    async def fake(path, method="GET", body=None):
        assert path == "/workspaces" and method == "POST"
        return {"workspaceId": "ws1", "name": body["name"]}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.create_noosphere("My WS", "desc")
    assert "ws1" in out and "My WS" in out


@pytest.mark.asyncio
async def test_trigger_simmer_general_vs_domain(monkeypatch):
    paths = []

    async def fake(path, method="GET", body=None):
        paths.append(path)
        return {"job_id": "j1"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    await mcp_server.trigger_simmer()
    await mcp_server.trigger_simmer("techniques/wet-blending")
    assert paths == ["/simmer/general", "/simmer/techniques/wet-blending"]


@pytest.mark.asyncio
async def test_trigger_normalization(monkeypatch):
    async def fake(path, method="GET", body=None):
        assert path == "/normalize" and method == "POST"
        return {"plural_merges": 3, "queued_for_review": 1}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.trigger_normalization()
    assert "complete" in out.lower()


@pytest.mark.asyncio
async def test_get_job_status_single_with_progress(monkeypatch):
    async def fake(path, **k):
        assert path == "/jobs"                       # list endpoint; resolves id client-side
        return [{"id": "j1", "type": "extract_batch", "status": "running",
                 "progress": {"docs_done": 2, "docs_total": 6, "entities_so_far": 40}, "results": None}]

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.get_job_status("j1")
    assert "2/6" in out and "running" in out


@pytest.mark.asyncio
async def test_get_job_status_unknown_id(monkeypatch):
    async def fake(path, **k):
        return [{"id": "other", "type": "x", "status": "running"}]

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.get_job_status("j1")
    assert "no job" in out.lower()


@pytest.mark.asyncio
async def test_get_job_status_api_error_is_readable(monkeypatch):
    # call_api's error envelope carries a `status` int — the tool must not crash on it.
    async def fake(path, **k):
        return {"status": 500, "detail": "boom"}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.get_job_status("j1")
    assert "could not fetch" in out.lower() and "boom" in out


@pytest.mark.asyncio
async def test_get_job_status_lists_only_active(monkeypatch):
    async def fake(path, **k):
        return [
            {"id": "j1", "type": "extract_batch", "status": "running",
             "progress": {"docs_done": 1, "docs_total": 3}},
            {"id": "j2", "type": "simmer_general", "status": "completed"},
        ]

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.get_job_status()
    assert "j1" in out and "j2" not in out
