"""Slice params on the read MCP tools + the scoped-shape fix for list_entities/get_document.

Needs the MCP SDK (imports src.mcp_server), so it skips where `mcp` is absent."""
import pytest

pytest.importorskip("mcp")

import src.mcp_server as mcp_server  # noqa: E402

# The REAL scope shape the server returns (routes.entities._scope == dataclasses.asdict(Slice)).
SCOPE = {"since": "2026-10-14 00:00:00", "until": None, "domains": None,
         "collections": None, "silo": None, "kind": None}


def _recorder(monkeypatch, responder):
    calls = []

    async def fake(path, method="GET", body=None, params=None):
        calls.append({"path": path, "method": method, "body": body, "params": params})
        return responder(path)

    monkeypatch.setattr(mcp_server, "call_api", fake)
    return calls


def _responder(path):
    if path.startswith("/search"):
        return {"query": "q", "total_entities": 0, "total_chunks": 0, "entities": [],
                "chunks": [], "scope": SCOPE}
    if path.startswith("/graph/neighborhood"):
        return {"seed": {"id": "e1", "name": "A", "type": "Concept"}, "nodes": [], "node_count": 0,
                "edge_count": 0, "depth": 1, "scope": SCOPE}
    if path == "/entities/e1":
        return {"canonical_name": "A", "type": "Concept", "sources": [], "scope": SCOPE}
    if path.endswith("/cooccurrences"):
        return []
    if path.startswith("/graph/shared-context"):
        return {"entity_a": {"name": "A", "type": "T"}, "entity_b": {"name": "B", "type": "T"},
                "summary": {"docs_in_common": 0, "neighbors_in_common": 0, "domains_in_common": 0},
                "direct_weight": 0, "shared_documents": [], "shared_neighbors": [],
                "shared_domains": [], "scope": SCOPE}
    if path.startswith("/graph/paths"):
        return {"entity_a": {"name": "A"}, "entity_b": {"name": "B"}, "path_count": 0,
                "searched_depth": 4, "paths": [], "scope": SCOPE}
    if path.startswith("/graph/subgraph"):
        return {"seeds": [{"name": "A", "type": "T"}], "node_count": 1, "edge_count": 0,
                "nodes": [], "edges": [], "scope": SCOPE}
    if path.startswith("/graph/domain-overview"):
        return {"domain": {"path": "x", "document_count": 0}, "documents": [],
                "entity_type_distribution": {}, "top_entities": [], "related_domains": [],
                "scope": SCOPE}
    raise AssertionError(path)


CASES = [
    ("search_knowledge_graph", ("q",), "/search"),
    ("get_entity", ("A",), "/entities/e1"),
    ("get_neighborhood", ("A",), "/graph/neighborhood"),
    ("get_shared_context", ("A", "B"), "/graph/shared-context"),
    ("find_paths", ("A", "B"), "/graph/paths"),
    ("get_subgraph", (["A"],), "/graph/subgraph"),
    ("explore_domain", ("x",), "/graph/domain-overview"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,prefix", CASES)
async def test_read_tools_forward_window(monkeypatch, tool, args, prefix):
    calls = _recorder(monkeypatch, _responder)
    out = await getattr(mcp_server, tool)(*args, window="1w")
    hit = [c for c in calls if c["path"].startswith(prefix) and not c["path"].endswith("/cooccurrences")]
    assert hit and all(c["params"] == {"window": "1w"} for c in hit), calls
    assert "Slice: " in out and "since=2026-10-14 00:00:00" in out
    assert "until=" not in out and "silo=" not in out


@pytest.mark.asyncio
async def test_get_subgraph_keeps_body_and_puts_facets_in_params(monkeypatch):
    calls = _recorder(monkeypatch, _responder)
    await mcp_server.get_subgraph(["A"], max_hops=2, since="-7d")
    c = calls[0]
    assert c["body"] == {"entity_names": ["A"], "max_hops": 2}
    assert c["params"] == {"since": "-7d"}


@pytest.mark.asyncio
async def test_unsliced_call_sends_no_params(monkeypatch):
    calls = _recorder(monkeypatch, _responder)
    await mcp_server.get_neighborhood("A")
    assert calls[0]["params"] is None


ENT = [{"id": "e1", "canonical_name": "Widget", "type": "Concept", "source_count": 3}]


@pytest.mark.asyncio
async def test_list_entities_bare_list(monkeypatch):
    _recorder(monkeypatch, lambda p: ENT)
    out = await mcp_server.list_entities()
    assert "Entities (1)" in out and "Widget" in out and "Slice:" not in out


@pytest.mark.asyncio
async def test_list_entities_scoped_shape(monkeypatch):
    """Regression: under a view /entities returns {entities, scope}, which used to break the tool."""
    calls = _recorder(monkeypatch, lambda p: {"entities": ENT, "scope": SCOPE})
    out = await mcp_server.list_entities(window="1w")
    assert "Entities (1)" in out and "Widget" in out
    assert "Slice: " in out and "since=2026-10-14" in out
    assert calls[0]["params"] == {"window": "1w"}


@pytest.mark.asyncio
async def test_list_entities_error_is_reported(monkeypatch):
    _recorder(monkeypatch, lambda p: {"detail": "API 500: boom"})
    assert "500" in await mcp_server.list_entities()


@pytest.mark.asyncio
async def test_get_document_handles_scoped_documents_shape(monkeypatch):
    def responder(path):
        if path == "/documents":
            return {"documents": [{"id": "d1", "title": "My Doc"}], "scope": SCOPE}
        return {"document": {"id": "d1", "title": "My Doc", "domains": []}, "entities": [],
                "total_mentions": 0, "segments": [{"text": "hello"}]}

    _recorder(monkeypatch, responder)
    out = await mcp_server.get_document("my doc")
    assert "Document: My Doc" in out and "hello" in out


DOCS = [{"id": "d1", "title": "Alpha", "created_at": "2026-10-15", "content_type": "text"}]


@pytest.mark.asyncio
async def test_list_documents_forwards_facets_order_limit(monkeypatch):
    calls = _recorder(monkeypatch, lambda p: DOCS)
    out = await mcp_server.list_documents(window="1w", domain="a/b", order="created_at", limit=5)
    assert calls[0]["path"] == "/documents"
    assert calls[0]["params"] == {"window": "1w", "domain": "a/b", "order": "created_at", "limit": 5}
    assert "Alpha" in out and "2026-10-15" in out and "text" in out and "Slice:" not in out


@pytest.mark.asyncio
async def test_list_documents_scoped_shape(monkeypatch):
    calls = _recorder(monkeypatch, lambda p: {"documents": DOCS, "scope": SCOPE})
    out = await mcp_server.list_documents(window="1w")
    assert calls[0]["params"] == {"window": "1w", "limit": 20}
    assert "Alpha" in out and "Slice: " in out and "since=2026-10-14" in out


@pytest.mark.asyncio
async def test_list_documents_error(monkeypatch):
    _recorder(monkeypatch, lambda p: {"detail": "API 500: boom"})
    assert "Error" in await mcp_server.list_documents()


@pytest.mark.asyncio
async def test_create_view_posts_body(monkeypatch):
    calls = _recorder(monkeypatch, lambda p: {"id": "v1", "name": "Last week"})
    out = await mcp_server.create_view("Last week", "ws1", window="1w", domain="x")
    c = calls[0]
    assert c["path"] == "/views" and c["method"] == "POST"
    assert c["body"] == {"name": "Last week", "parent": "ws1",
                         "slice": {"window": "1w", "domain": "x"}}
    assert "v1" in out and "Last week" in out and "window=1w" in out and "select_noosphere" in out


@pytest.mark.asyncio
async def test_create_view_error(monkeypatch):
    _recorder(monkeypatch, lambda p: {"detail": "API 400: slice has no facets"})
    out = await mcp_server.create_view("v", "ws1")
    assert out.startswith("Error: ") and "no facets" in out


@pytest.mark.asyncio
async def test_list_noospheres_marks_views(monkeypatch):
    regs = [{"id": "w1", "name": "Real"},
            {"id": "v1", "name": "Recent", "kind": "virtual", "parent": "w1",
             "slice": {"window": "1w"}}]
    _recorder(monkeypatch, lambda p: regs)
    out = await mcp_server.list_noospheres()
    real = next(l for l in out.splitlines() if "Real" in l)
    view = next(l for l in out.splitlines() if "Recent" in l)
    assert "view of" not in real
    assert "[view of w1: window=1w]" in view


@pytest.mark.asyncio
async def test_explore_domain_does_not_forward_domain_facet(monkeypatch):
    calls = _recorder(monkeypatch, _responder)
    await mcp_server.explore_domain("x", window="1w", domain="ignored")
    assert calls[0]["params"] == {"window": "1w"}


def test_slice_line_generic_facets():
    line = mcp_server._slice_line({**SCOPE, "since": None, "domains": ["a", "b"], "silo": "s1"})
    assert line == "\nSlice: domains=a,b silo=s1"
    assert mcp_server._slice_line({**SCOPE, "since": None}) == ""
    assert mcp_server._slice_line(None) == ""


@pytest.mark.asyncio
async def test_get_entity_slices_cooccurrences(monkeypatch):
    calls = _recorder(monkeypatch, _responder)
    await mcp_server.get_entity("A", domain="a/b")
    co = [c for c in calls if c["path"].endswith("/cooccurrences")]
    assert co and all(c["params"] == {"domain": "a/b"} for c in co), calls
