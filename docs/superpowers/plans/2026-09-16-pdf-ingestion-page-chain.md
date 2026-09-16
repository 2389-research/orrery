# PDF Ingestion (Page-by-Page Chain) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /ingest/pdf` that ingests a PDF as an ordered chain of page-documents under a `kind='pdf'` collection — each page carrying a pypdf text pass, a vision-LLM description, and a SigLIP image embedding — reaching full parity with the existing image+text pipeline.

**Architecture:** Two-phase, mirroring `ingest_repo`. The orchestrator route validates + creates the collection + enqueues an `ingest_pdf` worker job (no model work inline). The worker rasterizes each page (`pypdfium2`), pulls text (`pypdf`), writes a page-PNG artifact, describes it via the relay vision path, SigLIP-embeds it, and writes one `content_type='pdf_page'` document per page (member of the collection, `metadata.page=N`, `status='classified'`) via raw SQL; then classifies the PDF once and enqueues phase-2 `extract_batch` with a new `pdf_page` scope. The two orchestrator-only image helpers (`image_prep.py`, `image_embedding.py`) are byte-mirrored into the worker, exactly as `classifier.py`/`silo.py` already are.

**Tech Stack:** Python 3.12, FastAPI (orchestrator), background worker, SQLite (WAL, file-backed in tests), `pypdfium2` (rasterize), `pypdf` (text), SigLIP via `transformers`/`torch` (already present), `orrery-relay` (LLM calls).

**Spec:** `docs/superpowers/specs/2026-09-16-pdf-ingestion-page-chain-design.md`
**Branch:** `feat/ingest-pdf-page-chain` (already created)

---

## File Structure

**New files**
- `worker/src/pdf_pages.py` — pure rasterize + per-page text + thin-text threshold + `%PDF` magic check. One responsibility: turn PDF bytes into per-page (png_bytes, text) pairs. No DB, no LLM.
- `worker/src/image_prep.py` — **byte-identical mirror** of `orchestrator/src/pipeline/image_prep.py`.
- `worker/src/image_embedding.py` — **byte-identical mirror** of `orchestrator/src/pipeline/image_embedding.py`.
- `worker/src/jobs/ingest_pdf.py` — the phase-1 worker job (`run_ingest_pdf`).
- `worker/tests/fixtures/sample.pdf` — a committed 2-page fixture PDF with known text.
- `worker/tests/test_pdf_pages.py` — unit tests for `pdf_pages.py`.
- `worker/tests/test_ingest_pdf_job.py` — job test (relay vision + `embed_image` mocked).
- `orchestrator/tests/test_ingest_pdf_route.py` — endpoint test.

**Modified files**
- `worker/pyproject.toml` — add `pypdfium2`, `pypdf`.
- `worker/src/main.py:114-149` — add an `ingest_pdf` branch to `handle_job`.
- `worker/src/jobs/extract_batch.py` — add a `pdf_page` scope branch (after `session_intent`).
- `orchestrator/src/models.py:82` — add `PdfIngestRequest` (after `RepoIngestRequest`).
- `orchestrator/src/routes/ingest.py` — add `POST /ingest/pdf` (after the `ingest_repo` route ~L494).
- `orchestrator/tests/test_schema_mirror.py` — add two byte-identical mirror tests.

**Reference patterns (read before implementing):**
- Two-phase route: `orchestrator/src/routes/ingest.py:425-494` (`ingest_repo`).
- Phase-1 job + raw-SQL doc writes + enqueue: `worker/src/jobs/ingest_repo.py:165-207, 265-393`.
- Mirror enforcement: `orchestrator/tests/test_schema_mirror.py` (`test_classifier_is_mirrored`).
- Vision call in the worker: `worker/src/jobs/extract_batch_image.py:69-77`.
- Worker test harness: `worker/tests/conftest.py` (`test_db` fixture = file-backed SQLite via `tmp_path`).

---

## Task 1: Worker dependencies

**Files:**
- Modify: `worker/pyproject.toml`

- [ ] **Step 1: Add the two deps**

In `worker/pyproject.toml`, add to the `dependencies` array (keep alphabetical/grouped as the file does):
```toml
    "pypdfium2>=4.30.0",
    "Pillow>=10.0.0",
```
**Do NOT add `pypdf`.** `import pypdf` triggers a `cryptography` import whose native binding SIGILLs (exit 132) in Docker Desktop's ARM VM on Apple Silicon (cryptography 50.0.1; same root cause as the local `simmer_domain` crash). `pypdfium2` does both rasterize AND text extraction and pulls no cryptography, so we use it for both. `Pillow` is needed by pypdfium2's `to_pil()` and by the mirrored `image_prep`/`image_embedding` (the worker currently lacks PIL).

- [ ] **Step 2: Rebuild the worker image and verify the imports resolve**

```bash
cd /Users/michaelsugimura/Documents/GitHub/Noospheric-Orrery
docker compose -f docker-compose.yml -f docker-compose.ollama.yml build worker \
  || docker-compose -f docker-compose.yml -f docker-compose.ollama.yml build worker
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d worker \
  || docker-compose -f docker-compose.yml -f docker-compose.ollama.yml up -d worker
docker exec noospheric-orrery-worker-1 /app/worker/.venv/bin/python -c "import pypdfium2, PIL; print('ok', pypdfium2.__version__)"
```
Expected: `ok <version>` (no ImportError). Do NOT verify `import pypdf` — it SIGILLs (see above) and is not a dependency.

- [ ] **Step 3: Commit**
```bash
git add worker/pyproject.toml
git commit -m "build(worker): add pypdfium2 + pypdf for PDF ingestion"
```

---

## Task 2: Mirror the two image helpers into the worker

The worker cannot import orchestrator packages, so `image_prep.py`/`image_embedding.py` are byte-mirrored (same mechanism as `classifier.py`). We enforce it with a test FIRST (TDD).

**Files:**
- Modify: `orchestrator/tests/test_schema_mirror.py`
- Create: `worker/src/image_prep.py`, `worker/src/image_embedding.py`

- [ ] **Step 1: Write the failing mirror tests**

Read `orchestrator/tests/test_schema_mirror.py`'s `test_classifier_is_mirrored` for the exact byte-comparison helper it uses (it compares the two files below their ABOUTME header). Add, following that exact pattern:
```python
def test_image_prep_is_mirrored():
    orch = _ROOT / "orchestrator" / "src" / "pipeline" / "image_prep.py"
    work = _ROOT / "worker" / "src" / "image_prep.py"
    assert _body(orch) == _body(work), "image_prep.py drifted between orchestrator and worker"

def test_image_embedding_is_mirrored():
    orch = _ROOT / "orchestrator" / "src" / "pipeline" / "image_embedding.py"
    work = _ROOT / "worker" / "src" / "image_embedding.py"
    assert _body(orch) == _body(work), "image_embedding.py drifted between orchestrator and worker"
```
(Use whatever the file's existing helper is named — reuse it, do not invent `_body` if the file calls it something else. `_ROOT` already exists in that file.)

- [ ] **Step 2: Run the tests to verify they fail**
```bash
cd orchestrator && python -m pytest tests/test_schema_mirror.py::test_image_prep_is_mirrored tests/test_schema_mirror.py::test_image_embedding_is_mirrored -v
```
Expected: FAIL (worker files do not exist → FileNotFoundError / assertion).

- [ ] **Step 3: Copy the two files verbatim into the worker**
```bash
cp orchestrator/src/pipeline/image_prep.py      worker/src/image_prep.py
cp orchestrator/src/pipeline/image_embedding.py worker/src/image_embedding.py
```
Do NOT edit them — byte-identical is the point.

- [ ] **Step 4: Run the tests to verify they pass**
```bash
cd orchestrator && python -m pytest tests/test_schema_mirror.py -v
```
Expected: PASS (all mirror tests, including the two new ones). Run **natively** (not in Docker) — the schema-mirror suite is native per CLAUDE.md.

- [ ] **Step 5: Commit**
```bash
git add orchestrator/tests/test_schema_mirror.py worker/src/image_prep.py worker/src/image_embedding.py
git commit -m "feat(worker): mirror image_prep + image_embedding (SigLIP parity); enforce in mirror test"
```

---

## Task 3: `worker/src/pdf_pages.py` — rasterize + per-page text

**Files:**
- Create: `worker/tests/fixtures/sample.pdf`, `worker/tests/test_pdf_pages.py`, `worker/src/pdf_pages.py`

- [ ] **Step 1: Generate the fixture PDF (one-off, committed)**

Create a 2-page PDF with known, extractable text using `fpdf2` (pure-python; install only to generate — it is NOT a runtime/test dep):
```bash
python -m pip install --quiet fpdf2
mkdir -p worker/tests/fixtures
python - <<'PY'
from fpdf import FPDF
pdf = FPDF()
pdf.add_page(); pdf.set_font("Helvetica", size=24); pdf.cell(0, 20, "PAGE ONE ALPHA")
pdf.add_page(); pdf.set_font("Helvetica", size=24); pdf.cell(0, 20, "PAGE TWO BRAVO")
pdf.output("worker/tests/fixtures/sample.pdf")
print("wrote worker/tests/fixtures/sample.pdf")
PY
```
Verify it opens and has 2 pages:
```bash
python -c "import pypdf; print(len(pypdf.PdfReader('worker/tests/fixtures/sample.pdf').pages))"
```
Expected: `2`.

- [ ] **Step 2: Write the failing unit tests**

`worker/tests/test_pdf_pages.py`:
```python
from pathlib import Path
from src.pdf_pages import rasterize_pdf, pdf_page_texts, THIN_TEXT_CHARS, is_pdf_bytes

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"

def _bytes():
    return FIXTURE.read_bytes()

def test_rasterize_returns_one_png_per_page():
    pngs = rasterize_pdf(_bytes())
    assert len(pngs) == 2
    for p in pngs:
        assert p[:4] == b"\x89PNG"          # valid PNG magic

def test_page_texts_are_ordered_and_match_pages():
    texts = pdf_page_texts(_bytes())
    assert len(texts) == 2
    assert "ALPHA" in texts[0].upper()
    assert "BRAVO" in texts[1].upper()

def test_rasterize_and_text_agree_on_page_count():
    assert len(rasterize_pdf(_bytes())) == len(pdf_page_texts(_bytes()))

def test_is_pdf_bytes():
    assert is_pdf_bytes(_bytes()) is True
    assert is_pdf_bytes(b"not a pdf") is False

def test_thin_text_threshold_is_sane():
    assert 0 < THIN_TEXT_CHARS < 200
```

- [ ] **Step 3: Run to verify it fails**
```bash
cd worker && python -m pytest tests/test_pdf_pages.py -v
```
Expected: FAIL (module `src.pdf_pages` does not exist).

- [ ] **Step 4: Implement `worker/src/pdf_pages.py`**
```python
# ABOUTME: Pure PDF helpers for page-by-page ingestion — rasterize pages to PNG bytes
# ABOUTME: (pypdfium2) and extract per-page text (pypdf). No DB, no LLM, no I/O to disk.

from __future__ import annotations

# Below this many non-whitespace chars a page's text is "thin" (drives vision_mode='fallback').
THIN_TEXT_CHARS = 40


def is_pdf_bytes(data: bytes) -> bool:
    """True if the bytes start with the %PDF magic marker."""
    return data[:4] == b"%PDF"


def rasterize_pdf(file_bytes: bytes, dpi: int = 150) -> list[bytes]:
    """Render each page to PNG bytes, in page order. In-memory only."""
    import io
    import pypdfium2 as pdfium

    scale = dpi / 72.0  # pdfium's default user space is 72 DPI
    pdf = pdfium.PdfDocument(file_bytes)
    try:
        out: list[bytes] = []
        for page in pdf:
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            out.append(buf.getvalue())
        return out
    finally:
        pdf.close()


def pdf_page_texts(file_bytes: bytes) -> list[str]:
    """Extract per-page text via pypdfium2, same length/order as rasterize_pdf.

    Uses pypdfium2 (NOT pypdf) — pypdf pulls in cryptography, whose native binding
    SIGILLs in Docker Desktop's ARM VM on this machine.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(file_bytes)
    try:
        return [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()
```
Notes (verified against pypdfium2 5.13.0 in the worker container):
- Text: `page.get_textpage().get_text_range()` returns the full page text (born-digital pages give clean text; may contain `\r\n`).
- Render→PNG needs `Pillow`: `bitmap.to_pil().save(buf, "PNG")`. If `.to_pil()` is unavailable in a different version, fall back to `bitmap.to_numpy()` + `PIL.Image.fromarray`.

- [ ] **Step 5: Run to verify it passes**
```bash
cd worker && python -m pytest tests/test_pdf_pages.py -v
```
Expected: PASS. (Run inside the worker container if `pypdfium2` isn't in the native env: `docker exec noospheric-orrery-worker-1 sh -c 'cd /app/worker && .venv/bin/python -m pytest tests/test_pdf_pages.py -v'`.)

- [ ] **Step 6: Commit**
```bash
git add worker/src/pdf_pages.py worker/tests/test_pdf_pages.py worker/tests/fixtures/sample.pdf
git commit -m "feat(worker): pdf_pages — rasterize (pypdfium2) + per-page text (pypdf)"
```

---

## Task 4: `pdf_page` scope in `extract_batch`

Phase 2 must find the new page-docs. `extract_batch` scopes by a fixed enum; add a `pdf_page` branch mirroring `code_intent`/`session_intent`.

**Files:**
- Modify: `worker/src/jobs/extract_batch.py` (the `elif scope == "session_intent":` block ~L40-46)
- Test: `worker/tests/test_extract_batch_scope.py` (extend)

- [ ] **Step 1: Add a failing scope test**

Read `worker/tests/test_extract_batch_scope.py` for the existing scope-test shape; add a case that inserts a `content_type='pdf_page'`, `status='classified'` document and asserts `scope='pdf_page'` selects exactly it (and does not select a `code_intent` doc). Follow the file's existing helpers/fixtures.

- [ ] **Step 2: Run to verify it fails**
```bash
cd worker && python -m pytest tests/test_extract_batch_scope.py -v -k pdf_page
```
Expected: FAIL (scope not handled → selects nothing).

- [ ] **Step 3: Implement the scope branch**

After the `session_intent` branch in `run_extract_batch`, add:
```python
    elif scope == "pdf_page":
        # Phase 2 of PDF ingest — scoped by content_type so a PDF batch never sweeps
        # another collection's docs. status='classified' keeps it idempotent.
        docs = conn.execute(
            "SELECT id FROM documents WHERE content_type = 'pdf_page' AND status = 'classified'"
        ).fetchall()
```

- [ ] **Step 4: Run to verify it passes**
```bash
cd worker && python -m pytest tests/test_extract_batch_scope.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add worker/src/jobs/extract_batch.py worker/tests/test_extract_batch_scope.py
git commit -m "feat(worker): extract_batch pdf_page scope"
```

---

## Task 5: `worker/src/jobs/ingest_pdf.py` — the phase-1 job

**Files:**
- Create: `worker/src/jobs/ingest_pdf.py`, `worker/tests/test_ingest_pdf_job.py`

Read `worker/src/jobs/ingest_repo.py:165-207, 265-393` first — this job mirrors its shape (read config → per-item raw-SQL writes → classify once → enqueue extract_batch → `mark_graph_dirty` → commit). Reuse its imports (`get_connection`, `get_settings`, `Relay`, `mark_graph_dirty`, `classify_document`, `json`, `uuid`, `hashlib`, `os`, `time`).

- [ ] **Step 1: Write the failing job test**

`worker/tests/test_ingest_pdf_job.py` — use the `test_db` fixture. Seed a `kind='pdf'` collection + general spec + a queued `ingest_pdf` job whose config points at the fixture PDF, then run the job with the vision relay call and `embed_image` **mocked** (patch `src.jobs.ingest_pdf`'s relay + `embed_image`), and assert:
```python
import json, uuid, asyncio
from pathlib import Path
import pytest
from src.db import get_connection
from src.jobs.ingest_pdf import run_ingest_pdf

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample.pdf")

def _seed(db, *, vision_mode="always"):
    conn = get_connection(db)
    cid, sid, jid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO collections (id, name, path, root_path, kind) VALUES (?, 'sample', 'sample', ?, 'pdf')", (cid, FIXTURE))
    conn.execute("INSERT INTO specs (id, domain_path, version, spec_content) VALUES (?, NULL, 1, 'general spec')", (sid,))
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'ingest_pdf', ?, 'running', ?)",
                 (jid, cid, json.dumps({"root_path": FIXTURE, "collection_id": cid,
                                        "collection_name": "sample", "spec_id": sid, "vision_mode": vision_mode})))
    conn.commit(); conn.close()
    return cid, sid, jid

def test_ingest_pdf_creates_page_chain(test_db, monkeypatch):
    cid, sid, jid = _seed(test_db)
    # mock the vision describe + SigLIP embed so no model/network is needed
    import src.jobs.ingest_pdf as mod
    async def fake_describe(relay, model, png_path): return "vision desc"
    monkeypatch.setattr(mod, "describe_page", fake_describe, raising=False)
    monkeypatch.setattr(mod, "embed_image", lambda p: __import__("numpy").zeros(768, dtype="float32"))
    monkeypatch.setattr(mod, "classify_document", _fake_classify)  # returns a fixed domain
    asyncio.run(run_ingest_pdf({"id": jid, "config": _cfg(test_db, jid)}, test_db))

    conn = get_connection(test_db)
    rows = conn.execute("""SELECT d.content_type, d.metadata, dc.role FROM documents d
                           JOIN document_collections dc ON dc.document_id = d.id
                           WHERE dc.collection_id = ? ORDER BY json_extract(d.metadata,'$.page')""", (cid,)).fetchall()
    assert len(rows) == 2
    assert [r[0] for r in rows] == ["pdf_page", "pdf_page"]
    assert [json.loads(r[1])["page"] for r in rows] == [0, 1]
    # image_embedding populated on the page chunks
    embs = conn.execute("SELECT COUNT(*) FROM chunks WHERE image_embedding IS NOT NULL").fetchone()[0]
    assert embs == 2
    # phase-2 enqueued
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='extract_batch' AND status='queued'").fetchone()[0] == 1
```
Also add `test_vision_mode_off_writes_no_description_or_embedding` (assert 0 chunks with `image_embedding`, docs still created from text) and `test_one_page_vision_failure_does_not_fail_job` (make the mocked describe raise on page 1; assert 2 docs still created). Fill in the small helpers (`_cfg`, `_fake_classify`) to match the job's actual config/classify usage — keep them minimal.

- [ ] **Step 2: Run to verify it fails**
```bash
cd worker && python -m pytest tests/test_ingest_pdf_job.py -v
```
Expected: FAIL (`src.jobs.ingest_pdf` does not exist).

- [ ] **Step 3: Implement `worker/src/jobs/ingest_pdf.py`**

Structure (mirror `ingest_repo`, use raw SQL exactly like `ingest_repo.py:280-331`):
```python
# ABOUTME: Phase-1 PDF ingest — one page-document per page (text + vision description
# ABOUTME: + SigLIP image embedding), chained under a kind='pdf' collection; enqueues extract_batch.

import os, io, json, time, uuid, hashlib, asyncio
from orrery_relay import Relay
from ..config import get_settings
from ..db import get_connection, mark_graph_dirty
from ..classifier import classify_document
from ..pdf_pages import rasterize_pdf, pdf_page_texts, THIN_TEXT_CHARS, is_pdf_bytes
from ..image_prep import make_image_content_block
from ..image_embedding import embed_image, embed_image_text

_DESCRIBE_PROMPT = "Describe this page in 2-3 sentences. What is on it? Note any diagrams, tables, or figures."


async def describe_page(relay: Relay, model: str, png_path: str) -> str:
    """Vision description of one page PNG, via the worker's own relay image call."""
    resp = await relay.complete(
        model=model, max_tokens=512,
        messages=[{"role": "user", "content": [
            make_image_content_block(__import__("pathlib").Path(png_path)),
            {"type": "text", "text": _DESCRIBE_PROMPT},
        ]}],
    )
    return (resp.text or "").strip()


async def run_ingest_pdf(job: dict, db_path: str) -> None:
    settings = get_settings()
    relay = Relay.from_settings(settings)
    config = json.loads(job["config"]) if job["config"] else {}
    root_path = config["root_path"]; collection_id = config["collection_id"]
    collection_name = config["collection_name"]; spec_id = config["spec_id"]
    vision_mode = config.get("vision_mode", "always")
    started = time.monotonic()

    file_bytes = __import__("pathlib").Path(root_path).read_bytes()
    if not is_pdf_bytes(file_bytes):
        raise ValueError(f"not a PDF: {root_path}")
    pngs = rasterize_pdf(file_bytes)
    texts = pdf_page_texts(file_bytes)

    docs_dir = os.path.join(settings.documents_dir, "pdf", collection_id)
    os.makedirs(docs_dir, exist_ok=True)

    # Classify the PDF ONCE on a joined text summary (like repo classifies on its summary).
    conn = get_connection(db_path)
    try:
        taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains").fetchall()]
    finally:
        conn.close()
    summary = "\n\n".join(t for t in texts if t.strip())[:8000] or collection_name
    classification = await classify_document(
        relay=relay, title=collection_name, excerpt=f"Document: {collection_name}\n\n{summary}",
        existing_taxonomy=taxonomy, model=settings.classification_model)
    primary_dom = classification["primary_domain"]; confidence = classification.get("confidence", 1.0)

    # Build page docs OUTSIDE the write transaction (vision calls are slow; don't hold the conn).
    pages = []
    for n, (png, text_n) in enumerate(zip(pngs, texts, strict=True)):
        art_path = os.path.join(docs_dir, f"page-{n}.png")
        with open(art_path, "wb") as f:
            f.write(png)
        run_vision = vision_mode == "always" or (vision_mode == "fallback" and len(text_n.strip()) < THIN_TEXT_CHARS)
        desc, emb = "", None
        if run_vision:
            try:
                desc = await describe_page(relay, settings.classification_model, art_path)
                emb = embed_image(__import__("pathlib").Path(art_path))
                if emb is None and desc:
                    emb = embed_image_text(desc)
            except Exception as e:  # one page's vision failure must not kill the job
                print(f"[ingest_pdf] page {n} vision failed ({type(e).__name__}: {e})", flush=True)
        content = "\n\n".join(x for x in (text_n.strip(), desc) if x)
        if not content:  # vision off + empty text -> skip
            print(f"[ingest_pdf] page {n} skipped (no text, no vision)", flush=True)
            continue
        pages.append({"page": n, "content": content, "art_path": art_path, "emb": emb})

    # One write transaction (mirror ingest_repo.py:265-393 raw-SQL pattern).
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO domains (id, path, parent_path, document_count) VALUES (?, ?, ?, 0)",
                     (str(uuid.uuid4()), primary_dom, primary_dom.rsplit("/", 1)[0] if "/" in primary_dom else None))
        import numpy as np
        for pg in pages:
            doc_id = str(uuid.uuid4()); chunk_id = str(uuid.uuid4())
            content = pg["content"]; ch = hashlib.sha256(content.encode()).hexdigest()
            conn.execute("INSERT INTO documents (id, title, content, content_hash, source_path, content_type, status, metadata) "
                         "VALUES (?, ?, ?, ?, ?, 'pdf_page', 'classified', ?)",
                         (doc_id, f"{collection_name} p{pg['page']}", content, ch,
                          f"{root_path}#page={pg['page']}", json.dumps({"page": pg["page"]})))
            conn.execute("INSERT INTO chunks (id, document_id, chunk_index, text, offset, length) VALUES (?, ?, 0, ?, 0, ?)",
                         (chunk_id, doc_id, content, len(content)))
            if pg["emb"] is not None:
                conn.execute("UPDATE chunks SET image_embedding = ? WHERE id = ?",
                             (np.asarray(pg["emb"], dtype=np.float32).tobytes(), chunk_id))
            conn.execute("INSERT INTO document_collections (document_id, collection_id, parent_path, role, emits_cooccurrence) "
                         "VALUES (?, ?, ?, 'leaf', 1)", (doc_id, collection_id, root_path))
            conn.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES (?, ?, 1, ?)",
                         (doc_id, primary_dom, confidence))
            conn.execute("UPDATE domains SET document_count = document_count + 1 WHERE path = ?", (primary_dom,))
            conn.execute("UPDATE collections SET document_count = document_count + 1 WHERE id = ?", (collection_id,))
        conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'extract_batch', ?, 'queued', ?)",
                     (str(uuid.uuid4()), collection_id, json.dumps({"spec_id": spec_id, "scope": "pdf_page"})))
        result = {"collection_name": collection_name, "pages": len(pages),
                  "primary_domain": primary_dom, "elapsed_s": round(time.monotonic() - started, 1)}
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(result), job["id"]))
        mark_graph_dirty(conn)
        conn.commit()
        print(f"[ingest_pdf] {collection_name}: {len(pages)} pages — enqueued extract_batch", flush=True)
    finally:
        conn.close()
```
Verify against the real code during implementation: the exact `classify_document` return keys (`primary_domain`/`confidence`), the `documents` columns (does `documents` have a `metadata` column? — yes, `documents.metadata TEXT`; confirm the INSERT column list matches the live schema), and `settings.documents_dir`. Adjust names to match.

- [ ] **Step 4: Run to verify it passes**
```bash
docker exec noospheric-orrery-worker-1 sh -c 'cd /app/worker && .venv/bin/python -m pytest tests/test_ingest_pdf_job.py -v'
```
Expected: PASS (all three cases).

- [ ] **Step 5: Commit**
```bash
git add worker/src/jobs/ingest_pdf.py worker/tests/test_ingest_pdf_job.py
git commit -m "feat(worker): ingest_pdf phase-1 job (page chain, vision + SigLIP)"
```

---

## Task 6: Register `ingest_pdf` in the worker dispatcher

**Files:**
- Modify: `worker/src/main.py:114-149`

- [ ] **Step 1: Add the dispatch branch**

In `handle_job`, after the `ingest_ccvault` branch:
```python
    elif job["type"] == "ingest_pdf":
        from .jobs.ingest_pdf import run_ingest_pdf
        await run_ingest_pdf(job, db_path)
```

- [ ] **Step 2: Verify the worker imports cleanly**
```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d worker \
  || docker-compose -f docker-compose.yml -f docker-compose.ollama.yml up -d worker
docker exec noospheric-orrery-worker-1 /app/worker/.venv/bin/python -c "from src.main import handle_job; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**
```bash
git add worker/src/main.py
git commit -m "feat(worker): dispatch ingest_pdf jobs"
```

---

## Task 7: `POST /ingest/pdf` endpoint + `PdfIngestRequest`

**Files:**
- Modify: `orchestrator/src/models.py:82`, `orchestrator/src/routes/ingest.py`
- Create: `orchestrator/tests/test_ingest_pdf_route.py`

Read the `ingest_repo` route (`orchestrator/src/routes/ingest.py:425-494`) — copy its conflict/rollback shape.

- [ ] **Step 1: Write the failing endpoint test**

`orchestrator/tests/test_ingest_pdf_route.py` — use the orchestrator test-client fixture. **The fixture is named `test_client`** (confirm in `orchestrator/tests/conftest.py`; sibling tests `test_ingest_repo.py`/`test_ingest_ccvault_route.py` use `test_client, test_store`). Write the fixture PDF to a `tmp_path` file and POST its path:
```python
def test_ingest_pdf_creates_collection_and_enqueues(test_client, tmp_path):
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(_two_page_pdf_bytes())  # reuse the fpdf2 gen or copy the worker fixture
    r = test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "s"})
    assert r.status_code == 202
    body = r.json(); assert "job_id" in body and "collection_id" in body

def test_ingest_pdf_rejects_non_pdf(test_client, tmp_path):
    f = tmp_path / "x.pdf"; f.write_bytes(b"not a pdf")
    assert test_client.post("/ingest/pdf", json={"path": str(f), "name": "x"}).status_code == 400

def test_ingest_pdf_rejects_bad_vision_mode(test_client, tmp_path):
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(_two_page_pdf_bytes())
    assert test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "s", "vision_mode": "bogus"}).status_code == 422

def test_ingest_pdf_duplicate_name_conflicts(test_client, tmp_path):
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(_two_page_pdf_bytes())
    test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "dup"})
    assert test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "dup"}).status_code == 409
```

- [ ] **Step 2: Run to verify it fails**
```bash
cd orchestrator && python -m pytest tests/test_ingest_pdf_route.py -v
```
Expected: FAIL (route 404).

- [ ] **Step 3: Add the model** (`orchestrator/src/models.py`, after `RepoIngestRequest`):
```python
class PdfIngestRequest(BaseModel):
    """A server-side PDF to ingest as an ordered chain of page-documents."""
    path: str
    name: str
    vision_mode: str = "always"          # always | fallback | off
    provenance_kind: str | None = None
```

- [ ] **Step 4: Add the route** (`orchestrator/src/routes/ingest.py`, after the `ingest_repo` route; import `PdfIngestRequest`). Mirror `ingest_repo` exactly, plus the magic + enum checks:
```python
@router.post("/ingest/pdf", status_code=status.HTTP_202_ACCEPTED)
async def ingest_pdf(request: PdfIngestRequest, auth: AuthStore = Depends(get_auth_store)):
    p = Path(request.path)
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {request.path}")
    if p.read_bytes()[:4] != b"%PDF":
        raise HTTPException(status_code=400, detail=f"Not a PDF: {request.path}")
    if request.vision_mode not in ("always", "fallback", "off"):
        raise HTTPException(status_code=422, detail="vision_mode must be always|fallback|off")
    store = auth.store
    try:
        existing = store.collections.get_by_path(request.name)
        if existing:
            raise HTTPException(status_code=409, detail=f"Collection '{request.name}' already exists (id {existing['id']})")
        spec = store.specs.get_general()
        if spec:
            spec_id = spec.id
        else:
            spec_id = str(uuid.uuid4()); store.specs.create(spec_id, None, 1, GENERAL_TEXT_SPEC)
        collection_id = str(uuid.uuid4())
        try:
            store.collections.create(collection_id, request.name, request.name, request.path,
                                     kind="pdf", provenance_kind=request.provenance_kind)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"Collection '{request.name}' already exists")
        job_id = str(uuid.uuid4())
        try:
            store.jobs.create(job_id, "ingest_pdf", collection_id, {
                "root_path": request.path, "collection_id": collection_id,
                "collection_name": request.name, "spec_id": spec_id, "vision_mode": request.vision_mode})
        except Exception:
            store.collections.delete(collection_id); raise
    finally:
        store.close()
    return {"job_id": job_id, "collection_id": collection_id}
```
Confirm `store.collections.create(...)` accepts a `kind=` kwarg (the ccvault route uses it — `ingest.py:606`) and that `GENERAL_TEXT_SPEC` is already imported at the top of `ingest.py` (it is).

- [ ] **Step 5: Run to verify it passes**
```bash
cd orchestrator && python -m pytest tests/test_ingest_pdf_route.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**
```bash
git add orchestrator/src/models.py orchestrator/src/routes/ingest.py orchestrator/tests/test_ingest_pdf_route.py
git commit -m "feat(api): POST /ingest/pdf — create kind='pdf' collection + enqueue ingest_pdf"
```

---

## Task 8: Full-suite check + manual acceptance

- [ ] **Step 1: Run the worker + orchestrator suites**
```bash
docker exec noospheric-orrery-worker-1 sh -c 'cd /app/worker && .venv/bin/python -m pytest tests/ -q'
cd orchestrator && python -m pytest tests/ -q     # native (schema-mirror must run here)
```
Expected: all pass (no regressions; the two new mirror tests + new files green).

- [ ] **Step 2: Manual acceptance on the local ollama stack**

Stage a real deck where the container can see it, then ingest:
```bash
mkdir -p data/ingest/decks
cp "/Users/michaelsugimura/Documents/GitHub/DS-scratch/grainger_transcripts/agent-skills-2389.pdf" data/ingest/decks/
WS=<a fresh workspace id from POST /workspaces>
curl -s -X POST http://localhost:8100/ingest/pdf -H "Content-Type: application/json" -H "X-Workspace-Id: $WS" \
  -d '{"path":"/data/ingest/decks/agent-skills-2389.pdf","name":"agent-skills-deck","vision_mode":"always"}'
```
Then verify (after the worker finishes phase-1 + extract_batch):
```bash
curl -s "http://localhost:8100/documents?limit=30" -H "X-Workspace-Id: $WS" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print([x['title'] for x in (d if isinstance(d,list) else d['documents']) if 'p' in x['title']][:10])"
# expect ~8 page docs (agent-skills is 8 pages), content_type pdf_page, ordered, with entities after extract_batch
```
Confirm: page docs exist and are ordered; `chunks.image_embedding` populated (query the workspace DB); entities extracted; the collection shows `kind='pdf'`.

- [ ] **Step 3: Clean up staging + commit nothing (data/ is gitignored)**
```bash
rm -rf data/ingest/decks
```

---

## Task 9: Open PR

- [ ] **Step 1: Push and open the PR**
```bash
git push -u origin feat/ingest-pdf-page-chain
gh pr create --title "feat: page-by-page PDF ingestion (text + vision + SigLIP chain)" \
  --body "$(cat <<'EOF'
Implements the PDF page-chain ingestion feature per
docs/superpowers/specs/2026-09-16-pdf-ingestion-page-chain-design.md.

- POST /ingest/pdf (two-phase, like /ingest/repo): kind='pdf' collection of ordered page-docs.
- Each page: pypdf text + vision description + SigLIP image_embedding (full image-pipeline parity).
- image_prep.py / image_embedding.py mirrored into the worker (enforced by test_schema_mirror.py).
- pypdfium2 rasterize; extract_batch pdf_page scope. No schema change.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Dispatch a code-review agent on the PR** (per the repo's standing PR-flow: branch → PR → review → merge; the user merges).

---

## Notes for the implementer
- **TDD throughout:** every task writes the failing test first. Do not skip the "run to verify it fails" step — it proves the test exercises the new code.
- **File-backed SQLite only** in tests (`tmp_path`), never `:memory:` (see CLAUDE.md). The worker `test_db` fixture already does this.
- **Mirror discipline:** `image_prep.py`/`image_embedding.py` stay byte-identical between orchestrator and worker; the mirror test enforces it. If you must change one, change both.
- **Ollama vision:** the worker relay already routes Ollama through native `/api/chat` with `think:false`; `settings.classification_model` (gemma4:26b on the local tier) is the vision-capable model — do not switch it to `extraction_model`.
- **Don't hold the DB connection across vision calls** — the job builds page content first (slow, no conn), then writes in one short transaction (mirrors why `ingest_repo` runs codesum off the connection).
- **Job test mocking:** `run_ingest_pdf` calls `Relay.from_settings(settings)` at the top. `classify_document`/`describe_page`/`embed_image` are monkeypatched, but if `Relay.from_settings` requires live credentials at construction in the test env, patch it too (`monkeypatch.setattr(mod, "Relay", <stub with from_settings>)`). Under the default `gateway` backend it should construct without network — confirm during Step 4.
