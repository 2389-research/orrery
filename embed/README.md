# Galaxy embed

A self-contained, **offline** HTML version of the Orrery galaxy, for embedding a
curated slice of the graph on an external page (e.g. the marketing site) — no
running Orrery services, no network calls.

It does **not** fork the viz. `build_embed.py` reads the canonical
`frontend/public/viz/`, applies a few standalone patches at build time (bake in the
data, add the local detail panel + double-click zoom the Next.js shell normally
provides via postMessage, drop the on-click cooccurrence fetch), and bundles the
ES modules with esbuild. Every patch asserts on a unique anchor, so a viz change
fails the build loudly instead of shipping a broken embed.

## What it produces

- `dist/orrery-galaxy.html` — one file, everything inlined, **zero network
  requests**. Good for an artifact preview, `<iframe srcdoc>`, or opening from disk.
- `dist/folder/` — `index.html` + `core/` + `renderers/` + `graph.json`. Host the
  folder and `<iframe src=".../index.html">`; ES modules resolve over HTTP and the
  data caches separately from the code. **Best fit for a static site.**

Nothing is written until the single-file build passes its self-containment check,
so a failure can't leave `dist/` half-updated.

## Build it

```bash
# 1) cut a slice from a workspace DB's materialized /graph snapshot
#    (open /graph once against that DB so graph_snapshot is populated)
python embed/build_embed.py subset \
    --db ~/orrery-data/<clone>/workspaces/default/orrery.db \
    --out embed/sample/graph.json \
    --max-entities 300

# 2) bundle the real viz + that payload (needs esbuild via npx, once)
python embed/build_embed.py build --graph embed/sample/graph.json --out embed/dist
```

`embed/sample/graph.json` is committed, so step 2 works without the (private) source DB.

### `subset` flags

| Flag | Effect |
|---|---|
| `--max-entities N` | Cap entities, keeping those best connected **within the slice** (co-occurrence via shared slice documents). `0` = no cap. |
| `--repo-depth root\|group\|all` | codesum depth for the product repos. **`root`** (default) = one summary doc per repo. `group` = module level. `all` = every file. |
| `--no-collections` | Drop the product-repo layer entirely. |

`--repo-depth` is the load-bearing choice. The 20 product repos hold 2,667 docs and
2,305 of those are per-file leaves; `all` pulls in **~17k entities** — every
implementation detail in every codebase. `root` is 20 docs / ~1.8k entities before
capping.

## The slice

The slice is a **subject** — "2389, its blogs, its products" — not a single source,
so it spans the three places that subject lives:

1. the website's `content/posts` + `content/products` pages
2. the repos for those products
3. the obsidian docs written about those products

The product list is **derived from the site's own `content/products/*` pages**, so
the sample tracks the site rather than a hardcoded list. Repos are matched on exact
basename; products with no match are printed as a warning rather than silently
dropped (a repo synced as `coven-app`/`coven-gateway` won't match `coven`). Vault
docs are matched by title on a word-boundary regex, so short slugs (`ish`, `mux`,
`jeff`) don't hit inside unrelated words.

Change `WEBSITE_SILOS` / `WEBSITE_PREDICATE` at the top of `build_embed.py` to point
at a different corpus.

**Current sample** (`--max-entities 300`): 178 docs (115 website + 20 repo roots + 43
vault) → **300 entities · 20 product repos · 246 domains**, 2.7 MB single file /
**180 KB gzipped**.

## Magnitudes are rescaled to the slice

Counts and sizes are recomputed against the slice, not inherited from the full
graph. This matters: unrescaled, a domain reported "1121 documents" inside a
178-document payload, and `state.js` sizes domain radius off that count — so the
artifact would render the whole graph's geometry while presenting itself as a slice.

- domain `document_count` → slice docs in that domain
- entity `degree` → slice documents the entity appears in
- collection `degree` → slice **entities** belonging to that repo (not docs: at
  `root` depth every repo has exactly one doc, which would flatten every repo
  marker to the same size)
- domain trade routes are **recomputed from the kept entities** (weight = how many
  share both domains), rather than inheriting full-graph weights. Inherited, 97% of
  the graph's routes survived and were two thirds of the payload bytes.

Output is deterministic: identical DB + flags produce a byte-identical
`graph.json` regardless of `PYTHONHASHSEED`.

## Interaction (works fully offline)

- pan / zoom / hover tooltip
- **layer bars** L0 domains · L1 + repos · L2 + entities · auto
- **click** a node → left detail panel (entity / domain / repository)
- **double-click** → fly-and-zoom into a star, repo, or domain

## Known limits

- **Entity↔entity edges are not baked in.** The app fetches
  `/entities/{id}/cooccurrences` on click; offline that's disabled, so
  click-to-light-a-neighborhood is inert. The domain trade routes *are* present.
- **Drill-in and search are shell-side.** `star.html` / `collection.html` and the
  app's search bar fetch their own APIs, so the export is galaxy-level only.
- **It's a point-in-time snapshot** — refreshing means re-running both steps.
- `taxonomy` entries can reference a `parent_path` outside the payload. Harmless
  today because the renderer is flat and has no `parent_path` readers; a future
  hierarchy walk would need the ancestors kept.
- Dark theme only.
