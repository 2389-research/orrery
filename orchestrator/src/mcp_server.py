"""MCP server exposing the Orrery knowledge graph as tools.

Run with: python -m src.mcp_server

Install (Claude Code) — register the stdio server once (orchestrator must be running):
    claude mcp add noospheric-orrery -- \
        env ORCHESTRATOR_URL=http://localhost:8100 python -m src.mcp_server
(run from the `orchestrator/` dir, or point at its venv). Transport is stdio today; an
HTTP transport for Docker/remote callers is a follow-up.

Tools — Session:
- list_noospheres() — see available workspaces
- select_noosphere(name_or_id) — set active workspace for subsequent calls

Tools — Search & Read:
- search_knowledge_graph(query, top_k, include_images, expand) — semantic search over entities and chunks (optionally cross-modal image hits; expand=True adds LLM query enrichment)
- search_images(query, top_k) — SigLIP cross-modal image search
- get_entity(name) — look up entity details, sources, co-occurrences
- get_document(title) — read a document with entity highlights (notes content_type for image docs)
- list_domains() — see the domain taxonomy
- list_entities(type, limit) — browse entities by type

Tools — Graph Traversal:
- get_neighborhood(entity_name, depth, max_nodes) — multi-hop neighborhood expansion
- get_shared_context(entity_a, entity_b) — shared docs, neighbors, domains between two entities
- find_paths(entity_a, entity_b, max_depth) — shortest path(s) through co-occurrence edges
- get_subgraph(entity_names, max_hops) — bounded subgraph around seed entities
- explore_domain(domain_path) — domain overview with top entities and related domains

Tools — Write (ingestion & jobs; direct writes, no human gate):
- ingest_text(title, content) — ingest a document from raw text into the active noosphere
- ingest_repo(path, name, provenance_kind) — ingest a git repo (codesum summaries + entities)
    into a collection; `path` is a SERVER-SIDE dir the services can see, not a URL/upload. Async — poll get_job_status.
- create_noosphere(name, description) — create a new workspace
- trigger_simmer(domain) — start a spec-simmer job (general, or a domain path)
- trigger_normalization() — cluster + merge near-duplicate entities (uncertain pairs → review queue)
- get_job_status(job_id) — a job's status + live progress, or list running/queued jobs

Tools — Corrections (write; human-gated):
- propose_correction(action, entity, rationale, target_b, proposed_type, proposed_name) — file a graph correction for human review

Machine-traceable ids (issue #93): every read tool emits stable ids alongside the prose so
a session's graph use can be correlated to the exact nodes it touched from its own log —
`[query:qry_…]` (the API-owned correlation id, SURFACED from the response body — the MCP never
mints its own), and `[entity:…]` / `[doc:…]` on the lines (mirrors `[image:{document_id}]`).
See _query_tag / _eid / _did below and tests/test_mcp_capture_ids.py.
"""

import os
import json
import httpx
from mcp.server.fastmcp import FastMCP
from urllib.parse import quote

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8100")

mcp = FastMCP("noospheric-orrery")


# ── Machine-traceable ids for "active work" capture (issue #93) ────────────────
# Read tools emit stable ids alongside the human prose so a Claude Code / Codex
# session's use of the graph can later be correlated to the exact nodes it touched
# (session logs persist tool_result content verbatim; ccvault archives it). The tag
# format MIRRORS the existing `[image:{document_id}]` convention. Recovery is by RESERVED
# PREFIX only: read ids from `[query:...]`, `[entity:...]`, `[doc:...]` and `[image:...]`
# (images keep their own prefix), and ignore other bracketed text — document titles are
# printed in `[...]` and are free-form, so match the prefixes, not any `[...]`. One
# alternation `\[(query|entity|doc|image):([^\]]+)\]` recovers them all.
# The `query_id` is OWNED BY THE API (issue #93): every read endpoint mints it and returns it
# in the response body (and an X-Query-Id header). The MCP SURFACES that id — it never mints its
# own — so an MCP call and a bare `curl` to the same endpoint reference one id-space, and the
# server-side query_log (future) is the single source of truth.

def _query_tag(result) -> str:
    """The server's per-request correlation id, surfaced from the response body. Empty string
    when the endpoint didn't return one (e.g. list endpoints, or an error) — never minted here."""
    qid = result.get("query_id") if isinstance(result, dict) else None
    return f" [query:{qid}]" if qid else ""


def _eid(entity: dict) -> str:
    """Parseable entity-id tag; empty string when no id is present (never raises)."""
    v = entity.get("id")
    return f" [entity:{v}]" if v else ""


def _did(obj: dict) -> str:
    """Parseable document-id tag; empty string when absent (never raises).

    Accepts either `document_id` (search chunks) or `id` (document/reader payloads,
    shared-context docs, domain-overview docs) so every doc-bearing shape can use this
    one helper. Only ever called on document/chunk dicts, where a bare `id` IS the doc id
    (never on an entity dict, whose `id` is the entity id — those use `_eid`)."""
    v = obj.get("document_id") or obj.get("id")
    return f" [doc:{v}]" if v else ""

# Session-level workspace selection.
# Safe as a module global: FastMCP runs as a per-client subprocess via stdio,
# so each MCP client launches its own server process and this state is
# effectively per-client. If we ever expose this server over SSE/HTTP with
# multiple concurrent clients, move this onto the connection context.
_active_workspace: str | None = None


async def call_api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Call the orchestrator API. Returns the JSON body on success, or a
    {"detail": ...} dict on transport / status / decode errors so MCP tools
    can surface a readable message instead of crashing.

    Status errors also carry `status`, so a caller that needs to distinguish "the graph
    does not have this" from "the lookup failed" can branch on the code instead of
    pattern-matching the human-readable message."""
    headers = {}
    if _active_workspace:
        headers["X-Workspace-Id"] = _active_workspace
    try:
        async with httpx.AsyncClient() as client:
            if method == "GET":
                resp = await client.get(f"{ORCHESTRATOR_URL}{path}", headers=headers, timeout=30)
            else:
                resp = await client.post(f"{ORCHESTRATOR_URL}{path}", headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        return {"status": e.response.status_code,
                "detail": f"API {e.response.status_code}: {e.response.text[:200]}"}
    except httpx.RequestError as e:
        return {"detail": f"Connection error: {e}"}
    except json.JSONDecodeError:
        return {"detail": "Invalid (non-JSON) response from orchestrator"}


# ── Session ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_noospheres() -> str:
    """List all available noospheres (workspaces). Use select_noosphere() to pick one before querying."""
    workspaces = await call_api("/workspaces")
    if isinstance(workspaces, dict) and "detail" in workspaces:
        return f"Error: {workspaces['detail']}"
    lines = ["Available noospheres:\n"]
    for ws in workspaces:
        active = " ← active" if _active_workspace == ws["id"] else ""
        lines.append(f"  • {ws['name']} (id: {ws['id']}){active}")
    if not _active_workspace:
        lines.append("\nNo noosphere selected — use select_noosphere() to pick one.")
    return "\n".join(lines)


@mcp.tool()
async def select_noosphere(name_or_id: str) -> str:
    """Select a noosphere (workspace) by name or ID. All subsequent tool calls will query this noosphere."""
    global _active_workspace
    workspaces = await call_api("/workspaces")
    if isinstance(workspaces, dict) and "detail" in workspaces:
        return f"Error: {workspaces['detail']}"
    match = next(
        (ws for ws in workspaces
         if ws["id"] == name_or_id or ws["name"].lower() == name_or_id.lower()),
        None,
    )
    if not match:
        names = ", ".join(ws["name"] for ws in workspaces)
        return f"No noosphere matching '{name_or_id}'. Available: {names}"
    _active_workspace = match["id"]
    stats = await call_api("/stats")
    return (
        f"Switched to noosphere: {match['name']} (id: {match['id']})\n"
        f"  {stats.get('document_count', 0)} documents, "
        f"{stats.get('entity_count', 0)} entities, "
        f"{stats.get('domain_count', 0)} domains"
    )


# ── Search & Read ───────────────────────────────────────────────────────────

@mcp.tool()
async def search_knowledge_graph(query: str, top_k: int = 15, include_images: bool = False, expand: bool = False) -> str:
    """Search the knowledge graph for entities and document chunks matching a query.
    Returns ranked entities with types and relevant document excerpts.
    Set include_images=True to also surface visually-matching image documents via SigLIP cross-modal search.
    Set expand=True to LLM-generate sub-queries for higher recall — off by default because it adds
    latency and depends on the LLM backend being reachable (plain semantic search does not).
    The galaxy visualization will light up showing where the results live in the graph."""
    inc = "true" if include_images else "false"
    exp = "true" if expand else "false"
    result = await call_api(f"/search?q={quote(query)}&top_k={top_k}&expand={exp}&include_images={inc}")
    if "detail" in result and "query" not in result:
        return f"Search error: {result['detail']}"
    lines = [f"Search: \"{result['query']}\" — {result['total_entities']} entities, {result['total_chunks']} chunks{_query_tag(result)}"]
    subs = result.get("sub_queries_used") or []
    # With expand=false the pipeline reports the original query as its own sole sub-query;
    # don't echo it back as a "Sub-queries" line — only show genuine expansion.
    if subs and subs != [result.get("query")]:
        lines.append(f"Sub-queries: {', '.join(subs)}")
    lines.append("\nTop entities:")
    for e in result["entities"][:10]:
        paths = ",".join(e.get("paths", []))
        hits = e.get("appearances", 0)
        # silo_id/kind resolved live by the orchestrator (task 11a): a source
        # re-classified after ingest shows up here on the next search, no re-ingest.
        silo = e.get("silo_id") or "no silo"
        kind = e.get("kind") or "unspecified kind"
        lines.append(f"  • {e['name']} ({e['type']}) — {e.get('source_count', 0)} docs, score {e['score']:.4f} "
                     f"[{hits} sub-query hits, via {paths}] [{silo}, {kind}]{_eid(e)}")
    lines.append("\nRelevant excerpts:")
    for c in result["chunks"][:5]:
        overlap = c.get("entity_overlap", 0)
        matching = c.get("matching_entities", "")
        silo = c.get("silo_id") or "no silo"
        kind = c.get("kind") or "unspecified kind"
        lines.append(f"  [{c['document_title']}]{_did(c)} (entities:{overlap}) [{silo}, {kind}]: {c['text'][:200]}")
        if matching:
            lines.append(f"    entities in chunk: {matching}")
    images = result.get("images") or []
    if images:
        lines.append(f"\nImage matches ({len(images)}):")
        for img in images[:5]:
            lines.append(f"  [image:{img['document_id']}] {img['title']} (score {img.get('score', 0):.3f}) — {img.get('description', '')}")
    return "\n".join(lines)


@mcp.tool()
async def search_images(query: str, top_k: int = 10) -> str:
    """Cross-modal image search via SigLIP. Returns image documents whose visual content
    matches the text query — pictures of "miniature painting", "city street", "fish tank",
    etc., even when the query text doesn't appear in the image description.
    Falls back to sentence-transformer text similarity on descriptions if SigLIP is unavailable."""
    result = await call_api(f"/search?q={quote(query)}&top_k={top_k}&expand=false&include_images=true")
    if "detail" in result and "query" not in result:
        return f"Image search error: {result['detail']}"
    images = result.get("images") or []
    if not images:
        return f"No image documents matched \"{query}\"."
    lines = [f"Image search: \"{query}\" — {len(images)} matches{_query_tag(result)}"]
    for img in images:
        lines.append(f"  [image:{img['document_id']}] {img['title']} (score {img.get('score', 0):.3f})")
        desc = (img.get('description') or '').strip()
        if desc:
            lines.append(f"    {desc}")
    return "\n".join(lines)


@mcp.tool()
async def get_entity(name: str) -> str:
    """Look up a specific entity by name. Returns its type, source documents, merge history, and co-occurring entities."""
    # Resolve server-side. This used to fetch `?limit=500` and scan here, so an entity
    # past position 500 was reported "not found" — on a 94k-node graph, almost all of
    # them. /neighborhood resolves by name in SQL and returns the node it matched.
    # The name goes in a QUERY parameter, not a path segment, because a path cannot
    # carry it faithfully. Two independent failures, both measured rather than assumed:
    #
    #  - 2.3% of names in a real graph (1331 of 57155) contain "/". The server
    #    percent-decodes the path before routing, so %2F becomes a separator again and
    #    the lookup 404s for an entity that plainly exists.
    #  - `name` is LLM-supplied, and a value of ".." is a dot segment that httpx
    #    normalises away CLIENT-side: `/graph/neighborhood/..` leaves as `/graph`, so
    #    the tool would report the entire graph payload as this entity's neighborhood.
    #
    # A query value is opaque to both — no path normalisation, no re-splitting.
    seed = await call_api(f"/graph/neighborhood?name={quote(name, safe='')}&depth=1&max_nodes=1")
    if "detail" in seed:
        # Only a genuine 404 means "no such entity". Reporting a 500 or a connection
        # failure as "not found" tells the caller the graph lacks something it may well
        # contain, which is worse than an error — they stop looking.
        #
        # Branch on the status, not the message: the message is assembled for humans,
        # and matching "API 404" inside it makes the control flow depend on wording that
        # nothing guarantees.
        if seed.get("status") == 404:
            return f"Entity '{name}' not found"
        return f"Error looking up '{name}': {seed['detail']}"
    entity_id = seed["seed"]["id"]
    detail = await call_api(f"/entities/{entity_id}")
    if isinstance(detail, dict) and "canonical_name" not in detail:
        return f"Error fetching entity detail: {detail.get('detail', detail)}"
    coocs = await call_api(f"/entities/{entity_id}/cooccurrences")
    cooc_warning: str | None = None
    if isinstance(coocs, dict) and "detail" in coocs:
        # Distinguish "fetch failed" from "no co-occurrences" so the agent isn't misled.
        cooc_warning = coocs["detail"]
        coocs = []
    lines = [f"{detail['canonical_name']} ({detail['type']}) [entity:{entity_id}]{_query_tag(detail)}"]
    lines.append(f"Sources: {len(detail['sources'])} mentions across {len(set(s['document_id'] for s in detail['sources']))} docs")
    # Silo + kind per source (task 11a) — resolved live by the orchestrator on every
    # call, so a source re-classified after ingest is reflected immediately here too.
    # Distinct, not per-mention: an entity usually sits in one silo, and spelling it
    # out per source would just repeat the same value N times.
    if detail["sources"]:
        silos = sorted({s.get("silo_id") or "(none)" for s in detail["sources"]})
        kinds = sorted({s.get("kind") or "(unspecified)" for s in detail["sources"]})
        lines.append(f"Silo(s): {', '.join(silos)} — kind(s): {', '.join(kinds)}")
    if detail.get("merge_history"):
        lines.append(f"Also known as: {', '.join(detail['merge_history'])}")
    if cooc_warning:
        lines.append(f"Co-occurrences unavailable: {cooc_warning}")
    elif coocs:
        lines.append("\nOften appears with:")
        for c in coocs[:8]:
            lines.append(f"  • {c['canonical_name']} ({c['type']}) — weight {c['weight']}{_eid(c)}")
    return "\n".join(lines)


@mcp.tool()
async def get_document(title: str) -> str:
    """Read a document with entity highlights. Returns the document text segmented with entity annotations.
    For image documents the body is the vision-model description, and the result notes the content_type
    so the agent knows to fetch the raw image via /images/{document_id} if needed."""
    docs = await call_api("/documents")
    if isinstance(docs, dict) and "detail" in docs:
        return f"Error: {docs['detail']}"
    match = next((d for d in docs if title.lower() in d["title"].lower()), None)
    if not match:
        return f"Document matching '{title}' not found"
    reader = await call_api(f"/documents/{match['id']}/reader")
    if isinstance(reader, dict) and "detail" in reader and "document" not in reader:
        return f"Error reading document: {reader['detail']}"
    doc = reader["document"]
    content_type = doc.get("content_type") or match.get("content_type") or "text"
    lines = [f"Document: {doc['title']} ({content_type}){_did(doc)}{_query_tag(reader)}"]
    if content_type == "image":
        lines.append(f"Image URL: /images/{doc['id']}")
    lines.append(f"Entities: {len(reader['entities'])} | Mentions: {reader['total_mentions']}")
    lines.append(f"Domains: {', '.join(doc['domains'])}")
    lines.append("\n--- Content ---")
    text = "".join(seg["text"] for seg in reader["segments"])
    lines.append(text[:2000])
    if len(text) > 2000:
        lines.append(f"\n... [{len(text) - 2000} more characters]")
    return "\n".join(lines)


@mcp.tool()
async def list_domains() -> str:
    """List all domains in the knowledge graph taxonomy with document counts and spec status."""
    domains = await call_api("/domains")
    lines = ["Domain Taxonomy:\n"]
    for d in domains:
        spec = f"v{d['spec_version']}" if d.get("spec_version") else "no spec"
        lines.append(f"  {d['path']} — {d['document_count']} docs, {spec}")
    return "\n".join(lines)


@mcp.tool()
async def list_entities(type: str = "", limit: int = 20) -> str:
    """Browse entities, optionally filtered by type (Person, Organization, Product, Technology, Event, Concept, Location, Domain, etc.)."""
    path = f"/entities?limit={limit}"
    if type:
        path += f"&type={type}"
    entities = await call_api(path)
    lines = [f"Entities ({len(entities)}):{_query_tag(entities)}\n"]
    for e in entities:
        lines.append(f"  • {e['canonical_name']} ({e['type']}) — {e['source_count']} sources{_eid(e)}")
    return "\n".join(lines)


# ── Graph Traversal ─────────────────────────────────────────────────────────

@mcp.tool()
async def get_neighborhood(entity_name: str, depth: int = 1, max_nodes: int = 20) -> str:
    """Expand the neighborhood around an entity. Returns connected entities within N hops.
    Use depth=1 for immediate neighbors, depth=2 to see friends-of-friends.
    This is the primary graph exploration tool — start here after search."""
    result = await call_api(f"/graph/neighborhood?name={quote(entity_name, safe='')}&depth={depth}&max_nodes={max_nodes}")
    if "detail" in result:
        return f"Error: {result['detail']}"
    seed = result["seed"]
    seed_kind = seed.get("kind") or "unspecified kind"
    seed_silo = seed.get("silo_id") or "no silo"
    lines = [f"Neighborhood of {seed['name']} ({seed['type']}) [{seed_silo}, {seed_kind}]{_eid(seed)} — "
             f"{result['node_count']} nodes, {result['edge_count']} edges, depth {result['depth']}{_query_tag(result)}"]
    # Group nodes by depth
    by_depth: dict[int, list] = {}
    for n in result["nodes"]:
        by_depth.setdefault(n["depth"], []).append(n)
    for d in sorted(by_depth.keys()):
        if d == 0:
            continue
        nodes = sorted(by_depth[d], key=lambda x: -x["source_count"])
        lines.append(f"\n  Hop {d}:")
        for n in nodes:
            # silo_id/kind resolved live by the orchestrator (task 11a): a source
            # re-classified after ingest shows up here on the next call, no re-ingest.
            silo = n.get("silo_id") or "no silo"
            kind = n.get("kind") or "unspecified kind"
            lines.append(f"    • {n['name']} ({n['type']}) — {n['source_count']} docs [{silo}, {kind}]{_eid(n)}")
    return "\n".join(lines)


@mcp.tool()
async def get_shared_context(entity_a: str, entity_b: str) -> str:
    """Find what two entities have in common: shared documents, shared neighbors, shared domains.
    Use this to understand WHY two entities are related or to discover non-obvious connections."""
    result = await call_api(f"/graph/shared-context?a={quote(entity_a, safe='')}&b={quote(entity_b, safe='')}")
    if "detail" in result:
        return f"Error: {result['detail']}"
    ea, eb = result["entity_a"], result["entity_b"]
    summary = result["summary"]
    lines = [f"Shared context: {ea['name']} ({ea['type']}){_eid(ea)} ↔ {eb['name']} ({eb['type']}){_eid(eb)}{_query_tag(result)}"]
    if result["direct_weight"]:
        lines.append(f"  Direct co-occurrence weight: {result['direct_weight']}")
    lines.append(f"  {summary['docs_in_common']} shared documents, {summary['neighbors_in_common']} shared neighbors, {summary['domains_in_common']} shared domains")
    if result["shared_documents"]:
        lines.append("\n  Shared documents:")
        for doc in result["shared_documents"][:8]:
            lines.append(f"    • {doc['title']}{_did(doc)}")
    if result["shared_neighbors"]:
        lines.append("\n  Shared neighbors (entities connected to both):")
        for n in result["shared_neighbors"][:8]:
            lines.append(f"    • {n['name']} ({n['type']}) — weight to A: {n['weight_to_a']}, to B: {n['weight_to_b']}{_eid(n)}")
    if result["shared_domains"]:
        lines.append(f"\n  Shared domains: {', '.join(result['shared_domains'])}")
    return "\n".join(lines)


@mcp.tool()
async def find_paths(entity_a: str, entity_b: str, max_depth: int = 4) -> str:
    """Find shortest path(s) between two entities through co-occurrence edges.
    Shows HOW two entities connect through the graph — useful for discovering indirect relationships."""
    result = await call_api(f"/graph/paths?a={quote(entity_a, safe='')}&b={quote(entity_b, safe='')}&max_depth={max_depth}")
    if "detail" in result:
        return f"Error: {result['detail']}"
    ea, eb = result["entity_a"], result["entity_b"]
    lines = [f"Paths: {ea['name']} → {eb['name']} — {result['path_count']} path(s) found (searched up to depth {result['searched_depth']}){_query_tag(result)}"]
    if not result["paths"]:
        lines.append("  No path found within the search depth.")
    for i, path in enumerate(result["paths"]):
        chain = " → ".join(f"{n['name']} ({n['type']}){_eid(n)}" for n in path["nodes"])
        lines.append(f"\n  Path {i+1} (length {path['length']}): {chain}")
    return "\n".join(lines)


@mcp.tool()
async def get_subgraph(entity_names: list[str], max_hops: int = 1) -> str:
    """Get the subgraph connecting a set of entities. Given seed entities (from a search or exploration),
    returns all nodes and edges between them. Use this to understand how search results relate to each other."""
    result = await call_api("/graph/subgraph", method="POST", body={"entity_names": entity_names, "max_hops": max_hops})
    if "detail" in result:
        return f"Error: {result['detail']}"
    seed_names = [s["name"] for s in result["seeds"]]
    lines = [f"Subgraph around {len(result['seeds'])} seeds: {', '.join(seed_names)}{_query_tag(result)}"]
    lines.append(f"  {result['node_count']} nodes, {result['edge_count']} edges (max_hops={max_hops})")
    # Show seeds
    lines.append("\n  Seed entities:")
    for s in result["seeds"]:
        silo = s.get("silo_id") or "no silo"
        kind = s.get("kind") or "unspecified kind"
        lines.append(f"    ★ {s['name']} ({s['type']}) [{silo}, {kind}]{_eid(s)}")
    # Show discovered (non-seed) nodes
    discovered = [n for n in result["nodes"] if not n.get("is_seed")]
    if discovered:
        lines.append(f"\n  Discovered entities ({len(discovered)}):")
        for n in discovered[:20]:
            silo = n.get("silo_id") or "no silo"
            kind = n.get("kind") or "unspecified kind"
            lines.append(f"    • {n['name']} ({n['type']}) [{silo}, {kind}]{_eid(n)}")
    # Show strongest edges
    if result["edges"]:
        top_edges = sorted(result["edges"], key=lambda e: -e["weight"])[:10]
        lines.append("\n  Strongest connections:")
        # Build name lookup
        name_map = {n["id"]: n["name"] for n in result["nodes"]}
        for e in top_edges:
            lines.append(f"    {name_map.get(e['source'], '?')} ↔ {name_map.get(e['target'], '?')} (weight {e['weight']})")
    return "\n".join(lines)


@mcp.tool()
async def explore_domain(domain_path: str) -> str:
    """Overview of a domain: documents, top entities, entity type distribution, and related domains.
    Use this to understand what a domain contains before diving into individual entities."""
    result = await call_api(f"/graph/domain-overview/{quote(domain_path, safe='/')}")
    if "detail" in result:
        return f"Error: {result['detail']}"
    domain = result["domain"]
    lines = [f"Domain: {domain['path']} — {domain['document_count']} documents" +
             (f", spec v{domain['spec_version']}" if domain.get("spec_version") else ", no spec") +
             _query_tag(result)]
    # Documents
    lines.append(f"\n  Documents ({len(result['documents'])}):")
    for doc in result["documents"]:
        lines.append(f"    • {doc['title']} ({doc['content_type']}){_did(doc)}")
    # Entity type distribution
    if result["entity_type_distribution"]:
        lines.append("\n  Entity types:")
        for etype, count in sorted(result["entity_type_distribution"].items(), key=lambda x: -x[1]):
            lines.append(f"    {etype}: {count}")
    # Top entities
    if result["top_entities"]:
        lines.append("\n  Top entities:")
        for e in result["top_entities"][:15]:
            lines.append(f"    • {e['name']} ({e['type']}) — in {e['doc_count']} docs{_eid(e)}")
    # Related domains
    if result["related_domains"]:
        lines.append("\n  Related domains:")
        for rd in result["related_domains"]:
            lines.append(f"    • {rd['path']} ({rd['shared_entities']} shared entities)")
    return "\n".join(lines)


# ── Corrections (write) ───────────────────────────────────────────────────────

@mcp.tool()
async def propose_correction(
    action: str,
    entity: str,
    rationale: str = "",
    target_b: str = "",
    proposed_type: str = "",
    proposed_name: str = "",
) -> str:
    """Propose a correction to the knowledge graph. This files a pending issue for
    human review — it never mutates the graph directly.

    action: one of 'invalidate' (entity is not a real entity — a metaphor/analogy/artifact),
            'merge' (entity and target_b are the same referent), 'retype' (entity's type is
            wrong; give proposed_type), 'rename' (entity's name is garbled; give proposed_name).
    entity: the entity name (or id) to correct.
    rationale: why — cite what you saw in the sources.
    target_b: (merge only) the other entity to merge with.
    proposed_type / proposed_name: (retype / rename only) the corrected value.
    """
    body = {
        "action": action, "entity": entity, "rationale": rationale,
        "proposer": "mcp-agent",
    }
    if target_b:
        body["target_b"] = target_b
    if proposed_type:
        body["proposed_type"] = proposed_type
    if proposed_name:
        body["proposed_name"] = proposed_name
    result = await call_api("/corrections/propose", method="POST", body=body)
    if "detail" in result:
        return f"Could not file correction: {result['detail']}"
    return f"Filed correction {result['issue_id']} ({action} on '{entity}') — status: {result['status']}. It will be reviewed by a human."


# ── Write: ingestion, jobs, workspaces ────────────────────────────────────────
# These are DIRECT writes (ingestion, job triggers, workspace creation) — fine to apply
# without human review. NOTE: trigger_normalization runs batch normalization, which
# auto-merges confident duplicate entities — sanctioned pipeline behaviour, already
# reachable via REST, not new here. What stays HUMAN-GATED is an agent proposing a
# graph correction from its OWN judgment (invalidate/merge/retype/rename a specific
# node): that goes only through propose_correction above, which files for approval. Do
# not add a tool that edits the graph directly on the agent's say-so.

@mcp.tool()
async def ingest_text(title: str, content: str) -> str:
    """Ingest a new document into the active noosphere from raw text (no file needed).
    Runs the full pipeline: classify into a domain, extract entities, compute co-occurrence.
    Select a noosphere first with select_noosphere(). Returns the new document id, its
    assigned domains, and how many entities were extracted."""
    if _active_workspace is None:
        return "No noosphere selected — call select_noosphere() first so the document lands in the right workspace."
    result = await call_api("/ingest/text", method="POST", body={"title": title, "content": content})
    if "document_id" not in result:
        return f"Ingest failed: {result.get('detail', result)}"
    domains = ", ".join(result.get("domains") or []) or "none"
    return (f"Ingested '{result['title']}' (id: {result['document_id']}) — "
            f"domains: {domains}, {result.get('entity_count', 0)} entities extracted.")


@mcp.tool()
async def ingest_repo(path: str, name: str, provenance_kind: str = "") -> str:
    """Ingest a git repository into the active noosphere: summarize the code with codesum
    into a collection of per-file/-module "code intent" documents, then extract entities —
    so the graph is a MAP over the code, not a copy of it.

    IMPORTANT — `path` is a SERVER-SIDE directory the orchestrator can already see, NOT an
    upload and NOT a GitHub URL. The repo must live on a path the Orrery services can read
    (the mounted data volume, e.g. `/data/repos/my-repo`, or a directory bind-mounted into
    the orchestrator + worker). Clone/copy the checkout there first, then pass that path.
    `name` becomes the collection's name (must be unique in the noosphere).

    This is asynchronous: it returns immediately with a job id and collection id, and the
    worker does the (many-LLM-call) summarization in the background. Poll it with
    get_job_status(job_id). Select a noosphere first with select_noosphere().

    For ongoing/auto-updating repo sync instead of a one-shot import, register a watched
    source (`POST /watched-sources` with type='repo') so the worker re-syncs on a cadence."""
    if _active_workspace is None:
        return "No noosphere selected — call select_noosphere() first so the repo lands in the right workspace."
    body = {"path": path, "name": name}
    if provenance_kind:
        body["provenance_kind"] = provenance_kind
    result = await call_api("/ingest/repo", method="POST", body=body)
    if result.get("status") == 409:
        return (f"A collection named '{name}' already exists in this noosphere. "
                f"Re-ingest under a different name, or inspect what is already there. ({result.get('detail')})")
    if "job_id" not in result:
        detail = result.get("detail", result)
        hint = ""
        if isinstance(detail, str) and "Not a directory" in detail:
            hint = (" — the path must be a directory the Orrery services can see "
                    "(under the data volume or a bind-mount), not a local-only path or a URL.")
        return f"Repo ingest failed: {detail}{hint}"
    return (f"Started repo ingest of '{name}' (collection {result['collection_id']}) — "
            f"job {result['job_id']}. The worker is summarizing + extracting in the background; "
            f"poll with get_job_status('{result['job_id']}').")


@mcp.tool()
async def create_noosphere(name: str, description: str = "") -> str:
    """Create a new noosphere (workspace). Does NOT switch to it — call
    select_noosphere() with the returned id to start working in it."""
    result = await call_api("/workspaces", method="POST",
                            body={"name": name, "description": description})
    if "workspaceId" not in result:
        return f"Could not create noosphere: {result.get('detail', result)}"
    return (f"Created noosphere '{result['name']}' (id: {result['workspaceId']}). "
            f"Use select_noosphere('{result['workspaceId']}') to switch to it.")


@mcp.tool()
async def trigger_simmer(domain: str = "") -> str:
    """Start a spec-simmer job. With no domain, simmers the GENERAL extraction spec; with
    a domain path (e.g. 'techniques/wet-blending') simmers that domain's spec. When the
    simmer finishes, the worker automatically runs a batch re-extraction with the improved
    spec. Returns the job id — track it with get_job_status()."""
    if _active_workspace is None:
        return "No noosphere selected — call select_noosphere() first."
    path = "/simmer/general" if not domain else f"/simmer/{quote(domain, safe='/')}"
    result = await call_api(path, method="POST")
    if "job_id" not in result:
        return f"Could not start simmer: {result.get('detail', result)}"
    scope = "the general spec" if not domain else f"domain '{domain}'"
    return f"Started simmer for {scope} — job {result['job_id']}. Poll with get_job_status('{result['job_id']}')."


@mcp.tool()
async def trigger_normalization() -> str:
    """Run entity normalization over the active noosphere: cluster near-duplicate entities
    by embedding similarity and LLM-review the ambiguous pairs. Confident duplicates merge
    automatically; uncertain pairs go to the human review queue. Returns a summary of what
    merged and what awaits review."""
    if _active_workspace is None:
        return "No noosphere selected — call select_noosphere() first."
    result = await call_api("/normalize", method="POST")
    if "detail" in result:
        return f"Normalization failed: {result['detail']}"
    return f"Normalization complete: {json.dumps(result)}"


@mcp.tool()
async def get_job_status(job_id: str = "") -> str:
    """Check background job status. With a job_id, returns that job's status (plus live
    extraction progress — docs done / total, entities so far — when the running job reports
    it). With no id, lists the jobs currently running or queued in the active noosphere.
    Reads the job list (GET /jobs) and resolves the id client-side."""
    jobs = await call_api("/jobs")
    if isinstance(jobs, dict) and "detail" in jobs:
        return f"Could not fetch jobs: {jobs['detail']}"
    if job_id:
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if job is None:
            return f"No job with id '{job_id}' in the active noosphere."
        lines = [f"Job {job['id']} ({job['type']}) — {job['status']}"]
        prog = job.get("progress")
        if prog:
            lines.append(f"  progress: {prog.get('docs_done')}/{prog.get('docs_total')} docs, "
                         f"{prog.get('entities_so_far')} entities so far")
        if job.get("results"):
            lines.append(f"  result: {json.dumps(job['results'])}")
        return "\n".join(lines)
    active = [j for j in jobs if j.get("status") in ("running", "queued")]
    if not active:
        return "No running or queued jobs."
    lines = [f"Active jobs ({len(active)}):"]
    for j in active:
        prog = j.get("progress")
        p = f" — {prog['docs_done']}/{prog['docs_total']} docs" if prog else ""
        lines.append(f"  • {j['id']} ({j['type']}) — {j['status']}{p}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
