"""The MCP entity lookup: encode the name, and don't call an outage a 404."""

import pytest

pytest.importorskip("mcp")  # FastMCP optional dep; skip these when absent
import src.mcp_server as mcp_server


@pytest.mark.asyncio
async def test_the_name_is_encoded_as_a_path_segment(monkeypatch):
    """An entity name is DATA in a path segment. Unencoded, a name containing `?`,
    `#` or `/` is truncated or split, and the lookup fails for a reason the caller
    cannot see from the message."""
    seen = {}

    async def fake_call_api(path, *a, **k):
        if "neighborhood" in path:
            seen["path"] = path
            return {"seed": {"id": "e1"}}
        if "cooccurrences" in path:
            return []
        return {"canonical_name": "x", "type": "Concept", "sources": []}

    monkeypatch.setattr(mcp_server, "call_api", fake_call_api)
    await mcp_server.get_entity("c++/cli?x#y")

    assert "c%2B%2B%2Fcli%3Fx%23y" in seen["path"], seen["path"]


@pytest.mark.asyncio
async def test_a_server_error_is_not_reported_as_a_missing_entity(monkeypatch):
    """Reporting an outage as "not found" tells the caller the graph lacks something
    it may well contain — worse than an error, because they stop looking."""
    async def failing(path, *a, **k):
        return {"detail": "API 500: upstream exploded"}

    monkeypatch.setattr(mcp_server, "call_api", failing)
    out = await mcp_server.get_entity("widget")
    assert "not found" not in out.lower()
    assert "500" in out


@pytest.mark.asyncio
async def test_a_real_404_still_reads_as_not_found(monkeypatch):
    async def missing(path, *a, **k):
        return {"detail": "API 404: Entity not found: widget"}

    monkeypatch.setattr(mcp_server, "call_api", missing)
    assert "not found" in (await mcp_server.get_entity("widget")).lower()
