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

**Deleted (after the new path works):** `core/star-layout.js`'s **ring/packing only** — `assignRings`, `packRings`, `layoutStar`, and the `RING_TARGETS/RING_RADII/RING_BAND` constants — plus their tests. **Moved into `segmentum-layout.js`** (so nothing re-implements them and the delete is clean): `midLevel`, `orderDomains`, `sectorColor`, `documentStrength`, `coEntityBand`, `MISC_LABEL`, `MISC_COLOR`. The co-entity band is **kept** (co-entities are not part of the territory) and re-based on `R1` instead of the deleted `RING_OUTER`. So after this work, `renderers/star.js` and `star.html` import these from `segmentum-layout.js`, and `star-layout.js` is gone entirely.

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
    // band clamped to [r0,r1] (spec Stage 0) so extremes don't spill past the map radii
    const b0 = Math.max(r0, rTarget-band/2), b1 = Math.min(r1, rTarget+band/2);
    return {...d, p, rTarget, band:[b0, b1]};
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

test('fiedler returns null on a DISCONNECTED domain graph (>=2 zero eigenvalues)', () => {
  // two components: a-b and c-d, no cross edge
  const domains = ['a','b','c','d'];
  const W = [[0,1,0,0],[1,0,0,0],[0,0,0,1],[0,0,1,0]];
  assert.equal(fiedlerOrder(domains, W), null);
});

test('anchors: disconnected graph -> count-order even spacing, all angles distinct', () => {
  // 'a' has 3 docs, 'b' has 1; no shared co-entity -> disconnected -> count order
  const docs = [{id:'1',domain:'a'},{id:'2',domain:'a'},{id:'3',domain:'a'},{id:'4',domain:'b'}];
  const a = domainAnchors(docs, []);
  assert.ok(a.a !== undefined && a.b !== undefined && a.a !== a.b);
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
 *  Returns null when the graph is DISCONNECTED (>=2 near-zero eigenvalues) — the
 *  Fiedler vector is then an arbitrary null-space vector, so the caller falls back to
 *  count-order even spacing (spec Stage 1). Sign fixed deterministically. */
export function fiedlerOrder(domains, W){
  const n = domains.length;
  if(n<=1) return [...domains];
  const L = W.map((row,i)=>{ const deg=row.reduce((a,b)=>a+b,0); return row.map((w,j)=> i===j?deg-w:-w); });
  const {values, vectors} = jacobiEigen(L);
  const idxSorted = values.map((v,i)=>[v,i]).sort((a,b)=> (a[0]-b[0]) || (a[1]-b[1])).map(x=>x[1]);
  const EPS = 1e-6;
  const nearZero = idxSorted.filter(i=>Math.abs(values[i])<EPS).length;
  if(nearZero >= 2) return null;             // disconnected -> caller uses fallback
  const fied = idxSorted[1];                 // 2nd smallest eigenvalue
  let vec = vectors.map(row=>row[fied]);     // column `fied` of V = the eigenvector
  const firstNZ = vec.find(x=>Math.abs(x)>1e-9) || 1;
  if(firstNZ < 0) vec = vec.map(x=>-x);      // deterministic sign
  return domains.map((d,i)=>[d,vec[i]]).sort((a,b)=> (a[1]-b[1]) || (a[0]<b[0]?-1:1)).map(x=>x[0]);
}

/** Domain -> anchor angle, evenly spaced around the circle in Fiedler order.
 *  Fallback (disconnected/single/no-edges): order by doc count desc, id tiebreak. */
export function domainAnchors(docs, coEntities, {strategy='local'}={}){
  const {domains, matrix} = domainCooccur(docs, coEntities);
  let order = strategy==='local' ? fiedlerOrder(domains, matrix) : null;
  if(order == null){                          // disconnected, or non-local strategy
    const count = {}; for(const d of docs) count[d.domain]=(count[d.domain]||0)+1;
    order = [...domains].sort((a,b)=> (count[b]-count[a]) || (a<b?-1:1));
  }
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

- [ ] **Step 3: Implement** `extractBoundaries(cells)`. Precise scheme (this is the hardest task — no hand-waving):

  **Vertex + edge keys.** A cell `(i,j)` (i = angular index 0..A-1, j = radial ring 0..J-1) has 4 corner vertices `(i, j)`, `(i+1, j)`, `(i, j+1)`, `(i+1, j+1)` where the vertex angular index is taken **mod A** (this is the seam normalization: the right edge of cell `(A-1, j)` uses vertex angular index `A mod A = 0`, identical to the left edge of cell `(0, j)`, so a seam-straddling domain's edges cancel). An edge is keyed by its two vertices as a canonical sorted string `"${a_i},${a_j}|${b_i},${b_j}"`.
  Cell `(i,j)`'s 4 edges: inner-arc `[(i,j),(i+1,j)]`, outer-arc `[(i,j+1),(i+1,j+1)]`, cw-radial `[(i+1,j),(i+1,j+1)]`, ccw-radial `[(i,j),(i,j+1)]`.

  **Cancellation.** Per domain, XOR the edge multiset: an edge shared by two same-domain cells appears twice and is dropped; survivors are the boundary. (∂∂ = 0, so survivors always form cycles.)

  **Walk into loops.** Build an adjacency `vertex → [neighbour vertices]` from surviving edges. Most vertices have degree 2. Walk: pick an unused edge, follow degree-2 vertices until back to start.

  **Degree-4 pinch (worked example).** A vertex `v` has degree 4 only at a diagonal touch: same-domain cells at `(i,j)` and `(i+1,j+1)`, foreign/empty at `(i+1,j)` and `(i,j+1)` (or the mirror). The four surviving edges meet at `v`. Split deterministically by **connecting the two edges that keep each same-domain cell's interior on the left as you traverse clockwise** — concretely, pair the edge entering from cell `(i,j)`'s side with the edge leaving along `(i,j)`'s other boundary, and likewise for `(i+1,j+1)`, so the two same-domain cells become two separate degree-2 corners rather than an X-crossing. This yields two loops that touch at `v` but never cross, and every loop closes (asserted).

  Corner fillets (≈0.35 cell) applied as a post-step, radius ≤ half the shorter adjacent edge.

  **Test helpers to define in the test file:** `block(w,h,dom)` → a `cells` grid with a solid `w×h` same-domain rectangle; `seamStraddle(dom)` → cells occupied at columns `0` and `A-1` of one row; `diagonalPinch(dom)` → the 2×2 diagonal-touch pattern above; `closes(loop)` → asserts the loop's last vertex equals its first; `hasRadialEdgeAt(loops, angIdx)` → whether any loop contains a radial edge at angular index `angIdx`.

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 5a — edge-cancellation boundaries (seam + pinch safe)`.

---

## Task 8: Stage 5b — draw guard (coherence) + `layoutSegmentum` orchestrator

- [ ] **Step 1: Failing tests:** `drawGuard` marks a confetti domain (largest-component/total < coherenceFloor AND total<minSectorCells) as `suppressed`, a coherent one as drawable. `layoutSegmentum(graph,config)` returns `{points, sectors, suppressed, diagnostics}` and is deterministic end-to-end on a fixture.

- [ ] **Step 3: Implement** `drawGuard(components,{coherenceFloor:0.35,minSectorCells:12})` and `layoutSegmentum` chaining S0→S5 with the config object (all defaults from the spec's Parameters table). Small-N path: below a threshold, skip rasterization and render points only (spec's staged delivery handles tiny entities gracefully — a 10-doc entity needs no territories).

- [ ] **Steps 4–5:** pass; commit `feat(viz): segmentum Stage 5b — draw guard + layoutSegmentum orchestrator`.

---

## Task 9: Wire `star.html` + draw territories in `renderers/star.js`

**Files:** modify both. Integration glue — the load-bearing risk is preserving `star.html`'s downstream consumers (this class of gap bit the v1 plan), so each is called out explicitly. Validated visually in Task 10.

- [ ] **Step 1 — star.html imports + `buildStarView`.** Replace `import * as SL from './core/star-layout.js'` with `import * as SEG from './core/segmentum-layout.js'`. In `buildStarView`: `const seg = SEG.layoutSegmentum(graph);` then position `docs[]` from `seg.points` — each carries `id`, `domain`, `domain_path`, `theta`, `r`; compute `x = cx + cos(theta)*r`, `y = sin(theta)*r`, and set `_px/_py = x/y` (hit-testing + `drawConnections` read these — preserve them). Build `docIndex`/`docById` and a `docAngle` map (`id → theta`) from `seg.points` — the co-entity band needs it.
- [ ] **Step 2 — co-entity band (kept, re-based).** Keep the existing co-entity block but source its helpers from `SEG`: `SEG.coEntityBand(filteredCo, docAngle, { rInner: SEG.R1 + 60, rOuter: SEG.R1 + 60 + Math.min(200, 40 + filteredCo.length) })` (replaces the deleted `SL.RING_OUTER`). Preserve the `labelThreshold` computation and every field on the pushed co-entity object (`sharedDocIds`, `labelThreshold`, `_px/_py`) — `renderers/star.js:176` reads `labelThreshold` for resting labels.
- [ ] **Step 3 — stash for the renderer + hover.** `window.__SEG__ = seg; window.__STAR_PALETTE__ = graph.palette || {}`. **Rewrite the pointer-hover block** (currently ~star.html:408-423, which reads `window.__STAR_LAYOUT__.rings` + `SL.RING_RADII/RING_BAND`): a hovered doc → `hit.domain`; else map the pointer to a lattice cell via `seg` (angular index from `atan2`, radial ring from `r` vs `[R0,R1]`) and read that cell's domain from `seg.sectors`/the raster. Set `window.__STAR_HOVER__ = domain`. Delete all `__STAR_LAYOUT__`/`RING_*` references.
- [ ] **Step 4 — renderer.** In `renderers/star.js`: repoint the import `{ sectorColor, MISC_COLOR }` to `../core/segmentum-layout.js` and **remove** `RING_RADII`/`RING_BAND` from it and the now-dead `bandInner/bandOuter` helpers. Replace `drawSectors` with `drawTerritories(ctx, seg, cx, cy, view)`: for each **drawable** sector fill its cells (one path, ~8–12% alpha, `sectorColor`), stroke the filleted loops (~1px, ~80%), label the largest component; contested `'gap'`; **suppressed domains draw nothing** (docs still coloured by domain via the existing `docColor`). Keep the doc domain-colouring and the hovered-domain dimming in `drawDocuments`.
- [ ] **Step 5 — render loop.** Call `drawTerritories(ctx, window.__SEG__, 2500, 2500, view)` before `drawDocuments`, guarding null `__SEG__`.
- [ ] **Step 6 — manual smoke.** Serve viz (Task 10), open a large + small entity; DevTools console: 0 errors; territories render; hover (both a doc and empty sector space) highlights the domain; small entity shows points only.
- [ ] **Step 7: Commit** `feat(viz): wire star page to segmentum layout + draw territories`.

---

## Task 10: Run it for visual review

- [ ] **Step 1:** Full suites — `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py -q` (backend unchanged, still green) and `cd frontend/public/viz/core && node --test segmentum-layout.test.mjs`.
- [ ] **Step 2:** Serve viz: `cd frontend/public/viz && python3 -m http.server 8795` (Next swallows `/viz/`, so serve directly). Orchestrator already runs on :8100 with `get_star_graph`.
- [ ] **Step 3:** Headless-render openai `3a18e649-8d6c-4888-86dc-8dbc8fb78f40`, gemini, docker, a <20-doc entity; assert 0 page errors and coherent territories (few components per domain) on the large ones, matching the prototype's Variant-C numbers. Use a small playwright driver that loads the URL, waits for the `Star loaded` console line, reads `window.__SEG__` (mode, per-domain component counts, suppressed list) and screenshots. A working driver from this project's prior session lives at `/private/tmp/claude-501/-Users-michaelsugimura-Documents-GitHub-Noospheric-Orrery/311265c4-af13-44b5-b634-33cb93efeeec/scratchpad/star_render.mjs` (env: `PW`=playwright-core path, `EXE`=chrome-headless-shell, `URL`, `OUT`); adapt it to read `__SEG__` instead of `__STAR_LAYOUT__`, or write an equivalent ~20-line driver if that path is gone.
- [ ] **Step 4:** Hand the URLs to the user for feedback: `http://localhost:8795/star.html?entity=<id>&workspace=default&api=http://localhost:8100`. Tuning knobs: `BAND`, `kAnchor/kLink/kBand`, `coherenceFloor`, contested style, fillet radius.

---

## Task 11: Remove the dead v1 path

- [ ] After Task 10 is accepted: the shared helpers (`midLevel`, `orderDomains`, `sectorColor`, `documentStrength`, `coEntityBand`, `MISC_*`) already live in `segmentum-layout.js` (moved in Tasks 1–8), and Task 9 already repointed every importer (`star.html`, `renderers/star.js`) off `star-layout.js`. So this task **deletes `core/star-layout.js` and `core/star-layout.test.mjs` outright** and greps to confirm no remaining `star-layout` import anywhere under `frontend/public/viz/`. Run `node --test core/segmentum-layout.test.mjs` (green) and reload the app once. Commit `chore(viz): remove v1 arc-packing path superseded by segmentum view`.

---

## Notes for the executor

- **All layout math lives in `segmentum-layout.js` and is `node --test`'d.** The renderer and `star.html` must not re-derive an angle, radius, or cell — if you compute geometry in the renderer, it belongs in the module with a test.
- **The two traded invariants are load-bearing, not bugs** (spec): radius is honest to the *band* (±BAND/2), and domains drive placement via soft forces. Do not "restore" hard radial pinning or remove domain anchoring — that reintroduces the confetti the prototype measured.
- **Determinism is a hard requirement:** no `Math.random`; hashed jitter/tiebreaks; the sim and Fiedler are deterministic. Acceptance #2 asserts byte-identical runs.
- **Follow CLAUDE.md:** viz stays self-contained ESM (no bundler, no new npm dep — Fiedler is hand-rolled). Backend fixtures are file-backed, never `:memory:`.
