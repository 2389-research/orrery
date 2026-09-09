# ABOUTME: Build a self-contained, offline HTML export of a noosphere's galaxy —
# ABOUTME: the /graph payload plus per-entity detail, and the viz patches that let
# ABOUTME: the canonical renderer run with no services behind it.
"""Static HTML export of the galaxy.

The viz (`frontend/public/viz/`) is already a static Canvas app: its only required
backend call is one `GET /graph`. So an "export as HTML" is not a second
visualization — it is the SAME renderer with the payload baked in and the few
shell-provided behaviours substituted locally.

This module is the canonical implementation of that, used by the route
(`routes/export.py`). It deliberately imports **stdlib only** and takes a plain
`sqlite3` connection plus an already-built snapshot dict, so a script can load a DB
by file path without dragging in the FastAPI app.

`embed/build_embed.py` (the offline CLI) carries its OWN copy of the panel and the
patches, because it must also produce the single-file build. That duplication is not
free — the two `PANEL_JS` bodies have already drifted cosmetically — and
`tests/test_carve.py::test_export_panel_copies_have_not_drifted` is what keeps the
drift from becoming behavioural. The document SELECTION is genuinely shared:
`select_subject_docs` has one definition, imported by path.

Two things do NOT survive going static, and are substituted here:

  * the detail panel and drill-in are handled by the Next shell over postMessage;
    with no parent frame those messages go nowhere. `PANEL_CSS`/`PANEL_JS` add a
    local panel and a local double-click zoom.
  * the panel's richest content comes from live calls
    (`/entities/{id}/cooccurrences`, `entity.sources`). Those are just data we can
    compute here, so `entity_detail` bakes them; that also restores neighbourhood
    highlighting, which is otherwise inert.

What still does not survive: the sector/system/star drill-in pages (`star.html`,
`collection.html`) fetch their own APIs, and search is shell-side. The export is
galaxy-level.

Sizing, measured rather than assumed — the two costs are independent:

  * ENTITIES cost framerate. The app renders up to 3000
    (`graph_snapshot.DEFAULT_MAX_RENDER_NODES`), so that is the natural ceiling.
    Note this does NOT mean a big graph fails to export: the snapshot is already
    pruned to that cap, so a 97k-entity noosphere exports as its top 3000 by degree.
    That is reported (`export_counts`, `pruned`) rather than refused, because it is
    a legitimate thing to want — but it is not the whole graph, and saying so is the
    difference between a useful artifact and a misleading one.
  * DOMAIN TRADE ROUTES cost bytes, not framerate — `drawTradeRoutes` early-returns
    unless a node is hovered or pinned — but they are ~3/4 of the payload, so
    `min_route_weight` is the size dial.
"""

from __future__ import annotations

import json
from collections import defaultdict

# Entities above this render badly and download badly. NOTE: the snapshot's render
# set is ALREADY pruned to graph_render_max_nodes (3000 by default), so this bound is
# rarely what stops you — the meaningful signal is whether the snapshot was pruned at
# all, i.e. whether the export is the whole noosphere or the top-N slice of a much
# larger one. `export_counts()` reports both so callers can say which.
MAX_EXPORTABLE_ENTITIES = 3000


def export_counts(snapshot: dict) -> dict:
    """What an export of this snapshot would actually contain, and what it omits.

    A snapshot is a RENDER set: `graph_snapshot` stores positions for every node but
    only ships the top-N by degree. Exporting a big graph therefore never fails on
    size — it quietly produces a top-N-by-degree dump of the whole corpus wearing the
    noosphere's name. Surfacing `entities_total` vs `entities_rendered` is what lets
    the UI say so instead of implying the export is complete.
    """
    nodes = snapshot.get("nodes", ())
    meta = snapshot.get("meta", {})
    by_type = (meta.get("counts_by_type") or {})
    rendered = sum(1 for n in nodes if n.get("type") == "entity")
    total = int(((by_type.get("entity") or {}).get("total")) or rendered)
    return {
        "entities": rendered,
        "entities_total": total,
        "pruned": total > rendered,
        "collections": sum(1 for n in nodes if n.get("type") == "collection"),
        "domains": len(snapshot.get("taxonomy", ())),
        "routes": sum(1 for e in snapshot.get("edges", ()) if e.get("scope") == "domain"),
    }


def _chunks(seq, n=400):
    """SQLite caps bound parameters (999 before 3.32); chunk anything graph-sized."""
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _pretty_title(t: str | None) -> str:
    """Doc titles are raw source paths; an exported panel is public-facing."""
    t = (t or "").strip()
    for pre in ("content/posts/", "content/products/"):
        if t.startswith(pre):
            t = t[len(pre):]
    for suf in ("/index.md", "/index.en.md", ".md"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t.replace("-", " ").replace("/", " / ") or "(untitled)"


# A document mentioning more exported entities than this contributes no pairs. The
# pair count is quadratic in per-document fan-out, and this runs inside a request
# handler: at 3000 exported entities an unbounded pass can build millions of nested
# dict entries before the top-N trim below discards nearly all of them. A long note
# naming 400 entities also says almost nothing about which two belong together, so the
# cap costs little signal.
MAX_DOC_FANOUT_FOR_COOCCURRENCE = 60


def entity_detail(conn, entity_ids, *, neighbours: int = 12) -> dict:
    """Per-entity `{docs, nbrs}` for the offline panel.

    `nbrs` is co-occurrence computed from shared documents *within this export*, so
    it reflects the exported graph rather than a wider corpus the viewer cannot see.
    """
    kept = set(entity_ids)
    if not kept:
        return {}

    doc_ents: dict[str, set[str]] = defaultdict(set)
    for batch in _chunks(sorted(kept)):
        ph = ",".join("?" * len(batch))
        for did, eid in conn.execute(
            f"""SELECT es.document_id, es.entity_id
                  FROM entity_sources es
                  JOIN entities e ON e.id = es.entity_id
                 WHERE es.entity_id IN ({ph}) AND e.invalid_at IS NULL""",
            batch,
        ):
            doc_ents[did].add(eid)

    titles: dict[str, str] = {}
    for batch in _chunks(sorted(doc_ents)):
        ph = ",".join("?" * len(batch))
        for did, title in conn.execute(
            f"SELECT id, title FROM documents WHERE id IN ({ph})", batch
        ):
            titles[did] = title

    ent_docs: dict[str, set[str]] = defaultdict(set)
    co: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for did, ents in doc_ents.items():
        inside = sorted(ents & kept)
        for a in inside:
            ent_docs[a].add(did)          # the doc list is NOT capped, only the pairs
        if len(inside) > MAX_DOC_FANOUT_FOR_COOCCURRENCE:
            continue
        for a in inside:
            for b in inside:
                if a != b:
                    co[a][b] += 1

    out = {}
    for eid in sorted(kept):
        top = sorted(co.get(eid, {}).items(), key=lambda kv: (-kv[1], kv[0]))[:neighbours]
        out[eid] = {
            "docs": sorted({_pretty_title(titles.get(d))[:120] for d in ent_docs.get(eid, ())})[:12],
            "nbrs": [[n, w] for n, w in top],
        }
    return out


def build_export_payload(
    conn,
    snapshot: dict,
    *,
    max_entities: int = 0,
    per_repo_min: int = 8,
    min_route_weight: int = 2,
    detail_neighbours: int = 12,
) -> dict:
    """Turn a `/graph` snapshot into an export payload.

    No rescaling happens here, unlike carving a slice out of a larger graph: the
    noosphere IS the scope, so the snapshot's own counts already describe it.
    """
    nodes = list(snapshot.get("nodes", ()))
    ents = [n for n in nodes if n.get("type") == "entity"]
    colls = [n for n in nodes if n.get("type") == "collection"]

    if max_entities and len(ents) > max_entities:
        # A flat cap starves collections: entities that recur across many documents
        # dominate any connectivity ranking and crowd out entities specific to one
        # repo, which then renders as a marker with nothing around it. Reserve a
        # per-collection quota first, then fill the remainder by degree.
        by_coll: dict[str, list] = defaultdict(list)
        coll_ids = {c["id"] for c in colls}
        for n in ents:
            for m in n.get("memberships", ()):
                if m.get("container_type") == "collection" and m.get("weight", 0) > 0 \
                        and m.get("id") in coll_ids:
                    by_coll[m["id"]].append(n)

        def rank(n):
            return (-(n.get("degree") or 0), n["id"])

        keep: dict[str, dict] = {}
        if per_repo_min and by_coll:
            quota = max(1, min(per_repo_min, max_entities // max(len(by_coll), 1)))
            for cid in sorted(by_coll):
                for n in sorted(by_coll[cid], key=rank)[:quota]:
                    keep[n["id"]] = n
        for n in sorted(ents, key=rank):
            if len(keep) >= max_entities:
                break
            keep.setdefault(n["id"], n)
        ents = sorted(keep.values(), key=rank)

    # Entities + collections only. Document nodes are deliberately not carried: the
    # viz has no document branch (nothing in core/state.js or renderers/galaxy.js reads
    # one), and a filter keyed on entity/collection ids could never have matched a
    # document id anyway. If the viz gains a document layer, select them here on
    # `memberships`, not on an id intersection.
    render = ents + colls

    edges = [
        e for e in snapshot.get("edges", ())
        if (e.get("scope") == "domain" and (e.get("weight") or 0) >= min_route_weight)
        or (e.get("scope") != "domain" and e.get("source") in kept_ids and e.get("target") in kept_ids)
    ]

    meta = snapshot.get("meta", {})
    payload = {
        # Only what the renderer reads — the snapshot's meta carries pruning stats
        # and whole-graph totals that have no business in a published artifact.
        "meta": {
            "schema_version": meta.get("schema_version"),
            "export": True,
            "counts": {"nodes_included": len(render), "nodes_total": len(render)},
        },
        "taxonomy": snapshot.get("taxonomy", []),
        "nodes": render,
        # node_index exists to hydrate nodes OUTSIDE the render set; in an export the
        # render set is the whole payload, so a slim map avoids shipping every node
        # twice.
        "node_index": {
            n["id"]: {"id": n["id"], "type": n.get("type"), "label": n.get("label")}
            for n in render
        },
        "edges": edges,
        "layout": snapshot.get("layout", {}),
        # Not part of the v5 contract; the renderer ignores unknown keys.
        "entity_detail": entity_detail(
            conn, [n["id"] for n in ents], neighbours=detail_neighbours
        ),
    }
    return payload


# ── viz patches: make the canonical index.html run standalone ────────────────────

PANEL_CSS = """
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
#detail .d-link { cursor: pointer; border-radius: 4px; padding: 2px 4px; margin: 0 -4px; }
#detail .d-link:hover { background: rgba(100,180,255,0.10); }
#detail .d-w { margin-left: auto; font-size: 10px; color: rgba(140,200,255,0.55); }
#detail .d-doc { font-size: 11px; color: #aab6cc; padding: 2px 0 2px 15px; text-indent: -15px; line-height: 1.45; }
"""

PANEL_JS = """
// ---- local detail panel (export; the Next shell normally provides this) ----
const _detail = document.getElementById('detail');
const _dbody = document.getElementById('d-body');
document.getElementById('d-close').addEventListener('click', () => { _detail.classList.remove('show'); state.pinnedId = null; });
function _esc(x){ return String(x==null?'':x).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function _domColor(path){ try { return (state.domainColors && state.domainColors[path]) || '#7aa0d8'; } catch(_) { return '#7aa0d8'; } }
function _entName(id){
  const e = state.entities.get(id);
  if (e) return e.label || e.name;
  const r = state.nodeIndex && state.nodeIndex.get(id);
  return r ? (r.label || r.name) : null;
}
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
    const det = (window.__ORRERY_DETAIL__ || {})[d.id] || {};
    if ((det.nbrs || []).length) {
      html += '<div class="d-h">Connected</div>';
      for (const [nid, w] of det.nbrs) {
        const nn = _entName(nid);
        if (!nn) continue;
        html += `<div class="d-dom d-link" data-eid="${_esc(nid)}"><span class="d-dot" style="color:#7aa0d8;background:#7aa0d8"></span>${_esc(nn)}<span class="d-w">${w}</span></div>`;
      }
    }
    if ((det.docs || []).length) {
      html += '<div class="d-h">Appears in</div>';
      for (const t of det.docs) html += `<div class="d-doc">${_esc(t)}</div>`;
    }
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
// Connected rows are clickable, so the panel is navigable with no API behind it.
_dbody.addEventListener('click', ev => {
  const row = ev.target.closest('.d-link');
  if (!row) return;
  const id = row.dataset.eid;
  const e = state.entities.get(id);
  if (!e) return;
  state.pinnedId = id;
  state.attractNeighbors = new Set([id, ...(((window.__ORRERY_DETAIL__||{})[id]||{}).nbrs||[]).map(x=>x[0])]);
  renderPanel({ type:'node_selected', nodeType:'entity', data:{
    id, name: e.label || e.name, entityType: e.type,
    source_count: e.sourceCount, domain_weights: e.domainWeights } });
});
"""


def patch_viz_index(src: str, *, graph_url: str = "./graph.json") -> str:
    """Rewrite the canonical viz index.html for standalone use.

    Every patch asserts on a UNIQUE anchor. That matters both ways: a missing anchor
    means the viz moved and the export would ship broken; a duplicated anchor would
    inject a helper twice and produce a blank page on an otherwise successful build.
    """
    def rep(s: str, a: str, b: str) -> str:
        n = s.count(a)
        if n != 1:
            raise RuntimeError(f"viz export patch anchor found {n}x (want 1): {a[:70]!r}")
        return s.replace(a, b, 1)

    s = rep(src, "const graphUrl = `${API_URL}/graph`;",
            f"const graphUrl = {json.dumps(graph_url)};  // baked export")
    s = rep(s, "</style>", PANEL_CSS + "</style>")
    s = rep(s, '<div id="hud"></div>',
            '<div id="detail"><span class="d-close" id="d-close">✕</span><div id="d-body"></div></div>\n<div id="hud"></div>')
    s = rep(s, "const state = new WorldState();", "const state = new WorldState();\n" + PANEL_JS)
    s = rep(s, "  state.loadGraphData(data);",
            "  window.__ORRERY_DETAIL__ = data.entity_detail || {};\n  state.loadGraphData(data);")
    # panel in/out on the existing pin flow
    s = rep(s,
            "        window.parent.postMessage(payload, '*');\n      } else {\n        window.parent.postMessage({ type: 'node_cleared' }, '*');\n      }",
            "        window.parent.postMessage(payload, '*');\n        renderPanel(payload);\n      } else {\n        window.parent.postMessage({ type: 'node_cleared' }, '*');\n        hidePanel();\n      }")
    s = rep(s,
            "      state.pinnedId = null;\n      window.parent.postMessage({ type: 'node_cleared' }, '*');",
            "      state.pinnedId = null;\n      window.parent.postMessage({ type: 'node_cleared' }, '*');\n      hidePanel();")
    # neighbourhood comes from the baked detail instead of a live fetch
    s = rep(s,
            "    const resp = await fetch(`${API_URL}/entities/${encodeURIComponent(ent.id)}/cooccurrences?limit=20`, { headers: fetchHeaders, signal: coFetchCtrl.signal });\n    if (resp.ok) co = await resp.json();",
            "    co = (((window.__ORRERY_DETAIL__ || {})[ent.id] || {}).nbrs || [])\n           .map(([id, weight]) => ({ id, weight }));  // baked, not fetched")
    # entity / collection double-click posts to a shell that isn't there; zoom locally
    s = rep(s, "      type: 'enter_star', entityId: hit.id, entityName: hit.label,\n    }, '*');",
            "      type: 'enter_star', entityId: hit.id, entityName: hit.label,\n    }, '*');\n    camera.flyTo(hit.x, hit.y, Math.max(camera.zoom*2.2, 1.4), 800);")
    s = rep(s, "      type: 'enter_collection', collectionId: hit.collectionId, collectionName: hit.label,\n    }, '*');",
            "      type: 'enter_collection', collectionId: hit.collectionId, collectionName: hit.label,\n    }, '*');\n    camera.flyTo(hit.x, hit.y, Math.max(camera.zoom*2.2, 1.0), 800);")
    return s


def viz_asset_dir():
    """Where the canonical viz lives, in the image or a dev checkout.

    The orchestrator image copies it to `orchestrator/viz/`; in a checkout it sits at
    `frontend/public/viz/`. Returns None when neither exists, so the route can fail
    with a real explanation instead of an empty zip.
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    for cand in (
        here.parents[2] / "viz",                              # orchestrator/viz (image)
        here.parents[3] / "frontend" / "public" / "viz",      # repo checkout
    ):
        if (cand / "index.html").is_file():
            return cand
    return None
