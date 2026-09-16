# ABOUTME: Batch extraction job runner — processes all docs in scope with a given spec.
# ABOUTME: Calls the extraction model per chunk and stores entities + co-occurrence edges.

import json
from orrery_relay import Relay
from ..db import get_connection, mark_graph_dirty, recompute_cooccurrence
from ..config import get_settings
from .upsert_document import extract_document_entities

async def run_extract_batch(job: dict, db_path: str) -> None:
    settings = get_settings()
    conn = get_connection(db_path)
    relay = Relay.from_settings(settings)

    job_id = job["id"]
    config = json.loads(job["config"]) if job["config"] else {}
    spec_id = config.get("spec_id")
    scope = config.get("scope", "all_classified")

    spec_row = conn.execute("SELECT spec_content, version, domain_path FROM specs WHERE id = ?", (spec_id,)).fetchone()
    if not spec_row:
        conn.close()
        raise ValueError(f"Spec not found: {spec_id}")
    spec = spec_row[0]
    spec_version = spec_row[1]
    spec_domain = spec_row[2]
    spec_label = f"domain/{spec_domain}_v{spec_version}" if spec_domain else f"general_v{spec_version}"

    if scope == "all_classified":
        docs = conn.execute("SELECT id FROM documents WHERE status = 'classified'").fetchall()
    elif scope == "code_intent":
        # Phase 2 of collection ingest. Scoped by content_type rather than by
        # collection id because a single ingest enqueues one batch and the docs it
        # produced are exactly the unswept code_intent ones — and the `status`
        # predicate is what makes a re-run idempotent (already-extracted docs move
        # to 'extracted' and drop out of scope).
        docs = conn.execute(
            "SELECT id FROM documents WHERE content_type = 'code_intent' AND status = 'classified'"
        ).fetchall()
    elif scope == "session_intent":
        # Phase 2 of ccvault (Flow A) ingest — same shape as code_intent but a distinct
        # content_type so a ccvault batch and a repo/tracker batch never sweep each other's
        # docs. `status='classified'` keeps it idempotent (extracted docs drop out of scope).
        docs = conn.execute(
            "SELECT id FROM documents WHERE content_type = 'session_intent' AND status = 'classified'"
        ).fetchall()
    elif scope == "pdf_page":
        # Phase 2 of PDF ingest — scoped by content_type so a PDF batch never sweeps
        # another collection's docs. status='classified' keeps it idempotent.
        docs = conn.execute(
            "SELECT id FROM documents WHERE content_type = 'pdf_page' AND status = 'classified'"
        ).fetchall()
    else:
        domain = config.get("domain")
        docs = conn.execute("""SELECT d.id FROM documents d
            JOIN document_domains dd ON d.id = dd.document_id WHERE dd.domain_path = ?""", (domain,)).fetchall()

    conn.close()

    # Track stats
    total_entities = 0
    new_entities = 0
    matched_entities = 0
    docs_processed = 0

    # Seed progress so the detail page shows the denominator immediately (issue #51).
    seed_conn = get_connection(db_path)
    seed_conn.execute(
        "UPDATE jobs SET progress = ? WHERE id = ?",
        (json.dumps({"docs_done": 0, "docs_total": len(docs), "entities_so_far": 0}), job_id))
    seed_conn.commit()
    seed_conn.close()

    for doc_row in docs:
        doc_id = doc_row[0]
        conn = get_connection(db_path)
        chunks = conn.execute("SELECT id, text FROM chunks WHERE document_id = ? ORDER BY chunk_index", (doc_id,)).fetchall()

        # Identity context for the noise filter: a doc's own path/basename and the
        # name of the collection it came from are identity, not intent. LEFT JOINed
        # so an ordinary upload (no collection) simply yields empty strings and the
        # filter degrades to the file-extension rule.
        ident = conn.execute(
            "SELECT d.title, c.name FROM documents d "
            "LEFT JOIN document_collections dc ON dc.document_id = d.id "
            "LEFT JOIN collections c ON c.id = dc.collection_id "
            "WHERE d.id = ?",
            (doc_id,),
        ).fetchone()
        doc_path = (ident[0] or "") if ident else ""
        collection_name = (ident[1] or "") if ident else ""

        # Extraction is shared with upsert_document (spec 2026-08-14 §7): one
        # implementation of "chunk text -> resolved entity_sources", so the batch path
        # and the sync path cannot drift. extract_batch keeps its own chunking /
        # classification (docs arrive already chunked + classified); only the per-chunk
        # extraction body is the shared primitive.
        res = await extract_document_entities(
            conn, relay, settings, doc_id=doc_id,
            chunks=[(c[0], c[1]) for c in chunks], spec=spec, spec_version=spec_version,
            scope=scope, job_id=job_id, doc_path=doc_path, collection_name=collection_name)
        chunk_entities = res["chunk_entities"]
        total_entities += res["total"]
        new_entities += res["new"]
        matched_entities += res["matched"]

        # Co-occurrence is a pure projection of entity_sources (db.recompute_cooccurrence).
        # The helper honours `emits_cooccurrence` on BOTH endpoints: a root or group
        # summary mentions everything beneath it, so its chunk pairs are excluded from
        # the projection entirely (a doc outside any collection has no row and emits by
        # default). Recompute the neighbourhood this document touched — invalidated edges
        # are preserved, shared-pair weights are the exact shared-chunk count.
        affected = {eid for eids in chunk_entities.values() for eid in eids}
        if affected:
            recompute_cooccurrence(conn, list(affected))

        new_status = "enriched" if scope == "domain" else "extracted"
        conn.execute("UPDATE documents SET status = ? WHERE id = ?", (new_status, doc_id))
        docs_processed += 1
        # Live progress for the detail-page bar (issue #51); WAL keeps this per-doc write cheap.
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE id = ?",
            (json.dumps({"docs_done": docs_processed, "docs_total": len(docs),
                         "entities_so_far": total_entities}), job_id))
        conn.commit()
        conn.close()
        print(f"  Extracted doc {docs_processed}/{len(docs)}: {total_entities} entities ({new_entities} new)", flush=True)

    # Store batch results summary on the job
    results_conn = get_connection(db_path)
    results_conn.execute(
        "UPDATE jobs SET result = ? WHERE id = ?",
        (json.dumps({
            "entities_found": total_entities,
            "entities_new": new_entities,
            "entities_matched": matched_entities,
            "docs_processed": docs_processed,
            "spec_version": spec_label,
        }), job_id),
    )
    results_conn.commit()
    results_conn.close()

    # Normalization only has work when NEW entities were added: batch normalization
    # re-embeds the entity set, so a trailing no-op batch (every doc already swept)
    # would otherwise pay that cost to decide nothing. The graph still has to be
    # marked dirty whenever documents were touched, though — status and co-occurrence
    # changed even with no new entities — so the two conditions are separate.
    if new_entities > 0:
        from ..normalizer import run_batch_normalization
        norm_conn = get_connection(db_path)
        try:
            norm_results = run_batch_normalization(norm_conn)
            print(f"Normalization: {norm_results}", flush=True)
        except Exception as e:
            print(f"Normalization failed: {e}", flush=True)
        finally:
            # In the `finally` on purpose: a failed normalization still leaves the
            # extracted entities behind, so the graph has changed either way and a
            # clean-path-only flag would serve a stale snapshot until the next
            # unrelated write.
            mark_graph_dirty(norm_conn)
            norm_conn.commit()
            norm_conn.close()
    elif docs_processed > 0:
        norm_conn = get_connection(db_path)
        try:
            mark_graph_dirty(norm_conn)
            norm_conn.commit()
        finally:
            norm_conn.close()
        print(f"Normalization skipped: 0 new entities ({docs_processed} docs processed)", flush=True)
    else:
        print("Normalization skipped: no-op batch (0 docs, 0 new entities)", flush=True)
