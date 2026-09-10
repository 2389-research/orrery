import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  resolveDocs, R0, R1, BAND, SEED_SPREAD_DEG,
  domainCooccur, fiedlerOrder, domainAnchors,
  seed, buildDocEdges, simulate, placeDocs,
} from './segmentum-layout.js';

// ── Stage 0 ─────────────────────────────────────────────────────────────────────
test('radius: rank+sqrt, most relevant innermost, all within [R0,R1]', () => {
  const docs = [2, 3, 4, 10, 50].map((n, i) => ({ id: `d${i}`, n_entities: n, domain_path: 'software/a/x' }));
  const out = resolveDocs(docs);
  const byId = Object.fromEntries(out.map(d => [d.id, d]));
  assert.ok(byId.d0.rTarget < byId.d4.rTarget);
  for (const d of out) assert.ok(d.rTarget >= R0 && d.rTarget <= R1);
});

test('radius: ties broken by id -> distinct percentiles (no clot)', () => {
  const out = resolveDocs([{ id: 'b', n_entities: 6, domain_path: 'x/y' }, { id: 'a', n_entities: 6, domain_path: 'x/y' }]);
  assert.notEqual(out[0].rTarget, out[1].rTarget);
});

test('band clamped to [R0,R1]', () => {
  const [d] = resolveDocs([{ id: 'a', n_entities: 4, domain_path: 'x/y' }]);
  assert.ok(d.band[0] >= R0 && d.band[1] <= R1);
});

// ── Stage 1a ─────────────────────────────────────────────────────────────────────
test('domain co-occurrence: two domains sharing a co-entity get an edge', () => {
  const docs = [{ id: 'd1', domain: 'a' }, { id: 'd2', domain: 'a' }, { id: 'd3', domain: 'b' }];
  const co = [{ id: 'c1', shared_doc_ids: ['d1', 'd3'] }];
  const W = domainCooccur(docs, co);
  const i = W.domains.indexOf('a'), j = W.domains.indexOf('b');
  assert.ok(W.matrix[i][j] > 0 && W.matrix[j][i] > 0);
});

test('fiedler order: path a-b-c puts endpoints farthest apart', () => {
  const order = fiedlerOrder(['a', 'b', 'c'], [[0, 1, 0], [1, 0, 1], [0, 1, 0]]);
  const idx = Object.fromEntries(order.map((d, i) => [d, i]));
  assert.equal(Math.abs(idx.a - idx.c), 2);
});

test('fiedler order is deterministic', () => {
  const W = [[0, 1, 0], [1, 0, 1], [0, 1, 0]];
  assert.deepEqual(fiedlerOrder(['a', 'b', 'c'], W), fiedlerOrder(['a', 'b', 'c'], W));
});

test('fiedler returns null on a DISCONNECTED graph (>=2 zero eigenvalues)', () => {
  assert.equal(fiedlerOrder(['a', 'b', 'c', 'd'], [[0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 1], [0, 0, 1, 0]]), null);
});

test('anchors: disconnected graph -> count-order fallback, angles distinct', () => {
  const docs = [{ id: '1', domain: 'a' }, { id: '2', domain: 'a' }, { id: '3', domain: 'a' }, { id: '4', domain: 'b' }];
  const a = domainAnchors(docs, []);
  assert.ok(a.a !== undefined && a.b !== undefined && a.a !== a.b);
});

// ── Stage 1b ─────────────────────────────────────────────────────────────────────
test('seed: doc angle within +/-SEED_SPREAD of its domain anchor; r = rTarget', () => {
  const docs = resolveDocs([{ id: 'a', n_entities: 4, domain_path: 'software/x/y' }]);
  const s = seed(docs, { 'software/x': 1.0 });
  assert.ok(Math.abs(s[0].theta - 1.0) <= (SEED_SPREAD_DEG + 1e-6) * Math.PI / 180);
  assert.equal(s[0].r, docs[0].rTarget);
});

// ── Stage 2 ──────────────────────────────────────────────────────────────────────
function mkGraph(n) {
  return {
    documents: Array.from({ length: n }, (_, i) => ({
      id: `d${i}`, n_entities: (i % 12) + 2, domain_path: `software/${['a', 'b', 'c'][i % 3]}/x`,
    })),
    co_entities: [
      { id: 'c0', shared_doc_ids: Array.from({ length: n }, (_, i) => `d${i}`).filter((_, i) => i % 3 === 0) },
      { id: 'c1', shared_doc_ids: Array.from({ length: n }, (_, i) => `d${i}`).filter((_, i) => i % 3 === 1) },
    ],
  };
}

test('doc edges: docs sharing a co-entity are linked, weight = shared count', () => {
  const e = buildDocEdges([{ id: 'd1' }, { id: 'd2' }], [{ id: 'c', shared_doc_ids: ['d1', 'd2'] }]);
  assert.equal(e.get('d1').get('d2'), 1);
});

test('sim keeps every doc inside its band (radius honest to the band)', () => {
  const { points } = placeDocs(mkGraph(200), { maxTicks: 120 });
  for (const d of points) assert.ok(d.r >= d.band[0] - 1e-6 && d.r <= d.band[1] + 1e-6);
});

test('sim is deterministic (two runs byte-identical)', () => {
  const a = placeDocs(mkGraph(60), { maxTicks: 80 }).points.map(d => [d.r, d.theta]);
  const b = placeDocs(mkGraph(60), { maxTicks: 80 }).points.map(d => [d.r, d.theta]);
  assert.deepEqual(a, b);
});

test('sim organizes angle toward the domain anchor (docs of a domain cluster)', () => {
  const { points, anchors } = placeDocs(mkGraph(150), { maxTicks: 200 });
  // circular mean angle of domain 'software/a' docs should sit near its anchor
  const da = points.filter(p => p.domain === 'software/a');
  let sx = 0, sy = 0; for (const p of da) { sx += Math.cos(p.theta); sy += Math.sin(p.theta); }
  const mean = Math.atan2(sy, sx);
  const anchor = anchors['software/a'];
  const diff = Math.abs(Math.atan2(Math.sin(mean - anchor), Math.cos(mean - anchor)));
  assert.ok(diff < Math.PI / 2, `domain mean ${mean.toFixed(2)} should be near anchor ${anchor.toFixed(2)}`);
});
