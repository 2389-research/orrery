# Entity Segmentum View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the entity page's arc-packing layout with the validated **place-first, derive-territory-after** pipeline: radius = relevance band, angle = domain-anchor pull + doc↔doc connectivity, sectors rasterized from where docs land.

**Architecture:** A new pure ESM module `core/segmentum-layout.js` carries Stages 0–5 compute (all `node --test`-covered); `star.html` runs it and stashes the result; `renderers/star.js` draws territories from the rasterized boundaries. Hand-rolled deterministic force sim (no d3). The empirically-winning model is **Variant C** (domain anchors from a local Fiedler embedding + soft radial band) — see the spec's "Empirical validation".

**Tech Stack:** vanilla ESM + Canvas2D, `node --test`, playwright headless for visual regression. No new npm deps (Fiedler is hand-rolled Jacobi).

**Spec:** `docs/superpowers/specs/2026-09-10-entity-segmentum-view-design.md` — read it first; this implements Variant C exactly, including the two knowingly-traded invariants (band-honest radius; domains drive placement) and all parameter defaults.

**Build order = the spec's staged delivery.** Each stage consumes only the previous stage's output, so a broken territory is debugged in pipeline order (is the point wrong before asking if the border drew wrong).

---

## File Structure

**New:**
- `frontend/public/viz/core/segmentum-layout.js` — the pipeline. Exports constants + `resolveDocs` (S0), `domainAnchors` (S1, incl. `fiedlerOrder`), `seed` (S1), `simulate` (S2), `rasterize` (S3), `cleanupRegions` (S4), `extractBoundaries` + `drawGuard` (S5 compute), and `layoutSegmentum(graph, config)` orchestrating them.
- `frontend/public/viz/core/segmentum-layout.test.mjs` — `node --test`.

**Modified:**
- `frontend/public/viz/star.html` — replace the `star-layout` wiring with `segmentum-layout`; stash `window.__SEG__` (points + sectors + palette).
- `frontend/public/viz/renderers/star.js` — replace `drawSectors` with territory drawing from rasterized boundaries; keep domain doc-colouring + hover.

**Deleted (after the new path works):**
- `core/star-layout.js` ring/packing exports (`assignRings`, `packRings`, `layoutStar`, `coEntityBand`) and their tests. **Keep** `midLevel`, `orderDomains`, `sectorColor`, `documentStrength`, `MISC_*` — move them into `segmentum-layout.js` (or import) so nothing re-implements them.

**Constants** live in `segmentum-layout.js` as named exports (shared by tests + renderer).

---

## Task 1: Stage 0 — relevance → radius band

**Files:** Create `core/segmentum-layout.js`, `core/segmentum-layout.test.mjs`.

- [ ] **Step 1: Failing tests**

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveDocs, R0, R1, BAND } from './segmentum-layout.js';

test('radius: rank+sqrt, most relevant innermost, all within [R0,R1]', () => {
  const docs = [2,3,4,10,50].map((n,i)=>({id:`d${i}`, n_entities:n, domain_path:'software/a/x'}));
  const out = resolveDocs(docs);
  const byId = Object.fromEntries(out.map(d=>[d.id,d]));
  assert.ok(byId.d0.rTarget < byId.d4.rTarget);         // n=2 strongest -> innermost
  for (const d of out) assert.ok(d.rTarget >= R0 && d.rTarget <= R1);
  assert.ok(byId.d0.rTarget >= R0 - 1e-9 && byId.d0.rTarget <= R0 + (R1-R0)*0.5); // p=0 near R0
});

test('radius: ties broken by id -> distinct percentiles (no clot)', () => {
  const docs = [{id:'b',n_entities:6,domain_path:'x/y'},{id:'a',n_entities:6,domain_path:'x/y'}];
  const out = resolveDocs(docs);
  assert.notEqual(out[0].rTarget, out[1].rTarget);      // same rel, different radius
});

test('band bounds are rTarget +/- BAND/2', () => {
  const [d] = resolveDocs([{id:'a',n_entities:4,domain_path:'x/y'}]);
  assert.equal(d.band[0], d.rTarget - BAND/2);
  assert.equal(d.band[1], d.rTarget + BAND/2);
});
```

- [ ] **Step 2: Run, watch fail** — `cd frontend/public/viz/core && node --test segmentum-layout.test.mjs` → module-not-found.

- [ ] **Step 3: Implement**

```js
/** Segmentum view layout — place first, derive territory after. Pure; node --test'd.
 *  Spec: docs/superpowers/specs/2026-09-10-entity-segmentum-view-design.md (Variant C). */
export const R0 = 60, R1 = 500, BAND = 90;            // radius band
export const R_ANCHOR_MARGIN = 40;
export const SEED_SPREAD_DEG = 8;
export const MISC_LABEL = 'misc', MISC_COLOR = '#6d7280';
const TAU = Math.PI*2, DEG = Math.PI/180;

// deterministic hash -> [0,1)
function hash01(s){ let h=2166136261>>>0; for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);} return (h>>>0)/4294967296; }

export function midLevel(p){ if(!p) return MISC_LABEL; const a=p.split('/'); return a.length>=2?a.slice(0,2).join('/'):p; }

/** Stage 0: rel = 1/n_entities; rank+sqrt -> rTarget; band = rTarget +/- BAND/2. */
export function resolveDocs(docs, {r0=R0,r1=R1,band=BAND}={}){
  const withRel = docs.map(d=>({...d, rel: 1/(d.n_entities||1), domain: midLevel(d.domain_path)}));
  const order = [...withRel].sort((a,b)=> (b.rel-a.rel) || (a.id<b.id?-1:a.id>b.id?1:0));
  const n = order.length || 1;
  return order.map((d,i)=>{
    const p = n>1 ? i/(n-1) : 0;
    const rTarget = r0 + (r1-r0)*Math.sqrt(p);
    return {...d, p, rTarget, band:[rTarget-band/2, rTarget+band/2]};
  });
}
```

- [ ] **Step 4: Run, watch pass.** **Step 5: Commit** `feat(viz): segmentum Stage 0 — relevance radius band`.

---

## Task 2: Stage 1a — domain co-occurrence graph + Fiedler order (hand-rolled)

The trickiest task. Fiedler vector = eigenvector of the 2nd-smallest eigenvalue of the graph Laplacian `L = D − W`. Hand-roll the **Jacobi eigenvalue algorithm** (robust for small symmetric matrices; domains ≤ ~30). No numpy, no npm.

**Files:** modify both.

- [ ] **Step 1: Failing tests**

```js
import { domainCooccur, fiedlerOrder, domainAnchors } from './segmentum-layout.js';

test('domain co-occurrence: two domains sharing co-entities get an edge', () => {
  // docs d1,d2 in domain A; d3 in B. co-entity c1 shares d1&d3 -> A,B co-occur.
  const docs = [{id:'d1',domain:'a'},{id:'d2',domain:'a'},{id:'d3',domain:'b'}];
  const co = [{id:'c1', shared_doc_ids:['d1','d3']}];
  const W = domainCooccur(docs, co);      // {domains:[...], matrix:[[...]]}
  const i = W.domains.indexOf('a'), j = W.domains.indexOf('b');
  assert.ok(W.matrix[i][j] > 0 && W.matrix[j][i] > 0);
});

test('fiedler order: a path graph a-b-c orders them a,b,c (or reverse) not a,c,b', () => {
  const domains = ['a','b','c'];
  const W = [[0,1,0],[1,0,1],[0,1,0]];
  const order = fiedlerOrder(domains, W);
  const idx = Object.fromEntries(order.map((d,i)=>[d,i]));
  assert.ok(Math.abs(idx.a-idx.c) === 2);   // endpoints are farthest apart
});

test('fiedler order is deterministic (sign fixed by a rule)', () => {
  const domains=['a','b','c'], W=[[0,1,0],[1,0,1],[0,1,0]];
  assert.deepEqual(fiedlerOrder(domains,W), fiedlerOrder(domains,W));
});

test('anchors: disconnected/single domain -> even spacing fallback, all angles distinct', () => {
  const a = domainAnchors([{id:'d',domain:'only'}], []);
  assert.equal(a.only !== undefined, true);
});
```

- [ ] **Step 2: Run, watch fail.**

- [ ] **Step 3: Implement** — Jacobi eigensolver + Fiedler + anchors:

```js
/** Symmetric eigendecomposition via cyclic Jacobi rotations. Returns {values, vectors}
 *  (vectors as columns). Deterministic. n is small (domain count). */
function jacobiEigen(Ain){
  const n = Ain.length;
  const A = Ain.map(r=>r.slice());
  const V = Array.from({length:n},(_,i)=>Array.from({length:n},(_,j)=>i===j?1:0));
  for(let sweep=0; sweep<100; sweep++){
    let off=0; for(let p=0;p<n;p++)for(let q=p+1;q<n;q++)off+=A[p][q]*A[p][q];
    if(off < 1e-12) break;
    for(let p=0;p<n;p++)for(let q=p+1;q<n;q++){
      if(Math.abs(A[p][q])<1e-15) continue;
      const th=(A[q][q]-A[p][p])/(2*A[p][q]);
      const t=Math.sign(th||1)/(Math.abs(th)+Math.sqrt(th*th+1));
      const c=1/Math.sqrt(t*t+1), s=t*c;
      for(let k=0;k<n;k++){ const akp=A[k][p],akq=A[k][q]; A[k][p]=c*akp-s*akq; A[k][q]=s*akp+c*akq; }
      for(let k=0;k<n;k++){ const apk=A[p][k],aqk=A[q][k]; A[p][k]=c*apk-s*aqk; A[q][k]=s*apk+c*aqk; }
      for(let k=0;k<n;k++){ const vkp=V[k][p],vkq=V[k][q]; V[k][p]=c*vkp-s*vkq; V[k][q]=s*vkp+c*vkq; }
    }
  }
  return { values: A.map((r,i)=>r[i]), vectors: V };
}

/** Domain co-occurrence weight matrix from shared co-entities. */
export function domainCooccur(docs, coEntities){
  const domById = Object.fromEntries(docs.map(d=>[d.id, d.domain]));
  const domains = [...new Set(docs.map(d=>d.domain))].sort();
  const idx = Object.fromEntries(domains.map((d,i)=>[d,i]));
  const W = domains.map(()=>domains.map(()=>0));
  for(const c of coEntities){
    const doms = [...new Set((c.shared_doc_ids||[]).map(id=>domById[id]).filter(Boolean))];
    for(let a=0;a<doms.length;a++)for(let b=a+1;b<doms.length;b++){
      const i=idx[doms[a]], j=idx[doms[b]]; W[i][j]++; W[j][i]++;
    }
  }
  return { domains, matrix: W };
}

/** Fiedler ordering: 2nd-smallest eigenvector of L=D-W; sort domains by its value.
 *  Sign fixed deterministically (first nonzero component positive). */
export function fiedlerOrder(domains, W){
  const n = domains.length;
  if(n<=1) return [...domains];
  const L = W.map((row,i)=>{ const deg=row.reduce((a,b)=>a+b,0); return row.map((w,j)=> i===j?deg-w:-w); });
  const {values, vectors} = jacobiEigen(L);
  const idxSorted = values.map((v,i)=>[v,i]).sort((a,b)=>a[0]-b[0]).map(x=>x[1]);
  const fied = idxSorted[1];                 // 2nd smallest eigenvalue
  let vec = vectors.map(row=>row[fied]);
  const firstNZ = vec.find(x=>Math.abs(x)>1e-9) || 1;
  if(firstNZ < 0) vec = vec.map(x=>-x);      // deterministic sign
  return domains.map((d,i)=>[d,vec[i]]).sort((a,b)=> (a[1]-b[1]) || (a[0]<b[0]?-1:1)).map(x=>x[0]);
}

/** Domain -> anchor angle, evenly spaced around the circle in Fiedler order. */
export function domainAnchors(docs, coEntities, {strategy='local'}={}){
  const {domains, matrix} = domainCooccur(docs, coEntities);
  const order = strategy==='local' ? fiedlerOrder(domains, matrix)
              : [...domains].sort();          // 'global'/'blend' plug in here later
  const m = order.length || 1;
  const angles = {};
  order.forEach((d,i)=>{ angles[d] = -Math.PI/2 + (i/m)*TAU; });
  return angles;
}
```

- [ ] **Step 4: Run, watch pass** (the path-graph test proves Fiedler puts endpoints farthest apart — the core property).
- [ ] **Step 5: Commit** `feat(viz): segmentum Stage 1a — domain co-occurrence + hand-rolled Fiedler order`.

---

## Task 3: Stage 1b — seed docs at their domain anchor

- [ ] **Step 1: Failing test**

```js
import { seed } from './segmentum-layout.js';
test('seed: doc angle within +/-SEED_SPREAD of its domain anchor; radius = rTarget', () => {
  const docs = resolveDocs([{id:'a',n_entities:4,domain_path:'software/x/y'}]);
  const anchors = {'software/x': 1.0};
  const s = seed(docs, anchors);
  assert.ok(Math.abs(s[0].theta - 1.0) <= (SEED_SPREAD_DEG+1e-6)*Math.PI/180);
  assert.equal(s[0].r, docs[0].rTarget);
});
```

- [ ] **Step 3: Implement**

```js
export function seed(docs, anchors){
  return docs.map(d=>{
    const base = anchors[d.domain] ?? (hash01(d.id)*TAU - Math.PI);
    const jitter = (hash01(d.id+'j')*2-1) * SEED_SPREAD_DEG*DEG;
    return {...d, theta: base + jitter, r: d.rTarget};
  });
}
```

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 1b — seed at domain anchor`.

---

## Task 4: Stage 2 — hand-rolled sim (domain pull + doc↔doc + collision + soft band)

- [ ] **Step 1: Failing tests** — the two traded invariants are the assertions:

```js
import { simulate, buildDocEdges } from './segmentum-layout.js';

test('sim keeps every doc inside its band (radius honest to the band, not the pixel)', () => {
  const docs = seed(resolveDocs(mkDocs(200)), anchorsFor(mkDocs(200)));
  const out = simulate(docs, [], {maxTicks:120});
  for(const d of out) assert.ok(d.r >= d.band[0]-1e-6 && d.r <= d.band[1]+1e-6);
});

test('sim is deterministic (two runs identical)', () => {
  const a = simulate(seed(resolveDocs(mkDocs(50)), {}), [], {maxTicks:60});
  const b = simulate(seed(resolveDocs(mkDocs(50)), {}), [], {maxTicks:60});
  assert.deepEqual(a.map(d=>[d.r,d.theta]), b.map(d=>[d.r,d.theta]));
});

test('doc edges: docs sharing a co-entity are linked, weight = shared count', () => {
  const e = buildDocEdges([{id:'d1'},{id:'d2'}], [{id:'c',shared_doc_ids:['d1','d2']}]);
  assert.equal(e.get('d1').get('d2'), 1);
});
```
(`mkDocs`/`anchorsFor` are small test helpers built from `resolveDocs`/`domainAnchors`.)

- [ ] **Step 3: Implement** — forces + **soft band clamp** (this is the mechanism):

```js
/** doc<->doc weight = # shared co-entities. Map<id, Map<id, w>>. */
export function buildDocEdges(docs, coEntities){
  const g = new Map(docs.map(d=>[d.id,new Map()]));
  for(const c of coEntities){
    const ids = (c.shared_doc_ids||[]).filter(id=>g.has(id));
    for(let i=0;i<ids.length;i++)for(let j=i+1;j<ids.length;j++){
      const a=ids[i],b=ids[j];
      g.get(a).set(b,(g.get(a).get(b)||0)+1);
      g.get(b).set(a,(g.get(b).get(a)||0)+1);
    }
  }
  return g;
}

/** Deterministic force sim in (x,y); radius kept inside the band by a spring + clamp.
 *  Forces: domain-anchor pull (primary angular org), doc<->doc attraction, collision.
 *  No Math.random anywhere. */
export function simulate(docs, coEntities, {maxTicks=300, dotR=6, pad=2,
    kAnchor=0.02, kLink=0.04, kBand=0.15, anchors={}}={}){
  const edges = buildDocEdges(docs, coEntities);
  const P = docs.map(d=>({...d, x:Math.cos(d.theta)*d.r, y:Math.sin(d.theta)*d.r}));
  const byId = new Map(P.map(p=>[p.id,p]));
  for(let t=0;t<maxTicks;t++){
    const fx=new Map(), fy=new Map();
    const add=(id,ax,ay)=>{ fx.set(id,(fx.get(id)||0)+ax); fy.set(id,(fy.get(id)||0)+ay); };
    // anchor pull toward (R_ANCHOR, theta_domain), tangential effect via x/y
    for(const p of P){
      const th = anchors[p.domain]; if(th==null) continue;
      const ax=Math.cos(th)*(R1+R_ANCHOR_MARGIN), ay=Math.sin(th)*(R1+R_ANCHOR_MARGIN);
      add(p.id, (ax-p.x)*kAnchor, (ay-p.y)*kAnchor);
    }
    // doc<->doc attraction (deterministic id order)
    for(const p of P){ for(const [q,w] of edges.get(p.id)){ const o=byId.get(q);
      add(p.id, (o.x-p.x)*kLink*Math.min(1,w/3), (o.y-p.y)*kLink*Math.min(1,w/3)); } }
    // collision, degenerate pairs separated by id order
    for(let i=0;i<P.length;i++)for(let j=i+1;j<P.length;j++){
      const a=P[i],b=P[j]; let dx=b.x-a.x, dy=b.y-a.y; let dist=Math.hypot(dx,dy);
      const min=dotR*2+pad;
      if(dist<min){ if(dist<1e-9){ dx=(a.id<b.id?1:-1); dy=0; dist=1; }
        const push=(min-dist)/2/dist; add(a.id,-dx*push,-dy*push); add(b.id,dx*push,dy*push); }
    }
    // integrate + soft radial band (spring toward rTarget, then CLAMP into band)
    for(const p of P){
      p.x += (fx.get(p.id)||0); p.y += (fy.get(p.id)||0);
      let r=Math.hypot(p.x,p.y)||1e-9, th=Math.atan2(p.y,p.x);
      r = r + (p.rTarget - r)*kBand;                 // spring
      r = Math.min(p.band[1], Math.max(p.band[0], r));// clamp into band
      p.x=Math.cos(th)*r; p.y=Math.sin(th)*r; p.r=r; p.theta=th;
    }
  }
  return P.map(p=>({...p}));
}
```

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 2 — deterministic sim, soft radial band`.

---

## Task 5: Stage 3 — rasterize occupancy (adaptive bandwidth)

- [ ] **Step 1: Failing tests:** a cluster of same-domain docs produces a contiguous block of that domain's cells; an isolated doc of another domain in the middle marks a contested/foreign cell, not a bleed. Assert `rasterize` returns `{A,J,cells}` with `cells[i][j] = {domain, contested}`; bandwidth shrinks when density rises (compare median-NN on sparse vs dense fixture).

- [ ] **Step 3: Implement** `rasterize(points, {A,J,...})`: A=`clamp(round(3*sqrt(n)),64,160)`, J=24; per doc gaussian votes into cells within ~3h using `d²=(Δθ·r_mid)²+Δr²`, `h=1.5×median-NN` clamped; winner=argmax>voteFloor else empty; contested if runner-up≥0.8×winner. Include a `medianNearestNeighbor(points)` helper (tested).

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 3 — occupancy raster, adaptive bandwidth`.

---

## Task 6: Stage 4 — region cleanup

- [ ] **Step 1: Failing tests** on hand-built lattices: despeckle removes a lone cell; a `<minRegionCells` island dissolves into the majority bordering domain; a doc-bearing enclosed region survives as an enclave; `maxEnclaves` caps smallest-first. Angular axis wraps (a region straddling column 0/A-1 is one component); radial does not.

- [ ] **Step 3: Implement** `cleanupRegions(cells,{minRegionCells,maxEnclaves})` — flood-fill with θ-wrap, the four policies, returns cleaned cells + per-domain components + dissolution log.

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 4 — region cleanup`.

---

## Task 7: Stage 5a — boundary extraction (θ-seam + degree-4 pinch, closed loops)

- [ ] **Step 1: Failing tests** (the reviewer's two gaps are explicit cases):

```js
test('edge cancellation yields closed loops for a solid block', () => {
  const cells = block(3,3,'a');           // 3x3 same-domain
  const loops = extractBoundaries(cells).a;
  assert.equal(loops.length, 1);
  loops.forEach(l => assert.ok(closes(l)));
});
test('a domain straddling the theta seam draws NO spurious line at 0/2pi', () => {
  const cells = seamStraddle('a');        // cells at col 0 and col A-1
  const loops = extractBoundaries(cells).a;
  assert.ok(!hasRadialEdgeAt(loops, 0));  // seam-normalized -> cancels
});
test('degree-4 diagonal pinch splits deterministically; every loop closes', () => {
  const cells = diagonalPinch('a');
  const loops = extractBoundaries(cells).a;
  loops.forEach(l => assert.ok(closes(l)));
});
```

- [ ] **Step 3: Implement** `extractBoundaries(cells)`: per domain, each occupied cell emits 4 edges keyed by canonical endpoint coords with **angular index mod A** (seam normalization); delete edges shared by two same-domain cells; link survivors into loops; at a **degree-4 vertex** apply the fixed clockwise-turn split so each region stays on its own side and the walk is degree-2; assert every loop closes. Corner fillets (≈0.35 cell) applied as a post-step (tested for radius ≤ half shorter edge).

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 5a — edge-cancellation boundaries (seam + pinch safe)`.

---

## Task 8: Stage 5b — draw guard (coherence) + `layoutSegmentum` orchestrator

- [ ] **Step 1: Failing tests:** `drawGuard` marks a confetti domain (largest-component/total < coherenceFloor AND total<minSectorCells) as `suppressed`, a coherent one as drawable. `layoutSegmentum(graph,config)` returns `{points, sectors, suppressed, diagnostics}` and is deterministic end-to-end on a fixture.

- [ ] **Step 3: Implement** `drawGuard(components,{coherenceFloor:0.35,minSectorCells:12})` and `layoutSegmentum` chaining S0→S5 with the config object (all defaults from the spec's Parameters table). Small-N path: below a threshold, skip rasterization and render points only (spec's staged delivery handles tiny entities gracefully — a 10-doc entity needs no territories).

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 5b — draw guard + layoutSegmentum orchestrator`.

---

## Task 9: Wire `star.html` + draw territories in `renderers/star.js`

**Files:** modify both. Integration glue; validated visually in Task 10.

- [ ] **Step 1:** In `star.html`, import `* as SEG from './core/segmentum-layout.js'`; in `buildStarView`, replace the `SL.layoutStar` block with `const seg = SEG.layoutSegmentum(graph)`, position `docs[]` from `seg.points` (carry `domain`, `domain_path`), place co-entities as before (band from v1 stays — co-entities are not part of the territory), and stash `window.__SEG__ = seg; window.__STAR_PALETTE__ = graph.palette || {}`.
- [ ] **Step 2:** In `renderers/star.js`, replace `drawSectors` with `drawTerritories(ctx, seg, cx, cy, view)`: for each **drawable** sector, fill its cells (one path, ~8–12% alpha, `sectorColor`), stroke the filleted boundary loops (~1px, ~80%), label the largest component; contested cells `'gap'` by default; suppressed domains draw nothing (their docs still coloured). Colour docs by domain (keep). Hover: pointer → cell → domain (or hovered doc's domain) → highlight domain, dim rest.
- [ ] **Step 3:** Call `drawTerritories` before `drawDocuments` in the render loop (guard null `__SEG__`).
- [ ] **Step 4: Manual smoke** — serve viz (Task 10 recipe), open a large + small entity, DevTools console: no errors; territories render; small entity shows points only.
- [ ] **Step 5: Commit** `feat(viz): wire star page to segmentum layout + draw territories`.

---

## Task 10: Run it for visual review

- [ ] **Step 1:** Full suites — `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py -q` (backend unchanged, still green) and `cd frontend/public/viz/core && node --test segmentum-layout.test.mjs`.
- [ ] **Step 2:** Serve viz: `cd frontend/public/viz && python3 -m http.server 8795` (Next swallows `/viz/`, so serve directly). Orchestrator already runs on :8100 with `get_star_graph`.
- [ ] **Step 3:** Headless-render (reuse `scratchpad/star_render.mjs`, point `window.__SEG__`) openai `3a18e649-…`, gemini, docker, a <20-doc entity; assert 0 page errors and that large entities show coherent territories (few components per domain), matching the prototype's Variant-C numbers.
- [ ] **Step 4:** Hand the URLs to the user for feedback: `http://localhost:8795/star.html?entity=<id>&workspace=default&api=http://localhost:8100`. Tuning knobs: `BAND`, `kAnchor/kLink/kBand`, `coherenceFloor`, contested style, fillet radius.

---

## Task 11: Remove the dead v1 path

- [ ] After Task 10 is accepted: delete `star-layout.js`'s ring/packing exports + their tests; keep the shared helpers (moved to `segmentum-layout.js` or re-exported). Run `node --test` + `tsc`/build. Commit `chore(viz): remove v1 arc-packing path superseded by segmentum view`.

---

## Notes for the executor

- **All layout math lives in `segmentum-layout.js` and is `node --test`'d.** The renderer and `star.html` must not re-derive an angle, radius, or cell — if you compute geometry in the renderer, it belongs in the module with a test.
- **The two traded invariants are load-bearing, not bugs** (spec): radius is honest to the *band* (±BAND/2), and domains drive placement via soft forces. Do not "restore" hard radial pinning or remove domain anchoring — that reintroduces the confetti the prototype measured.
- **Determinism is a hard requirement:** no `Math.random`; hashed jitter/tiebreaks; the sim and Fiedler are deterministic. Acceptance #2 asserts byte-identical runs.
- **Follow CLAUDE.md:** viz stays self-contained ESM (no bundler, no new npm dep — Fiedler is hand-rolled). Backend fixtures are file-backed, never `:memory:`.
