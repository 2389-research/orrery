# PDF Ingestion (Page-by-Page Chain) — Design

**Status:** Design approved (2026-09-16), pending spec review
**Goal:** Ingest a PDF as an ordered chain of page-documents, where each page carries both a
text representation (pypdf) and a vision representation (rasterize → vision LLM description),
so slide decks and paginated documents land in the graph with their per-page text *and* visual
content — not as one flattened text blob.

---

## 1. Motivation

Today a PDF ingests through `_ingest_document` as **one document**: `extract_text_from_pdf`
(pypdf) concatenates every page into a single `content` string, which is then chunked by size.
This works for short, born-digital prose PDFs (verified: the Grainger recaps ingested cleanly),
but it has two structural gaps:

1. **No visual content.** pypdf pulls only the text layer. A slide deck's diagrams, architecture
   figures, screenshots, and spatial layout — often the *point* of the deck — are dropped. There
   is no OCR/vision fallback for scanned or image-only pages (extraction just raises).
2. **No page structure.** Pages are flattened and re-chunked by character count, so page
   boundaries, page numbers, and reading order are lost.

A PDF's topology is **linear** — an ordered sequence of pages. The right model is a **chain of
page-nodes**, matching the artifact, mirroring the existing `ingest_tracker_runs` trajectory and
`ingest_repo` collection patterns. Orrery already has every primitive needed (collections,
document membership, the image/vision pipeline, SigLIP cross-modal embedding); this feature wires
them together for PDFs with **no schema change**.

## 2. Scope

**In scope**
- A new async endpoint `POST /ingest/pdf` (two-phase, `202 Accepted`), mirroring `POST /ingest/repo`.
- A worker job `ingest_pdf` that, per page, produces a text pass and (by policy) a vision pass,
  creates one ordered page-document per page under a `kind='pdf'` collection, embeds the page
  image for cross-modal search, classifies the PDF, and enqueues phase-2 `extract_batch`.
- A pure rasterize/text-extract module (`pipeline/pdf_pages.py`) built on `pypdfium2`.
- A `vision_mode` request flag: `always` (default) | `fallback` | `off`.

**Out of scope (non-goals for this PR)**
- Changing the existing inline flat-PDF behavior in `/ingest` (upload) or `/ingest/directory`.
  Those keep their current single-document PDF path untouched. Routing them into the new flow is a
  deliberate follow-up.
- Per-source entity provenance (which entities came from text vs. vision) — see §8, Approach B.
- OCR beyond what the vision LLM provides.
- Section/chapter (`role='group'`) hierarchy for very long PDFs. The flat page chain is the model;
  display-side collapsing of long chains is a viz concern, not this PR.
- `.pptx` ingestion (no parser installed; separate feature).

## 3. Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Structure | PDF = **one `collections` row** (`kind='pdf'`); pages = **ordered member documents** | Matches "chain of nested docs"; linear topology; no schema change. |
| Page node model | **Approach A** — one page-doc, content = page text **+** vision description, **single** extraction pass | Simplest; reuses `extract_batch` unchanged; text & vision "linked" by sharing the node. |
| Vision policy | **`always` (default), configurable** via `vision_mode: always\|fallback\|off` | Richest graph by default; `fallback` for cost-sensitive/scanned; `off` for text-only. |
| Entry point | **New `POST /ingest/pdf`**, two-phase async | Vision = many LLM calls → must be a worker job; keeps existing PDF path untouched; clean boundary. |
| Rasterizer | **`pypdfium2`** | Self-contained wheel (bundles pdfium), **no system deps/apt**, permissive license, in-container. |
| Ordering | `page_number` in `documents.metadata`; membership via `document_collections` | Order is derivable and viz-renderable; `collection_edges` `chain_next` reserved for chaining *PDFs into a series* later. |

## 4. Architecture

Two-phase, identical shape to `ingest_repo`:

```
POST /ingest/pdf {path, vision_mode?, provenance_kind?}
  └─ orchestrator (inline, no model work):
       - validate path is a file, magic-byte %PDF
       - reuse workspace general spec (seed general_text if absent)
       - create collections row (kind='pdf', root_path=file)
       - enqueue worker job 'ingest_pdf'
       - return 202 {job_id, collection_id}

worker job 'ingest_pdf' (Phase 1):
  - rasterize_pdf(bytes) -> [page PNG];  pdf_page_texts(bytes) -> [str]  (pypdfium2 + pypdf)
  - for each page N:
       text_N   = page text
       run_vision = (vision_mode == 'always')
                    or (vision_mode == 'fallback' and text_N is thin/empty)
       desc_N   = vision LLM description of page PNG  (if run_vision)  else ""
       content  = join(text_N, desc_N)               # Approach A: single blob
       create page document:
         - content, source_path=f"{file}#page={N}", metadata={"page": N},
           content_type='pdf_page'
         - join to collection via document_collections (role='leaf', parent_path=<pdf root>)
         - store page PNG artifact; SigLIP-embed -> chunks.image_embedding
  - classify the PDF once on a synthesized summary (like repo classifies on its summary)
  - enqueue Phase 2: extract_batch (scope = this collection's docs)

extract_batch (Phase 2, EXISTING, unchanged):
  - entity extraction per page-doc via the general/domain spec
  - co-occurrence recomputed, scoped to the PDF collection silo
```

## 5. Components & Interfaces

### 5.1 `orchestrator/src/pipeline/pdf_pages.py` (new, pure)
- `rasterize_pdf(file_bytes: bytes, dpi: int = 150) -> list[bytes]` — returns per-page PNG bytes via
  `pypdfium2`. No I/O beyond the in-memory buffer; unit-testable on a fixture.
- `pdf_page_texts(file_bytes: bytes) -> list[str]` — per-page text via `pypdf` (`page.extract_text()`),
  same length/order as `rasterize_pdf`. Aligns page index across both.
- Constant `THIN_TEXT_CHARS = 40` — below this a page's text is "thin" (drives `fallback`).
- `_check_magic(b"%PDF")` reused from `file_extractor` (or duplicated locally) for early rejection.

### 5.2 `orchestrator/src/routes/ingest.py` (extend)
- `PdfIngestRequest(BaseModel)`: `path: str`, `name: str | None`, `vision_mode: str = "always"`,
  `provenance_kind: str | None` — mirrors `RepoIngestRequest`.
- `POST /ingest/pdf` (`status_code=202`): validates path/magic, get-or-409 on collection name,
  reuses/seeds the general spec, creates the `kind='pdf'` collection, enqueues `ingest_pdf`, returns
  `{job_id, collection_id}`. Reuses `ingest_repo`'s conflict/rollback handling verbatim in shape.
- `vision_mode` validated to the enum `{always, fallback, off}` (422 otherwise).

### 5.3 `worker/src/jobs/ingest_pdf.py` (new)
- Mirrors `ingest_repo.py`: reads job config (`root_path`, `collection_id`, `collection_name`,
  `spec_id`, `vision_mode`), does Phase-1 work, enqueues `extract_batch` (scope over the collection).
- Vision reuse: the page PNG goes through the **existing** image path — `image_to_base64` /
  `make_image_content_block` + a `classify_image`-style relay vision call — to produce `desc_N`.
  On Ollama this uses the configured `CLASSIFICATION_MODEL` via the native `/api/chat` `think:false`
  path (same as `_ingest_image`).
- SigLIP embedding reuse: `embed_image` (fallback `embed_image_text`) → `chunks.image_embedding`,
  identical to `_ingest_image`.
- Page-doc creation uses the store's document + `document_collections` helpers; page order is
  `metadata.page`.
- Register `ingest_pdf` in the worker dispatcher (`worker/src/main.py`) alongside `ingest_repo`.

### 5.4 Dependencies
- Add `pypdfium2` to `orchestrator/pyproject.toml` and `worker/pyproject.toml`. Rebuild both images
  (self-contained wheel; no apt/system packages).

## 6. Data Model (no schema change)

- **Collection:** `collections` row, `kind='pdf'`, `root_path`=source file, `name`=PDF name (unique
  `path`). Renders in the galaxy as one node, like a repo/tracker collection.
- **Pages:** one `documents` row per page, `content_type='pdf_page'`, `source_path=file.pdf#page=N`,
  `metadata={"page": N}`, joined via `document_collections` (`role='leaf'`, `parent_path`=pdf root,
  `emits_cooccurrence` default). Ordered by `metadata.page`.
- **Image:** page PNG stored as the document artifact; `chunks.image_embedding` populated (SigLIP)
  for cross-modal search — exactly like an ingested image file.
- **Chain:** realized as the ordered set of member documents. `collection_edges` (`chain_next`) is
  **not** used within a PDF (it is collection→collection); it stays available for chaining separate
  PDFs into a series in a future feature.
- **Silo / provenance:** the collection is the silo; `provenance_kind` flow-defaults to
  `neutral_summary` (overridable on the request), matching repo/tracker.

## 7. Error Handling

- **Not a file / not a PDF:** `400`; magic-byte check rejects non-`%PDF` before any work.
- **Duplicate ingest (same collection name):** `409` with the existing collection id, mirroring
  `/ingest/repo` (get-or-409 + UNIQUE-constraint fallback + rollback on job-create failure).
- **Page with no extractable text:** if `vision_mode` runs vision, the page still gets a doc from
  the description. If `vision_mode=off` and text is empty, the page is **skipped with a logged
  warning** (no empty-content doc).
- **Vision call fails on a page:** log and continue with text-only for that page (never fail the
  whole job on one page), mirroring `_ingest_image`'s try/except around description.
- **Corrupt/rasterize failure:** the job fails with a clear message; the collection row is left for
  inspection/reingest (same as repo behavior).
- **Idempotency:** re-ingesting the same file re-conflicts at the collection name (409). (Content-
  hash dedup on page docs is inherited from the document layer.)

## 8. Alternatives Considered

- **Approach B (per-source provenance):** one page-doc, **two labeled extraction passes**
  (`extraction_pass='pdf_text'` and `'pdf_vision'`) so text-derived vs vision-derived entities are
  distinguishable. Truer to "two representations," but runs extract twice per page and needs the
  phase-2 path to support two passes per doc. **Deferred** — a clean follow-up on top of Approach A.
- **Page-as-collection chain (`collection_edges` `chain_next`), exact tracker mirror:** each page a
  separate collection linked by `chain_next`. Gives literal traversable trajectory edges, but N
  collections per PDF and no single "PDF" node without a synthetic parent. **Rejected** — over-
  structured for a linear artifact; the ordered-member-docs model is simpler and keeps one PDF node.
- **Length-adaptive structure (flat vs section-hierarchy):** **Rejected** — a PDF is uniformly a
  chain regardless of length; per-page cost is linear and identical; long-chain density is a
  display concern (collapse behind the collection node), not a modeling one.
- **Rasterize via poppler/`pdftoppm` (apt) or host pre-render:** viable, but adds a system dependency
  (poppler) to the image or an out-of-band host step. `pypdfium2` keeps it a single in-container pip
  wheel with no system packages.

## 9. Testing

All DB-backed tests use **file-backed SQLite** (`tmp_path`), never `:memory:`. The
`classifier.py`/`db.py` mirror tests still run natively.

- **`pipeline/pdf_pages.py` unit tests** (pure, no LLM): on a **small committed fixture PDF**
  (2–3 pages), assert `rasterize_pdf` returns N valid PNGs (magic `\x89PNG`), `pdf_page_texts`
  returns N strings in order, and the thin-text threshold classifies a text-light page correctly.
- **`worker` test for `ingest_pdf`** with the relay/vision **mocked**: assert N page-docs created,
  each with `content_type='pdf_page'`, `metadata.page` ordered `0..N-1`, all members of the
  `kind='pdf'` collection; assert a phase-2 `extract_batch` job is enqueued; assert `vision_mode=off`
  path creates no vision description and `always` calls the mocked vision fn once per page.
- **Endpoint test:** `POST /ingest/pdf` returns `202 {job_id, collection_id}`, creates the
  `kind='pdf'` collection, rejects a non-PDF path (`400`), and 409s on duplicate name.
- **Manual acceptance:** ingest the two Grainger decks (`agent-skills-2389.pdf`,
  `how-i-work-with-agents-synthwave.pdf`) into the local ollama workspace; verify page chain,
  vision descriptions present, entities extracted, and cross-modal search returns page hits.

## 10. File Change Summary

| File | Change |
|------|--------|
| `orchestrator/src/pipeline/pdf_pages.py` | **new** — `rasterize_pdf`, `pdf_page_texts`, thresholds |
| `orchestrator/src/routes/ingest.py` | **extend** — `PdfIngestRequest`, `POST /ingest/pdf` |
| `orchestrator/src/models.py` | **extend** — `PdfIngestRequest` (if models live there) |
| `worker/src/jobs/ingest_pdf.py` | **new** — Phase-1 job (mirror `ingest_repo`) |
| `worker/src/main.py` | **extend** — dispatch `ingest_pdf` |
| `orchestrator/pyproject.toml`, `worker/pyproject.toml` | **extend** — add `pypdfium2` |
| `orchestrator/tests/…`, `worker/tests/…` | **new** — unit + job + endpoint tests, fixture PDF |

## 11. Open Questions

None blocking. Confirm during planning: the exact rasterize DPI (default 150) and the `THIN_TEXT_CHARS`
threshold (default 40) — both are easily tunable constants, not structural.
