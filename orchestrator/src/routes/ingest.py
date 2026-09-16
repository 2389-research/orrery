# ABOUTME: Ingest route — accepts file uploads (text + images) and directory paths.
# ABOUTME: Handles dedup, chunking, classification, extraction, and job queuing.

import uuid
import os
import json
import hashlib
import sqlite3
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Response, status
from orrery_relay import Relay

from ..config import get_settings
from ..dependencies import get_auth_store, AuthStore
from ..models import (IngestResult, DirectoryIngestRequest, RepoIngestRequest,
                      TrackerRunsIngestRequest, TextIngestRequest, CcvaultIngestRequest,
                      PdfIngestRequest)
from ..pipeline.chunker import chunk_document
from ..pipeline.excerpt import build_classification_excerpt
from ..pipeline.classifier import classify_document
from ..pipeline.domain_normalizer import assign_document_domains
from ..pipeline.extractor import extract_document
from ..pipeline.normalizer import normalize_entity
from ..db import recompute_cooccurrence
from ..pipeline.image_prep import is_image_file
from ..pipeline.file_extractor import extract_text, NOTEBOOK_EXTENSIONS, PDF_EXTENSIONS, DOCX_EXTENSIONS, ALL_SUPPORTED_EXTENSIONS

router = APIRouter()

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# General specs loaded from orchestrator/specs/*.md — edit those files to update
_SPECS_DIR = Path(__file__).resolve().parent.parent.parent / "specs"


def _load_general_spec(name: str) -> str:
    """Load a general spec from the specs directory."""
    path = _SPECS_DIR / f"{name}.md"
    return path.read_text()


GENERAL_TEXT_SPEC = _load_general_spec("general_text")
GENERAL_IMAGE_SPEC = _load_general_spec("general_image")
GENERAL_CODE_SPEC = _load_general_spec("general_code")


def _unique_title(store, title: str) -> str:
    """Keep same-filename uploads distinguishable: if another document already
    has this title, append an incrementing suffix (README.md -> README.md (2)).
    Exact-content duplicates are handled separately by content-hash dedup."""
    if not store.documents.title_exists(title):
        return title
    n = 2
    while store.documents.title_exists(f"{title} ({n})"):
        n += 1
    return f"{title} ({n})"


async def _ingest_document(store, title: str, content: str, source_path: str | None) -> dict:
    settings = get_settings()
    relay = Relay.from_settings(settings)

    # Dedup: skip if document with same content hash already exists
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = store.documents.get_by_hash(content_hash)
    if existing:
        domains = [d.domain_path for d in store.domains.get_domains_for_document(existing.id)]
        return {
            "document_id": existing.id,
            "title": title,
            "domains": domains,
            "entity_count": 0,
            "jobs_queued": [],
            "content_type": "text",
        }

    doc_id = str(uuid.uuid4())
    title = _unique_title(store, title)

    # 1. Store document (loose upload: no source, no collection -> null silo)
    store.documents.create(doc_id, title, content, content_hash, source_path, silo_id=None)

    # 1b. Chunk and store
    chunks = chunk_document(content, chunk_size=settings.chunk_size)
    from ..repositories.interfaces import Chunk
    chunk_objs = []
    for chunk in chunks:
        chunk["id"] = str(uuid.uuid4())
        chunk_objs.append(Chunk(
            id=chunk["id"],
            document_id=doc_id,
            chunk_index=chunk["chunk_index"],
            text=chunk["text"],
            offset=chunk["offset"],
            length=chunk["length"],
        ))
    store.chunks.create_batch(chunk_objs)

    # 2. Classify
    excerpt = build_classification_excerpt(title, content)
    taxonomy = store.domains.get_all_paths()

    classification = await classify_document(
        relay=relay, title=title, excerpt=excerpt,
        existing_taxonomy=taxonomy, model=settings.classification_model,
    )

    domains = assign_document_domains(store, doc_id, classification)
    store.documents.update_status(doc_id, "classified")

    # 3. Extract — use simmered general spec if available, otherwise built-in general spec
    entity_count = 0
    chunk_entities: dict[str, list[str]] = {}
    spec = store.specs.get_general()
    spec_content = spec.spec_content if spec else GENERAL_TEXT_SPEC
    spec_version = spec.version if spec else 0
    extraction_pass = "general_simmered" if spec else "general"

    entities = await extract_document(
        relay=relay, chunks=chunks, spec=spec_content, model=settings.extraction_model,
    )
    for entity in entities:
        entity_id = normalize_entity(store, entity["name"], entity["type"], silo=None)
        store.entity_sources.create(
            entity_id=entity_id,
            document_id=doc_id,
            chunk_id=entity.get("chunk_id"),
            extraction_pass=extraction_pass,
            spec_version=spec_version,
        )
        chunk_id = entity.get("chunk_id")
        if chunk_id:
            chunk_entities.setdefault(chunk_id, []).append(entity_id)

    entity_count = len(entities)
    store.documents.update_status(doc_id, "extracted")

    # 4. Cascade through domain specs
    domain_entity_count = 0
    seen_specs = set()

    for domain_path in domains:
        parts = domain_path.split("/")
        ancestor_paths = ["/".join(parts[:i+1]) for i in range(len(parts))]

        for ancestor in reversed(ancestor_paths):  # deepest first
            domain_spec = store.specs.get_for_domain(ancestor)

            if domain_spec and domain_spec.id not in seen_specs:
                seen_specs.add(domain_spec.id)
                d_entities = await extract_document(
                    relay=relay, chunks=chunks,
                    spec=domain_spec.spec_content, model=settings.extraction_model,
                )
                for entity in d_entities:
                    entity_id = normalize_entity(store, entity["name"], entity["type"], silo=None)
                    store.entity_sources.create(
                        entity_id=entity_id,
                        document_id=doc_id,
                        chunk_id=entity.get("chunk_id"),
                        extraction_pass="domain-specific",
                        spec_version=domain_spec.version,
                    )
                    chunk_id = entity.get("chunk_id")
                    if chunk_id:
                        chunk_entities.setdefault(chunk_id, []).append(entity_id)
                domain_entity_count += len(d_entities)

    if domain_entity_count > 0:
        entity_count += domain_entity_count
        store.documents.update_status(doc_id, "enriched")

    # Co-occurrence is a pure projection of entity_sources (db.recompute_cooccurrence),
    # written once over every entity this document touched — general + domain passes.
    affected = {eid for eids in chunk_entities.values() for eid in eids}
    if affected:
        recompute_cooccurrence(store.conn, list(affected))
        store.conn.commit()

    # 5. Queue domain simmers (general spec always available via built-in, no auto-simmer needed)
    jobs_queued = []

    for domain_path in domains:
        domain = store.domains.get(domain_path)
        if domain and domain.document_count >= settings.domain_spec_threshold and domain.spec_version is None:
            existing_job = store.jobs.get_existing("simmer_domain", domain_path, ["queued", "running"])
            if not existing_job:
                job_id = str(uuid.uuid4())
                store.jobs.create(job_id, "simmer_domain", domain_path, {"domain": domain_path})
                jobs_queued.append(job_id)

    # Rebuild search index inline after extraction
    if entity_count > 0:
        try:
            from ..pipeline.search.retrieval import embed_new_entities, embed_new_chunks
            embed_new_entities(store.conn)
            embed_new_chunks(store.conn)
        except Exception as e:
            print(f"Search index update after ingest: {e}")

    return {
        "document_id": doc_id,
        "title": title,
        "domains": domains,
        "entity_count": entity_count,
        "jobs_queued": jobs_queued,
        "content_type": "text",
    }


async def _ingest_image(store, title: str, file_bytes: bytes, image_path: str) -> dict:
    """Ingest an image: describe via vision LLM, classify, extract entities."""
    settings = get_settings()
    relay = Relay.from_settings(settings)

    # Dedup
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    existing = store.documents.get_by_hash(content_hash)
    if existing:
        domains = [d.domain_path for d in store.domains.get_domains_for_document(existing.id)]
        return {
            "document_id": existing.id, "title": title, "domains": domains,
            "entity_count": 0, "jobs_queued": [], "content_type": "image",
        }

    doc_id = str(uuid.uuid4())

    # 1. Describe the image via vision LLM
    from ..pipeline.image_prep import image_to_base64, make_image_content_block
    b64, media_type = image_to_base64(Path(image_path))

    description = ""
    try:
        desc_response = await relay.complete(
            model=settings.classification_model, max_tokens=512,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Describe this image in 2-3 sentences. What is it? What details are visible?"},
            ]}],
        )
        description = desc_response.text.strip()
    except Exception as e:
        print(f"Image description failed: {e}", flush=True)
        description = f"Image: {title}"

    # Store document with description as content
    title = _unique_title(store, title)
    store.documents.create(doc_id, title, description, content_hash, image_path, content_type="image")

    # Store a single chunk with the description
    chunk_id = str(uuid.uuid4())
    from ..repositories.interfaces import Chunk
    store.chunks.create_batch([Chunk(
        id=chunk_id, document_id=doc_id, chunk_index=0,
        text=description, offset=0, length=len(description),
    )])

    # 2. Classify using image vision
    from ..pipeline.classifier import classify_image
    taxonomy = store.domains.get_all_paths()

    classification = await classify_image(
        relay=relay, image_base64=b64, media_type=media_type,
        existing_taxonomy=taxonomy, model=settings.classification_model,
    )

    domains = assign_document_domains(store, doc_id, classification)
    store.documents.update_status(doc_id, "classified")

    # 3. Extract entities using general image spec
    entity_count = 0
    chunk_entities: dict[str, list[str]] = {}
    spec = store.specs.get_general()
    spec_content = spec.spec_content if spec else GENERAL_IMAGE_SPEC
    spec_version = spec.version if spec else 0

    # Extract entities from the image description using the spec
    chunks = [{"id": chunk_id, "chunk_index": 0, "text": description, "offset": 0, "length": len(description)}]
    entities = await extract_document(
        relay=relay, chunks=chunks, spec=spec_content, model=settings.extraction_model,
    )
    for entity in entities:
        entity_id = normalize_entity(store, entity["name"], entity["type"], silo=None)
        store.entity_sources.create(
            entity_id=entity_id, document_id=doc_id,
            chunk_id=chunk_id, extraction_pass="general",
            spec_version=spec_version,
        )
        chunk_entities.setdefault(chunk_id, []).append(entity_id)

    entity_count = len(entities)
    # Co-occurrence via the shared projection helper (same invariant as the text path).
    affected = {eid for eids in chunk_entities.values() for eid in eids}
    if affected:
        recompute_cooccurrence(store.conn, list(affected))
        store.conn.commit()
    store.documents.update_status(doc_id, "extracted")

    # Rebuild search index — text (sentence-transformers) for compatibility
    try:
        from ..pipeline.search.retrieval import embed_new_entities, embed_new_chunks
        embed_new_entities(store.conn)
        embed_new_chunks(store.conn)
    except Exception as e:
        print(f"Search index update after image ingest: {e}")

    # SigLIP embedding — populates chunks.image_embedding for cross-modal search.
    # Prefers pixel embedding; falls back to description text in the same SigLIP latent space.
    try:
        import numpy as np
        from ..pipeline.image_embedding import embed_image, embed_image_text

        img_emb = embed_image(Path(image_path))
        if img_emb is None and description:
            img_emb = embed_image_text(description)

        if img_emb is not None:
            store.conn.execute(
                "UPDATE chunks SET image_embedding = ? WHERE id = ?",
                (img_emb.astype(np.float32).tobytes(), chunk_id),
            )
            store.conn.commit()
    except Exception as e:
        print(f"SigLIP embedding after image ingest: {e}", flush=True)

    return {
        "document_id": doc_id, "title": title, "domains": domains,
        "entity_count": entity_count, "jobs_queued": [], "content_type": "image",
    }


@router.post("/ingest", response_model=IngestResult, status_code=status.HTTP_201_CREATED)
async def ingest_file(
    response: Response,
    file: UploadFile = File(...),
    auth: AuthStore = Depends(get_auth_store),
):
    file_bytes = await file.read()
    title = file.filename or "untitled"
    settings = get_settings()
    os.makedirs(settings.documents_dir, exist_ok=True)

    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large: max {_MAX_UPLOAD_BYTES // (1024*1024)} MB")

    suffix = Path(title).suffix.lower()
    if suffix not in ALL_SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {suffix or '(no extension)'}")

    store = auth.store
    try:
        if is_image_file(title):
            doc_path = os.path.join(settings.documents_dir, f"{uuid.uuid4()}_{title}")
            with open(doc_path, "wb") as f:
                f.write(file_bytes)
            result = await _ingest_image(store, title, file_bytes, doc_path)
        else:
            try:
                content = extract_text(title, file_bytes)
            except ValueError as e:
                msg = str(e)
                if "magic bytes" in msg:
                    raise HTTPException(status_code=415, detail=msg)
                raise HTTPException(status_code=422, detail=msg)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Could not extract text from file: {e}")
            doc_path = os.path.join(settings.documents_dir, f"{uuid.uuid4()}_{title}")
            with open(doc_path, "wb") as f:
                f.write(file_bytes)
            result = await _ingest_document(store, title, content, doc_path)
    finally:
        store.close()
    doc_id = result.get("document_id") if isinstance(result, dict) else getattr(result, "document_id", None)
    if doc_id:
        response.headers["Location"] = f"/documents/{doc_id}"
    return result


@router.post("/ingest/text", response_model=IngestResult, status_code=status.HTTP_201_CREATED)
async def ingest_text(request: TextIngestRequest, auth: AuthStore = Depends(get_auth_store)):
    """Ingest a document from raw text (JSON) — the programmatic / MCP entry point.

    Runs the same pipeline as a file upload (classify -> extract -> co-occurrence) minus
    file handling: the text IS the source, so nothing is written to `documents_dir` and
    `source_path` is None (GET /documents/{id}/file will 404, which is correct — there is
    no raw artifact)."""
    store = auth.store
    try:
        result = await _ingest_document(store, request.title, request.content, None)
    finally:
        store.close()
    return result


@router.post("/ingest/directory")
async def ingest_directory(request: DirectoryIngestRequest, auth: AuthStore = Depends(get_auth_store)):
    dir_path = Path(request.path)
    if not dir_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {request.path}")

    store = auth.store
    results = []
    try:
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if suffix not in ALL_SUPPORTED_EXTENSIONS:
                continue
            file_bytes = file_path.read_bytes()
            if is_image_file(file_path.name):
                result = await _ingest_image(store, file_path.name, file_bytes, str(file_path))
            else:
                try:
                    content = extract_text(file_path.name, file_bytes)
                except Exception:
                    continue
                result = await _ingest_document(store, file_path.stem, content, str(file_path))
            results.append(result)
    finally:
        store.close()

    return {"documents": results, "total": len(results)}


@router.post("/ingest/repo", status_code=status.HTTP_202_ACCEPTED)
async def ingest_repo(request: RepoIngestRequest, auth: AuthStore = Depends(get_auth_store)):
    """Summarize a git checkout into a collection of code_intent documents.

    Returns 202 and does no model work inline: summarizing a repo is many LLM calls,
    so this only creates the collection row and enqueues the phase-1 worker job. The
    worker then enqueues phase 2 (extract_batch, scope=code_intent) itself.
    """
    dir_path = Path(request.path)
    if not dir_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {request.path}")

    store = auth.store
    try:
        # `collections.path` is UNIQUE, so a repeat ingest of the same name would
        # otherwise surface as an IntegrityError and a 500. Report the conflict with
        # the existing id so the caller can decide (re-ingest under a new name, or go
        # look at what is already there).
        existing = store.collections.get_by_path(request.name)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Collection '{request.name}' already exists (id {existing['id']})")

        # Reuse the existing general spec if the workspace has one, else seed the
        # built-in general_code spec. Extraction is spec-driven, so a workspace with a
        # simmered spec must keep using it rather than being reset by a repo ingest.
        spec = store.specs.get_general()
        if spec:
            spec_id = spec.id
        else:
            spec_id = str(uuid.uuid4())
            store.specs.create(spec_id, None, 1, GENERAL_CODE_SPEC)

        # Classification is deliberately NOT done here. It happens in the worker, on
        # the grounded repo-level summary (read the code, then decide) rather than on a
        # README excerpt — which works for undocumented repos too, and aligns the
        # domain with the vocabulary the extraction actually produces.
        collection_id = str(uuid.uuid4())
        try:
            store.collections.create(collection_id, request.name, request.name, request.path,
                                      provenance_kind=request.provenance_kind)
        except sqlite3.IntegrityError:
            # The get_by_path check above is not a lock, so two requests for the same
            # name can both pass it. The UNIQUE constraint is what actually decides;
            # report the loser as the same 409 rather than a 500, so a race and a repeat
            # look identical to the caller.
            raise HTTPException(
                status_code=409,
                detail=f"Collection '{request.name}' already exists")

        job_id = str(uuid.uuid4())
        try:
            store.jobs.create(job_id, "ingest_repo", collection_id, {
                "root_path": request.path,
                "collection_id": collection_id,
                "collection_name": request.name,
                "spec_id": spec_id,
            })
        except Exception:
            # `collections.create` commits, so a failure here would leave a collection
            # with no job behind — and because the name is UNIQUE and this route answers
            # 409 on a repeat, that orphan would block every retry of the same repo
            # permanently. Undo it so the request is genuinely retryable.
            store.collections.delete(collection_id)
            raise
    finally:
        store.close()

    return {"job_id": job_id, "collection_id": collection_id}


@router.post("/ingest/pdf", status_code=status.HTTP_202_ACCEPTED)
async def ingest_pdf(request: PdfIngestRequest, auth: AuthStore = Depends(get_auth_store)):
    """Ingest a server-side PDF as an ordered chain of page-documents.

    Two-phase, like /ingest/repo: this does NO model work and NO rasterization. It
    validates the path (must exist and start with the %PDF magic bytes), creates a
    kind='pdf' collection, and enqueues the ingest_pdf worker job. The worker rasterizes
    each page and does all model work, then enqueues phase 2 (extract_batch) itself.
    """
    p = Path(request.path)
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {request.path}")
    if p.read_bytes()[:4] != b"%PDF":
        raise HTTPException(status_code=400, detail=f"Not a PDF: {request.path}")
    if request.vision_mode not in ("always", "fallback", "off"):
        raise HTTPException(status_code=422, detail="vision_mode must be always|fallback|off")

    store = auth.store
    try:
        # `collections.path` is UNIQUE — report a repeat ingest of the same name as a 409
        # with the existing id rather than letting the IntegrityError surface as a 500.
        existing = store.collections.get_by_path(request.name)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Collection '{request.name}' already exists (id {existing['id']})")

        # Reuse the workspace's general spec if present, else seed the built-in general_text
        # spec — the page-documents are extracted by extract_batch, which is spec-driven.
        spec = store.specs.get_general()
        if spec:
            spec_id = spec.id
        else:
            spec_id = str(uuid.uuid4())
            store.specs.create(spec_id, None, 1, GENERAL_TEXT_SPEC)

        collection_id = str(uuid.uuid4())
        try:
            store.collections.create(collection_id, request.name, request.name, request.path,
                                     kind="pdf", provenance_kind=request.provenance_kind)
        except sqlite3.IntegrityError:
            # get_by_path isn't a lock; a concurrent create wins the UNIQUE(path) race.
            # Report the loser as the same 409 so a race and a repeat look identical.
            raise HTTPException(
                status_code=409,
                detail=f"Collection '{request.name}' already exists")

        job_id = str(uuid.uuid4())
        try:
            store.jobs.create(job_id, "ingest_pdf", collection_id, {
                "root_path": request.path,
                "collection_id": collection_id,
                "collection_name": request.name,
                "spec_id": spec_id,
                "vision_mode": request.vision_mode,
            })
        except Exception:
            # `collections.create` commits; a failure here would orphan a collection whose
            # UNIQUE name then blocks every retry. Undo it so the request is retryable.
            store.collections.delete(collection_id)
            raise
    finally:
        store.close()

    return {"job_id": job_id, "collection_id": collection_id}


@router.post("/ingest/tracker-runs", status_code=status.HTTP_202_ACCEPTED)
async def ingest_tracker_runs(request: TrackerRunsIngestRequest,
                              auth: AuthStore = Depends(get_auth_store)):
    """Ingest tracker code-gen runs: one COLLECTION per run, trajectory as edges.

    A run IS a collection as far as everything downstream is concerned — extraction,
    co-occurrence, normalization, the graph snapshot, the viz. It needed no schema
    change to land in the same table, which is the evidence that the abstraction is
    "collection" rather than "git repo". Same two-phase shape as /ingest/repo: this
    enqueues phase 1, which enqueues phase 2 itself.
    """
    path = Path(request.path)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {request.path}")

    # Bundle mode when the summaries already exist (no model calls at all); otherwise
    # the worker summarizes the raw runs, which needs tracker's `distill` importable.
    bundled = (path / "index.json").is_file()

    store = auth.store
    try:
        # A run label becomes a collection's UNIQUE `path`, so re-ingesting the same
        # corpus collides. Answering 409 here is the friendly, early rejection; the
        # WORKER owns the guarantee, because only it knows every label (in raw mode they
        # do not exist until the runs are summarized). Checking `request.chain` alone was
        # not enough: a repeat ingest WITHOUT a chain sailed past this and failed inside
        # the worker's insert loop, which is the partial ingest this comment claimed to
        # prevent. In bundle mode the labels are right there in index.json, so use them.
        # UNION, not either/or. Reading index.json only when `chain` was empty meant a
        # PARTIAL chain (naming runA while the corpus also holds an already-ingested
        # runB) skipped the index labels entirely and answered 202 — the conflict then
        # surfacing from the worker instead.
        known_labels = list(request.chain or [])
        if bundled:
            try:
                index = json.loads((path / "index.json").read_text())
                # Valid JSON is not a valid index: `{}` iterates string KEYS and `42`
                # raises TypeError, neither caught below — so both became a 500 instead
                # of being deferred to the worker's clear error. A label must also be a
                # non-empty STRING; `run_label: []` is truthy and would reach get_by_path.
                if isinstance(index, list):
                    known_labels += [
                        row["run_label"] for row in index
                        if isinstance(row, dict)
                        and isinstance(row.get("run_label"), str)
                        and row["run_label"].strip()
                    ]
            except (OSError, ValueError):
                # index.json is caller-supplied data; if it is unreadable the worker will
                # say so properly. Skipping the early check is not skipping the check —
                # and an explicit chain, if given, is still checked.
                pass
        clash = [c for c in known_labels if store.collections.get_by_path(c)]
        if clash:
            raise HTTPException(
                status_code=409,
                detail=f"collections already exist for run label(s): {', '.join(sorted(set(clash)))}")

        spec = store.specs.get_general()
        if spec:
            spec_id = spec.id
        else:
            spec_id = str(uuid.uuid4())
            store.specs.create(spec_id, None, 1, GENERAL_CODE_SPEC)

        job_id = str(uuid.uuid4())
        store.jobs.create(job_id, "ingest_tracker_runs", "tracker-runs", {
            "out_dir": str(path) if bundled else None,
            "raw_root": None if bundled else str(path),
            "spec_id": spec_id,
            "chain": request.chain,
            # Where the raw dip/spec artifacts are staged, so documents.source_path
            # resolves for the worker (map -> territory drill-down). Only needed in
            # bundle mode — raw runs are already resolvable at their own paths.
            "runs_dir": request.runs_dir or str(path.parent / "runs"),
        })
    finally:
        store.close()

    return {"job_id": job_id, "mode": "bundle" if bundled else "raw"}


@router.post("/ingest/ccvault", status_code=status.HTTP_202_ACCEPTED)
async def ingest_ccvault(request: CcvaultIngestRequest, auth: AuthStore = Depends(get_auth_store)):
    """Ingest a staged ccvault session archive into the ACTIVE (target) noosphere.

    Two-phase, like /ingest/repo: this creates (or reuses) the single per-workspace ccvault
    collection and enqueues the worker job; the worker does all model work (Flow A session
    summaries now; Flow B active-work extraction is a follow-up) and enqueues extract_batch.

    Target a CLONE of the source noosphere (docs/ccvault-ingestion.md), never a formal one.
    Idempotent: incrementality lives in the ccvault_* ledgers, so re-ingesting the same
    archive reuses the collection and picks up only new sessions.
    """
    p = Path(request.path)
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"Path not found: {request.path}")

    store = auth.store
    try:
        # ONE persistent ccvault collection per workspace (not one per session — that would
        # make every session its own silo). Get-or-create, keyed on the label as the unique
        # `path`; incrementality comes from the ledgers, not a fresh collection each call.
        existing = store.collections.get_by_path(request.label)
        if existing:
            collection_id = existing["id"]
        else:
            collection_id = str(uuid.uuid4())
            try:
                store.collections.create(collection_id, request.label, request.label,
                                         request.path, kind="ccvault",
                                         provenance_kind=request.provenance_kind)
            except sqlite3.IntegrityError:
                # get_by_path isn't a lock; a concurrent create wins the UNIQUE(path) race.
                # Re-fetch and reuse it rather than 500 — the point is idempotency.
                existing = store.collections.get_by_path(request.label)
                if not existing:
                    raise
                collection_id = existing["id"]

        # Reuse the workspace's general spec if present, else seed the built-in one — the
        # Flow-A docs are extracted by extract_batch, which is spec-driven (same as repo).
        spec = store.specs.get_general()
        if spec:
            spec_id = spec.id
        else:
            spec_id = str(uuid.uuid4())
            store.specs.create(spec_id, None, 1, GENERAL_CODE_SPEC)

        job_id = str(uuid.uuid4())
        store.jobs.create(job_id, "ingest_ccvault", collection_id, {
            "archive_path": request.path,
            "collection_id": collection_id,
            "collection_label": request.label,
            "spec_id": spec_id,
        })
    finally:
        store.close()

    return {"job_id": job_id, "collection_id": collection_id}
