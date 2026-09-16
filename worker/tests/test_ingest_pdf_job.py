# ABOUTME: Tests for the Phase 1 ingest_pdf worker job — one page-document per PDF page
# ABOUTME: (text + vision description + SigLIP image embedding), chained under a kind='pdf'
# ABOUTME: collection, then Phase 2 (extract_batch, scope='pdf_page') enqueue.

import asyncio
import json
import uuid
from pathlib import Path

import numpy as np

from src.db import get_connection

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample.pdf")


class _StubRelay:
    """Stand-in for orrery_relay.Relay: from_settings must not need live creds.
    Never called — describe_page/classify_document are monkeypatched."""

    @classmethod
    def from_settings(cls, settings):
        return cls()


def _seed(db_path, *, root_path=FIXTURE):
    """Seed a kind='pdf' collection + general spec + a queued ingest_pdf job."""
    collection_id = str(uuid.uuid4())
    spec_id = str(uuid.uuid4())
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO collections (id, name, path, root_path, kind) VALUES (?, ?, ?, ?, 'pdf')",
        (collection_id, "sample.pdf", "sample-pdf", root_path),
    )
    conn.execute(
        "INSERT INTO specs (id, domain_path, version, spec_content) VALUES (?, NULL, 1, 'spec body')",
        (spec_id,),
    )
    conn.commit()
    conn.close()

    job = {
        "id": str(uuid.uuid4()),
        "type": "ingest_pdf",
        "config": json.dumps(
            {
                "root_path": root_path,
                "collection_id": collection_id,
                "collection_name": "sample.pdf",
                "spec_id": spec_id,
            }
        ),
    }
    # Seed the queued ingest_pdf job row so the job's own `UPDATE jobs SET result`
    # lands (in the real flow the runner picks an existing row).
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'ingest_pdf', ?, 'running', ?)",
        (job["id"], collection_id, job["config"]),
    )
    conn.commit()
    conn.close()
    return job, collection_id, spec_id


def _patch(monkeypatch, tmp_path, *, describe=None, embed=None):
    """Monkeypatch the module so no LLM / network / model load happens, and PNG
    artifacts land under tmp_path instead of /data."""
    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path / "docs"))

    import src.jobs.ingest_pdf as mod

    monkeypatch.setattr(mod, "Relay", _StubRelay)

    if describe is None:
        async def describe(relay, model, png_path):
            return f"description of {Path(png_path).name}"
    monkeypatch.setattr(mod, "describe_page", describe)

    if embed is None:
        embed = lambda p: np.zeros(768, dtype="float32")  # noqa: E731
    monkeypatch.setattr(mod, "embed_image", embed)

    async def fake_classify(relay, title, excerpt, existing_taxonomy, model):
        return {"primary_domain": "x/y", "confidence": 1.0}
    monkeypatch.setattr(mod, "classify_document", fake_classify)
    return mod


def _run(job, db_path):
    from src.jobs.ingest_pdf import run_ingest_pdf
    asyncio.run(run_ingest_pdf(job, db_path))


def test_creates_two_page_docs_and_enqueues_phase2(test_db, tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    job, collection_id, spec_id = _seed(test_db)

    _run(job, test_db)

    conn = get_connection(test_db)
    try:
        docs = conn.execute(
            "SELECT id, content_type, metadata FROM documents WHERE content_type = 'pdf_page' "
            "ORDER BY json_extract(metadata, '$.page')"
        ).fetchall()
        assert len(docs) == 2
        assert [d["content_type"] for d in docs] == ["pdf_page", "pdf_page"]
        assert [json.loads(d["metadata"])["page"] for d in docs] == [0, 1]

        # all members of the kind='pdf' collection
        for d in docs:
            m = conn.execute(
                "SELECT 1 FROM document_collections WHERE document_id = ? AND collection_id = ?",
                (d["id"], collection_id),
            ).fetchone()
            assert m is not None

        # both pages have an image embedding (vision_mode defaults to 'always')
        with_emb = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE image_embedding IS NOT NULL"
        ).fetchone()[0]
        assert with_emb == 2

        # Phase 2 enqueued with scope='pdf_page'
        ej = conn.execute("SELECT config FROM jobs WHERE type = 'extract_batch'").fetchone()
        assert ej is not None
        cfg = json.loads(ej["config"])
        assert cfg["scope"] == "pdf_page"
        assert cfg["spec_id"] == spec_id
    finally:
        conn.close()


def test_vision_off_writes_text_docs_but_no_embeddings(test_db, tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    job, collection_id, spec_id = _seed(test_db)
    # flip vision_mode to 'off' in the job config
    cfg = json.loads(job["config"])
    cfg["vision_mode"] = "off"
    job["config"] = json.dumps(cfg)

    _run(job, test_db)

    conn = get_connection(test_db)
    try:
        docs = conn.execute(
            "SELECT id FROM documents WHERE content_type = 'pdf_page'"
        ).fetchall()
        assert len(docs) == 2  # text-only docs still created
        with_emb = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE image_embedding IS NOT NULL"
        ).fetchone()[0]
        assert with_emb == 0
    finally:
        conn.close()


def test_vision_exception_on_one_page_still_yields_two_docs(test_db, tmp_path, monkeypatch):
    async def flaky_describe(relay, model, png_path):
        if "page-0" in png_path:
            raise RuntimeError("boom")
        return "a description"

    _patch(monkeypatch, tmp_path, describe=flaky_describe)
    job, collection_id, spec_id = _seed(test_db)

    _run(job, test_db)  # must NOT raise

    conn = get_connection(test_db)
    try:
        docs = conn.execute(
            "SELECT id FROM documents WHERE content_type = 'pdf_page'"
        ).fetchall()
        assert len(docs) == 2  # failed page still produced (text survives)
        # the job completed and recorded a result
        j = conn.execute("SELECT result FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        assert j["result"] is not None
    finally:
        conn.close()
