# ABOUTME: Main search pipeline — 5 stages (expansion → retrieval → entity-boost → fusion → response).
# ABOUTME: Accepts an optional Relay instance for LLM-powered query expansion.

from __future__ import annotations

from ...repositories.graph_reads import degrees_of, entities_by_ids
import sqlite3
import threading
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from orrery_relay import Relay

from .config import SearchConfig
from .models import SubQueryResults, ScoredEntity, ScoredChunk, SearchResponse
from .retrieval import (
    build_indexes, embed_text, embed_new_entities, embed_new_chunks,
    search_entities_semantic, search_chunks_semantic, search_entities_exact,
    _ws_key,
)
from .entity_boost import boost_chunks_via_entities
from .fusion import rrf_fuse

_config = SearchConfig()
_DEFAULTS = SearchConfig()           # pristine knob values, to reset the shared _config
_SCOPE_OVERFETCH = 5                 # retrieve top_k * this when a time scope will post-filter
# Per-workspace readiness (#113): the set of `_ws_key`s whose index is built and current.
# A single bool latched True on the first workspace's build, so no other workspace ever
# rebuilt and every search hit the first one's vectors.
_indexes_ready: set[str] = set()

# Guards the check-build-publish sequence as one unit, and the staleness flag with it.
#
# Without it the mark is simply lost: a search thread reads `_indexes_ready == False`,
# spends seconds inside `build_indexes` (snapshotting the graph as it was), a correction
# thread commits and sets the flag False, and the search thread then sets it True —
# publishing an index built from pre-correction rows as fresh. Both threads are real:
# `/search` is `async def` (event loop) while `/corrections/review` is a sync `def`
# (anyio threadpool).
#
# It also protects `retrieval`'s module globals, which is the more serious of the two:
# `_entity_ids` and `_entity_index` are assigned separately, so two overlapping builds
# can pair one build's id list with the other's index and return the WRONG entities —
# not merely a lost ranking slot.
#
# Deliberately a plain Lock, not an RLock: nothing reachable from `build_indexes`
# re-enters this module, so reentrancy is not needed today — and if someone later adds a
# `mark_indexes_stale()` inside the build path, an RLock would silently swallow it (a
# lost mark, exactly the bug this guards against) whereas a Lock deadlocks loudly on the
# first test run.
_index_lock = threading.Lock()


def mark_indexes_stale(conn: sqlite3.Connection) -> None:
    """Force a FAISS rebuild on the next search of THIS workspace (keyed by `conn`).

    The index is built once per workspace. A correction that invalidates or merges an
    entity changes what the index SHOULD contain but leaves the index itself alone, so
    the stale vector keeps occupying a top-k slot. `_enrich_results` drops it from the
    output — which is why a deleted entity no longer appears — but the slot it consumed
    is simply lost: an active entity ranked just below it never surfaces. Filtering the
    results is therefore necessary but not sufficient.

    Scoped per workspace (#113): a correction in one workspace must not force a rebuild
    of every other workspace's index. Cheap: the rebuild reads stored embeddings rather
    than re-embedding.
    """
    with _index_lock:
        _indexes_ready.discard(_ws_key(conn))


def _ensure_indexes_built(conn: sqlite3.Connection) -> None:
    """Check, build and publish as one unit — the sequence `_index_lock` exists for.

    Splitting them is what lets a concurrent staleness mark be overwritten by the very
    build it was meant to force. Blocking is deliberate: `mark_indexes_stale` waits for
    an in-flight build to publish readiness and then clears it, so the mark survives.
    Do NOT "optimise" that back into a lock-free assignment. Keyed per workspace (#113).

    Call from a worker thread, never the event loop — see `search_knowledge_graph`.
    """
    with _index_lock:
        key = _ws_key(conn)
        if key not in _indexes_ready:
            build_indexes(conn)
            _indexes_ready.add(key)


def rebuild_indexes_now(conn: sqlite3.Connection):
    """Force a rebuild under the same lock the search path uses, and publish it ready.

    The manual rebuild endpoint called `build_indexes` directly, which is the same
    unguarded write to `retrieval`'s module globals as an unlocked search — two builds
    overlapping can pair one's `_entity_ids` with the other's `_entity_index`, so FAISS
    positions resolve to the wrong entities. It also left readiness untouched, so an
    explicit rebuild did not clear staleness. Keyed per workspace (#113).
    """
    with _index_lock:
        stats = build_indexes(conn)
        _indexes_ready.add(_ws_key(conn))
        return stats


def _enrich_results(conn: sqlite3.Connection, results: SubQueryResults):
    """Fill in entity names/types and chunk text from DB."""
    # DROP entities whose active lookup fails, do not merely skip enriching them. The
    # FAISS index is built periodically, so an entity invalidated since the last build
    # still comes back as a hit; leaving it in the list with blank metadata let fusion
    # return a soft-deleted entity anyway — the exact bug the filter was added to fix.
    #
    # Two batched reads rather than two queries per hit: this runs once per SUB-query,
    # so a 4-sub-query search over 20 hits each was ~160 round trips for what the
    # primitives answer in 2. Iterating `semantic_entities` preserves the ranked order.
    ids = [e.entity_id for e in results.semantic_entities]
    active = entities_by_ids(conn, ids)
    degrees = degrees_of(conn, list(active))
    active_entities = []
    for e in results.semantic_entities:
        row = active.get(e.entity_id)
        if row is None:
            continue
        e.name = row["canonical_name"]
        e.entity_type = row["type"]
        e.source_count = degrees.get(e.entity_id, 0)
        active_entities.append(e)
    results.semantic_entities = active_entities

    for c in results.semantic_chunks:
        row = conn.execute(
            "SELECT c.text, c.document_id, d.title FROM chunks c JOIN documents d ON c.document_id = d.id WHERE c.id = ? AND d.invalid_at IS NULL",
            (c.chunk_id,)
        ).fetchone()
        if row:
            c.text = row[0][:300]
            c.document_id = row[1]
            c.document_title = row[2] or ""


def _retrieve_for_subquery(conn: sqlite3.Connection, query: str, key: str) -> SubQueryResults:
    """Stage 1: Run all retrieval channels for a single sub-query.

    `key` selects the workspace's own FAISS index (#113); the exact channel uses `conn`
    directly and is already workspace-scoped."""
    query_embedding = embed_text(query)

    # Channel A: Semantic entity search
    semantic_entities = search_entities_semantic(query_embedding, key, top_k=_config.entity_top_k)

    # Channel B: Semantic chunk search
    semantic_chunks = search_chunks_semantic(query_embedding, key, top_k=_config.chunk_top_k)

    # Channel C: Exact match
    exact_entities = search_entities_exact(conn, query, min_term_length=_config.exact_min_term_length)

    results = SubQueryResults(
        query=query,
        semantic_entities=semantic_entities,
        semantic_chunks=semantic_chunks,
        exact_entities=exact_entities,
    )

    # Enrich with names/text from DB
    _enrich_results(conn, results)

    return results


async def search_knowledge_graph(
    conn: sqlite3.Connection,
    query: str,
    expand: bool = True,
    relay: "Relay | None" = None,
    top_k: int = 20,
    since: str | None = None,
    until: str | None = None,
) -> SearchResponse:
    """Full search pipeline. Returns ranked entities + chunks.

    `since`/`until` (already resolved to SQLite UTC strings) restrict results to a time
    window (#116): the FAISS index is flat and all-time, so we over-fetch and then drop
    hits whose document is out of window. An entity is kept only if it has an in-window
    source; a chunk only if its document is in-window. With no window, behavior and fetch
    sizes are exactly as before.
    """
    scoped = bool(since or until)
    fetch_k = top_k * _SCOPE_OVERFETCH if scoped else top_k
    # The shared _config is mutated per-request (pre-existing); reset the fetch knobs each
    # call so a scoped request's widened fetch never leaks into a later unscoped one.
    _config.max_results = fetch_k
    _config.entity_top_k = fetch_k if scoped else _DEFAULTS.entity_top_k
    _config.chunk_top_k = fetch_k if scoped else _DEFAULTS.chunk_top_k

    # Off the event loop, NOT `with _index_lock:` inline.
    #
    # This coroutine runs on the loop thread, so taking the lock here would block the
    # whole process — not just this request — for as long as someone else holds it. And
    # `/search/rebuild` is a sync def (anyio threadpool) that now holds it across an
    # entire build, which on a large graph is seconds, or a model download on first use.
    # Serialising the builds is the point; stalling every other route while one runs is
    # not. Awaiting a worker thread also fixes the pre-existing stall where a stale index
    # blocked the loop for the duration of its own rebuild.
    #
    # Safe to hand `conn` across: get_connection opens with check_same_thread=False, and
    # this awaits the thread, so the connection is never used from two threads at once.
    await anyio.to_thread.run_sync(_ensure_indexes_built, conn)
    key = _ws_key(conn)   # select this workspace's index for the semantic channels (#113)

    # Stage 0: Query expansion
    if expand and relay is not None:
        from .expansion import expand_query
        sub_queries = await expand_query(
            relay=relay,
            query=query,
            max_sub_queries=_config.expansion_max_sub_queries,
        )
    else:
        sub_queries = [query]

    # Get total doc count for specificity
    total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    # Stage 1 + 2: Retrieve + entity-boost for each sub-query
    all_results = []
    for sq in sub_queries:
        result = _retrieve_for_subquery(conn, sq, key)
        result = boost_chunks_via_entities(conn, result, _config, total_docs)
        all_results.append(result)

    # Stage 3: RRF fusion
    response = rrf_fuse(all_results, query, _config)

    # Stage 4 (optional): time-window post-filter. Done here, after fusion, so out-of-
    # window hits never consumed a top_k slot a valid one should have had (the reason for
    # the over-fetch above). Truncate to the caller's real top_k at the end.
    if scoped:
        response = _filter_response_to_window(conn, response, since, until, top_k)

    return response


def _docs_in_window(conn: sqlite3.Connection, doc_ids: list[str],
                    since: str | None, until: str | None) -> dict:
    """Return {document_id: created_at} for those of `doc_ids` inside the window.

    `until` doubles as an as-of cutoff: a doc soft-deleted AFTER it still counts as of
    then (invalid_at > until), matching the documents list."""
    if not doc_ids:
        return {}
    ph = ",".join("?" * len(doc_ids))
    clauses = [f"id IN ({ph})"]
    params: list = list(doc_ids)
    if until:
        clauses.append("(invalid_at > ? OR invalid_at IS NULL)"); params.append(until)
    else:
        clauses.append("invalid_at IS NULL")
    if since:
        clauses.append("created_at >= ?"); params.append(since)
    if until:
        clauses.append("created_at < ?"); params.append(until)
    rows = conn.execute(
        f"SELECT id, created_at FROM documents WHERE {' AND '.join(clauses)}", params).fetchall()
    return {r[0]: r[1] for r in rows}


def _entities_with_in_window_source(conn: sqlite3.Connection, entity_ids: list[str],
                                    since: str | None, until: str | None) -> set:
    """The subset of `entity_ids` that have at least one source document in the window."""
    if not entity_ids:
        return set()
    ph = ",".join("?" * len(entity_ids))
    clauses = [f"es.entity_id IN ({ph})"]
    params: list = list(entity_ids)
    if until:
        clauses.append("(d.invalid_at > ? OR d.invalid_at IS NULL)"); params.append(until)
    else:
        clauses.append("d.invalid_at IS NULL")
    if since:
        clauses.append("d.created_at >= ?"); params.append(since)
    if until:
        clauses.append("d.created_at < ?"); params.append(until)
    rows = conn.execute(
        "SELECT DISTINCT es.entity_id FROM entity_sources es "
        "JOIN documents d ON es.document_id = d.id "
        f"WHERE {' AND '.join(clauses)}", params).fetchall()
    return {r[0] for r in rows}


def _filter_response_to_window(conn: sqlite3.Connection, response: SearchResponse,
                               since: str | None, until: str | None, top_k: int) -> SearchResponse:
    in_win_docs = _docs_in_window(
        conn, [c["document_id"] for c in response.chunks if c.get("document_id")], since, until)
    kept_chunks = []
    for c in response.chunks:
        if c["document_id"] in in_win_docs:
            c["created_at"] = in_win_docs[c["document_id"]]   # expose for timeline sorting
            kept_chunks.append(c)

    in_win_ents = _entities_with_in_window_source(
        conn, [e["id"] for e in response.entities], since, until)
    kept_entities = [e for e in response.entities if e["id"] in in_win_ents]

    response.total_entities = len(kept_entities)
    response.total_chunks = len(kept_chunks)
    response.entities = kept_entities[:top_k]
    response.chunks = kept_chunks[:top_k]
    return response


# Re-export for backward compat
__all__ = ["search_knowledge_graph", "build_indexes", "embed_new_entities", "embed_new_chunks",
           "mark_indexes_stale", "rebuild_indexes_now"]
