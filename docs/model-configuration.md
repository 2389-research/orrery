# Model configuration

Orrery runs its whole LLM pipeline through **two model tiers** and one **backend**. Set
these and every step — ingest, classification, extraction, simmer, search query
expansion, normalization judge, commentary, judge board — uses them. There is no
per-task model to configure and nothing is hardwired to a specific model.

## The three knobs

| Env var | What it is | Default |
|---|---|---|
| `ANTHROPIC_BACKEND` | Where calls go: `gateway` (Anthropic API or an OpenAI/Anthropic-compatible proxy at `GATEWAY_URL`), `bedrock` (AWS), or `ollama` (local, `OLLAMA_URL`) | `gateway` |
| `CLASSIFICATION_MODEL` | **Capable tier** — reasoning-heavy work | `claude-sonnet-4-6` |
| `EXTRACTION_MODEL` | **Small/fast tier** — high-volume, lighter work | `claude-haiku-4-5` |

### What each tier drives

- **Capable (`CLASSIFICATION_MODEL`):** document/repo classification, the simmer
  judge / generator / reflect stages, subdomain discovery, the judge board, PDF page
  vision descriptions.
- **Small (`EXTRACTION_MODEL`):** per-chunk entity extraction, the simmer clerk, spec
  evaluation, **search query expansion**, the **normalization judge**, and Magos Lex
  commentary.

## What to pick

The tiers are a quality/cost split — put a stronger model on the capable tier and a
cheaper/faster one on the small tier. Both resolve through whatever `ANTHROPIC_BACKEND`
points at, so the model *names* are provider-specific.

| | Capable tier | Small tier |
|---|---|---|
| Anthropic / gateway | Claude Sonnet or Opus | Claude Haiku |
| Local (ollama) | `gemma4:26b` | `gemma4:e4b` (or another small gemma) |
| A proxy/router (e.g. an OpenAI-compatible gateway) | a strong model (DeepSeek-R1, etc.) | a small/fast model |

**The whole pipeline is very usable on small local models** — the default local setup
runs entirely on the gemma4 family — so the small tier can be genuinely small. Choose
based on the quality you want from classification/simmer (capable) versus how much
high-volume extraction/search/commentary you'll do (small).

## Memory footprint

If both tiers point at a **remote** provider (`ANTHROPIC_BACKEND=gateway` or `bedrock`),
Orrery loads **no local LLM at all** — the multi-GB cost of running a local model like
`gemma4:26b` disappears. What remains local is only the embedding models
(`all-MiniLM-L6-v2` for text; SigLIP for images, loaded **only** when you ingest/search
images) plus torch and the faiss index — a much smaller floor.

## Running one idle task on a local model (opt-in)

The **normalization judge** is low-priority background work. If you run a local Ollama
alongside a remote pipeline and want that one task to use the local model (to save
tokens), set:

```
NORMALIZATION_JUDGE_PREFER_LOCAL=1
NORMALIZATION_JUDGE_LOCAL_MODEL=gemma4:26b   # optional; this is the default local model
```

By default (`prefer_local` off) the judge uses `EXTRACTION_MODEL` like everything else
and never probes Ollama.

> **Upgrade note:** the default for `NORMALIZATION_JUDGE_PREFER_LOCAL` changed from on to
> off. An all-in-one Ollama deployment that relied on the judge auto-using a local model
> should either set `NORMALIZATION_JUDGE_PREFER_LOCAL=1`, or simply run with
> `ANTHROPIC_BACKEND=ollama` (which makes the small tier local anyway).

## Not configurable (yet)

- **Embedding models** (text MiniLM, image SigLIP) are local and fixed. They can be
  swapped with modest refactoring, but any change alters the vector space and requires
  re-embedding the whole corpus + rebuilding the index — a separate effort.
- **Per-user / per-organization** model selection. Configuration is deploy-time/global
  for now; per-account selection is future work (onboarding/tutorials).
