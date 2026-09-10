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
export function resolveDocs(docs, { r0 = R0, r1 = R1, band = BAND } = {}) {
  const withRel = docs.map(d => ({ ...d, rel: 1 / (d.n_entities || 1), domain: midLevel(d.domain_path) }));
  const order = [...withRel].sort((a, b) => (b.rel - a.rel) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const n = order.length || 1;
  return order.map((d, i) => {
    const p = n > 1 ? i / (n - 1) : 0;
    const rTarget = r0 + (r1 - r0) * Math.sqrt(p);
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
    kAnchor = 0.02, kLink = 0.04, kBand = 0.15, anchors = {} } = {}) {
  const edges = buildDocEdges(docs, coEntities);
  const P = docs.map(d => ({ ...d, x: Math.cos(d.theta) * d.r, y: Math.sin(d.theta) * d.r }));
  const byId = new Map(P.map(p => [p.id, p]));
  for (let t = 0; t < maxTicks; t++) {
    const fx = new Map(), fy = new Map();
    const add = (id, ax, ay) => { fx.set(id, (fx.get(id) || 0) + ax); fy.set(id, (fy.get(id) || 0) + ay); };
    for (const p of P) {
      const th = anchors[p.domain]; if (th == null) continue;
      const ax = Math.cos(th) * (R1 + R_ANCHOR_MARGIN), ay = Math.sin(th) * (R1 + R_ANCHOR_MARGIN);
      add(p.id, (ax - p.x) * kAnchor, (ay - p.y) * kAnchor);
    }
    for (const p of P) for (const [q, w] of edges.get(p.id)) {
      const o = byId.get(q);
      add(p.id, (o.x - p.x) * kLink * Math.min(1, w / 3), (o.y - p.y) * kLink * Math.min(1, w / 3));
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
