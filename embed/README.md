# Galaxy embed

A self-contained, **offline** build of the real Orrery galaxy viz (`frontend/public/viz/`)
for embedding a curated slice of the graph on an external page (e.g. the marketing site) —
no running Orrery services, no network calls.

It does **not** fork the viz. `build_embed.py` reads the canonical `frontend/public/viz/`,
applies a few standalone patches at build time (bake in the data, add a local detail panel +
double-click zoom that the Next.js shell normally provides via postMessage), and bundles the
ES modules with esbuild.

## What it produces

- `dist/orrery-galaxy.html` — one file, everything inlined, **zero network requests**. Good
  for a Claude artifact preview or an `<iframe srcdoc>` / dropped-in file.
- `dist/folder/` — `index.html` + `core/` + `renderers/` + `graph.json`. Host the folder and
  `<iframe src=".../index.html">`; ES modules resolve over HTTP. Best fit for a static site.

## Build it

```bash
# 1) extract a subset payload from a workspace DB's materialized /graph snapshot.
#    (open /graph once against that DB so graph_snapshot is populated.)
python embed/build_embed.py subset \
    --db ~/orrery-data/<clone>/workspaces/default/orrery.db \
    --out embed/sample/graph.json

# 2) bundle the real viz + that payload into the embed (needs esbuild via npx, once).
python embed/build_embed.py build --graph embed/sample/graph.json --out embed/dist
```

`embed/sample/graph.json` is committed so step 2 works without the (private) source DB.

## The subset

The default slice is **2389 blogs + products**: entities extracted from the website repos'
`content/posts/` + `content/products/` docs. It keeps the **real UMAP domain layout**
(`layout.positions`) and the domains/collections those entities touch, so the slice sits
exactly where it does in the full galaxy — just fewer stars. Change `SILOS` /
`TITLE_PREDICATE` at the top of `build_embed.py` to slice a different corpus.

Current sample: 407 entities · 82 collections (repos) · 239 domains · domain trade routes +
collection edges.

## Interaction (works fully offline)

- pan / zoom / hover tooltip
- **layer bars** L0 domains · L1 + repos · L2 + entities · auto
- **click** a node → left detail panel (entity / domain / repository)
- **double-click** → fly-and-zoom into a star, repo, or domain

Neighbor lines (the app's on-click `/entities/{id}/cooccurrences` fetch) are off in the embed;
baking entity-entity edges into `graph.json` is a possible follow-up.

## Theme

Currently dark cosmic only. Light / dark-mode variants for the site are the next step.
