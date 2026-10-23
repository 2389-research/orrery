# ABOUTME: Shared recursive-summary helpers for session ingestion (ccvault + meridian):
# ABOUTME: segment/rollup summary prompts, collection-doc writer, and domain upsert.

import hashlib
import json
import uuid

_SEGMENT_INSTRUCTIONS = (
    "You are summarizing ONE SEGMENT of an AI coding-agent session for a knowledge graph. Write a "
    "NEUTRAL, factual summary of what happened in THIS segment only: what was examined / done, the "
    "files / systems / topics / entities touched, and any finding. Describe — do not evaluate, "
    "praise, or criticize. No first person. Do not reference other segments or judge overall "
    "success. Do not invent detail absent from the segment. Two or three sentences."
)

_ROLLUP_INSTRUCTIONS = (
    "You are writing the top-level NEUTRAL summary of an AI coding-agent session for a knowledge "
    "graph, from the per-segment summaries below. State what the session set out to do, the areas / "
    "systems / topics it worked across, and its outcome. Describe — do not evaluate or praise. No "
    "first person. A few short paragraphs."
)


def parent_of(domain_path):
    if not domain_path or "/" not in domain_path:
        return None
    return domain_path.rsplit("/", 1)[0]


async def summarize_segment(relay, model, context: str, segment_text: str) -> str:
    prompt = (
        f"{_SEGMENT_INSTRUCTIONS}\n\n"
        f"{context}\n\n"
        f"SEGMENT (chronological turns):\n{segment_text[:8000]}\n\n"
        "Neutral segment summary:"
    )
    resp = await relay.complete(model=model, max_tokens=384,
                                messages=[{"role": "user", "content": prompt}])
    return (resp.text or "").strip()


async def summarize_rollup(relay, model, context: str, segment_summaries: list[str]) -> str:
    joined = "\n".join(f"- {s}" for s in segment_summaries)
    prompt = (
        f"{_ROLLUP_INSTRUCTIONS}\n\n"
        f"{context}\n\n"
        f"PER-SEGMENT SUMMARIES (in order):\n{joined}\n\n"
        "Neutral session summary:"
    )
    resp = await relay.complete(model=model, max_tokens=1024,
                                messages=[{"role": "user", "content": prompt}])
    return (resp.text or "").strip()


def write_doc(conn, collection_id, content, content_type, role, parent_path, status, title, dom,
              metadata: dict | None = None, source_path: str | None = None):
    """Write one collection doc (group or leaf) + its chunk + collection membership + domain.
    emits_cooccurrence is always 0 — a ccvault doc is an agent_report account, not neutral
    co-occurrence evidence (the silo gate also excludes it). Returns the doc id."""
    doc_id = str(uuid.uuid4())
    ch = hashlib.sha256(content.encode()).hexdigest()
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, source_path, content_type, "
        "status, silo_id, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, title, content, ch, source_path or f"ccvault:{title}", content_type, status, collection_id,
         json.dumps(metadata) if metadata is not None else None))
    conn.execute(
        "INSERT INTO chunks (id, document_id, chunk_index, text, offset, length) VALUES (?, ?, 0, ?, 0, ?)",
        (str(uuid.uuid4()), doc_id, content, len(content)))
    conn.execute(
        "INSERT INTO document_collections (document_id, collection_id, parent_path, role, "
        "emits_cooccurrence) VALUES (?, ?, ?, ?, 0)", (doc_id, collection_id, parent_path, role))
    conn.execute("UPDATE collections SET document_count = document_count + 1 WHERE id = ?", (collection_id,))
    conn.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) "
                 "VALUES (?, ?, 1, 1.0)", (doc_id, dom))
    conn.execute("UPDATE domains SET document_count = document_count + 1 WHERE path = ?", (dom,))
    return doc_id


def upsert_domain(conn, dom):
    conn.execute("INSERT OR IGNORE INTO domains (id, path, parent_path, document_count) VALUES (?, ?, ?, 0)",
                 (str(uuid.uuid4()), dom, parent_of(dom)))
