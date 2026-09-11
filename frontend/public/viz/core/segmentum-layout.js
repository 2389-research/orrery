/**
 * Segmentum view layout — place first, derive territory after. Pure ESM, no DOM/canvas;
 * every stage is node --test'd in segmentum-layout.test.mjs. star.html runs
 * layoutSegmentum() and the renderer draws the result.
 *
 * Model (empirically validated "Variant C" — see
 * docs/superpowers/specs/2026-09-10-entity-segmentum-view-design.md):
 *   radius = a relevance BAND (soft, not pinned), angle = domain-anchor pull +
 *   doc<->doc connectivity via a deterministic force sim; domain territories are
 *   RASTERIZED after placement, never a layout input.
 * Two invariants are knowingly traded and must not be "restored": radius is honest to
 * the band (+/-BAND/2), and domains drive placement via soft forces.
 */

// ── constants (shared by tests + renderer) ──────────────────────────────────────
export const R0 = 60, R1 = 500, BAND = 90;
export const R_ANCHOR_MARGIN = 40;
export const SEED_SPREAD_DEG = 8;
export const MISC_LABEL = 'misc', MISC_COLOR = '#6d7280';
const TAU = Math.PI * 2, DEG = Math.PI / 180;

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

// deterministic hash -> [0,1); no Math.random anywhere in this module
function hash01(s) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0) / 4294967296;
}

export function midLevel(p) {
  if (!p) return MISC_LABEL;
  const a = p.split('/');
  return a.length >= 2 ? a.slice(0, 2).join('/') : p;
}

// ── Stage 0: relevance -> radius band ───────────────────────────────────────────
/** rel = 1/n_entities; rank+sqrt -> rTarget (most relevant innermost); band clamped
 *  to [r0,r1]. Ties in rel broken by id so percentiles are distinct (no radial clot). */
export function resolveDocs(docs, { r0 = R0, r1 = R1, band = BAND, radiusMode = 'sqrt' } = {}) {
  const withRel = docs.map(d => ({ ...d, rel: 1 / (d.n_entities || 1), domain: midLevel(d.domain_path) }));
  const order = [...withRel].sort((a, b) => (b.rel - a.rel) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const n = order.length || 1;
  // 'value' normalizes by the ACTUAL relevance range, so radius reflects real relevance:
  // the single LEAST-relevant doc sits at the rim (r1), the most relevant at r0, and docs
  // that share a relevance sit at the same radius (jittered by the sim) instead of being
  // spread out just to fill the disk. No pile-up at the edge unless many truly tie there.
  const relMax = Math.max(...withRel.map(d => d.rel)), relMin = Math.min(...withRel.map(d => d.rel));
  const relRange = (relMax - relMin) || 1;
  return order.map((d, i) => {
    const p = n > 1 ? i / (n - 1) : 0;
    // 'sqrt' = equal-AREA density (edge-heavy per radius; good behind a partition).
    // 'even' = equal count per radius band (rank-linear).
    // 'value' = by relevance value, normalized to the farthest (no rim clump; ties share r).
    const rTarget = radiusMode === 'value' ? r0 + (r1 - r0) * (relMax - d.rel) / relRange
      : radiusMode === 'even' ? r0 + (r1 - r0) * p
        : r0 + (r1 - r0) * Math.sqrt(p);
    const b0 = Math.max(r0, rTarget - band / 2), b1 = Math.min(r1, rTarget + band / 2);
    return { ...d, p, rTarget, band: [b0, b1] };
  });
}

// ── Stage 1a: domain co-occurrence + Fiedler order (hand-rolled, no numpy) ───────
/** Symmetric eigendecomposition via cyclic Jacobi rotations. Returns {values, vectors}
 *  with eigenvectors as COLUMNS of `vectors`. Deterministic; n is small (domain count). */
function jacobiEigen(Ain) {
  const n = Ain.length;
  const A = Ain.map(r => r.slice());
  const V = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => i === j ? 1 : 0));
  for (let sweep = 0; sweep < 100; sweep++) {
    let off = 0;
    for (let p = 0; p < n; p++) for (let q = p + 1; q < n; q++) off += A[p][q] * A[p][q];
    if (off < 1e-12) break;
    for (let p = 0; p < n; p++) for (let q = p + 1; q < n; q++) {
      if (Math.abs(A[p][q]) < 1e-15) continue;
      const th = (A[q][q] - A[p][p]) / (2 * A[p][q]);
      const t = Math.sign(th || 1) / (Math.abs(th) + Math.sqrt(th * th + 1));
      const c = 1 / Math.sqrt(t * t + 1), s = t * c;
      for (let k = 0; k < n; k++) { const akp = A[k][p], akq = A[k][q]; A[k][p] = c * akp - s * akq; A[k][q] = s * akp + c * akq; }
      for (let k = 0; k < n; k++) { const apk = A[p][k], aqk = A[q][k]; A[p][k] = c * apk - s * aqk; A[q][k] = s * apk + c * aqk; }
      for (let k = 0; k < n; k++) { const vkp = V[k][p], vkq = V[k][q]; V[k][p] = c * vkp - s * vkq; V[k][q] = s * vkp + c * vkq; }
    }
  }
  return { values: A.map((r, i) => r[i]), vectors: V };
}

/** Domain co-occurrence weight matrix from shared co-entities. */
export function domainCooccur(docs, coEntities) {
  const domById = Object.fromEntries(docs.map(d => [d.id, d.domain]));
  const domains = [...new Set(docs.map(d => d.domain))].sort();
  const idx = Object.fromEntries(domains.map((d, i) => [d, i]));
  const W = domains.map(() => domains.map(() => 0));
  for (const c of (coEntities || [])) {
    const doms = [...new Set((c.shared_doc_ids || []).map(id => domById[id]).filter(Boolean))];
    for (let a = 0; a < doms.length; a++) for (let b = a + 1; b < doms.length; b++) {
      const i = idx[doms[a]], j = idx[doms[b]]; W[i][j]++; W[j][i]++;
    }
  }
  return { domains, matrix: W };
}

/** Fiedler ordering: 2nd-smallest eigenvector of L=D-W; sort domains by its value.
 *  Returns null when the graph is DISCONNECTED (>=2 near-zero eigenvalues) — the
 *  Fiedler vector is then arbitrary, so the caller uses a count-order fallback.
 *  Sign fixed deterministically (first non-zero component positive). */
export function fiedlerOrder(domains, W) {
  const n = domains.length;
  if (n <= 1) return [...domains];
  const L = W.map((row, i) => { const deg = row.reduce((a, b) => a + b, 0); return row.map((w, j) => i === j ? deg - w : -w); });
  const { values, vectors } = jacobiEigen(L);
  const idxSorted = values.map((v, i) => [v, i]).sort((a, b) => (a[0] - b[0]) || (a[1] - b[1])).map(x => x[1]);
  const EPS = 1e-6;
  const nearZero = idxSorted.filter(i => Math.abs(values[i]) < EPS).length;
  if (nearZero >= 2) return null;
  const fied = idxSorted[1];
  let vec = vectors.map(row => row[fied]);        // column `fied` of V = the eigenvector
  const firstNZ = vec.find(x => Math.abs(x) > 1e-9) || 1;
  if (firstNZ < 0) vec = vec.map(x => -x);
  return domains.map((d, i) => [d, vec[i]]).sort((a, b) => (a[1] - b[1]) || (a[0] < b[0] ? -1 : 1)).map(x => x[0]);
}

/** Domain -> anchor angle, evenly spaced around the circle in Fiedler order.
 *  Fallback (disconnected/non-local strategy): order by doc count desc, id tiebreak. */
export function domainAnchors(docs, coEntities, { strategy = 'local' } = {}) {
  const { domains, matrix } = domainCooccur(docs, coEntities);
  let order = strategy === 'local' ? fiedlerOrder(domains, matrix) : null;
  if (order == null) {
    const count = {};
    for (const d of docs) count[d.domain] = (count[d.domain] || 0) + 1;
    order = [...domains].sort((a, b) => (count[b] - count[a]) || (a < b ? -1 : 1));
  }
  const m = order.length || 1;
  const angles = {};
  order.forEach((d, i) => { angles[d] = -Math.PI / 2 + (i / m) * TAU; });
  return angles;
}

// ── Stage 1b: seed docs at their domain anchor ──────────────────────────────────
export function seed(docs, anchors) {
  return docs.map(d => {
    const base = anchors[d.domain] ?? (hash01(d.id) * TAU - Math.PI);
    const jitter = (hash01(d.id + 'j') * 2 - 1) * SEED_SPREAD_DEG * DEG;
    return { ...d, theta: base + jitter, r: d.rTarget };
  });
}

// ── Stage 2: deterministic sim, soft radial band ────────────────────────────────
/** doc<->doc weight = # shared co-entities. Map<id, Map<id, w>>. */
export function buildDocEdges(docs, coEntities) {
  const g = new Map(docs.map(d => [d.id, new Map()]));
  for (const c of (coEntities || [])) {
    const ids = (c.shared_doc_ids || []).filter(id => g.has(id));
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
      const a = ids[i], b = ids[j];
      g.get(a).set(b, (g.get(a).get(b) || 0) + 1);
      g.get(b).set(a, (g.get(b).get(a) || 0) + 1);
    }
  }
  return g;
}

/** Force sim in (x,y); radius kept inside the band by a spring + clamp. Forces:
 *  domain-anchor pull (primary angular organizer), doc<->doc attraction, collision.
 *  No Math.random; degenerate collisions separated by id order -> byte-deterministic. */
export function simulate(docs, coEntities, { maxTicks = 300, dotR = 6, pad = 2,
    kAnchor = 0.02, kLink = 0.04, kBand = 0.15, kRepel = 0, repelR = 0, anchors = {},
    kCo = 0, coPos = null, docCo = null } = {}) {
  const edges = buildDocEdges(docs, coEntities);
  const P = docs.map(d => ({ ...d, x: Math.cos(d.theta) * d.r, y: Math.sin(d.theta) * d.r }));
  const byId = new Map(P.map(p => [p.id, p]));
  const rr2 = repelR * repelR;
  for (let t = 0; t < maxTicks; t++) {
    const fx = new Map(), fy = new Map();
    const add = (id, ax, ay) => { fx.set(id, (fx.get(id) || 0) + ax); fy.set(id, (fy.get(id) || 0) + ay); };
    // domain-anchor pull (skipped when kAnchor is 0 — the domain-free organic view)
    if (kAnchor > 0) for (const p of P) {
      const th = anchors[p.domain]; if (th == null) continue;
      const ax = Math.cos(th) * (R1 + R_ANCHOR_MARGIN), ay = Math.sin(th) * (R1 + R_ANCHOR_MARGIN);
      add(p.id, (ax - p.x) * kAnchor, (ay - p.y) * kAnchor);
    }
    for (const p of P) for (const [q, w] of edges.get(p.id)) {
      const o = byId.get(q);
      add(p.id, (o.x - p.x) * kLink * Math.min(1, w / 3), (o.y - p.y) * kLink * Math.min(1, w / 3));
    }
    // Spring each doc toward EACH co-entity it connects to (co-entities are fixed rim
    // anchors). A doc settles at the balance of its real connections — its natural point
    // — rather than at a bland membership-mean seed. Split across its co-entities so a
    // highly-connected doc isn't yanked harder than a sparse one.
    if (kCo > 0 && coPos && docCo) for (const p of P) {
      const cids = docCo.get(p.id); if (!cids || !cids.length) continue;
      const k = kCo / cids.length;
      for (const cid of cids) { const c = coPos.get(cid); if (c) add(p.id, (c.x - p.x) * k, (c.y - p.y) * k); }
    }
    // pairwise repulsion — spreads docs to FILL 2D area (so labels aren't stacked).
    // Only within repelR so it stays local and O(n²) stays cheap enough at ~600 docs.
    if (kRepel > 0) for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
      const a = P[i], b = P[j]; let dx = a.x - b.x, dy = a.y - b.y; const d2 = dx * dx + dy * dy;
      if (d2 > rr2 || d2 < 1e-9) { if (d2 < 1e-9) { dx = (a.id < b.id ? 1 : -1); dy = 0; } else continue; }
      const f = kRepel / (d2 + 100); add(a.id, dx * f, dy * f); add(b.id, -dx * f, -dy * f);
    }
    for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
      const a = P[i], b = P[j]; let dx = b.x - a.x, dy = b.y - a.y; let dist = Math.hypot(dx, dy);
      const min = dotR * 2 + pad;
      if (dist < min) {
        if (dist < 1e-9) { dx = (a.id < b.id ? 1 : -1); dy = 0; dist = 1; }
        const push = (min - dist) / 2 / dist; add(a.id, -dx * push, -dy * push); add(b.id, dx * push, dy * push);
      }
    }
    for (const p of P) {
      p.x += (fx.get(p.id) || 0); p.y += (fy.get(p.id) || 0);
      let r = Math.hypot(p.x, p.y) || 1e-9; const th = Math.atan2(p.y, p.x);
      r = r + (p.rTarget - r) * kBand;                     // spring toward relevance
      r = Math.min(p.band[1], Math.max(p.band[0], r));     // clamp INTO the band
      p.x = Math.cos(th) * r; p.y = Math.sin(th) * r; p.r = r; p.theta = th;
    }
  }
  return P.map(p => ({ ...p }));
}

/** Convenience: run Stages 0-2 and return placed points + the anchors used. */
export function placeDocs(graph, config = {}) {
  const docs0 = resolveDocs(graph.documents || [], config);
  const anchors = domainAnchors(docs0, graph.co_entities || [], config);
  const seeded = seed(docs0, anchors);
  const points = simulate(seeded, graph.co_entities || [], { ...config, anchors });
  return { points, anchors };
}

// ── cell geometry (lattice is a rendering device, unrelated to relevance) ────────
export function cellAngle(i, A) { return -Math.PI + (i + 0.5) / A * TAU; }   // centre
export function cellRadius(j, J, r0 = R0, r1 = R1) { return r0 + (j + 0.5) / J * (r1 - r0); }
function angIndex(theta, A) { let t = ((theta + Math.PI) % TAU + TAU) % TAU; return Math.floor(t / TAU * A) % A; }
function radIndex(r, J, r0 = R0, r1 = R1) { return clamp(Math.floor((r - r0) / (r1 - r0) * J), 0, J - 1); }

export function medianNearestNeighbor(points) {
  if (points.length < 2) return 1;
  const d = points.map(p => {
    let best = Infinity;
    for (const q of points) { if (q === p) continue; const dx = q.x - p.x, dy = q.y - p.y; const dd = dx * dx + dy * dy; if (dd < best) best = dd; }
    return Math.sqrt(best);
  }).sort((a, b) => a - b);
  return d[Math.floor(d.length / 2)] || 1;
}

// ── Stage 3: rasterize as a KNN / Voronoi partition ─────────────────────────────
/** Assign EVERY lattice cell to the domain that owns the majority of its K nearest
 *  documents (distance-weighted), so the whole disk is partitioned into contiguous
 *  territories with no dead void — the Warhammer-40k segmentum look. `contested` marks
 *  a cell where the top-two domains are close. Cells beyond `reach` from any doc are
 *  left null (a genuinely empty far region), controlled by `maxReachMul` × median-NN.
 *  Returns {A,J,cells} with cells[i][j] = {domain, contested} | null. */
export function rasterize(points, { A, J = 24, r0 = R0, r1 = R1, K = 8, contestRatio = 0.7,
    maxReachMul = 6 } = {}) {
  const n = points.length;
  A = A ?? clamp(Math.round(3 * Math.sqrt(n)), 64, 160);
  if (!n) return { A, J, cells: Array.from({ length: A }, () => Array(J).fill(null)) };
  // reach floored at a few cell-diagonals so coincident/dense docs (median-NN -> 0)
  // don't collapse it to zero and blank the whole disk.
  const cellDiag = Math.hypot(TAU / A * (r0 + r1) / 2, (r1 - r0) / J);
  const reach = Math.max(maxReachMul * medianNearestNeighbor(points), 4 * cellDiag);
  const reach2 = reach * reach;
  const cells = Array.from({ length: A }, (_, i) => Array.from({ length: J }, (_, j) => {
    const th = cellAngle(i, A), r = cellRadius(j, J, r0, r1);
    const cx = Math.cos(th) * r, cy = Math.sin(th) * r;
    // K nearest docs to this cell centre (small insertion buffer)
    const best = [];   // [{d2, domain}], kept sorted ascending, length <= K
    for (const p of points) {
      const dx = p.x - cx, dy = p.y - cy, d2 = dx * dx + dy * dy;
      if (best.length < K) { best.push({ d2, domain: p.domain }); best.sort((a, b) => a.d2 - b.d2); }
      else if (d2 < best[K - 1].d2) { best[K - 1] = { d2, domain: p.domain }; best.sort((a, b) => a.d2 - b.d2); }
    }
    if (!best.length || best[0].d2 > reach2) return null;   // nothing within reach -> empty
    // distance-weighted vote among the K nearest
    const tally = new Map();
    for (const b of best) { const w = 1 / (1 + b.d2); tally.set(b.domain, (tally.get(b.domain) || 0) + w); }
    let win = null, w1 = 0, w2 = 0;
    for (const [dom, w] of tally) { if (w > w1) { w2 = w1; w1 = w; win = dom; } else if (w > w2) w2 = w; }
    return { domain: win, contested: w2 >= contestRatio * w1 };
  }));
  return { A, J, cells };
}

// ── Stage 4: region cleanup (θ wraps; radial does not) ──────────────────────────
function neighbours4(i, j, A, J) {
  return [[(i + 1) % A, j], [(i - 1 + A) % A, j], [i, j + 1], [i, j - 1]]
    .filter(([, jj]) => jj >= 0 && jj < J);
}
/** Per-domain connected components (4-connected, θ-wrap). */
function components(cells, A, J, domain) {
  const seen = Array.from({ length: A }, () => Array(J).fill(false));
  const comps = [];
  for (let i = 0; i < A; i++) for (let j = 0; j < J; j++) {
    if (seen[i][j] || !cells[i][j] || cells[i][j].domain !== domain) continue;
    const comp = []; const stack = [[i, j]]; seen[i][j] = true;
    while (stack.length) {
      const [ci, cj] = stack.pop(); comp.push([ci, cj]);
      for (const [ni, nj] of neighbours4(ci, cj, A, J))
        if (!seen[ni][nj] && cells[ni][nj] && cells[ni][nj].domain === domain) { seen[ni][nj] = true; stack.push([ni, nj]); }
    }
    comps.push(comp);
  }
  return comps;
}
export function cleanupRegions(raster, { minRegionCells = 4, maxEnclaves = 3, fillWhole = true } = {}) {
  const { A, J } = raster;
  const cells = raster.cells.map(col => col.map(c => c ? { ...c } : null));
  // Reassign a component's cells to the majority BORDERING domain. In fillWhole mode
  // (the KNN/Voronoi partition) never leave a hole — fall back to any bordering domain;
  // only null when the component truly has no neighbour (isolated in empty space).
  const dissolve = (comp, dom) => {
    const border = {};
    for (const [ci, cj] of comp) for (const [ni, nj] of neighbours4(ci, cj, A, J)) {
      const nc = cells[ni][nj]; if (nc && nc.domain !== dom) border[nc.domain] = (border[nc.domain] || 0) + 1;
    }
    const maj = Object.entries(border).sort((a, b) => b[1] - a[1])[0];
    const to = maj ? maj[0] : null;
    for (const [ci, cj] of comp) cells[ci][cj] = to ? { domain: to, contested: false } : (fillWhole ? cells[ci][cj] : null);
  };
  // 1. despeckle (a lone cell with no same-domain neighbour dissolves into a neighbour)
  for (let i = 0; i < A; i++) for (let j = 0; j < J; j++) {
    const c = cells[i][j]; if (!c) continue;
    if (!neighbours4(i, j, A, J).some(([ni, nj]) => cells[ni][nj] && cells[ni][nj].domain === c.domain)) dissolve([[i, j]], c.domain);
  }
  // 2. islands + 4. enclave cap, per domain — both dissolve into a neighbour, not null
  const domains = [...new Set(cells.flat().filter(Boolean).map(c => c.domain))];
  const compsByDomain = {};
  for (const dom of domains) {
    let comps = components(cells, A, J, dom).sort((a, b) => b.length - a.length);
    const survivors = [];
    for (const comp of comps) {
      if (comp.length >= minRegionCells) survivors.push(comp); else dissolve(comp, dom);
    }
    for (const comp of survivors.slice(1 + maxEnclaves)) dissolve(comp, dom);
    compsByDomain[dom] = survivors.slice(0, 1 + maxEnclaves);
  }
  return { A, J, cells, components: compsByDomain };
}

// ── Stage 5a: boundary extraction (edge cancellation; seam + degree-4 safe) ──────
const vKey = (i, j, A) => `${((i % A) + A) % A},${j}`;
const eKey = (a, b) => a < b ? `${a}|${b}` : `${b}|${a}`;
/** Per-domain closed boundary loops as vertex sequences {i,j}. XOR of cell edges =>
 *  cycles; seam handled by angular index mod A in vKey; degree-4 pinch split by a
 *  consistent turn so every loop closes. */
export function extractBoundaries(raster) {
  const { A, J, cells } = raster;
  const domains = [...new Set(cells.flat().filter(Boolean).map(c => c.domain))];
  const out = {};
  for (const dom of domains) {
    // XOR edges
    const edges = new Map();      // eKey -> [vA, vB] (vertices as {i,j})
    const seen = new Set();
    const emit = (ai, aj, bi, bj) => {
      const ka = vKey(ai, aj, A), kb = vKey(bi, bj, A), k = eKey(ka, kb);
      if (edges.has(k)) edges.delete(k); else edges.set(k, [{ i: ai, j: aj }, { i: bi, j: bj }]);
    };
    for (let i = 0; i < A; i++) for (let j = 0; j < J; j++) {
      if (!cells[i][j] || cells[i][j].domain !== dom) continue;
      emit(i, j, i + 1, j);          // inner arc
      emit(i, j + 1, i + 1, j + 1);  // outer arc
      emit(i + 1, j, i + 1, j + 1);  // cw radial
      emit(i, j, i, j + 1);          // ccw radial
    }
    // adjacency
    const adj = new Map();
    for (const [, [a, b]] of edges) {
      const ka = vKey(a.i, a.j, A), kb = vKey(b.i, b.j, A);
      (adj.get(ka) || adj.set(ka, []).get(ka)).push({ v: b, k: kb });
      (adj.get(kb) || adj.set(kb, []).get(kb)).push({ v: a, k: ka });
    }
    // walk loops (degree-4 vertices are just visited twice; each edge used once)
    const usedEdge = new Set();
    const loops = [];
    for (const [, [a, b]] of edges) {
      const startK = vKey(a.i, a.j, A), e0 = eKey(startK, vKey(b.i, b.j, A));
      if (usedEdge.has(e0)) continue;
      const loop = [a]; let prevK = startK, cur = b;
      usedEdge.add(e0);
      let guard = 0;
      while (guard++ < A * J * 8) {
        loop.push(cur);
        const curK = vKey(cur.i, cur.j, A);
        if (curK === startK) break;
        // pick an unused incident edge (prefer not going straight back)
        const cands = (adj.get(curK) || []).filter(nb => !usedEdge.has(eKey(curK, nb.k)));
        if (!cands.length) break;
        const next = cands.find(nb => nb.k !== prevK) || cands[0];
        usedEdge.add(eKey(curK, next.k));
        prevK = curK; cur = next.v;
      }
      loops.push(loop);
    }
    out[dom] = loops;
  }
  return out;
}

// ── Stage 5b: draw guard ────────────────────────────────────────────────────────
/** coherence = largest-component-cells / total-cells; a domain is drawable if
 *  coherence >= coherenceFloor OR total >= minSectorCells. Confetti is suppressed. */
export function drawGuard(components, { coherenceFloor = 0.35, minSectorCells = 12 } = {}) {
  const guard = {};
  for (const [dom, comps] of Object.entries(components)) {
    const total = comps.reduce((a, c) => a + c.length, 0);
    const largest = comps.length ? Math.max(...comps.map(c => c.length)) : 0;
    const coherence = total ? largest / total : 0;
    guard[dom] = { total, coherence, drawable: total > 0 && (coherence >= coherenceFloor || total >= minSectorCells) };
  }
  return guard;
}

// ── orchestrator ─────────────────────────────────────────────────────────────────
/** Full pipeline S0..S5. Returns points + territory (cells/loops/guard) + palette-ready
 *  domain set. Small entities (< minForRaster) return points only (no territories). */
export function layoutSegmentum(graph, config = {}) {
  const { points, anchors } = placeDocs(graph, config);
  const minForRaster = config.minForRaster ?? 20;
  // placeOnly: the domain-free organic scatter — placement only, no territories.
  if (config.placeOnly) return { mode: 'points', points, anchors };
  if (points.length < minForRaster) return { mode: 'points', points, anchors };
  const raster = rasterize(points, config);
  const clean = cleanupRegions(raster, config);
  const loops = extractBoundaries(clean);
  const guard = drawGuard(clean.components, config);
  return { mode: 'territories', points, anchors, A: clean.A, J: clean.J,
    cells: clean.cells, loops, guard, R0, R1 };
}

/** Co-entities as a graduated band beyond the outer radius (reused from v1). Angle =
 *  circular mean of shared-doc angles; radius = strength (shared/entityDocCount) within
 *  the band + small deterministic jitter. */
export function coEntityBand(coEntities, docAngleById, { rInner, rOuter }) {
  return coEntities.map((co, i) => {
    let sx = 0, sy = 0, k = 0;
    for (const id of (co.shared_doc_ids || [])) {
      if (docAngleById.has(id)) { const a = docAngleById.get(id); sx += Math.cos(a); sy += Math.sin(a); k++; }
    }
    const angle = k ? Math.atan2(sy, sx) : (i * 2.399963);
    const denom = co._entityDocCount || 1;
    const strength = Math.min(1, (co.shared ?? (co.shared_doc_ids || []).length) / denom);
    const jitter = ((i % 5) - 2) * 4;
    const radius = rInner + (rOuter - rInner) * (1 - strength) + jitter;
    return { ...co, angle, radius, strength };
  });
}

/** A territory's colour: the mid-path hex if the palette has it, else the first present
 *  leaf under it; null (renderer uses MISC_COLOR) for misc. */
export function sectorColor(midPath, palette) {
  if (midPath === MISC_LABEL) return null;
  if (palette[midPath]) return palette[midPath];
  const leaves = Object.keys(palette).filter(p => p.startsWith(midPath + '/')).sort();
  return leaves.length ? palette[leaves[0]] : null;
}
