#!/usr/bin/env python3
# ABOUTME: Build a self-contained, offline, embeddable galaxy from the REAL Orrery viz.
# ABOUTME: Two steps: (1) `subset` a workspace's /graph snapshot down to a slice of entities
# ABOUTME: (keeping the real UMAP domain layout); (2) `build` bundles frontend/public/viz + the
# ABOUTME: subset into one HTML file that runs with zero network calls (for a blog/site iframe).
"""
Why this exists
---------------
`frontend/public/viz/` is the live galaxy renderer. In the app it fetches `GET /graph`
(a materialized v5 snapshot: taxonomy + real UMAP `layout.positions` + entity/collection
`nodes` placed by domain/collection membership + domain trade-route `edges`) and talks to a
Next.js shell via postMessage for its detail panel.

For an embed (e.g. a page on the marketing site) we want the SAME renderer, but:
  * no running Orrery services (data baked in),
  * a small, curated slice of entities rather than the whole graph,
  * the detail panel + double-click working standalone (the shell isn't there).

So we do NOT reinvent the viz. We reuse it verbatim and only:
  * subset the snapshot to the chosen entities, keeping the real domain layout;
  * repoint the data load at the baked payload;
  * add a local detail panel + local double-click zoom (mirroring the shell behaviour);
  * bundle the ES modules into one file with esbuild.

Usage
-----
  # 1) produce a subset payload from a workspace DB's stored snapshot
  python embed/build_embed.py subset \
      --db ~/orrery-data/clone2389/workspaces/default/orrery.db \
      --out embed/sample/graph.json

  # 2) bundle the real viz + a payload into a single offline HTML (+ a folder build)
  python embed/build_embed.py build \
      --graph embed/sample/graph.json --out embed/dist

`build` needs esbuild once, fetched via `npx esbuild` (network at build time only).
The subset filter is configured by SILOS + a title-prefix predicate below; change those to
slice a different corpus.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VIZ = REPO / "frontend" / "public" / "viz"

# ── subset config: the SUBJECT of the sample, not a single source ───────────────
# The slice is "2389, its blogs, and its products", which spans THREE sources:
#   1. the website itself (content/posts + content/products)
#   2. the repos for those products
#   3. the obsidian docs written about those products
# The product list is not hardcoded — it is derived from the website's own
# content/products/* pages, so the sample tracks the site.
#
# Repos are included at codesum ROOT depth by default (one repo-summary doc each).
# That matters: those 20 repos hold 2,667 docs and 2,305 of them are per-file
# leaves, which drag in ~16k entities — every implementation detail in the
# codebase. Root depth represents each product's repo without that explosion.
WEBSITE_SILOS = (
    "f39e1b88-4a3f-410a-8c39-46adf2b2627c",  # 2389.ai
    "d9e3ccf1-5284-45ac-880f-990a26b04ba9",  # 2389.dev
)
WEBSITE_PREDICATE = "(title LIKE 'content/posts/%' OR title LIKE 'content/products/%')"


def subset(db_path: Path, out_path: Path, max_entities: int = 0,
           with_collections: bool = True, repo_depth: str = "root") -> None:
    import sqlite3

    import os
    conn = sqlite3.connect(str(db_path))
    wph = ",".join("?" * len(WEBSITE_SILOS))

    # (1) the website's own blog + product pages
    web_docs = [r[0] for r in conn.execute(
        f"SELECT id FROM documents WHERE silo_id IN ({wph}) AND {WEBSITE_PREDICATE}", WEBSITE_SILOS)]

    # product slugs, derived from the site's content/products/* pages
    slugs = set()
    for (t,) in conn.execute(
            f"SELECT title FROM documents WHERE silo_id IN ({wph}) AND title LIKE 'content/products/%'",
            WEBSITE_SILOS):
        m = re.match(r"content/products/([^/]+)", t or "")
        if m:
            sl = m.group(1).replace(".md", "")
            if sl and not sl.startswith("_"):
                slugs.add(sl)

    sources = list(conn.execute("SELECT id, type, uri FROM watched_sources"))
    slugs_low = {x.lower() for x in slugs}
    repo_ids = [i for (i, t, u) in sources
                if t == "repo" and os.path.basename((u or "").rstrip("/")).lower() in slugs_low]
    vault_ids = [i for (i, t, u) in sources if t == "vault"]

    # (2) those products' repos, at the requested codesum depth
    repo_docs = []
    if repo_ids:
        rph = ",".join("?" * len(repo_ids))
        if repo_depth == "all":
            repo_docs = [r[0] for r in conn.execute(
                f"SELECT id FROM documents WHERE silo_id IN ({rph})", repo_ids)]
        else:
            roles = ("root",) if repo_depth == "root" else ("root", "group")
            rl = ",".join("?" * len(roles))
            repo_docs = [r[0] for r in conn.execute(
                f"""SELECT d.id FROM documents d
                     JOIN document_collections dc ON dc.document_id = d.id
                     WHERE d.silo_id IN ({rph}) AND dc.role IN ({rl})""", repo_ids + list(roles))]

    # (3) obsidian docs written about those products (word-boundary match, so short
    #     slugs like 'ish' / 'mux' / 'jeff' don't match inside unrelated words)
    pats = [re.compile(r"(?<![a-z0-9])" + re.escape(x).replace(r"\-", "[- ]?") + r"(?![a-z0-9])", re.I)
            for x in slugs]
    vault_docs = []
    if vault_ids:
        vph = ",".join("?" * len(vault_ids))
        vault_docs = [r[0] for r in conn.execute(
            f"SELECT id, title FROM documents WHERE silo_id IN ({vph})", vault_ids)
            if any(pt.search(r[1] or "") for pt in pats)]

    doc_ids = list({*web_docs, *repo_docs, *vault_docs})
    print(f"slice: {len(web_docs)} website + {len(repo_docs)} repo({repo_depth}) + "
          f"{len(vault_docs)} vault = {len(doc_ids)} docs over {len(slugs)} products / {len(repo_ids)} repos")

    ids = set()
    for k in range(0, len(doc_ids), 400):
        b = doc_ids[k:k + 400]
        dp = ",".join("?" * len(b))
        ids |= {r[0] for r in conn.execute(
            f"""SELECT DISTINCT es.entity_id FROM entity_sources es
                 JOIN entities e ON e.id = es.entity_id
                 WHERE es.document_id IN ({dp}) AND e.invalid_at IS NULL""", b)}
    row = conn.execute("SELECT payload FROM graph_snapshot WHERE payload IS NOT NULL LIMIT 1").fetchone()
    if not row:
        sys.exit("No materialized graph_snapshot in that DB — open /graph once to build it.")
    p = json.loads(row[0])
    ni = p["node_index"]
    render_by_id = {n["id"]: n for n in p["nodes"]}
    all_coll = {n["id"]: n for n in p["nodes"] if n["type"] == "collection"}
    posmap = p["layout"]["positions"]

    stars = [render_by_id.get(i) or ni.get(i) for i in ids if (i in render_by_id or i in ni)]

    # Optional cap: keep the best-connected entities by degree WITHIN the slice, so a
    # smaller sample still reads as a graph instead of scattered dust.
    if max_entities and len(stars) > max_entities:
        kept = {n["id"] for n in stars}
        deg = {n["id"]: 0 for n in stars}
        for e in p["edges"]:
            if e.get("scope") == "collection":
                if e["source"] in deg and e["target"] in kept: deg[e["source"]] += 1
                if e["target"] in deg and e["source"] in kept: deg[e["target"]] += 1
        stars.sort(key=lambda n: (-deg.get(n["id"], 0), -(n.get("degree") or 0)))
        stars = stars[:max_entities]

    # Collections (repos) the stars belong to — positions live in layout.positions by id.
    # Collections = the PRODUCTS' repos. Matched on the collection node's own label
    # so it stays aligned with the site's product list rather than pulling in every
    # repo the slice's entities happen to appear in.
    coll_ids = set()
    if with_collections:
        for cid, node in all_coll.items():
            lab = (node.get("label") or node.get("path") or "").lower()
            if lab in slugs_low and cid in posmap:
                coll_ids.add(cid)
    colls = [all_coll[c] for c in coll_ids]

    # Domains referenced (weight>0) by any kept star or collection — keep their REAL UMAP
    # positions so the slice sits exactly where it does in the full galaxy.
    kept_dom = {
        m["id"]
        for n in stars + colls
        for m in n.get("memberships", [])
        if m["container_type"] == "domain" and m["weight"] > 0
    }
    # Drop memberships pointing outside the kept sets (dangling refs).
    for n in stars + colls:
        n["memberships"] = [
            m
            for m in n.get("memberships", [])
            if (m["container_type"] == "domain" and m["id"] in kept_dom)
            or (m["container_type"] == "collection" and m["id"] in coll_ids)
        ]

    nodes = stars + colls
    nodeset = {n["id"] for n in nodes}
    sub = {
        "meta": {**p["meta"], "subset": "custom"},
        "taxonomy": [t for t in p["taxonomy"] if t["path"] in kept_dom],
        "nodes": nodes,
        "node_index": {n["id"]: n for n in nodes},
        "edges": [
            e
            for e in p["edges"]
            if (e["scope"] == "domain" and e["source"] in kept_dom and e["target"] in kept_dom)
            or (e["scope"] == "collection" and e["source"] in nodeset and e["target"] in nodeset)
        ],
        "layout": {
            **p["layout"],
            "positions": {k: v for k, v in posmap.items() if k in kept_dom or k in coll_ids},
            "palette": {k: v for k, v in p["layout"]["palette"].items() if k in kept_dom},
        },
    }
    sub["meta"]["counts"] = {"nodes_included": len(nodes), "nodes_total": len(nodes)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sub, separators=(",", ":")))
    dom_e = sum(1 for e in sub["edges"] if e["scope"] == "domain")
    coll_e = sum(1 for e in sub["edges"] if e["scope"] == "collection")
    print(f"subset → {out_path}: {len(stars)} entities, {len(colls)} collections, "
          f"{len(kept_dom)} domains, {dom_e} trade routes, {coll_e} collection edges")


# ── build: patch the real viz for standalone use, bundle, inline the payload ─────

# The three source edits below turn the app viz into a standalone embed. They are exact
# string patches against frontend/public/viz/index.html; each asserts, so a viz change
# that moves them fails loudly here instead of shipping a broken embed.
_PANEL_CSS = """
#detail { position: fixed; top: 60px; left: 14px; z-index: 25; width: 300px; max-height: calc(100vh - 90px);
  overflow-y: auto; display: none; background: rgba(6,13,34,0.94); border: 1px solid rgba(100,180,255,0.22);
  border-radius: 8px; padding: 16px 18px; backdrop-filter: blur(8px); }
#detail.show { display: block; }
#detail .d-close { position: absolute; top: 10px; right: 12px; cursor: pointer; color: rgba(140,200,255,0.6); font-size: 14px; }
#detail .d-close:hover { color: #eef0f8; }
#detail .d-kind { font-size: 9px; letter-spacing: 0.18em; text-transform: uppercase; color: rgba(140,200,255,0.6); }
#detail .d-name { color: #eef0f8; font-size: 17px; font-weight: bold; margin: 6px 0 4px; line-height: 1.25; word-break: break-word; }
#detail .d-meta { color: rgba(140,200,255,0.7); font-size: 11px; line-height: 1.7; }
#detail .d-h { font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase; color: rgba(140,200,255,0.5); margin: 14px 0 6px; }
#detail .d-dom { display: flex; align-items: center; gap: 7px; font-size: 12px; color: #c8d2ea; padding: 2px 0; }
#detail .d-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; box-shadow: 0 0 6px currentColor; }
"""

_PANEL_HELPER = """
// ---- local detail panel (standalone embed; mirrors the shell's galaxy-panel) ----
const _detail = document.getElementById('detail');
const _dbody = document.getElementById('d-body');
document.getElementById('d-close').addEventListener('click', () => { _detail.classList.remove('show'); state.pinnedId = null; });
function _esc(x){ return String(x==null?'':x).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function _domColor(path){ try { return (state.domainColors && state.domainColors[path]) || '#7aa0d8'; } catch(_) { return '#7aa0d8'; } }
function renderPanel(payload){
  const d = payload.data || {}; let html = '';
  if (payload.nodeType === 'entity') {
    html += `<div class="d-kind">${_esc(d.entityType||'entity')}</div><div class="d-name">${_esc(d.name)}</div>`;
    html += `<div class="d-meta">${Number(d.source_count||0)} source${Number(d.source_count)===1?'':'s'}</div>`;
    const dw = d.domain_weights || {};
    const doms = Object.entries(dw).filter(([,w])=>w>0).sort((a,b)=>b[1]-a[1]).slice(0,8);
    if (doms.length){ html += '<div class="d-h">Domains</div>';
      for (const [p] of doms){ const leaf=p.split('/').pop().replace(/-/g,' ');
        html += `<div class="d-dom"><span class="d-dot" style="color:${_domColor(p)};background:${_domColor(p)}"></span>${_esc(leaf)}</div>`; } }
  } else if (payload.nodeType === 'domain') {
    html += `<div class="d-kind">Domain</div><div class="d-name">${_esc(d.name)}</div>`;
    html += `<div class="d-meta">${_esc(d.path||'')}<br>${Number(d.document_count||0)} documents</div>`;
  } else if (payload.nodeType === 'collection') {
    html += `<div class="d-kind">Repository</div><div class="d-name">▣ ${_esc(d.name)}</div>`;
    html += `<div class="d-meta">${Number(d.document_count||0)} documents${d.domain?` · ${_esc(String(d.domain).split('/').pop())}`:''}</div>`;
  }
  _dbody.innerHTML = html; _detail.classList.add('show');
}
function hidePanel(){ _detail.classList.remove('show'); }
"""


def _patch_index(src: str) -> str:
    def rep(s, a, b):
        assert a in s, f"patch anchor not found: {a[:60]!r}"
        return s.replace(a, b)

    # data load → baked payload (folder build fetches ./graph.json; single-file overrides window global)
    s = rep(src, "const graphUrl = `${API_URL}/graph`;",
            "const graphUrl = './graph.json';  // baked subset")
    # neutralize the on-click cooccurrence fetch (there is no server in the embed; the app's
    # own try/catch already tolerates co=[] — neighbor lines simply don't draw offline)
    s = rep(s,
            "    const resp = await fetch(`${API_URL}/entities/${encodeURIComponent(ent.id)}/cooccurrences?limit=20`, { headers: fetchHeaders, signal: coFetchCtrl.signal });\n    if (resp.ok) co = await resp.json();",
            "    co = [];  // offline embed: no live cooccurrence fetch")
    # detail panel: css, markup, helper, wiring
    s = rep(s, "</style>", _PANEL_CSS + "</style>")
    s = rep(s, '<div id="hud"></div>',
            '<div id="detail"><span class="d-close" id="d-close">✕</span><div id="d-body"></div></div>\n<div id="hud"></div>')
    s = rep(s, "const state = new WorldState();", "const state = new WorldState();\n" + _PANEL_HELPER)
    s = rep(s,
            "        window.parent.postMessage(payload, '*');\n      } else {\n        window.parent.postMessage({ type: 'node_cleared' }, '*');\n      }",
            "        window.parent.postMessage(payload, '*');\n        renderPanel(payload);\n      } else {\n        window.parent.postMessage({ type: 'node_cleared' }, '*');\n        hidePanel();\n      }")
    s = rep(s,
            "      state.pinnedId = null;\n      window.parent.postMessage({ type: 'node_cleared' }, '*');",
            "      state.pinnedId = null;\n      window.parent.postMessage({ type: 'node_cleared' }, '*');\n      hidePanel();")
    # double-click: local zoom for entity/collection (were postMessage-only in-app)
    s = rep(s, "      type: 'enter_star', entityId: hit.id, entityName: hit.label,\n    }, '*');",
            "      type: 'enter_star', entityId: hit.id, entityName: hit.label,\n    }, '*');\n    camera.flyTo(hit.x, hit.y, Math.max(camera.zoom*2.2, 1.4), 800);")
    s = rep(s, "      type: 'enter_collection', collectionId: hit.collectionId, collectionName: hit.label,\n    }, '*');",
            "      type: 'enter_collection', collectionId: hit.collectionId, collectionName: hit.label,\n    }, '*');\n    camera.flyTo(hit.x, hit.y, Math.max(camera.zoom*2.2, 1.0), 800);")
    return s


def build(graph_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    # copy the canonical modules + patched index into a build dir
    shutil.copytree(VIZ / "core", work / "core")
    shutil.copytree(VIZ / "renderers", work / "renderers")
    index = _patch_index((VIZ / "index.html").read_text())
    graph = graph_path.read_text()

    # --- folder build (fetches ./graph.json; ideal for hosting on a static site) ---
    folder = out_dir / "folder"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    shutil.copytree(VIZ / "core", folder / "core")
    shutil.copytree(VIZ / "renderers", folder / "renderers")
    (folder / "index.html").write_text(index)
    (folder / "graph.json").write_text(graph)

    # --- single-file build (inlines data + a bundled IIFE; zero network) ---
    m = re.search(r'<script type="module">(.*?)</script>', index, re.S)
    module = m.group(1).replace(
        "Promise.resolve(window.__ORRERY_GRAPH__)",  # idempotent if already patched
        "Promise.resolve(window.__ORRERY_GRAPH__)",
    ).replace(
        "fetch(graphUrl, { headers: fetchHeaders }).then(r => r.json()).then(data => {",
        "Promise.resolve(window.__ORRERY_GRAPH__).then(data => {",
    )
    (work / "entry.mjs").write_text(module)
    bundle_path = work / "bundle.js"
    npx = shutil.which("npx") or "npx"
    subprocess.run(
        [npx, "--yes", "esbuild", str(work / "entry.mjs"), "--bundle",
         "--format=iife", f"--outfile={bundle_path}", "--log-level=warning"],
        check=True,
    )
    bundle = bundle_path.read_text()
    shell = index[: m.start()] + "__SLOT__" + index[m.end():]
    inject = (
        '<script id="orrery-graph" type="application/json">' + graph.replace("</", "<\\/") + "</script>\n"
        '<script>window.__ORRERY_GRAPH__=JSON.parse(document.getElementById("orrery-graph").textContent);</script>\n'
        "<script>" + bundle + "</script>"
    )
    single = shell.replace("__SLOT__", inject)
    (out_dir / "orrery-galaxy.html").write_text(single)
    shutil.rmtree(work)

    assert "fetch(" not in single, "single-file build still contains a live fetch()"
    print(f"build → {out_dir}/orrery-galaxy.html ({len(single)//1024} KB, single file)")
    print(f"build → {folder}/  (folder: index.html + core/ + renderers/ + graph.json)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("subset", help="extract a subset graph.json from a workspace DB snapshot")
    s1.add_argument("--db", required=True, type=Path)
    s1.add_argument("--out", required=True, type=Path)
    s1.add_argument("--max-entities", type=int, default=0,
                    help="cap entities, keeping the best-connected (0 = no cap)")
    s1.add_argument("--no-collections", action="store_true",
                    help="drop the product-repo layer")
    s1.add_argument("--repo-depth", choices=("root", "group", "all"), default="root",
                    help="codesum depth for the product repos: root=one summary per repo "
                         "(default), group=module level, all=every file (~16k entities)")
    s2 = sub.add_parser("build", help="bundle the viz + a graph.json into an embed")
    s2.add_argument("--graph", required=True, type=Path)
    s2.add_argument("--out", default=REPO / "embed" / "dist", type=Path)
    a = ap.parse_args()
    if a.cmd == "subset":
        subset(a.db, a.out, max_entities=a.max_entities,
               with_collections=not a.no_collections, repo_depth=a.repo_depth)
    else:
        build(a.graph, a.out)


if __name__ == "__main__":
    main()
