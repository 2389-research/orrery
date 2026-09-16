# ABOUTME: Phase-1 PDF ingest — one page-document per page (text + vision description
# ABOUTME: + SigLIP image embedding), chained under a kind='pdf' collection; enqueues extract_batch.

import os
import json
import time
import uuid
import hashlib
from pathlib import Path

import numpy as np
from orrery_relay import Relay

from ..config import get_settings
from ..db import get_connection, mark_graph_dirty
from ..classifier import classify_document
from ..pdf_pages import rasterize_pdf, pdf_page_texts, THIN_TEXT_CHARS, is_pdf_bytes
from ..image_prep import make_image_content_block
from ..image_embedding import embed_image, embed_image_text

_DESCRIBE_PROMPT = (
    "Describe this page in 2-3 sentences. What is on it? "
    "Note any diagrams, tables, or figures."
)


async def describe_page(relay: Relay, model: str, png_path: str) -> str:
    """Vision-LLM description of one rasterized page."""
    resp = await relay.complete(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": [
            make_image_content_block(Path(png_path)),
            {"type": "text", "text": _DESCRIBE_PROMPT},
        ]}],
    )
    return (resp.text or "").strip()


async def run_ingest_pdf(job: dict, db_path: str) -> None:
    settings = get_settings()
    relay = Relay.from_settings(settings)

    config = json.loads(job["config"]) if job["config"] else {}
    root_path = config["root_path"]
    collection_id = config["collection_id"]
    collection_name = config["collection_name"]
    spec_id = config["spec_id"]
    # 'always' (default): describe+embed every page. 'fallback': only thin-text pages.
    # 'off': text only, no vision.
    vision_mode = config.get("vision_mode", "always")
    started = time.monotonic()

    file_bytes = Path(root_path).read_bytes()
    if not is_pdf_bytes(file_bytes):
        raise ValueError(f"not a PDF: {root_path}")

    # Both are pypdfium2 and same length/order — page N of one lines up with page N
    # of the other.
    pngs = rasterize_pdf(file_bytes)
    texts = pdf_page_texts(file_bytes)

    docs_dir = os.path.join(settings.documents_dir, "pdf", collection_id)
    os.makedirs(docs_dir, exist_ok=True)

    # Read the existing taxonomy in a short-lived connection — nothing held open
    # across the classification LLM call.
    conn = get_connection(db_path)
    try:
        taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains").fetchall()]
    finally:
        conn.close()

    # Classify the PDF ONCE, on its concatenated page text (falls back to the name for
    # a scan with no extractable text).
    summary = "\n\n".join(t for t in texts if t.strip())[:8000] or collection_name
    classification = await classify_document(
        relay=relay,
        title=collection_name,
        excerpt=f"Document: {collection_name}\n\n{summary}",
        existing_taxonomy=taxonomy,
        model=settings.classification_model,
    )
    primary_dom = classification["primary_domain"]
    confidence = classification.get("confidence", 1.0)

    # Build each page's document payload OUTSIDE the DB transaction: the vision calls
    # and embeddings are slow and must not hold a write lock.
    pages = []
    for n, (png, text_n) in enumerate(zip(pngs, texts, strict=True)):
        art_path = os.path.join(docs_dir, f"page-{n}.png")
        with open(art_path, "wb") as f:
            f.write(png)

        run_vision = vision_mode == "always" or (
            vision_mode == "fallback" and len(text_n.strip()) < THIN_TEXT_CHARS
        )
        desc, emb = "", None
        if run_vision:
            # A vision/embedding failure on ONE page must not lose the whole PDF: keep
            # the page's text and move on.
            try:
                desc = await describe_page(relay, settings.classification_model, art_path)
                emb = embed_image(Path(art_path))
                if emb is None and desc:
                    emb = embed_image_text(desc)
            except Exception as e:
                print(f"[ingest_pdf] page {n} vision failed "
                      f"({type(e).__name__}: {e})", flush=True)

        content = "\n\n".join(x for x in (text_n.strip(), desc) if x)
        if not content:
            print(f"[ingest_pdf] page {n} skipped (no text, no vision)", flush=True)
            continue
        pages.append({"page": n, "content": content, "emb": emb})

    # Persist everything in one transaction; always close the connection, and only
    # commit on success.
    conn = get_connection(db_path)
    try:
        parent = primary_dom.rsplit("/", 1)[0] if "/" in primary_dom else None
        conn.execute(
            "INSERT OR IGNORE INTO domains (id, path, parent_path, document_count) VALUES (?, ?, ?, 0)",
            (str(uuid.uuid4()), primary_dom, parent),
        )
        for pg in pages:
            doc_id = str(uuid.uuid4())
            chunk_id = str(uuid.uuid4())
            content = pg["content"]
            ch = hashlib.sha256(content.encode()).hexdigest()
            conn.execute(
                "INSERT INTO documents (id, title, content, content_hash, source_path, "
                "content_type, status, metadata) "
                "VALUES (?, ?, ?, ?, ?, 'pdf_page', 'classified', ?)",
                (doc_id, f"{collection_name} p{pg['page']}", content, ch,
                 f"{root_path}#page={pg['page']}", json.dumps({"page": pg["page"]})),
            )
            conn.execute(
                "INSERT INTO chunks (id, document_id, chunk_index, text, offset, length) "
                "VALUES (?, ?, 0, ?, 0, ?)",
                (chunk_id, doc_id, content, len(content)),
            )
            if pg["emb"] is not None:
                conn.execute(
                    "UPDATE chunks SET image_embedding = ? WHERE id = ?",
                    (np.asarray(pg["emb"], dtype=np.float32).tobytes(), chunk_id),
                )
            # A single page is a leaf that emits co-occurrence (its entities are local
            # to that page, unlike a repo/module rollup summary).
            conn.execute(
                "INSERT INTO document_collections (document_id, collection_id, parent_path, "
                "role, emits_cooccurrence) VALUES (?, ?, ?, 'leaf', 1)",
                (doc_id, collection_id, root_path),
            )
            conn.execute(
                "INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) "
                "VALUES (?, ?, 1, ?)",
                (doc_id, primary_dom, confidence),
            )
            conn.execute(
                "UPDATE domains SET document_count = document_count + 1 WHERE path = ?",
                (primary_dom,),
            )
            conn.execute(
                "UPDATE collections SET document_count = document_count + 1 WHERE id = ?",
                (collection_id,),
            )
        # Enqueue Phase 2 (extract_batch over the new pdf_page docs).
        conn.execute(
            "INSERT INTO jobs (id, type, target, status, config) "
            "VALUES (?, 'extract_batch', ?, 'queued', ?)",
            (str(uuid.uuid4()), collection_id,
             json.dumps({"spec_id": spec_id, "scope": "pdf_page"})),
        )
        result = {
            "collection_name": collection_name,
            "pages": len(pages),
            "primary_domain": primary_dom,
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?",
                     (json.dumps(result), job["id"]))
        mark_graph_dirty(conn)
        conn.commit()
        print(f"[ingest_pdf] {collection_name}: {len(pages)} pages — "
              f"enqueued extract_batch", flush=True)
    finally:
        conn.close()
