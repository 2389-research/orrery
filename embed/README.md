# Galaxy embed (offline CLI)

> **The app can do this now.** `GET /export/html` exports the active noosphere as a
> zip that runs offline — surfaced as **Export as HTML** in the noosphere menu. See
> `orchestrator/src/pipeline/graph_export.py`, which is the canonical implementation.
>
> This CLI remains for the two things the endpoint deliberately doesn't do: **carving
> a curated slice out of a much larger graph** (the app exports a noosphere whole),
> and the **single-file** build, which needs esbuild.

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
    --out embed/sample/graph.json

# 2) bundle the real viz + that payload (needs esbuild via npx, once)
python embed/build_embed.py build --graph embed/sample/graph.json --out embed/dist
```

`embed/sample/graph.json` is committed, so step 2 works without the (private) source DB.

### `subset` flags

| Flag | Effect |
|---|---|
| `--max-entities N` | Cap entities, keeping those best connected **within the slice**. **Default 0 (no cap)** — see "Why there is no entity cap". |
| `--per-repo-min N` | When capping, entities each repo keeps before the cap is filled globally (default 8). A flat cap starves repos. |
| `--min-route-weight N` | Drop domain trade routes below this slice weight (default 2). This is the **size** dial. |
| `--detail-neighbours N` | Co-occurring entities baked per entity for the offline panel (default 12). |
| `--repo-depth root\|group\|all` | codesum depth for the product repos. **`root`** (default) = one summary doc per repo. `group` = module level. `all` = every file. |
| `--no-collections` | Drop the product-repo layer entirely. |

`--repo-depth` is the load-bearing choice. The 20 product repos hold 2,667 docs and
2,305 of those are per-file leaves; `all` pulls in **~17k entities** — every
implementation detail in every codebase. `root` is 20 docs / ~1.8k entities.

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

**Current sample** (uncapped): 178 docs (115 website + 20 repo roots + 43 vault) →
**1,762 entities · 20 product repos · 257 domains**, 4.7 MB single file /
**390 KB gzipped**. Every repo has 5–134 entities around it.

## Why there is no entity cap

Measured, not assumed. Entities and edges cost different things:

| | framerate | payload bytes |
|---|---|---|
| entities | **yes** — 300 → 1,762 costs ~30% FPS | 19–39% |
| domain trade routes | **no** — pruning 17k → 10k changed FPS 0 | **~76%** |

Trade routes are free at rest because `drawTradeRoutes` early-returns unless a node
is hovered or pinned, so they only matter for download size (`--min-route-weight`).

The cap was dropped because the app itself renders up to 3,000 entities
(`DEFAULT_MAX_RENDER_NODES`), more than this whole slice contains — so 1,762 is
inside normal operating range rather than an extrapolation. `--max-entities` remains
for anyone targeting low-end devices; if you use it, keep `--per-repo-min`, because a
flat global cap crowds out repo-specific entities (website entities recur across 115
docs and dominate any connectivity ranking): at cap=300 eight repos fell to ≤2
entities and three rendered with **zero**.

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
- **click** a node → left detail panel (entity / domain / repository). For an
  entity: type, in-slice sources, its domains, its **connected entities**
  (clickable, so the panel is navigable) and the **documents it appears in** —
  baked from `entity_detail`, since the app gets these from
  `/entities/{id}/cooccurrences` and `entity.sources`.
- **double-click** → fly-and-zoom into a star, repo, or domain

## Known limits

- **Drill-in views are not included.** The app's sector / system / star levels are
  separate pages (`star.html`, `collection.html`) that fetch their own APIs, so the
  export is galaxy-level only: double-click flies and zooms locally instead of
  navigating into a star. Snippets (`/documents/{id}/reader`) are also not baked.
- **Drill-in and search are shell-side.** `star.html` / `collection.html` and the
  app's search bar fetch their own APIs, so the export is galaxy-level only.
- **It's a point-in-time snapshot** — refreshing means re-running both steps.
- `taxonomy` entries can reference a `parent_path` outside the payload. Harmless
  today because the renderer is flat and has no `parent_path` readers; a future
  hierarchy walk would need the ancestors kept.
- Dark theme only.
