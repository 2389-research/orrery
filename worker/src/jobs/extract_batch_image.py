# ABOUTME: Batch image extraction — runs the simmered image spec against all image documents.
# ABOUTME: Extracts entities + descriptions using Haiku vision, stores results.

import json
import uuid
import base64
from pathlib import Path
from orrery_relay import Relay
from ..db import get_connection
from ..config import get_settings
from .upsert_document import entity_scope


async def run_extract_batch_image(job: dict, db_path: str) -> None:
    settings = get_settings()
    conn = get_connection(db_path)
    relay = Relay.from_settings(settings)

    job_id = job["id"]
    config = json.loads(job["config"]) if job.get("config") else {}
    spec_id = config.get("spec_id")

    spec_row = conn.execute("SELECT spec_content, version FROM specs WHERE id = ?", (spec_id,)).fetchone()
    if not spec_row:
        conn.close()
        raise ValueError(f"Spec not found: {spec_id}")
    spec = spec_row[0]
    spec_version = spec_row[1]

    # Get image docs — scope to a domain when requested (mirrors extract_batch.py)
    scope = config.get("scope", "all_images")
    if scope == "domain":
        domain = config.get("domain")
        docs = conn.execute(
            """SELECT d.id, d.title, d.source_path FROM documents d
               JOIN document_domains dd ON d.id = dd.document_id
               WHERE dd.domain_path = ? AND d.content_type = 'image'
                 AND d.status IN ('classified', 'extracted', 'enriched')""",
            (domain,),
        ).fetchall()
    else:
        docs = conn.execute(
            "SELECT id, title, source_path FROM documents WHERE content_type = 'image' AND status IN ('classified', 'extracted')"
        ).fetchall()
    conn.close()

    schema = {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string"}}, "required": ["name", "type"]}},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["entities", "description", "tags"],
    }

    total_entities = 0
    new_entities = 0
    docs_processed = 0

    for doc in docs:
        doc_id = doc["id"]
        source_path = doc["source_path"]

        if not source_path or not Path(source_path).exists():
            print(f"  Skipping {doc['title']}: image not found", flush=True)
            continue

        b64 = base64.b64encode(Path(source_path).read_bytes()).decode()
        suffix = Path(source_path).suffix.lower()
        media_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"

        try:
            result = await relay.complete_structured(
                model=settings.extraction_model, max_tokens=4096,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": spec},
                ]}],
                schema=schema,
                tool_name="extract_image",
                tool_description="Extract entities and metadata from image",
            )
            if isinstance(result, list):        # tolerate a bare-list result (issue #36)
                result = {"entities": result}
        except Exception as e:
            print(f"  Failed {doc['title']}: {e}", flush=True)
            continue

        conn = get_connection(db_path)

        # Update document content with description
        description = result.get("description", "")
        conn.execute("UPDATE documents SET content = ? WHERE id = ?", (description, doc_id))

        # Update chunk text
        chunk = conn.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,)).fetchone()
        chunk_id = chunk["id"] if chunk else str(uuid.uuid4())
        if chunk:
            conn.execute("UPDATE chunks SET text = ?, length = ? WHERE id = ?", (description, len(description), chunk_id))
        else:
            conn.execute(
                "INSERT INTO chunks (id, document_id, chunk_index, text, length) VALUES (?, ?, 0, ?, ?)",
                (chunk_id, doc_id, description, len(description)),
            )

        # Clear previous extraction sources for this doc (general spec gets replaced by simmered)
        conn.execute("DELETE FROM entity_sources WHERE document_id = ?", (doc_id,))

        silo_row = conn.execute("SELECT silo_id FROM documents WHERE id = ?", (doc_id,)).fetchone()
        silo = silo_row[0] if silo_row else None
        scope_fn, scope_bind = entity_scope(conn, silo)

        # Store entities
        for entity in result.get("entities", []):
            name = entity.get("name", "").lower().strip()
            etype = entity.get("type", "Object")
            if not name:
                continue

            is_new = False
            row = conn.execute(
                f"SELECT e.id FROM entities e WHERE e.canonical_name = ? AND e.type = ? "
                f"AND {scope_fn('e.id')}", (name, etype, scope_bind)).fetchone()
            if row:
                entity_id = row["id"]
            else:
                entity_id = str(uuid.uuid4())
                conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, ?)", (entity_id, name, etype))
                is_new = True

            conn.execute(
                "INSERT INTO entity_sources (entity_id, document_id, chunk_id, extraction_pass, spec_version, job_id) VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, doc_id, chunk_id, "image_spec", spec_version, job_id),
            )

            total_entities += 1
            if is_new:
                new_entities += 1

        conn.execute("UPDATE documents SET status = 'extracted' WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()

        docs_processed += 1
        print(f"  Extracted {docs_processed}/{len(docs)}: {doc['title']} — {len(result.get('entities', []))} entities", flush=True)

    print(f"Image batch extraction: {docs_processed} docs, {total_entities} entities ({new_entities} new)", flush=True)

    # Embed descriptions + entities for search
    print("  Embedding descriptions and entities...", flush=True)
    conn = get_connection(db_path)
    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
        model = SentenceTransformer("all-MiniLM-L6-v2")

        # Embed image descriptions (chunks)
        chunks = conn.execute("""
            SELECT c.id, c.text FROM chunks c JOIN documents d ON c.document_id = d.id
            WHERE d.content_type = 'image' AND c.text != '' AND (c.embedding IS NULL OR length(c.embedding) = 0)
        """).fetchall()
        if chunks:
            texts = [c["text"] for c in chunks]
            embeddings = model.encode(texts, normalize_embeddings=True)
            for i, chunk in enumerate(chunks):
                conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?",
                    (embeddings[i].astype(np.float32).tobytes(), chunk["id"]))
            print(f"  Embedded {len(chunks)} image descriptions", flush=True)

        # Embed new entities
        ents = conn.execute("""
            SELECT id, canonical_name FROM entities WHERE embedding IS NULL
        """).fetchall()
        if ents:
            names = [e["canonical_name"] for e in ents]
            embeddings = model.encode(names, normalize_embeddings=True)
            for i, ent in enumerate(ents):
                conn.execute("UPDATE entities SET embedding = ? WHERE id = ?",
                    (embeddings[i].astype(np.float32).tobytes(), ent["id"]))
            print(f"  Embedded {len(ents)} entities", flush=True)

        conn.commit()
    except Exception as e:
        print(f"  Embedding failed: {e}", flush=True)
    finally:
        conn.close()
