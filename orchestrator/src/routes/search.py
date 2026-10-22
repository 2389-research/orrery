# ABOUTME: Search route — 5-stage pipeline with FAISS on SQLite.
# ABOUTME: Supports query expansion via Relay, optional image search.

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from orrery_relay import Relay
from ..config import get_settings
from ..dependencies import get_auth_store, require_writable, AuthStore, query_id
from ..broadcast import broadcast_search
from ..repositories.graph_reads import entity_silos
from ..pipeline.slice import ensure_slice_doc_temp_table
from .entities import _active_slice, _scope

router = APIRouter()


@router.get("/search")
async def search_query(q: str, top_k: int = 20, expand: bool = False, include_images: bool = False,
                       since: str | None = None, until: str | None = None, as_of: str | None = None,
                       window: str | None = None, domain: str | None = None,
                       collection: str | None = None,
                       auth: AuthStore = Depends(get_auth_store), qid: str = Depends(query_id)):
    """Search the knowledge graph.

    Full 5-stage pipeline: expansion → retrieval → entity-boost → fusion → response.
    include_images=true adds parallel image search results.

    expand defaults to FALSE. Query expansion is the only search stage that makes an LLM
    call (sub-query generation), so it adds latency and a hard per-search dependency on
    the configured backend — a search should not 500 because the LLM backend is momentarily
    unreachable. Callers who want the recall boost opt in with expand=true.

    Optional TimeScope (#116): `since`/`until`/`as_of` (ISO, bare date, or relative like
    `-7d`) restrict hits to documents in that window; the resolved `scope` is echoed back.
    """
    # Active slice (#119): a virtual noosphere's auth.slice wins, else ad-hoc facets.
    # 400 on bad input (closes the store). Time params are one facet of the slice.
    slc = _active_slice(auth, since, until, as_of, window, domain, collection)
    sliced = slc is not None

    settings = get_settings()
    store = auth.store
    relay = Relay.from_settings(settings)

    from ..pipeline.search import search_knowledge_graph
    if sliced:
        ensure_slice_doc_temp_table(store.conn, slc)
    result = await search_knowledge_graph(
        store.conn, q, expand=expand, relay=relay, top_k=top_k, in_slice=sliced,
    )

    # Silo + kind, resolved LIVE via the silo_kind view join (task 11a) — batched
    # over just the ids already in this result set, not a query per hit. This is
    # the primary agent read (search_knowledge_graph, MCP #48), so provenance has to
    # ride along here even though search itself sits outside repositories/graph_reads'
    # usual callers.
    entity_ids = [e["id"] for e in result.entities]
    ent_silo_info = entity_silos(store.conn, entity_ids) if entity_ids else {}
    for e in result.entities:
        sinfo = ent_silo_info.get(e["id"], {})
        e["silo_id"] = sinfo.get("silo_id")
        e["kind"] = sinfo.get("kind")

    doc_ids = [c["document_id"] for c in result.chunks]
    doc_meta = store.documents.get_titles(doc_ids) if doc_ids else {}
    for c in result.chunks:
        meta = doc_meta.get(c["document_id"], {})
        c["silo_id"] = meta.get("silo_id")
        c["kind"] = meta.get("kind")

    response = {
        "query": result.query,
        "entities": result.entities,
        "chunks": result.chunks,
        "sub_queries_used": result.sub_queries_used,
        "total_entities": result.total_entities,
        "total_chunks": result.total_chunks,
        "query_id": qid,
    }
    if sliced:
        response["scope"] = _scope(slc)

    # Parallel image search (opt-in)
    if include_images:
        try:
            response["images"] = _search_images(store.conn, q, top_k=top_k)
        except Exception as e:
            print(f"Image search failed: {e}", flush=True)
            response["images"] = []

    store.close()

    # Broadcast to viz
    entity_names = [e["name"] for e in response.get("entities", [])[:10] if e.get("name")]
    if entity_names:
        await broadcast_search(q, entity_names)

    return response


def _search_images(conn, query: str, top_k: int = 10) -> list[dict]:
    """Search image documents using SigLIP cross-modal embeddings.

    Embeds the text query via SigLIP text encoder, searches against the
    image_embedding column (SigLIP pixel/description embeddings in the same
    latent space). Falls back to sentence-transformers on the text description,
    then SQL LIKE matching, if SigLIP is unavailable.
    """
    # Stage 1: SigLIP cross-modal — query → SigLIP text encoder → similarity against image_embedding
    try:
        from ..pipeline.image_embedding import embed_image_text
        query_emb = embed_image_text(query)
        if query_emb is not None:
            rows = conn.execute("""
                SELECT c.id, c.text, c.image_embedding, d.id as doc_id, d.title, d.source_path
                FROM chunks c
                JOIN documents d ON c.document_id = d.id
                WHERE d.content_type = 'image' AND c.image_embedding IS NOT NULL AND d.invalid_at IS NULL
            """).fetchall()
            if rows:
                scored = []
                for row in rows:
                    emb = np.frombuffer(row[2], dtype=np.float32)
                    sim = float(np.dot(query_emb, emb) / (np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-8))
                    scored.append({
                        "document_id": row[3],
                        "title": row[4],
                        "description": (row[1] or "")[:200],
                        "score": round(sim, 3),
                    })
                scored.sort(key=lambda x: -x["score"])
                return scored[:top_k]
    except Exception as e:
        print(f"SigLIP image search failed, falling back: {e}", flush=True)

    # Stage 2: sentence-transformers on description text
    try:
        from ..pipeline.search.retrieval import _get_model
        model = _get_model()
        query_emb = model.encode([query], normalize_embeddings=True)[0]

        rows = conn.execute("""
            SELECT c.id, c.text, c.embedding, d.id as doc_id, d.title, d.source_path
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE d.content_type = 'image' AND c.embedding IS NOT NULL AND d.invalid_at IS NULL
        """).fetchall()

        if rows:
            scored = []
            for row in rows:
                emb = np.frombuffer(row[2], dtype=np.float32)
                sim = float(np.dot(query_emb, emb) / (np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-8))
                scored.append({
                    "document_id": row[3],
                    "title": row[4],
                    "description": (row[1] or "")[:200],
                    "score": round(sim, 3),
                })
            scored.sort(key=lambda x: -x["score"])
            return scored[:top_k]
    except Exception:
        pass

    # Stage 3: SQL LIKE on description text
    query_like = f"%{query}%"
    rows = conn.execute("""
        SELECT d.id, d.title, c.text
        FROM documents d
        JOIN chunks c ON c.document_id = d.id
        WHERE d.content_type = 'image' AND c.text LIKE ? AND d.invalid_at IS NULL
        LIMIT ?
    """, (query_like, top_k)).fetchall()

    return [{"document_id": r[0], "title": r[1], "description": (r[2] or "")[:200], "score": 1.0} for r in rows]


@router.post("/search/rebuild")
def rebuild_search_index(auth: AuthStore = Depends(require_writable)):
    """Rebuild FAISS search indexes — re-embeds entities and chunks missing embeddings."""
    store = auth.store

    from ..pipeline.search import rebuild_indexes_now, embed_new_entities, embed_new_chunks
    new_entities = embed_new_entities(store.conn)
    new_chunks = embed_new_chunks(store.conn)
    stats = rebuild_indexes_now(store.conn)
    store.close()
    return {"status": "rebuilt", "new_entities_embedded": new_entities, "new_chunks_embedded": new_chunks, **stats}


@router.post("/search/backfill-image-embeddings")
def backfill_image_embeddings(auth: AuthStore = Depends(require_writable)):
    """Populate chunks.image_embedding for image documents that lack a SigLIP embedding.
    Idempotent — only touches rows where image_embedding IS NULL."""
    from pathlib import Path
    from ..pipeline.image_embedding import embed_image, embed_image_text

    store = auth.store
    conn = store.conn

    rows = conn.execute("""
        SELECT c.id, c.text, d.source_path
        FROM chunks c
        JOIN documents d ON c.document_id = d.id
        WHERE d.content_type = 'image' AND c.image_embedding IS NULL
    """).fetchall()

    embedded = 0
    failures = 0
    for chunk_id, description, source_path in rows:
        try:
            emb = embed_image(Path(source_path)) if source_path and Path(source_path).exists() else None
            if emb is None and description:
                emb = embed_image_text(description)
            if emb is None:
                failures += 1
                continue
            conn.execute(
                "UPDATE chunks SET image_embedding = ? WHERE id = ?",
                (emb.astype(np.float32).tobytes(), chunk_id),
            )
            embedded += 1
        except Exception as e:
            print(f"backfill failed for chunk {chunk_id}: {e}", flush=True)
            failures += 1
    conn.commit()
    store.close()
    return {"status": "ok", "embedded": embedded, "failures": failures, "scanned": len(rows)}
