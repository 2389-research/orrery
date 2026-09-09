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
The slice is a SUBJECT spanning three sources (website + product repos + vault docs);
see WEBSITE_SILOS / WEBSITE_PREDICATE below and embed/README.md.
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


def _chunks(seq, n=400):
    """SQLite caps bound parameters (999 before 3.32), so every dynamic IN (...)
    list that can grow with the graph is chunked."""
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def subset(db_path: Path, out_path: Path, max_entities: int = 0,
           with_collections: bool = True, repo_depth: str = "root") -> None:
    import os
    import sqlite3
    from collections import defaultdict

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
    repo_ids, matched_slugs = [], set()
    for (i, t, u) in sources:
        if t != "repo":
            continue
        base = os.path.basename((u or "").rstrip("/")).lower()
        if base in slugs_low:
            repo_ids.append(i)
            matched_slugs.add(base)
    vault_ids = [i for (i, t, u) in sources if t == "vault"]

    # Products with no matching repo are reported, not silently dropped: repos are
    # matched on exact basename, so a repo synced under a suffixed name (coven ->
    # coven-app / coven-gateway) would otherwise vanish with no signal.
    missing = sorted(slugs_low - matched_slugs)
    if missing:
        print(f"  note: {len(missing)} product(s) with no exact-basename repo match: {', '.join(missing)}")

    # (2) those products' repos, at the requested codesum depth
    repo_docs = set()
    if repo_ids:
        if repo_depth == "all":
            for b in _chunks(repo_ids):
                ph = ",".join("?" * len(b))
                repo_docs |= {r[0] for r in conn.execute(
                    f"SELECT id FROM documents WHERE silo_id IN ({ph})", b)}
        else:
            roles = ("root",) if repo_depth == "root" else ("root", "group")
            rl = ",".join("?" * len(roles))
            for b in _chunks(repo_ids):
                ph = ",".join("?" * len(b))
                repo_docs |= {r[0] for r in conn.execute(
                    f"""SELECT DISTINCT d.id FROM documents d
                         JOIN document_collections dc ON dc.document_id = d.id
                         WHERE d.silo_id IN ({ph}) AND dc.role IN ({rl})""", list(b) + list(roles))}

    # (3) obsidian docs written about those products. The alternation is built from
    #     the UNESCAPED slug parts so the '-' -> '[- ]?' relaxation is explicit,
    #     rather than depending on re.escape() choosing to escape '-'. Word
    #     boundaries keep short slugs (ish / mux / jeff) out of unrelated words.
    pats = [re.compile(r"(?<![a-z0-9])" + r"[-_ ]?".join(re.escape(part) for part in x.split("-"))
                       + r"(?![a-z0-9])", re.I) for x in slugs]
    vault_docs = set()
    if vault_ids:
        for b in _chunks(vault_ids):
            ph = ",".join("?" * len(b))
            vault_docs |= {r[0] for r in conn.execute(
                f"SELECT id, title FROM documents WHERE silo_id IN ({ph})", b)
                if any(pt.search(r[1] or "") for pt in pats)}

    # sorted(): set iteration order varies with PYTHONHASHSEED, and the cap below
    # breaks ties — without this the same DB + flags produce different samples.
    doc_ids = sorted({*web_docs, *repo_docs, *vault_docs})
    print(f"slice: {len(web_docs)} website + {len(repo_docs)} repo({repo_depth}) + "
          f"{len(vault_docs)} vault = {len(doc_ids)} docs over {len(slugs)} products / {len(repo_ids)} repos")

    # doc -> entities, and the in-slice source count per entity
    doc_ents = defaultdict(set)
    for b in _chunks(doc_ids):
        dp = ",".join("?" * len(b))
        for did, eid in conn.execute(
                f"""SELECT es.document_id, es.entity_id FROM entity_sources es
                     JOIN entities e ON e.id = es.entity_id
                     WHERE es.document_id IN ({dp}) AND e.invalid_at IS NULL""", b):
            doc_ents[did].add(eid)
    slice_src = defaultdict(int)
    for ents in doc_ents.values():
        for e in ents:
            slice_src[e] += 1
    ids = set(slice_src)

    row = conn.execute("SELECT payload FROM graph_snapshot WHERE payload IS NOT NULL LIMIT 1").fetchone()
    if not row:
        sys.exit("No materialized graph_snapshot in that DB — open /graph once to build it.")
    p = json.loads(row[0])
    ni = p["node_index"]
    render_by_id = {n["id"]: n for n in p["nodes"]}
    all_coll = {n["id"]: n for n in p["nodes"] if n["type"] == "collection"}
    posmap = p["layout"]["positions"]

    stars = [render_by_id.get(i) or ni.get(i) for i in sorted(ids)
             if (i in render_by_id or i in ni)]

    # Cap by REAL in-slice connectivity: two entities are connected here if they
    # share a slice document. The previous version ranked on collection-scope
    # edges, but every such edge is collection<->collection (entity co-occurrence
    # is on_demand, never inline), so every score was 0 and the sort silently fell
    # through to each entity's GLOBAL degree in the 97k graph — selecting the
    # hubbiest generic vocabulary instead of the core of this slice.
    if max_entities and len(stars) > max_entities:
        nbrs = defaultdict(set)
        for ents in doc_ents.values():
            for e in ents:
                nbrs[e] |= ents
        stars.sort(key=lambda n: (-(len(nbrs.get(n["id"], ())) - 1),
                                  -slice_src.get(n["id"], 0), n["id"]))
        stars = stars[:max_entities]

    # Collections = the PRODUCTS' repos, matched on the collection's own label.
    coll_ids = set()
    if with_collections:
        for cid, node in all_coll.items():
            lab = (node.get("label") or node.get("path") or "").lower()
            if lab in slugs_low and cid in posmap:
                coll_ids.add(cid)
    colls = [all_coll[c] for c in sorted(coll_ids)]

    kept_dom = {
        m["id"]
        for n in stars + colls
        for m in n.get("memberships", [])
        if m["container_type"] == "domain" and m["weight"] > 0
    }
    # Drop dangling AND zero-weight memberships. A weight-0 collection membership
    # still makes state.js take the collection branch, where tw = 0**3 = 0 and the
    # node is discarded — so the payload would advertise entities the viz silently
    # never renders.
    for n in stars + colls:
        n["memberships"] = [
            m for m in n.get("memberships", [])
            if m["weight"] > 0 and (
                (m["container_type"] == "domain" and m["id"] in kept_dom)
                or (m["container_type"] == "collection" and m["id"] in coll_ids))
        ]

    # ── rescale magnitudes to the SLICE ──────────────────────────────────────────
    # Everything the renderer sizes/labels off was the full graph's: a domain read
    # "1121 documents" inside a 178-document slice, and state.js sizes domain radius
    # from that count. Recount against the slice so the artifact describes itself.
    dom_docs, coll_docs = defaultdict(int), defaultdict(int)
    for b in _chunks(doc_ids):
        dp = ",".join("?" * len(b))
        for (path,) in conn.execute(
                f"SELECT domain_path FROM document_domains WHERE document_id IN ({dp})", b):
            dom_docs[path] += 1
        for (cid,) in conn.execute(
                f"SELECT collection_id FROM document_collections WHERE document_id IN ({dp})", b):
            coll_docs[cid] += 1
    for n in stars:
        n["degree"] = slice_src.get(n["id"], 0)
    # Repos are sized by how many SLICE ENTITIES belong to them, not by doc count:
    # at root depth every repo contributes exactly one doc, so a doc-count rescale
    # would flatten every repo marker to the same size and destroy the signal.
    coll_ents = defaultdict(int)
    for n in stars:
        for m in n["memberships"]:
            if m["container_type"] == "collection":
                coll_ents[m["id"]] += 1
    for n in colls:
        n["degree"] = coll_ents.get(n["id"], coll_docs.get(n["id"], 0))

    # ── domain trade routes, recomputed FROM the slice ───────────────────────────
    # Previously any edge whose endpoints were both kept survived, carrying its
    # full-graph weight: 24,820 of 25,545 routes (97%) and two thirds of the bytes,
    # describing co-occurrence among 97k entities the viewer cannot see. Recompute
    # from the kept entities: weight = how many of THEM share both domains.
    pair = defaultdict(int)
    for n in stars:
        ds = sorted({m["id"] for m in n["memberships"] if m["container_type"] == "domain"})
        for a in range(len(ds)):
            for b2 in range(a + 1, len(ds)):
                pair[(ds[a], ds[b2])] += 1
    edges = [{"source": s, "target": t, "type": "cooccurrence", "scope": "domain", "weight": w}
             for (s, t), w in sorted(pair.items()) if w > 1]
    nodeset = {n["id"] for n in stars + colls}
    edges += [e for e in p["edges"]
              if e["scope"] == "collection" and e["source"] in nodeset and e["target"] in nodeset]

    nodes = stars + colls
    # Domains are sized by their in-slice ENTITY count, not doc count — the same
    # problem the repos had, worse. Domains are kept because ENTITIES are members of
    # them (entities carry multi-domain memberships from the full graph), so the
    # slice's 178 docs land in only ~40 of the 246 kept domains: 168 of them have a
    # document_count of 0 and the median is 0. state.js sizes every domain as
    # radius = 120 + sqrt(document_count)*35, so two thirds pin to the floor and the
    # galaxy goes uniform and sparse. Entity count has real spread (1 / 10 / 275).
    #
    # Caveat: the tooltip labels this field "docs", so it now reads as the domain's
    # weight in this slice rather than a literal document count. Sizing on the
    # honest doc count is what produced a flat map of nothing.
    dom_ents = defaultdict(int)
    for n in stars:
        for m in n["memberships"]:
            if m["container_type"] == "domain":
                dom_ents[m["id"]] += 1
    tax = [{**t, "document_count": dom_ents.get(t["path"], dom_docs.get(t["path"], 0))}
           for t in p["taxonomy"] if t["path"] in kept_dom]
    sub = {
        # Only what the renderer reads. The full snapshot's meta carried the whole
        # graph's shape (97,220 entities, pruning stats) into a public artifact.
        "meta": {"schema_version": p["meta"].get("schema_version"),
                 "subset": "custom",
                 "counts": {"nodes_included": len(nodes), "nodes_total": len(nodes)}},
        "taxonomy": tax,
        "nodes": nodes,
        # node_index exists for hydrating nodes OUTSIDE the render set; here the
        # render set is the whole payload, so a slim id/label map is enough and
        # avoids serializing every node twice.
        "node_index": {n["id"]: {"id": n["id"], "type": n["type"], "label": n.get("label")}
                       for n in nodes},
        "edges": edges,
        "layout": {
            **p["layout"],
            "positions": {k: v for k, v in sorted(posmap.items()) if k in kept_dom or k in coll_ids},
            "palette": {k: v for k, v in sorted(p["layout"]["palette"].items()) if k in kept_dom},
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sub, separators=(",", ":"), sort_keys=False))
    dom_e = sum(1 for e in edges if e["scope"] == "domain")
    coll_e = sum(1 for e in edges if e["scope"] == "collection")
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
        n = s.count(a)
        assert n == 1, f"patch anchor found {n}x (want exactly 1): {a[:60]!r}"
        return s.replace(a, b, 1)

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

    # --- single-file build (inlines data + a bundled IIFE; zero network) ---
    m = re.search(r'<script type="module">(.*?)</script>', index, re.S)
    assert m, "no <script type=\"module\"> found in the viz index.html"
    _load = "fetch(graphUrl, { headers: fetchHeaders }).then(r => r.json()).then(data => {"
    assert m.group(1).count(_load) == 1, "data-load call not found (or not unique) in the viz module"
    module = m.group(1).replace(_load, "Promise.resolve(window.__ORRERY_GRAPH__).then(data => {", 1)
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
    shutil.rmtree(work)

    # Validate BEFORE writing anything, so a failure can't leave dist/ half-updated.
    assert "fetch(" not in single, "single-file build still contains a live fetch()"

    # --- folder build (fetches ./graph.json; ideal for hosting on a static site) ---
    folder = out_dir / "folder"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    shutil.copytree(VIZ / "core", folder / "core")
    shutil.copytree(VIZ / "renderers", folder / "renderers")
    (folder / "index.html").write_text(index)
    (folder / "graph.json").write_text(graph)
    (out_dir / "orrery-galaxy.html").write_text(single)
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
