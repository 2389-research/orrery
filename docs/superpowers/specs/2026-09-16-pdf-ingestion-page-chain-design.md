# PDF Ingestion (Page-by-Page Chain) — Design

**Status:** Design approved (2026-09-16); revised after spec review (cross-package placement + SigLIP scope)
**Goal:** Ingest a PDF as an ordered chain of page-documents, where each page carries both a
text representation (pypdf) and a vision representation (rasterize → vision LLM description),
so slide decks and paginated documents land in the graph with their per-page text *and* visual
content — not as one flattened text blob.

---

## 1. Motivation

Today a PDF ingests through `_ingest_document` as **one document**: `extract_text_from_pdf`
(pypdf) concatenates every page into a single `content` string, chunked by size. This works for
short, born-digital prose PDFs (verified: the Grainger recaps ingested cleanly), but has two
structural gaps:

1. **No visual content.** pypdf pulls only the text layer. A slide deck's diagrams, architecture
   figures, screenshots, and spatial layout — often the *point* of the deck — are dropped, with no
   OCR/vision fallback for scanned or image-only pages (extraction just raises).
2. **No page structure.** Pages are flattened and re-chunked by character count, so page
   boundaries, page numbers, and reading order are lost.

A PDF's topology is **linear** — an ordered sequence of pages. The right model is a **chain of
page-nodes**, matching the artifact and mirroring the existing `ingest_tracker_runs` /`ingest_repo`
collection patterns. Orrery already has every primitive needed (collections, document membership,
worker-side vision via the relay); this feature wires them together for PDFs with **no schema
change**.

## 2. Scope

**In scope**
- A new async endpoint `POST /ingest/pdf` (two-phase, `202 Accepted`), mirroring `POST /ingest/repo`.
- A worker job `ingest_pdf` that, per page, produces a text pass and (by policy) a vision pass,
  creates one ordered page-document per page under a `kind='pdf'` collection, classifies the PDF,
  and enqueues phase-2 `extract_batch`.
- A pure rasterize/text-extract module **in the worker** (`worker/src/pdf_pages.py`) on `pypdfium2` + `pypdf`.
- A `vision_mode` request flag: `always` (default) | `fallback` | `off`.

**Out of scope (non-goals for this PR)**
- **SigLIP cross-modal page-image embedding** (`chunks.image_embedding`). The worker has **no
  SigLIP/`embed_image` path today** (the column exists in the schema, but nothing in `worker/src`
  populates it; the helpers live only in the orchestrator). Adding it is separable, so v1 stores the
  page PNG as an on-disk artifact and relies on the **vision description** to carry visual content
  into entities/text. Cross-modal search over PDF pages is a clean follow-up (mirror or relocate
  `image_embedding.py` into a shared/worker location; the torch/transformers deps are already in the
  worker image). See §8.
- Changing the existing inline flat-PDF behavior in `/ingest` (upload) or `/ingest/directory`.
  Those keep their current single-document PDF path untouched. Routing them into the new flow is a
  deliberate follow-up.
- Per-source entity provenance (text- vs vision-derived entities) — §8, Approach B.
- OCR beyond what the vision LLM provides.
- Section/chapter (`role='group'`) hierarchy for very long PDFs (display concern, not modeling).
- `.pptx` ingestion (no parser installed; separate feature).

## 3. Decisions (locked during brainstorming; adjusted per review)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Structure | PDF = **one `collections` row** (`kind='pdf'`); pages = **ordered member documents** | Matches "chain of nested docs"; linear topology; no schema change. |
| Page node model | **Approach A** — one page-doc, content = page text **+** vision description, **single** extraction pass | Simplest; reuses `extract_batch` unchanged; text & vision "linked" by sharing the node. |
| Vision policy | **`always` (default), configurable** via `vision_mode: always\|fallback\|off` | Richest graph by default; `fallback` for cost-sensitive/scanned; `off` for text-only. |
| Entry point | **New `POST /ingest/pdf`**, two-phase async | Vision = many LLM calls → must be a worker job; keeps existing PDF path untouched. |
| Rasterizer | **`pypdfium2`** (worker dep) | Self-contained wheel (bundles pdfium), **no system deps/apt**, permissive license. |
| Page image → graph | **Vision description now; SigLIP embedding deferred** | Worker has no SigLIP path today; the description delivers the visual content into entities. |
| Ordering | `page_number` in `documents.metadata`; membership via `document_collections`; writes via **raw SQL** | Order derivable/viz-renderable; `collection_edges` `chain_next` reserved for chaining *PDFs into a series* later. |

## 4. Cross-Package Placement (the load-bearing constraint)

This codebase forbids cross-package imports — **"neither process imports the other's package"**
(CLAUDE.md); `classifier.py`/`taxonomy.py`/`silo.py`/`db.py` are byte-mirrored precisely for this.
Because **all Phase-1 model work runs in the worker**, every new/reused piece it needs lives in the
**worker package**, and the orchestrator route does only validation + enqueue:

| Piece | Lives in | Notes |
|-------|----------|-------|
| Rasterize + per-page text (`pdf_pages.py`) | **worker** | Sole consumer is the worker job; no orchestrator caller → **no mirror needed**. |
| Vision description call | **worker** | Reuse the worker's **own** inline pattern (`base64.b64encode` + `relay.complete_structured` image content block) as in `worker/src/jobs/extract_batch_image.py:69–77` / `evaluate_image_spec.py:170–180`. **Do not** import the orchestrator's `image_prep`/`classify_image`. |
| Doc + membership + metadata writes | **worker** | Raw `conn.execute` INSERTs, mirroring `ingest_repo.py:280–303` (the store's `documents.create()` does **not** write `metadata`/membership). |
| `%PDF` magic-byte validation | **orchestrator route** | Inline byte check; the route does **not** rasterize, so it needs no PDF deps. |
| Deps | `pypdfium2` **+** `pypdf` in `worker/pyproject.toml` | Orchestrator already has `pypdf`; it needs **no** new dep (it doesn't rasterize/extract). |

## 5. Architecture & Components

Two-phase, identical shape to `ingest_repo`:

```
POST /ingest/pdf {path, vision_mode?, provenance_kind?}      (orchestrator, no model work)
  - validate path is a file; inline %PDF magic-byte check (400 otherwise)
  - validate vision_mode in {always,fallback,off} (422 otherwise)
  - get-or-409 on collection name; reuse workspace general spec (seed general_text if absent)
  - create collections row (kind='pdf', root_path=file)
  - enqueue worker job 'ingest_pdf'  (config: root_path, collection_id, collection_name,
                                      spec_id, vision_mode)
  - return 202 {job_id, collection_id}   (rollback collection if job-create fails, per ingest_repo)

worker job 'ingest_pdf'  (Phase 1)
  pages_png  = rasterize_pdf(bytes)      # worker/src/pdf_pages.py (pypdfium2)
  pages_txt  = pdf_page_texts(bytes)     # worker/src/pdf_pages.py (pypdf), same order/length
  for N, (png, text_N) in enumerate(...):
     run_vision = vision_mode=='always' or (vision_mode=='fallback' and len(text_N.strip())<THIN_TEXT_CHARS)
     desc_N = <worker inline base64(png) + relay vision call, model=CLASSIFICATION_MODEL>  if run_vision else ""
     content = join(text_N, desc_N)                          # Approach A: single blob
     write page PNG artifact under documents_dir; source_path = f"{file}#page={N}"
     raw-SQL INSERT documents(content, content_type='pdf_page', metadata={"page":N}, status=...)
     raw-SQL INSERT document_collections(document_id, collection_id, role='leaf', parent_path=<pdf root>)
  classify the PDF once on a synthesized page-text summary (like repo classifies on its summary) -> domain
  enqueue Phase 2: extract_batch (scope = this collection's docs)

extract_batch  (Phase 2, EXISTING, unchanged)
  - entity extraction per page-doc via general/domain spec; co-occurrence scoped to the PDF silo
```

### 5.1 `worker/src/pdf_pages.py` (new, pure)
- `rasterize_pdf(file_bytes: bytes, dpi: int = 150) -> list[bytes]` — per-page PNG bytes via `pypdfium2`
  (in-memory; no disk I/O). Unit-testable on a fixture.
- `pdf_page_texts(file_bytes: bytes) -> list[str]` — per-page text via `pypdf`, same length/order.
- `THIN_TEXT_CHARS = 40` — below this a page's text is "thin" (drives `fallback`).
- Local `%PDF` magic check helper (the worker can't import the orchestrator's `_check_magic`).

### 5.2 `orchestrator/src/routes/ingest.py` (extend)
- `PdfIngestRequest(BaseModel)` (in `models.py` beside `RepoIngestRequest`): `path: str`,
  `name: str | None`, `vision_mode: str = "always"`, `provenance_kind: str | None`.
- `POST /ingest/pdf` (`status_code=202`): inline `%PDF` check, `vision_mode` enum validation,
  get-or-409 on name, reuse/seed general spec, create `kind='pdf'` collection, enqueue `ingest_pdf`,
  return `{job_id, collection_id}`; reuse `ingest_repo`'s conflict/rollback shape verbatim.

### 5.3 `worker/src/jobs/ingest_pdf.py` (new)
- Mirrors `ingest_repo.py`: reads job config, does Phase-1 work, enqueues `extract_batch`.
- **Vision:** the worker's own inline base64 + `relay.complete_structured` image content block
  (pattern at `extract_batch_image.py:69–77`), `model = settings.classification_model`. On Ollama
  this rides the native `/api/chat` `think:false` path (same relay the worker already uses).
- **Writes:** page-doc, `document_collections` membership, `metadata={"page":N}`, `content_type`
  via raw `conn.execute` (mirroring `ingest_repo.py:280–303`) — **not** store helpers.
- **Page image:** written as an on-disk artifact under `documents_dir`; `source_path` = `file#page=N`.
  (No `image_embedding` write in v1 — see §2 / §8.)
- Register `ingest_pdf` in the worker dispatcher `if/elif` chain (`worker/src/main.py:115–138`),
  beside `ingest_repo`.

### 5.4 Dependencies
- Add `pypdfium2` **and** `pypdf` to `worker/pyproject.toml`. Rebuild the worker image
  (self-contained wheels; no apt/system packages). Orchestrator `pyproject.toml` is unchanged.

## 6. Data Model (no schema change)

- **Collection:** `collections` row, `kind='pdf'` (free-form tag; same mechanism as `ccvault`),
  `root_path`=source file, unique `path`=name. Renders as one galaxy node like repo/tracker.
- **Pages:** one `documents` row per page — `content_type='pdf_page'`, `content` = page text +
  vision description, `source_path=file.pdf#page=N`, `metadata={"page":N}` — joined via
  `document_collections` (`role='leaf'`, `parent_path`=pdf root, default `emits_cooccurrence`).
  Ordered by `metadata.page`. All written with raw SQL.
- **Image:** page PNG stored as an on-disk artifact. `chunks.image_embedding` is **not** populated
  in v1 (deferred SigLIP — §2/§8).
- **Chain:** the ordered set of member documents. `collection_edges` (`chain_next`) is **not** used
  within a PDF (it is collection→collection); reserved for chaining separate PDFs into a series later.
- **Silo / provenance:** the collection is the silo; `provenance_kind` flow-defaults to
  `neutral_summary` (overridable on the request), matching repo/tracker.

## 7. Error Handling

- **Not a file / not a PDF:** `400` (inline magic-byte check before any work).
- **Bad `vision_mode`:** `422`.
- **Duplicate ingest (same name):** `409` with existing collection id, mirroring `/ingest/repo`
  (get-or-409 + UNIQUE fallback + rollback on job-create failure).
- **Page with no extractable text:** if vision runs, the page still gets a doc from the description;
  if `vision_mode=off` and text is empty, the page is **skipped with a logged warning**.
- **Vision call fails on a page:** log and continue text-only for that page — never fail the whole
  job on one page (mirrors `_ingest_image`'s try/except).
- **Corrupt / rasterize failure:** job fails with a clear message; collection left for
  inspection/reingest (same as repo).

## 8. Alternatives Considered

- **SigLIP now vs deferred:** including cross-modal page-image embedding in v1 would require mirroring
  or relocating the orchestrator-only `image_embedding.py` into the worker (which has no SigLIP path
  today). Deferred to keep the PR focused and honest — the vision *description* already delivers the
  page's visual content into the graph; cross-modal *image-similarity search* is the separable extra.
- **Approach B (per-source provenance):** one page-doc, **two labeled extraction passes**
  (`pdf_text` / `pdf_vision`). Truer to "two representations," but runs extract twice per page and
  needs phase-2 to support two passes per doc. **Deferred** — clean follow-up on Approach A.
- **Page-as-collection chain (`chain_next`), exact tracker mirror:** each page its own collection.
  Literal traversable edges, but N collections/PDF and no single PDF node. **Rejected** —
  over-structured for a linear artifact.
- **Length-adaptive structure (flat vs section-hierarchy):** **Rejected** — a PDF is uniformly a
  chain; per-page cost is linear; long-chain density is a display concern.
- **Rasterize via poppler/`pdftoppm` (apt) or host pre-render:** adds a system dependency or an
  out-of-band step; `pypdfium2` keeps it a single in-container pip wheel.

## 9. Testing

DB-backed tests use **file-backed SQLite** (`tmp_path`), never `:memory:`; mirror tests run natively.

- **`worker` unit tests for `pdf_pages.py`** (pure, no LLM) on a **small committed fixture PDF**
  (2–3 pages): `rasterize_pdf` returns N valid PNGs (magic `\x89PNG`), `pdf_page_texts` returns N
  ordered strings, and `THIN_TEXT_CHARS` flags a text-light page.
- **`worker` test for `ingest_pdf`** with the relay/vision **mocked**: N page-docs created, each
  `content_type='pdf_page'`, `metadata.page` ordered `0..N-1`, all members of the `kind='pdf'`
  collection; a phase-2 `extract_batch` job enqueued; `vision_mode=off` writes no description while
  `always` calls the mocked vision fn once per page; a vision-fn exception on one page still yields
  a text-only doc and does not fail the job.
- **Orchestrator endpoint test:** `POST /ingest/pdf` returns `202 {job_id, collection_id}`, creates
  the `kind='pdf'` collection, rejects a non-PDF path (`400`), bad `vision_mode` (`422`), and 409s on
  duplicate name.
- **Manual acceptance:** ingest the two Grainger decks into the local ollama workspace; verify page
  chain, vision descriptions present, and entities extracted.

## 10. File Change Summary

| File | Change |
|------|--------|
| `worker/src/pdf_pages.py` | **new** — `rasterize_pdf` (pypdfium2), `pdf_page_texts` (pypdf), thresholds, magic check |
| `worker/src/jobs/ingest_pdf.py` | **new** — Phase-1 job (mirror `ingest_repo`; worker-inline vision; raw-SQL writes) |
| `worker/src/main.py` | **extend** — dispatch `ingest_pdf` |
| `worker/pyproject.toml` | **extend** — add `pypdfium2` + `pypdf` |
| `orchestrator/src/routes/ingest.py` | **extend** — `POST /ingest/pdf` (validate + enqueue only) |
| `orchestrator/src/models.py` | **extend** — `PdfIngestRequest` |
| `worker/tests/…` | **new** — `pdf_pages` unit tests + `ingest_pdf` job test + fixture PDF |
| `orchestrator/tests/…` | **new** — `/ingest/pdf` endpoint test |

## 11. Open Questions

None blocking. Confirm during planning: rasterize DPI (default 150) and `THIN_TEXT_CHARS` (default
40) — tunable constants, not structural.
