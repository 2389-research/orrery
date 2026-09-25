# Ingesting a repository into Orrery

A quick, agent-friendly guide to getting a git repo into the graph. If you are an agent
with limited context: **use the `ingest_repo` MCP tool** and read the "Server-side path"
rule below — that is the one thing that trips people up.

## What it does

`POST /ingest/repo` (MCP: `ingest_repo`) runs **codesum** over a git checkout and stores
per-file / per-module **"code intent" summaries** — plus the entities extracted from them —
as a **collection** in the active noosphere. The graph is a **map over the code, not a copy
of it**: the source is read where it sits, and only summaries land in the DB.

It is **two-phase and asynchronous**: the call returns immediately (`202`) with a `job_id`
and `collection_id`; the worker does the (many-LLM-call) summarization + extraction in the
background. Poll with `get_job_status(job_id)` / `GET /jobs`.

## The one rule that matters: `path` is server-side

`path` is a **directory the Orrery services can already read on their own filesystem** — it is
**not** a file upload and **not** a GitHub URL.

- In Docker (the normal setup) the host `./data` is mounted at `/data`, and both the
  orchestrator and worker can read it. So the repo must live under `./data` (→ `/data/...`
  inside the container), or under another directory you bind-mount into **both** services.
- Passing a path only your shell can see (e.g. `~/code/my-repo`) → `400 Not a directory`,
  because the orchestrator container cannot see it.
- Passing a URL (`https://github.com/...`) → `400`, for the same reason.

So the flow is always: **get the checkout onto a shared path first, then point at it.**

## Agent recipe (MCP)

```
select_noosphere("<name-or-id>")            # pick where the repo lands
ingest_repo(path="/data/repos/my-repo", name="my-repo")
# → "Started repo ingest of 'my-repo' (collection …) — job …. poll with get_job_status('…')."
get_job_status("<job_id>")                   # repeat until completed
```

`name` becomes the collection's name and must be unique in the noosphere (a repeat name
returns `409` with the existing collection id — re-ingest under a different name, or go look
at what is already there).

Optional: `provenance_kind` overrides the silo's provenance (defaults to `neutral_summary`
for a git repo — an agent's/neutral code summary, distinct from a human vault note).

## REST recipe (curl)

```bash
# 1. Put the checkout where the containers can read it (./data is mounted at /data)
git clone --depth 1 https://github.com/you/my-repo ./data/repos/my-repo

# 2. Kick off the ingest
curl -s -X POST http://localhost:8100/ingest/repo \
  -H 'Content-Type: application/json' \
  -H 'X-Workspace-Id: <noosphere-id>' \
  -d '{"path": "/data/repos/my-repo", "name": "my-repo"}'
# → {"job_id": "…", "collection_id": "…"}

# 3. Poll
curl -s http://localhost:8100/jobs -H 'X-Workspace-Id: <noosphere-id>'
```

Omit `X-Workspace-Id` to target the `default` noosphere.

## One-shot vs ongoing sync

- **One-shot import** (a snapshot of the repo as it is now): `POST /ingest/repo` — the above.
- **Ongoing sync** (re-summarize as the code changes, on a cadence): register a **watched
  source** instead — `POST /watched-sources` with `type: "repo"`, `uri` = the server-side
  path. The worker then re-scans on `cadence_hours`, re-summarizing only the files whose
  git HEAD changed (unchanged HEAD short-circuits cheaply). This is the same incremental
  pipeline the Obsidian-vault sync uses.

## Related ingest endpoints

Same server-side-path + async (`202` + `job_id`) shape:

- `POST /ingest/pdf` — a PDF as a page-chain (per-page text + a SigLIP image embedding).
- `POST /ingest/tracker-runs` — a corpus of tracker runs, one run per collection.
- `POST /ingest/text` (MCP `ingest_text`) — a single document from raw text (synchronous,
  no server-side path needed since the text *is* the source).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `400 Not a directory: <path>` | Path isn't visible to the orchestrator | Move the checkout under `./data` (→ `/data/...`) or bind-mount it into both services; pass that path |
| `409 Collection '<name>' already exists` | Name already used in this noosphere | Use a different `name`, or inspect the existing collection |
| Job stuck `running` a long time | Large repo / slow (esp. local Ollama) models; big generated-data dirs | Expected for big repos; exclude non-code data dirs; on a local GPU see `docs/troubleshooting-local-dev.md` |
| Nothing happens after `202` | Worker not running | Ensure the worker service is up (`docker compose ps`) |
