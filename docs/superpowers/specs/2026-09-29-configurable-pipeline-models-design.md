# Configurable pipeline models — design

Date: 2026-09-29
Status: proposed

## Problem

Orrery is being used primarily as an agentic interface (feedback from Dylan). A user
can set two model knobs today — `CLASSIFICATION_MODEL` and `EXTRACTION_MODEL` — and
almost the entire pipeline already flows through them and the `ANTHROPIC_BACKEND`
relay. But a few LLM call sites escaped the tiers and are hardwired, and one of them
bakes in local `gemma4`. The consequence: a deployer who points the two knobs at a
remote provider (e.g. LunaRoute / DeepSeek) still ends up with local-model behavior in
those spots, and — more importantly for Dylan — cannot fully escape the large local
memory footprint of running `gemma4` locally.

Goal: make **every** LLM call inherit one of the two configurable tiers, so setting the
two knobs (plus the backend) is sufficient to run the whole pipeline on a remote
provider and load **no local LLM at all**.

## Scope

**In scope (deploy-time / global):**
- Route the escaped LLM call sites into the existing two tiers.
- Remove the baked-in `gemma4` default from the normalization judge.
- A user-facing guidance doc on choosing models.

**Explicitly out of scope (named follow-ups):**
- **Per-user / per-org / per-noosphere** model config. Deploy-time/global only for now;
  richer selection belongs in future onboarding/tutorials.
- **Configurable / remote embeddings.** Text (`all-MiniLM-L6-v2`, 384-dim) and image
  (SigLIP) embeddings remain local torch models. A partial provider abstraction already
  exists (`orchestrator/src/services/embedding.py::embed_texts`, Vertex-768 / ST-384
  fallback); routing the remaining hardcoded sites (e.g. `search/retrieval.py`) through
  it is a modest code lift, BUT any model swap changes the vector space and requires
  **re-embedding the whole corpus + rebuilding the faiss index** (query-time and stored
  vectors must match). That data migration — not the abstraction — is the real cost, so
  it is its own project.
- **LunaRoute as a first-class backend/router.** Today a router that speaks the
  gateway/OpenAI-compat or Anthropic-compat protocol is usable via `ANTHROPIC_BACKEND=gateway`
  + `GATEWAY_URL`. A dedicated LunaRoute backend is deferred pending Dylan's setup details.

## Background: how model selection works today

Two tiers, both resolved through the relay → `ANTHROPIC_BACKEND` (gateway | bedrock | ollama):

- `CLASSIFICATION_MODEL` — **capable tier.** classify, simmer judge/generator/reflect,
  subdomain discovery, judge board, PDF page vision.
- `EXTRACTION_MODEL` — **small/fast tier.** per-chunk extraction, clerk, spec eval,
  commentary.

Repo/PDF/ccvault/tracker ingest and domain-refinement simmer already read these two
settings — they are **not** gaps.

### The escapees (the actual work)

1. **Query expansion** — `orchestrator/src/pipeline/search/expansion.py` hardcodes
   `model="claude-haiku-4-5"`. Not configurable.
2. **Normalization judge** — `worker/src/jobs/normalization_judge.py` (+ config) uses a
   bespoke scheme: `normalization_judge_prefer_local=True`,
   `normalization_judge_local_model="gemma4:26b"`, cloud fallback
   `normalization_judge_model` (→ `extraction_model` when empty). This is the one place
   `gemma4` is baked in, and `prefer_local=True` also does a wasted Ollama probe on
   pure-remote deploys.

## Design

### 1. Tiers (unchanged)

`CLASSIFICATION_MODEL` and `EXTRACTION_MODEL` remain the only two LLM knobs. No rename,
no aliases (avoid churn / breaking existing deploys). Their tier meaning is documented
(see the guidance doc).

### 2. Route the escapees into the tiers

- **Query expansion:** use `settings.extraction_model` (small tier) instead of the
  hardcoded Haiku. Query expansion is a light rewrite task; the small tier is the right
  default and it now follows whatever the deployer configured.
- **Normalization judge:** default the judge model to `settings.extraction_model`, and
  flip `normalization_judge_prefer_local` default **True → False**. The local knobs
  (`normalization_judge_prefer_local`, `normalization_judge_local_model`,
  `normalization_judge_model`) stay for anyone who wants the "run this one on local
  Ollama to save cost/tokens" optimization — but it is now **opt-in**. With it off, a
  remote deploy uses the small tier and never probes Ollama.

Net effect: with `CLASSIFICATION_MODEL` + `EXTRACTION_MODEL` pointed at a remote
provider and `ANTHROPIC_BACKEND=gateway`, **no local LLM is loaded**. The remaining
local footprint is the embedding models (MiniLM always; SigLIP only when images are
used) + torch + faiss — roughly a few hundred MB to ~1 GB, versus the tens of GB a
local `gemma4` requires.

### 3. Guidance doc — `docs/model-configuration.md`

- The two knobs and what each tier drives (capable vs small), and `ANTHROPIC_BACKEND`
  (gateway | bedrock | ollama).
- A "what to pick" table:
  - **Capable tier:** Claude Sonnet / Opus, or a strong open/remote model.
  - **Small tier:** Claude Haiku, or a small local/remote model (gemma4-small, DeepSeek).
  - Note: **the whole pipeline is very usable on small local models** — the current
    default runs on `gemma4` — so the small tier can be genuinely small.
- The opt-in local-normalization knob, for running just that idle task on a local model.
- Memory note: remote LLM ⇒ no local LLM loaded; the remaining floor is embeddings +
  torch + faiss (SigLIP only with images).
- A short "future" line: per-user/org config, remote embeddings, and LunaRoute routing.

### 4. Testing

- Query expansion passes `settings.extraction_model` (assert the model argument), and
  changing `EXTRACTION_MODEL` changes what it uses.
- Normalization-judge resolution: with `prefer_local=False` (the new default) and/or no
  Ollama, the judge resolves to the small tier (extend `worker/tests/test_config_judge.py`
  / the judge-resolve test); the local path still works when explicitly enabled.

## Risks / non-goals

- **Behavior change for existing Ollama deploys:** flipping `prefer_local` to `False`
  means an all-in-one Ollama deploy that *relied* on the normalization judge auto-using
  local gemma4 must now set `NORMALIZATION_JUDGE_PREFER_LOCAL=1` (or set the backend to
  ollama, which makes the small tier local anyway). Called out in the doc + release note.
- Not touching embeddings, per-user config, or LunaRoute (all named above).
