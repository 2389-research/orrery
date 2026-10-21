# ABOUTME: get_entity/get_document/MCP graph-traversal reads surface silo_id + kind,
# ABOUTME: resolved LIVE via the silo_kind view — never a per-doc/per-entity copy.

"""Task 11a: read-path exposure of provenance.

The distinguishing property under test isn't just "the field is present" — it's that
`kind` is resolved fresh on every call. Re-classifying a SOURCE's `provenance_kind`
must change what the very next `get_entity`/`get_document` call reports, with no
re-ingest and no per-row write anywhere near the document or entity itself. That's
what the `_reflects_updated_kind_with_no_reingest` tests pin.
"""

import numpy as np
import pytest

import src.mcp_server as mcp_server


def _seed_one_source(store, silo="ws1", kind="neutral_summary"):
    """One watched_source (the silo), one document silo'd to it, one entity sourced
    from that document, all in a domain so nothing here is "unplaceable"."""
    c = store.conn
    c.execute("INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'repo', '/tmp/x', ?)",
              (silo, kind))
    c.execute("INSERT INTO documents (id, title, silo_id) VALUES ('d1', 'Doc One', ?)", (silo,))
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 1)")
    c.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) "
              "VALUES ('d1', 'alpha', 1, 1.0)")
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1', 'Entity One', 'Concept')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('e1', 'd1')")
    c.commit()


# ── get_entity (REST) ─────────────────────────────────────────────────────────

def test_get_entity_sources_include_silo_and_kind(test_client, test_store):
    _seed_one_source(test_store)
    body = test_client.get("/entities/e1").json()
    assert body["sources"][0]["silo_id"] == "ws1"
    assert body["sources"][0]["kind"] == "neutral_summary"


def test_get_entity_source_with_no_silo_reports_none(test_client, test_store):
    c = test_store.conn
    c.execute("INSERT INTO documents (id, title) VALUES ('d1', 'Loose doc')")
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1', 'Loose Entity', 'Concept')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('e1', 'd1')")
    c.commit()
    body = test_client.get("/entities/e1").json()
    assert body["sources"][0]["silo_id"] is None
    assert body["sources"][0]["kind"] is None


# ── get_document (REST) ───────────────────────────────────────────────────────

def test_get_document_includes_silo_and_kind(test_client, test_store):
    _seed_one_source(test_store)
    body = test_client.get("/documents/d1").json()
    assert body["silo_id"] == "ws1"
    assert body["kind"] == "neutral_summary"


def test_get_document_with_no_silo_has_no_kind(test_client, test_store):
    test_store.conn.execute("INSERT INTO documents (id, title) VALUES ('d2', 'Loose')")
    test_store.conn.commit()
    body = test_client.get("/documents/d2").json()
    assert body["silo_id"] is None
    assert body["kind"] is None


# ── live resolution — no staleness, no re-ingest ──────────────────────────────

def test_get_entity_reflects_updated_kind_with_no_reingest(test_client, test_store):
    _seed_one_source(test_store, silo="ws2", kind="neutral_summary")
    before = test_client.get("/entities/e1").json()
    assert before["sources"][0]["kind"] == "neutral_summary"

    test_store.conn.execute(
        "UPDATE watched_sources SET provenance_kind = 'agent_report' WHERE id = 'ws2'")
    test_store.conn.commit()

    after = test_client.get("/entities/e1").json()
    assert after["sources"][0]["kind"] == "agent_report"
    assert after["sources"][0]["silo_id"] == "ws2"   # untouched — only the kind moved


def test_get_document_reflects_updated_kind_with_no_reingest(test_client, test_store):
    _seed_one_source(test_store, silo="ws3", kind="human_vault")
    before = test_client.get("/documents/d1").json()
    assert before["kind"] == "human_vault"

    test_store.conn.execute(
        "UPDATE watched_sources SET provenance_kind = 'agent_report' WHERE id = 'ws3'")
    test_store.conn.commit()

    after = test_client.get("/documents/d1").json()
    assert after["kind"] == "agent_report"
    assert after["silo_id"] == "ws3"


# ── MCP read path (#48) — the REST endpoint it proxies ────────────────────────

def test_graph_neighborhood_route_annotates_nodes_with_silo_and_kind(test_client, test_store):
    """`/graph/neighborhood` is what the MCP get_neighborhood tool calls."""
    _seed_one_source(test_store, silo="ws4", kind="human_reviewed")
    body = test_client.get("/graph/neighborhood", params={"name": "Entity One", "depth": 1}).json()
    assert body["seed"]["silo_id"] == "ws4"
    assert body["seed"]["kind"] == "human_reviewed"


def test_graph_subgraph_route_annotates_nodes_with_silo_and_kind(test_client, test_store):
    _seed_one_source(test_store, silo="ws5", kind="agent_report")
    body = test_client.post("/graph/subgraph", json={"entity_names": ["Entity One"], "max_hops": 1}).json()
    assert body["seeds"][0]["silo_id"] == "ws5"
    assert body["seeds"][0]["kind"] == "agent_report"


# ── MCP tool text formatting ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_get_entity_surfaces_silo_and_kind(monkeypatch):
    async def fake(path, method="GET", body=None):
        if "neighborhood" in path:
            return {"seed": {"id": "e1"}}
        if "cooccurrences" in path:
            return []
        return {"canonical_name": "Widget", "type": "Concept",
                "sources": [{"document_id": "d1", "silo_id": "ws1", "kind": "agent_report"}]}

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.get_entity("Widget")
    assert "ws1" in out and "agent_report" in out


@pytest.mark.asyncio
async def test_mcp_get_neighborhood_surfaces_silo_and_kind(monkeypatch):
    async def fake(path, method="GET", body=None):
        return {
            "seed": {"id": "e1", "name": "Widget", "type": "Concept",
                     "silo_id": "ws1", "kind": "agent_report"},
            "depth": 1, "node_count": 2, "edge_count": 1,
            "nodes": [
                {"id": "e1", "name": "Widget", "type": "Concept", "depth": 0,
                 "source_count": 3, "silo_id": "ws1", "kind": "agent_report"},
                {"id": "e2", "name": "Gadget", "type": "Concept", "depth": 1,
                 "source_count": 1, "silo_id": None, "kind": None},
            ],
            "edges": [],
        }

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.get_neighborhood("Widget")
    assert "ws1" in out and "agent_report" in out
    assert "no silo" in out  # the None-silo node is surfaced too, not silently dropped


# ── search_knowledge_graph (REST /search + the MCP tool) ──────────────────────
#
# The plan calls this out by name three times (Task 11, Task 12 acceptance, Definition
# of Done) — it's the primary agent read where provenance matters most.

def _seed_search_hit(store, silo="ws6", kind="agent_report"):
    """One document (silo'd), one chunk on it, one entity sourced from that chunk.

    The entity name is queried verbatim so `search_entities_exact` gives a guaranteed,
    deterministic hit (score 1.0) regardless of what the embedding model makes of it —
    and because that entity has a `chunk_id`, `boost_chunks_via_entities` pulls its
    chunk into `boosted_chunks`, giving a deterministic CHUNK hit too, with no
    dependence on semantic ranking for either channel. A stored embedding keeps
    `build_indexes` from calling the model for this entity; the query string itself
    still goes through the real (locally cached) sentence-transformer via `embed_text`,
    same as every other real `/search` call in this suite (see test_image_routes.py).
    """
    c = store.conn
    c.execute("INSERT INTO watched_sources (id, type, uri, provenance_kind) VALUES (?, 'repo', '/tmp/x', ?)",
              (silo, kind))
    c.execute("INSERT INTO documents (id, title, silo_id) VALUES ('d1', 'Doc One', ?)", (silo,))
    c.execute("INSERT INTO chunks (id, document_id, chunk_index, text) VALUES "
              "('c1', 'd1', 0, 'Some text about the provenance widget.')")
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    c.execute("INSERT INTO entities (id, canonical_name, type, embedding) VALUES (?, ?, 'Concept', ?)",
              ("e1", "provenance widget", vec.tobytes()))
    c.execute("INSERT INTO entity_sources (entity_id, document_id, chunk_id) VALUES ('e1', 'd1', 'c1')")
    c.commit()


@pytest.fixture(autouse=True)
def _force_fresh_search_index():
    """The FAISS index readiness is module-level (`pipeline._indexes_ready`, a per-
    workspace set since #113), so without this a search test can silently reuse an index
    another test built against a DIFFERENT (already-closed) database — same trap
    `test_search_invalid_at.py` guards against. Clearing before and restoring after keeps
    this file's search tests independent of run order."""
    from src.pipeline.search import pipeline as search_pipeline
    before = set(search_pipeline._indexes_ready)
    search_pipeline._indexes_ready.clear()
    yield
    search_pipeline._indexes_ready.clear()
    search_pipeline._indexes_ready.update(before)


def test_search_entity_and_chunk_hits_carry_silo_and_kind(test_client, test_store):
    _seed_search_hit(test_store)
    body = test_client.get("/search", params={"q": "provenance widget", "expand": "false"}).json()

    assert body["entities"], "the exact-match channel must have surfaced e1"
    entity_hit = next(e for e in body["entities"] if e["id"] == "e1")
    assert entity_hit["silo_id"] == "ws6"
    assert entity_hit["kind"] == "agent_report"

    assert body["chunks"], "entity-boost must have surfaced c1 via e1's chunk_id"
    chunk_hit = next(c for c in body["chunks"] if c["document_id"] == "d1")
    assert chunk_hit["silo_id"] == "ws6"
    assert chunk_hit["kind"] == "agent_report"


def test_search_reflects_updated_kind_with_no_reingest(test_client, test_store):
    _seed_search_hit(test_store, silo="ws7", kind="neutral_summary")
    before = test_client.get("/search", params={"q": "provenance widget", "expand": "false"}).json()
    before_entity = next(e for e in before["entities"] if e["id"] == "e1")
    assert before_entity["kind"] == "neutral_summary"

    test_store.conn.execute(
        "UPDATE watched_sources SET provenance_kind = 'agent_report' WHERE id = 'ws7'")
    test_store.conn.commit()

    from src.pipeline.search import pipeline as search_pipeline
    search_pipeline._indexes_ready.clear()   # force a fresh search, not a cached FAISS build

    after = test_client.get("/search", params={"q": "provenance widget", "expand": "false"}).json()
    after_entity = next(e for e in after["entities"] if e["id"] == "e1")
    assert after_entity["kind"] == "agent_report"
    assert after_entity["silo_id"] == "ws7"   # untouched — only the kind moved


@pytest.mark.asyncio
async def test_mcp_search_knowledge_graph_surfaces_silo_and_kind(monkeypatch):
    async def fake(path, method="GET", body=None):
        return {
            "query": "widget", "total_entities": 1, "total_chunks": 1,
            "sub_queries_used": [],
            "entities": [{"id": "e1", "name": "Widget", "type": "Concept",
                          "source_count": 2, "score": 1.0, "appearances": 1,
                          "paths": ["exact"], "silo_id": "ws1", "kind": "agent_report"}],
            "chunks": [{"document_id": "d1", "document_title": "Doc One", "text": "...",
                        "entity_overlap": 1, "matching_entities": "Widget",
                        "silo_id": "ws1", "kind": "agent_report"}],
        }

    monkeypatch.setattr(mcp_server, "call_api", fake)
    out = await mcp_server.search_knowledge_graph("widget")
    assert "ws1" in out and "agent_report" in out
