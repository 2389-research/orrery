# Orrery

An adaptive knowledge graph pipeline. Upload documents and images, the system classifies them into domains, builds extraction specs through iterative refinement, extracts entities, and visualizes the result as an interactive galaxy map.

## What It Does

1. **Ingest** — upload text files or images (drag-and-drop, dual upload zones)
2. **Classify** — LLM assigns documents to a hierarchical domain taxonomy it builds incrementally
3. **Extract** — entities extracted immediately using built-in general specs (text + image)
4. **Simmer** — background worker iteratively refines domain-specific extraction specs
5. **Normalize** — entities deduplicated via string rules, embedding similarity, and review queue
6. **Visualize** — interactive galaxy map with UMAP-based semantic domain layout

The system is queryable from the first upload. Domain-specific richness comes later as simmering completes.

## Quick Start

```bash
git clone https://github.com/2389-research/orrery.git
cd orrery
cp .env.example .env    # edit with your credentials
docker compose up       # or: docker-compose up
```

Open http://localhost:3100 — no sign-in required.

> If you have an older standalone `docker-compose` binary (no `docker compose` v2 subcommand), use `docker-compose` in place of `docker compose` everywhere in this README. If you're upgrading from an older cloud-era checkout that ran a Firebase emulator container, add `--remove-orphans` to the first `up` to clean it up.

### Three Backend Options

Configure in `.env`:

| Backend | Config | Models | What You Need |
|---------|--------|--------|---------------|
| **AWS Bedrock** | `ANTHROPIC_BACKEND=bedrock` | Sonnet/Haiku | AWS credentials with Bedrock access |
| **Anthropic API** | `ANTHROPIC_BACKEND=gateway` | Sonnet/Haiku | Anthropic API key (`sk-ant-...`) |
| **Ollama (fully local)** | `ANTHROPIC_BACKEND=ollama` | gemma4:26b/e4b | [Ollama](https://ollama.com) installed, models pulled |

#### Fully Local Mode (Ollama)

```bash
# Install Ollama and pull models
ollama pull gemma4:26b    # 17GB — classification, judging, generation
ollama pull gemma4:e4b    # 9.6GB — extraction (follows structured prompts reliably)

# Configure .env
ANTHROPIC_BACKEND=ollama
OLLAMA_URL=http://host.docker.internal:11434
CLASSIFICATION_MODEL=gemma4:26b
EXTRACTION_MODEL=gemma4:e4b

# Launch
docker compose up
```

Zero cloud dependencies. Text and image extraction, search, simmering all work locally.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  orchestrator (FastAPI, :8100)                          │
│  Ingest → Classify → Extract → Normalize → Search       │
│  Image serving, graph data, WebSocket broadcasts        │
└──────────────────────┬──────────────────────────────────┘
                       │ shared SQLite (WAL mode)
┌──────────────────────┴──────────────────────────────────┐
│  worker (Python, background)                            │
│  Polls jobs table every 5s                              │
│  Runs simmer (text + image), extract_batch, normalize   │
│  Uses simmer-sdk with direct API agent loop (no CLI)    │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  frontend (Next.js, :3100)                              │
│  Upload / Pipeline / Entities / Orrery / Simmer detail  │
│  Multi-noosphere, image search, ImagePane               │
└─────────────────────────────────────────────────────────┘
```

All LLM calls go through `orrery-relay` (`packages/orrery-relay/`), which handles backend routing:
- **Bedrock/Gateway**: Anthropic SDK with tool_use for structured output
- **Ollama**: Native `/api/chat` endpoint — supports text, vision, and structured output

## Features

### Text Pipeline
- Upload `.txt`, `.md`, `.json`, `.csv`
- Classification into hierarchical domains
- Entity extraction with general spec (immediate) + domain specs (after simmering)
- Co-occurrence edges, domain cascade, normalization

### Image Pipeline
- Upload `.jpg`, `.png`, `.webp`, `.gif`
- Vision LLM describes and classifies images
- Entity extraction from image descriptions
- Image search (toggle in orrery view)
- ImagePane renders actual images with entity tags

### Simmering (Spec Refinement)
- **General specs**: Built-in defaults for text and image make every upload queryable immediately; `/simmer/general` can manually refine the text general spec for a corpus
- **Text domains**: 2-phase (golden set → extraction spec), board judge with 2 panelists
- **Image domains**: Single-phase per-domain recognition context layered on the static general image spec
- **API backends**: Uses simmer-sdk direct API agent loop (2x faster than CLI, no hangs)
- **Ollama**: Deterministic pipeline — pre-scan → evaluate → review → score → generate
- Pipeline page shows per-domain text/image breakdown with conditional refine buttons

### UMAP Domain Layout
- 100 anchor domains seed the UMAP space for well-distributed initial layout
- `transform()` places new domains without re-fitting (stable positions)
- `NUMBA_CPU_NAME=generic` fixes ARM Docker SIGILL (numba#10388)

### Multi-Noosphere
- Each noosphere is a fully isolated knowledge graph — its own SQLite file under `data/workspaces/{id}/`, tracked by a JSON registry at `data/workspaces/registry.json`
- Create/switch noospheres from the UI at `/settings/noospheres`, or via the `/workspaces` API (the endpoint keeps the legacy name from an earlier cloud-era design)
- API calls scope to a noosphere via the `X-Workspace-Id` header; omitting it targets the `default` noosphere

## Running Without Docker

```bash
# Orchestrator
cd orchestrator && pip install -e . && uvicorn src.main:app --reload --port 8000

# Worker (separate terminal)
cd worker && python -m src.main

# Frontend (separate terminal)
cd frontend && NEXT_PUBLIC_AUTH_MODE=noop BACKEND_URL=http://localhost:8000 npm run dev
```

## Testing

### Backend (pytest)

```bash
# Run in Docker (recommended — matches production environment)
docker run --rm \
  -v $(pwd)/orchestrator/tests:/app/orchestrator/tests \
  -v $(pwd)/orchestrator/src:/app/orchestrator/src \
  -v $(pwd)/orchestrator/specs:/app/orchestrator/specs \
  -w /app/orchestrator \
  ghcr.io/2389-research/orrery-orchestrator:latest \
  sh -c "uv pip install pytest httpx pytest-asyncio && uv run python -m pytest tests/ -v"
```

73+ tests covering: DB schema + migrations, config defaults, auth/noosphere CRUD, ingest pipeline, image pipeline, entity normalization, domain layout, search, simmer triggers, REST hygiene (201 + Location headers on resource creation).

### Frontend (Playwright + axe)

Requires the stack to be running at `localhost:3100` and `:8100` (i.e. `docker-compose up`).

```bash
cd frontend
npm install
npx playwright install chromium   # one-time
npm run test:e2e                   # headless
npm run test:e2e:ui                # Playwright UI mode (great for debugging)
```

18+ tests covering:
- **Smoke**: every top-level route renders, including a real file upload via the dropzone
- **Accessibility**: WCAG 2.1 Level A + AA (via `@axe-core/playwright`) across upload / pipeline / entities / orrery / settings — fails on any violation
- **Copy**: no raw internal identifiers (`simmer_domain`, `extract_batch`) leak into the UI, placeholders are sentence-cased, timestamps render "just now" not "0s ago"

Each test creates a throwaway noosphere via the `/workspaces` API and soft-deletes on teardown, so tests don't depend on local state.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest` | Upload a single file (text or image, multipart) |
| `POST` | `/ingest/text` | Ingest a document from raw JSON text (no file) |
| `POST` | `/ingest/repo` | Ingest a git checkout → codesum summaries + entities (async, 202) |
| `POST` | `/ingest/pdf` | Ingest a PDF as a page-chain (per-page text + image, async, 202) |
| `POST` | `/ingest/tracker-runs` | Ingest a corpus of tracker runs as collections (async, 202) |
| `POST` | `/watched-sources` | Register a vault/repo dir for ongoing incremental sync |
| `GET` | `/documents` | List documents with content_type |
| `GET` | `/domains` | Domain taxonomy with text/image counts |
| `GET` | `/entities` | Entities, filterable by type/domain/job |
| `GET` | `/search?q=...&include_images=true` | Hybrid search with optional image results |
| `GET` | `/graph` | Graph data (UMAP positions, entities, trade routes) |
| `GET` | `/images/{id}` | Serve image file |
| `POST` | `/simmer/general` | Trigger text spec simmering |
| `POST` | `/simmer/{domain_path}` | Trigger domain-specific text simmering |
| `POST` | `/simmer/{domain_path}/image` | Trigger domain-specific image simmering |
| `GET` | `/stats` | Counts (documents, entities, domains, images, active jobs) |
| `GET` | `/workspaces` | List noospheres |
| `POST` | `/workspaces` | Create a noosphere |
| `PATCH` | `/workspaces/{id}` | Rename a noosphere |
| `DELETE` | `/workspaces/{id}` | Archive a noosphere (soft delete) |
| `GET` | `/health` | Health check |

All data endpoints (ingest, documents, entities, graph, search, simmer, …) accept an optional `X-Workspace-Id: <id>` header to scope the request to a specific noosphere. Omit the header to target `default`. The `/workspaces` path is the legacy name from a cloud-era multi-tenancy design — the UI calls these "noospheres."

Full interactive docs at http://localhost:8100/docs

### Ingesting a repository

The graph is a **map over code, not a copy of it**: `POST /ingest/repo` runs codesum over a
git checkout and stores per-file/-module summaries (plus extracted entities) as a collection.

**Agents: use the `ingest_repo` MCP tool** (`ingest_repo(path, name)`) — it's the discoverable
surface and its result gives you a `job_id` to poll with `get_job_status`.

Key rule (the thing that trips people up): **`path` is a server-side directory the Orrery
services can already see** — the mounted `./data` volume (`/data/...` inside the container) or a
directory bind-mounted into both the orchestrator and worker. It is **not** a file upload and
**not** a GitHub URL. Clone/copy the checkout somewhere under `./data` first, then point at it.

```bash
# 1. Put the checkout where the containers can read it (./data is mounted at /data)
git clone --depth 1 https://github.com/you/my-repo ./data/repos/my-repo

# 2. Kick off the ingest (async → 202 with a job id + collection id)
curl -s -X POST http://localhost:8100/ingest/repo \
  -H 'Content-Type: application/json' -H 'X-Workspace-Id: <noosphere-id>' \
  -d '{"path": "/data/repos/my-repo", "name": "my-repo"}'
# → {"job_id": "…", "collection_id": "…"}

# 3. Poll until done
curl -s "http://localhost:8100/jobs" -H 'X-Workspace-Id: <noosphere-id>'
```

For **ongoing** repo sync (re-summarize on a cadence as the code changes) register a watched
source instead: `POST /watched-sources` with `type: "repo"`. See
[docs/ingesting-repos.md](docs/ingesting-repos.md) for the full guide, gotchas, and the
one-shot-vs-watched decision.

## Design Principles

1. **Extraction specs are the artifact that improves, not the code.** The pipeline stays fixed; specs evolve through simmering.
2. **Expensive work is amortized.** Classification and simmering happen once. Per-document extraction is cheap.
3. **Queryable from moment one.** Every document produces entities immediately via built-in general specs.
4. **Works offline.** Ollama backend requires zero internet after model download.
5. **Prompt quality > model quality.** A simmered spec on a small model outperforms a generic prompt on a large one.
