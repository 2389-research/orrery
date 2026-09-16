# PDF Ingestion (Page-by-Page Chain) — Design

**Status:** Design approved (2026-09-16); revised twice after spec review — (1) cross-package
placement, (2) SigLIP kept in-scope via mirroring (full image-pipeline parity per page).
**Goal:** Ingest a PDF as an ordered chain of page-documents, where **each page is ingested exactly
like an image + a text** — same vision description, same SigLIP embedding, same entity extraction —
grouped into a `kind='pdf'` collection. Slide decks and paginated docs land with their per-page
text *and* visual content, not as one flattened text blob.

---

## 1. Motivation

Today a PDF ingests through `_ingest_document` as **one document**: `extract_text_from_pdf` (pypdf)
concatenates every page into a single `content` string, chunked by size. This works for short prose
PDFs (verified: the Grainger recaps ingested cleanly), but has two structural gaps: **(1) no visual
content** — pypdf pulls only the text layer, dropping diagrams/figures/screenshots with no vision
fallback; **(2) no page structure** — pages are flattened and re-chunked by character count, so page
boundaries, numbers, and reading order are lost.

A PDF's topology is **linear** — an ordered sequence of pages — so the model is a **chain of
page-nodes**. Crucially, this is **almost entirely reuse**: orrery already ingests images (vision +
SigLIP + extraction) and texts, and already builds ordered collections (repo/tracker/ccvault). This
feature stacks those two existing capabilities for PDFs, with **no schema change**.

## 2. Scope

**In scope**
- New async endpoint `POST /ingest/pdf` (two-phase, `202 Accepted`), mirroring `POST /ingest/repo`.
- Worker job `ingest_pdf` that, per page, produces a **text pass** (pypdf) and a **vision pass**
  (vision description **+ SigLIP image embedding**), assembles **one page-document** per page under a
  `kind='pdf'` collection, classifies the PDF once, and enqueues phase-2 `extract_batch`.
- A pure rasterize/text module in the worker (`worker/src/pdf_pages.py`) on `pypdfium2` + `pypdf`.
- **Mirror** the two image helpers into the worker (`image_prep.py`, `image_embedding.py`), the same
  way `classifier.py`/`silo.py`/`taxonomy.py`/`db.py` are mirrored, so the page image path reaches
  full parity with `_ingest_image` (vision + SigLIP). `classify_image` is **already** mirrored in
  `worker/src/classifier.py`.
- A `vision_mode` request flag: `always` (default) | `fallback` | `off`.

**Out of scope (non-goals)**
- Changing the existing inline flat-PDF behavior in `/ingest` / `/ingest/directory` (kept untouched;
  routing them into this flow is a deliberate follow-up).
- Per-source entity provenance (text- vs vision-derived entities) — §8, Approach B.
- OCR beyond what the vision LLM provides.
- Section/chapter (`role='group'`) hierarchy for very long PDFs (a display concern, not modeling).
- `.pptx` ingestion (no parser installed; separate feature).
- Relocating the image helpers into a shared package instead of mirroring (mirroring matches the
  established pattern; a future consolidation of all four mirror-pairs is out of scope here).

## 3. Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Structure | PDF = **one `collections` row** (`kind='pdf'`); pages = **ordered member documents** | Matches "chain of nested docs"; linear topology; no schema change. |
| Page node model | **Approach A** — one page-doc, content = page text **+** vision description, single extraction pass, **plus** SigLIP `image_embedding` on its chunk | Full parity with an image+text; "a page is one node." |
| Image path | **Full reuse of the image pipeline** — vision description + SigLIP — via **mirrored** `image_prep`/`image_embedding` (+ already-mirrored `classify_image`) | It IS the image pipeline; mirroring is how orrery shares code across the two processes. |
| Vision policy | **`always` (default), configurable** via `vision_mode: always\|fallback\|off` | Richest graph by default; `fallback` for cost/scanned; `off` for text-only. |
| Entry point | **New `POST /ingest/pdf`**, two-phase async | Per-page vision+SigLIP = many calls → worker job, exactly like repo/tracker; existing PDF path untouched. |
| Rasterizer | **`pypdfium2`** (worker dep) | Self-contained wheel, no system deps, permissive license. |
| Ordering | `page_number` in `documents.metadata`; membership via `document_collections`; writes via **raw SQL** | Order derivable/viz-renderable; `collection_edges` `chain_next` reserved for chaining *PDFs into a series* later. |

## 4. Cross-Package Placement (the load-bearing constraint)

Cross-package imports are forbidden — **"neither process imports the other's package"** (CLAUDE.md);
`classifier.py`/`taxonomy.py`/`silo.py`/`db.py` are byte-mirrored, enforced by
`orchestrator/tests/test_schema_mirror.py`. All Phase-1 model work runs in the worker, so:

| Piece | Placement | Notes |
|-------|-----------|-------|
| `classify_image` (domain classify of a page/PDF) | **already mirrored** in `worker/src/classifier.py:191` | No action needed. |
| `image_prep.py` (`image_to_base64`, `make_image_content_block`) | **mirror** → `worker/src/image_prep.py` | 62 lines, self-contained; add a `test_schema_mirror.py` byte-identical check. |
| `image_embedding.py` (`embed_image`, `embed_image_text` — SigLIP) | **mirror** → `worker/src/image_embedding.py` | 109 lines, self-contained (lazy SigLIP via transformers); add a mirror check. transformers/torch already in the worker image. |
| Rasterize + per-page text (`pdf_pages.py`) | **worker** (new) | Sole consumer is the worker job; no orchestrator caller → no mirror. |
| Vision description call | **worker** | The `_ingest_image` "describe this image" pattern: `relay.complete(model=classification_model, image block + describe prompt)`, using the mirrored `image_prep` for the base64/resize block. |
| Doc + membership + metadata + `image_embedding` writes | **worker** | Raw `conn.execute` INSERT/UPDATE, mirroring `ingest_repo.py:280–303` (the store's `documents.create()` writes neither `metadata` nor membership nor `image_embedding`). |
| `%PDF` magic-byte validation | **orchestrator route** | Inline byte check; the route does not rasterize → no new orchestrator dep. |
| Deps | `pypdfium2` **+** `pypdf` in `worker/pyproject.toml` | Orchestrator already has `pypdf`; needs no new dep. |

## 5. Architecture & Components

Two-phase, identical shape to `ingest_repo`:

```
POST /ingest/pdf {path, vision_mode?, provenance_kind?}      (orchestrator, no model work)
  - validate file + inline %PDF magic check (400); vision_mode in {always,fallback,off} (422)
  - get-or-409 on collection name; reuse workspace general spec (seed general_text if absent)
  - create collections row (kind='pdf', root_path=file)
  - enqueue worker job 'ingest_pdf' (config: root_path, collection_id, collection_name,
                                     spec_id, vision_mode)
  - return 202 {job_id, collection_id}   (rollback collection if job-create fails, per ingest_repo)

worker job 'ingest_pdf'  (Phase 1) — each page ingested like an image + text:
  pages_png = rasterize_pdf(bytes)   # worker/src/pdf_pages.py (pypdfium2)
  pages_txt = pdf_page_texts(bytes)  # worker/src/pdf_pages.py (pypdf), same order/length
  for N, (png_bytes, text_N) in enumerate(...):
     run_vision = vision_mode=='always' or (vision_mode=='fallback' and len(text_N.strip())<THIN_TEXT_CHARS)
     write page PNG artifact to disk under documents_dir; art_path; source_path=f"{file}#page={N}"
     desc_N = <relay.complete vision "describe" call, model=classification_model,
               image block via mirrored image_prep(art_path)>  if run_vision else ""
     img_emb = embed_image(art_path)  (mirrored SigLIP; needs the file on disk;
               fallback embed_image_text(desc_N))              if run_vision else None
     raw-SQL INSERT documents(content=join(text_N,desc_N), content_type='pdf_page',
                              metadata={"page":N}, status=...)
     raw-SQL INSERT document_collections(document_id, collection_id, role='leaf', parent_path=<pdf root>)
     raw-SQL INSERT the page chunk; if img_emb is not None: UPDATE chunks SET image_embedding=? (SigLIP)
  classify the PDF once on a synthesized page-text summary (like repo classifies on its summary) -> domain
  enqueue Phase 2: extract_batch (scope = this collection's docs)

extract_batch  (Phase 2, EXISTING, unchanged)
  - entity extraction per page-doc via general/domain spec; co-occurrence scoped to the PDF silo
```

### 5.1 `worker/src/pdf_pages.py` (new, pure)
- `rasterize_pdf(file_bytes, dpi=150) -> list[bytes]` (per-page PNG via `pypdfium2`, in-memory).
- `pdf_page_texts(file_bytes) -> list[str]` (per-page text via `pypdf`, same order/length).
- `THIN_TEXT_CHARS = 40`; a local `%PDF` magic check.

### 5.2 `worker/src/image_prep.py`, `worker/src/image_embedding.py` (new — byte-identical mirrors)
- Copies of `orchestrator/src/pipeline/image_prep.py` and `.../image_embedding.py`, kept byte-
  identical below the ABOUTME header, enforced by new `test_schema_mirror.py` cases. This is the
  same mechanism as `classifier.py`/`silo.py`. No logic changes.

### 5.3 `orchestrator/src/routes/ingest.py` + `models.py` (extend)
- `PdfIngestRequest`: `path`, `name?`, `vision_mode='always'`, `provenance_kind?` (mirror `RepoIngestRequest`).
- `POST /ingest/pdf` (`202`): inline `%PDF` check, `vision_mode` enum validation, get-or-409 on name,
  reuse/seed general spec, create `kind='pdf'` collection, enqueue `ingest_pdf`; reuse `ingest_repo`'s
  conflict/rollback shape.

### 5.4 `worker/src/jobs/ingest_pdf.py` (new)
- Mirrors `ingest_repo.py`: read config, do Phase-1, enqueue `extract_batch`.
- **Vision:** the `_ingest_image` describe pattern — `relay.complete(model=settings.classification_model,
  [image block via mirrored image_prep, "Describe this image…" prompt])`. **Note:** use
  `classification_model` (the vision-capable/generation model on the local tier), not
  `extraction_model` — do not "correct" it to match the `extract_batch_image` snippet.
- **SigLIP:** `embed_image(png)` (mirrored), fallback `embed_image_text(desc)`; write to
  `chunks.image_embedding`, exactly as `_ingest_image` does.
- **Writes:** page-doc + `document_collections` + `metadata` + `content_type` + chunk + embedding via
  raw `conn.execute` (mirroring `ingest_repo.py:280–303`).
- Register `ingest_pdf` in the worker dispatcher `handle_job` if/elif chain (`worker/src/main.py`,
  ~L114 onward, alongside `ingest_repo`/`ingest_ccvault`).

### 5.5 Dependencies
- Add `pypdfium2` + `pypdf` to `worker/pyproject.toml`; rebuild the worker image. Orchestrator
  `pyproject.toml` unchanged. SigLIP's deps are already present in the worker: `torch` is declared
  directly and `transformers` arrives **transitively via `sentence-transformers`** (same as the
  orchestrator today) — do **not** add an explicit `transformers` dep assuming it's missing.
  Note: `google/siglip-base-patch16-224` weights (~400MB) download on first call, identical to
  existing image ingest — bake/cache them if the worker must run fully offline (pre-existing behavior,
  not introduced here).

## 6. Data Model (no schema change)

- **Collection:** `collections` row, `kind='pdf'` (free-form tag, like `ccvault`), `root_path`=file,
  unique `path`=name. One galaxy node like repo/tracker.
- **Pages:** one `documents` row per page — `content_type='pdf_page'`, `content` = page text + vision
  description, `source_path=file.pdf#page=N`, `metadata={"page":N}` — joined via
  `document_collections` (`role='leaf'`, `parent_path`=pdf root). Ordered by `metadata.page`.
- **Image:** page PNG stored as an on-disk artifact; `chunks.image_embedding` **populated via SigLIP**
  (cross-modal search), identical to an ingested image file. All writes raw SQL.
- **Chain:** the ordered set of member documents. `collection_edges` (`chain_next`) is not used within
  a PDF (it is collection→collection); reserved for chaining separate PDFs into a series later.
- **Silo / provenance:** the collection is the silo; `provenance_kind` flow-defaults to
  `neutral_summary` (overridable), matching repo/tracker.

## 7. Error Handling

- Not a file / not a PDF → `400` (inline magic check). Bad `vision_mode` → `422`.
- Duplicate name → `409` with existing collection id (get-or-409 + UNIQUE fallback + rollback), per repo.
- Page with no text: if vision runs, the page still gets a doc from the description; if `vision_mode=off`
  and text empty → page skipped with a logged warning.
- Vision or SigLIP failure on a page → log and continue text-only for that page (never fail the whole
  job on one page), mirroring `_ingest_image`'s try/except around description and embedding.
- Corrupt / rasterize failure → job fails with a clear message; collection left for reingest (per repo).

## 8. Alternatives Considered

- **Approach B (per-source provenance):** one page-doc, **two labeled extraction passes**
  (`pdf_text`/`pdf_vision`). Truer to "two representations," but runs extract twice per page and needs
  phase-2 to support two passes per doc. **Deferred** — clean follow-up on Approach A.
- **Defer SigLIP:** considered and **rejected** — the worker lacked a SigLIP path, but mirroring
  `image_embedding.py` (109 lines, self-contained) is the established pattern and gives full image
  parity; deferring would drop cross-modal page search for no real saving.
- **Relocate image helpers to a shared package** (instead of mirroring): cleaner long-term, but a
  cross-cutting refactor of all mirror-pairs; **out of scope** — mirror to match current convention.
- **Page-as-collection chain (`chain_next`), exact tracker mirror:** N collections/PDF, no single PDF
  node. **Rejected** — over-structured for a linear artifact.
- **Length-adaptive structure:** **Rejected** — a PDF is uniformly a chain; per-page cost is linear;
  long-chain density is a display concern.
- **Rasterize via poppler (apt)/host pre-render:** adds a system dep or out-of-band step; `pypdfium2`
  keeps it one in-container wheel.

## 9. Testing

File-backed SQLite (`tmp_path`), never `:memory:`; mirror tests run natively.

- **`test_schema_mirror.py` additions:** byte-identical checks for `image_prep.py` and
  `image_embedding.py` (orchestrator vs worker), matching the existing `classifier.py` case.
- **`worker` unit tests for `pdf_pages.py`** (pure, no LLM) on a **small committed fixture PDF**:
  `rasterize_pdf` returns N valid PNGs (`\x89PNG`), `pdf_page_texts` returns N ordered strings,
  `THIN_TEXT_CHARS` flags a text-light page.
- **`worker` test for `ingest_pdf`** with relay/vision **and `embed_image` mocked**: N page-docs
  created, each `content_type='pdf_page'`, `metadata.page` ordered `0..N-1`, all members of the
  `kind='pdf'` collection; `chunks.image_embedding` set on vision pages; a phase-2 `extract_batch`
  enqueued; `vision_mode=off` writes no description/embedding while `always` calls the mocked vision +
  embed once per page; a vision/embed exception on one page still yields a text-only doc and doesn't
  fail the job.
- **Orchestrator endpoint test:** `POST /ingest/pdf` → `202 {job_id, collection_id}`, creates the
  `kind='pdf'` collection, rejects non-PDF (`400`), bad `vision_mode` (`422`), duplicate name (`409`).
- **Manual acceptance:** ingest the two Grainger decks into the local ollama workspace; verify page
  chain, vision descriptions, `image_embedding` populated, and entities extracted.

## 10. File Change Summary

| File | Change |
|------|--------|
| `worker/src/pdf_pages.py` | **new** — rasterize (pypdfium2) + page text (pypdf) + thresholds |
| `worker/src/image_prep.py` | **new (mirror)** of orchestrator `image_prep.py` |
| `worker/src/image_embedding.py` | **new (mirror)** of orchestrator `image_embedding.py` (SigLIP) |
| `worker/src/jobs/ingest_pdf.py` | **new** — Phase-1 job (worker vision + SigLIP + raw-SQL writes) |
| `worker/src/main.py` | **extend** — dispatch `ingest_pdf` |
| `worker/pyproject.toml` | **extend** — add `pypdfium2` + `pypdf` |
| `orchestrator/src/routes/ingest.py` | **extend** — `POST /ingest/pdf` (validate + enqueue) |
| `orchestrator/src/models.py` | **extend** — `PdfIngestRequest` |
| `orchestrator/tests/test_schema_mirror.py` | **extend** — mirror checks for the two image modules |
| `worker/tests/…`, `orchestrator/tests/…` | **new** — `pdf_pages` + `ingest_pdf` + endpoint tests, fixture PDF |

## 11. Open Questions

None blocking. Confirm during planning: rasterize DPI (default 150) and `THIN_TEXT_CHARS` (default 40)
— tunable constants, not structural.
