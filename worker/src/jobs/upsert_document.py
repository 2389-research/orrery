# ABOUTME: upsert_document — the shared per-document primitive (create / update / skip).
# ABOUTME: Keyed on source_path; co-occurrence is a projection via recompute_cooccurrence.
"""The invariant core of the sync pipeline (spec 2026-08-14 incremental-source-sync 6-9).

A featurizer turns a source into `(source_path, title, content)` tuples; this primitive
turns each into graph state, deciding create / update-in-place / skip by `source_path`
identity + `content_hash`. It is cardinality-agnostic: a featurizer yielding 200 docs
just calls it 200 times. Deletion (soft-delete) is handled by the caller (scan_source).

`emits_cooccurrence=False` (a repo/tracker rollup or module summary in Spec 2) suppresses
edge production for THIS doc's entities; always True for vault notes.
"""
import hashlib
import json
import uuid

from ..db import mark_graph_dirty, recompute_cooccurrence
from ..classifier import classify_document
from ..identity_filter import is_identity_noise
from ..silo import resolve_silo_id, silo_match

# Used only when no simmered general spec exists yet — keeps a fresh workspace's first
# vault sync from extracting nothing.
_GENERAL_EXTRACTION_FALLBACK = (
    "Extract the key named entities from the text: concepts, tools, techniques, "
    "people, organizations, products, and materials. Return each with a short type."
)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def chunk_text(text: str, chunk_size: int = 2000, overlap: int = 200) -> list[dict]:
    """Fixed-size chunker — the same shape the orchestrator's pipeline.chunker uses."""
    if len(text) <= chunk_size:
        return [{"chunk_index": 0, "offset": 0, "length": len(text), "text": text}]
    chunks, start, idx = [], 0, 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append({"chunk_index": idx, "offset": start, "length": end - start,
                       "text": text[start:end]})
        idx += 1
        start = end - overlap if end < len(text) else end
    return chunks


def _load_general_spec(conn) -> tuple[str, int]:
    row = conn.execute(
        "SELECT spec_content, version FROM specs WHERE domain_path IS NULL "
        "ORDER BY version DESC LIMIT 1").fetchone()
    if row and row[0]:
        return row[0], (row[1] or 0)
    return _GENERAL_EXTRACTION_FALLBACK, 0


async def extract_document_entities(conn, relay, settings, *, doc_id, chunks, spec,
                                    spec_version, scope, job_id, doc_path,
                                    collection_name) -> dict:
    """Extract entities from each chunk, resolve/dedupe against merge_map + existing
    entities, and write entity_sources. Factored verbatim from extract_batch so both
    the batch path and upsert_document share ONE extraction implementation.

    `chunks` is an iterable of (chunk_id, chunk_text). Returns per-doc stats and the
    chunk->entity map the caller projects co-occurrence from.
    """
    chunk_entities: dict[str, list[str]] = {}
    total = new = matched = 0

    silo_row = conn.execute("SELECT silo_id FROM documents WHERE id = ?", (doc_id,)).fetchone()
    silo = silo_row[0] if silo_row else None

    for chunk_id, chunk_text_ in chunks:
        response = await relay.complete(
            model=settings.extraction_model, max_tokens=4096,
            messages=[{"role": "user", "content": f"{spec}\n\nTEXT:\n{chunk_text_}\n\nRespond with JSON only: {{\"entities\": [{{\"name\": \"...\", \"type\": \"...\"}}]}}"}],
        )
        text = response.text
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        entities = parsed.get("entities", []) if isinstance(parsed, dict) else []
        if not isinstance(entities, list):
            entities = []

        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("name"), str):
                continue
            name = entity["name"].lower().strip()
            etype = entity.get("type") if isinstance(entity.get("type"), str) else "Thing"
            if not name:
                continue
            if is_identity_noise(name, doc_path=doc_path, collection_name=collection_name):
                continue

            is_new = False
            row = conn.execute(
                f"SELECT mm.to_entity_id FROM merge_map mm WHERE mm.from_name = ? "
                f"AND {silo_match('mm.to_entity_id')}", (name, silo)).fetchone()
            if row:
                entity_id = row[0]
            else:
                row = conn.execute(
                    f"SELECT e.id FROM entities e WHERE e.canonical_name = ? AND e.type = ? "
                    f"AND {silo_match('e.id')}", (name, etype, silo)).fetchone()
                if row:
                    entity_id = row[0]
                else:
                    entity_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, ?)", (entity_id, name, etype))
                    is_new = True

            extraction_pass = "domain-specific" if scope == "domain" else "general"
            conn.execute(
                "INSERT INTO entity_sources (entity_id, document_id, chunk_id, extraction_pass, spec_version, job_id) VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, doc_id, chunk_id, extraction_pass, spec_version, job_id),
            )
            chunk_entities.setdefault(chunk_id, []).append(entity_id)
            total += 1
            if is_new:
                new += 1
            else:
                matched += 1

    return {"chunk_entities": chunk_entities, "total": total, "new": new, "matched": matched}


def _ensure_domain(conn, path: str) -> None:
    parts = path.split("/")
    for i in range(len(parts)):
        p = "/".join(parts[: i + 1])
        parent = "/".join(parts[:i]) if i > 0 else None
        conn.execute("INSERT OR IGNORE INTO domains (id, path, parent_path) VALUES (?, ?, ?)",
                     (str(uuid.uuid4()), p, parent))


async def _classify_and_assign(conn, relay, settings, doc_id, title, content) -> None:
    """Best-effort document-level classification. A classifier failure must not abort
    the ingest — the graph's entities/co-occurrence do not depend on domains."""
    try:
        taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains").fetchall()]
        classification = await classify_document(
            relay=relay, title=title or "", excerpt=content[:2000],
            existing_taxonomy=taxonomy, model=settings.classification_model)
    except Exception as e:  # noqa: BLE001 — best-effort, logged
        print(f"[upsert_document] classify skipped ({type(e).__name__}: {e})", flush=True)
        return []
    primary = classification.get("primary_domain")
    rows = []
    if primary:
        rows.append((primary, 1, classification.get("confidence") or 0.0))
    seen = {primary}
    for sec in (classification.get("secondary_domains") or []):
        if sec and sec not in seen:
            seen.add(sec)
            rows.append((sec, 0, 0.0))
    assigned = []
    for path, is_primary, conf in rows:
        _ensure_domain(conn, path)
        conn.execute(
            "INSERT OR IGNORE INTO document_domains (document_id, domain_path, is_primary, confidence) "
            "VALUES (?, ?, ?, ?)", (doc_id, path, is_primary, conf))
        assigned.append(path)
    return assigned


def _recount_collection(conn, collection_id) -> None:
    """Refresh a collection's denormalized documents-per-collection count from its active
    membership. The one-shot ingest paths (ingest_repo/pdf/ccvault/tracker) increment it
    inline per doc; the sync path (sync_repo) rebuilds membership via upsert_document +
    soft-delete and never touched it — so watched-source repos read document_count=0
    despite having documents. Recount from the join so create/update/delete all settle."""
    if not collection_id:
        return
    conn.execute(
        "UPDATE collections SET document_count = ("
        "  SELECT COUNT(*) FROM document_collections dc "
        "  JOIN documents d ON d.id = dc.document_id "
        "  WHERE dc.collection_id = ? AND d.invalid_at IS NULL) WHERE id = ?",
        (collection_id, collection_id))


def _recount_domains(conn, paths) -> None:
    """Refresh the denormalized documents-per-domain count. The viz layout
    (domain_layout.ensure_layout -> _get_domain_paths) only positions domains with
    document_count > 0, so a stale zero leaves a domain's entities unplaceable."""
    for p in {x for x in paths if x}:
        conn.execute(
            "UPDATE domains SET document_count = "
            "(SELECT COUNT(*) FROM document_domains WHERE domain_path = ?) WHERE path = ?",
            (p, p))


async def upsert_document(conn, relay, settings, *, source_path, title, content,
                          source_id=None, emits_cooccurrence=True,
                          collection_id=None, role=None, parent_path=None,
                          content_type="text", metadata=None, domain_path=None,
                          classify=True, pre_chunked=False) -> dict:
    """Create / update-in-place / skip a document keyed on `source_path`.

    Vault notes use the defaults (fixed-size chunks, per-doc LLM classification,
    content_type='text', emits). Repo docs (Spec 2) pass the extras:
      - `collection_id` + `role` ('leaf'|'group'|'root') + `parent_path` — write a
        document_collections membership row; `emits_cooccurrence` lands on it, so the
        projection gate excludes group/root summaries automatically.
      - `content_type='code_intent'`, `pre_chunked=True` — a codesum summary is ONE
        chunk, not fixed-size split.
      - `domain_path=<repo domain>` + `classify=False` — repos classify once at the repo
        level, not per summary doc.

    Returns {"action": "created"|"updated"|"skipped"|"conflict",
             "document_id": ..., "entities": <count extracted this run>}.
    Commits once at the end (crash-safe / resumable per document).
    """
    chash = _content_hash(content)
    meta_json = json.dumps(metadata, default=str) if metadata else None

    existing = conn.execute(
        "SELECT id, content_hash, source_id, title, metadata FROM documents "
        "WHERE source_path = ? AND invalid_at IS NULL", (source_path,)).fetchone()

    if existing is not None:
        ex_id, ex_hash, ex_source = existing["id"], existing["content_hash"], existing["source_id"]
        # Adoption rule: match a doc already owned by THIS source, or an unmanaged one
        # (source_id IS NULL). Never steal a path owned by a different source.
        if ex_source is not None and source_id is not None and ex_source != source_id:
            return {"action": "conflict", "document_id": ex_id, "entities": 0}
        if ex_hash == chash:
            # Body unchanged -> skip the expensive re-chunk + re-extract. But the content
            # hash is over the cleaned BODY only, so a note whose frontmatter changed
            # (its title/metadata) without a body edit would otherwise keep stale fields.
            # Persist just those (cheap, no re-extraction), alongside source adoption.
            sets, params = [], []
            if ex_source is None and source_id is not None:
                sets.append("source_id = ?")
                params.append(source_id)
            if existing["title"] != title:
                sets.append("title = ?")
                params.append(title)
            if (existing["metadata"] or None) != meta_json:
                sets.append("metadata = ?")
                params.append(meta_json)
            if sets:
                params.append(ex_id)
                conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id = ?", params)
                conn.commit()
            return {"action": "skipped", "document_id": ex_id, "entities": 0}
        # Update-in-place: retract the old derived rows, keep the same document id.
        doc_id = ex_id
        old_entities = [r[0] for r in conn.execute(
            "SELECT DISTINCT entity_id FROM entity_sources WHERE document_id = ?", (doc_id,)).fetchall()]
        old_domain_paths = [r[0] for r in conn.execute(
            "SELECT domain_path FROM document_domains WHERE document_id = ?", (doc_id,)).fetchall()]
        conn.execute("DELETE FROM entity_sources WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM document_domains WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM document_collections WHERE document_id = ?", (doc_id,))
        effective_source_id = ex_source or source_id
        silo_id = resolve_silo_id(effective_source_id, collection_id)
        conn.execute(
            "UPDATE documents SET title = ?, content = ?, content_hash = ?, content_type = ?, "
            "metadata = ?, modified_at = CURRENT_TIMESTAMP, source_id = COALESCE(source_id, ?), "
            "silo_id = ?, status = 'pending' WHERE id = ?",
            (title, content, chash, content_type, meta_json, source_id, silo_id, doc_id))
        action = "updated"
    else:
        doc_id = str(uuid.uuid4())
        old_entities = []
        old_domain_paths = []
        silo_id = resolve_silo_id(source_id, collection_id)
        conn.execute(
            "INSERT INTO documents (id, title, content, content_hash, source_path, source_id, "
            "content_type, metadata, status, silo_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (doc_id, title, content, chash, source_path, source_id, content_type, meta_json, silo_id))
        action = "created"

    # Collection membership (repo hierarchy). The emits_cooccurrence flag lands HERE, so
    # the projection gate excludes group/root summaries at both endpoints.
    if collection_id is not None:
        conn.execute(
            "INSERT OR REPLACE INTO document_collections "
            "(document_id, collection_id, parent_path, role, emits_cooccurrence) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, collection_id, parent_path, role, int(bool(emits_cooccurrence))))

    # Chunk + persist. A pre-featurized summary (codesum) is one chunk; prose is split.
    if pre_chunked:
        chunk_meta = [{"chunk_index": 0, "offset": 0, "length": len(content), "text": content}]
    else:
        chunk_meta = chunk_text(content, settings.chunk_size)
    chunk_rows = []
    for cm in chunk_meta:
        cid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chunks (id, document_id, chunk_index, offset, length, text) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, doc_id, cm["chunk_index"], cm["offset"], cm["length"], cm["text"]))
        chunk_rows.append((cid, cm["text"]))

    assigned_domains = []
    if domain_path:
        _ensure_domain(conn, domain_path)
        conn.execute(
            "INSERT OR IGNORE INTO document_domains (document_id, domain_path, is_primary, confidence) "
            "VALUES (?, ?, 1, 1.0)", (doc_id, domain_path))
        assigned_domains = [domain_path]
    elif classify:
        assigned_domains = await _classify_and_assign(conn, relay, settings, doc_id, title, content)
    # Keep the denormalized documents-per-domain count fresh — the viz layout only
    # positions domains with document_count > 0.
    _recount_domains(conn, set(old_domain_paths) | set(assigned_domains))

    spec, spec_version = _load_general_spec(conn)
    res = await extract_document_entities(
        conn, relay, settings, doc_id=doc_id, chunks=chunk_rows, spec=spec,
        spec_version=spec_version, scope="general", job_id=None,
        doc_path=(title or ""), collection_name="")

    # Co-occurrence projection. Old entities are always recomputed (to retract this
    # doc's prior contribution on update); new entities only when this doc emits.
    affected = set(old_entities)
    if emits_cooccurrence:
        affected |= {eid for eids in res["chunk_entities"].values() for eid in eids}
    if affected:
        recompute_cooccurrence(conn, list(affected))

    conn.execute("UPDATE documents SET status = 'extracted' WHERE id = ?", (doc_id,))
    mark_graph_dirty(conn)
    conn.commit()
    return {"action": action, "document_id": doc_id, "entities": res["total"]}
