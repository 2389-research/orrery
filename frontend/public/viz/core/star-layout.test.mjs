import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  documentStrength, assignRings, MIN_DOCS_FOR_RINGS,
  midLevel, orderDomains, MISC_LABEL, MISC_CAP,
  packRings, layoutStar, sectorColor, coEntityBand,
} from './star-layout.js';

// ── Task 4: strength + tie-safe ring assignment ─────────────────────────────────

test('strength is 1 / active entity count', () => {
  assert.equal(documentStrength({ n_entities: 5 }), 0.2);
  assert.equal(documentStrength({ n_entities: 1 }), 1);
  assert.equal(documentStrength({ n_entities: 0 }), 1);   // guard: never divide by 0
});

test('ring assignment hits percentile targets when shares are distinct', () => {
  const docs = Array.from({ length: 10 }, (_, i) => ({ id: i, n_entities: i + 2 }));
  const rings = assignRings(docs);
  const counts = [0, 0, 0, 0];
  for (const d of rings) counts[d.ring]++;
  assert.deepEqual(counts, [1, 2, 3, 4]);
});

test('a tie group is never split across rings', () => {
  const docs = [
    ...Array.from({ length: 8 }, (_, i) => ({ id: `t${i}`, n_entities: 6 })),
    { id: 'w1', n_entities: 20 },
    { id: 'w2', n_entities: 40 },
  ];
  const rings = assignRings(docs);
  const tieRings = new Set(rings.filter(d => String(d.id).startsWith('t')).map(d => d.ring));
  assert.equal(tieRings.size, 1, 'all 8 tied docs in one ring');
});

test('strongest doc seats in ring 0 when small enough (no inversion at scale)', () => {
  const docs = Array.from({ length: 100 }, (_, i) => ({ id: i, n_entities: i + 2 }));
  const rings = assignRings(docs);
  const strongest = rings.find(d => d.id === 0);
  assert.equal(strongest.ring, 0);
});

// ── Task 5: roll-up, order, adaptive misc ───────────────────────────────────────

test('midLevel rolls a leaf path up to two segments', () => {
  assert.equal(midLevel('software/testing-qa/unit-testing/llm-judge'), 'software/testing-qa');
  assert.equal(midLevel('software'), 'software');
  assert.equal(midLevel(null), MISC_LABEL);
});

test('orderDomains: named grow until misc <= 10%, misc last', () => {
  const docs = [
    ...Array.from({ length: 45 }, () => ({ domain_path: 'software/a/x' })),
    ...Array.from({ length: 45 }, () => ({ domain_path: 'software/b/y' })),
    ...Array.from({ length: 10 }, (_, i) => ({ domain_path: `software/tiny${i}/z` })),
  ];
  const { order } = orderDomains(docs);
  assert.equal(order[order.length - 1], MISC_LABEL);
  assert.ok(order.length <= MISC_CAP + 1);
});

test('orderDomains: single-domain entity -> one sector, no misc', () => {
  const docs = Array.from({ length: 30 }, () => ({ domain_path: 'software/only/z' }));
  const { order } = orderDomains(docs);
  assert.deepEqual(order, ['software/only']);
});

test('orderDomains is stable: ties broken by path, misc last', () => {
  const docs = [
    { domain_path: 'software/b/z' }, { domain_path: 'software/a/z' },
    { domain_path: 'software/a/z' }, { domain_path: 'software/b/z' },
  ];
  const { order } = orderDomains(docs);
  assert.deepEqual(order, ['software/a', 'software/b']);
});

// ── Task 6: per-ring packing ────────────────────────────────────────────────────

function mk(ring, mid, n = 1) {
  return Array.from({ length: n }, (_, i) => ({
    id: `${mid}-${ring}-${i}`, ring, domain_path: `${mid}/leaf`,
  }));
}

test('slot band sums to MAX_FILL (sum of per-domain busiest-ring widths)', () => {
  // a: busiest ring 100 docs; b: busiest ring 50 -> slots 100+50 span MAX_FILL
  const docs = [...mk(3, 'software/a', 100), ...mk(2, 'software/a', 40),
                ...mk(3, 'software/b', 50)];
  const { slots } = packRings(docs, ['software/a', 'software/b']);
  const totalDeg = slots.reduce((a, s) => a + s.width, 0) * 180 / Math.PI;
  assert.ok(Math.abs(totalDeg - 340) < 1e-6);
  // slot widths are proportional to busiest-ring counts (100:50)
  assert.ok(Math.abs(slots[0].width / slots[1].width - 2) < 1e-6);
});

test('the slot band is centred on -90 degrees', () => {
  const docs = [...mk(3, 'software/a', 10), ...mk(3, 'software/b', 10)];
  const { slots } = packRings(docs, ['software/a', 'software/b']);
  const mid = (slots[0].start + slots[slots.length - 1].end) / 2;
  assert.ok(Math.abs(mid - (-Math.PI / 2)) < 1e-6);
});

test('CONTINUITY: a domain sits at the SAME centre angle on every ring', () => {
  // a appears on rings 3 and 2 with different counts; its centre must not drift.
  const docs = [...mk(3, 'software/a', 10), ...mk(2, 'software/a', 3),
                ...mk(3, 'software/b', 8), ...mk(2, 'software/b', 8)];
  const order = ['software/a', 'software/b'];
  const { rings } = packRings(docs, order);
  const centreOf = (r, dom) => rings[r].sectors.find(s => s.domain === dom).center;
  assert.equal(centreOf(3, 'software/a'), centreOf(2, 'software/a'));
  assert.equal(centreOf(3, 'software/b'), centreOf(2, 'software/b'));
});

test('BREATHING: fill width varies per ring within the fixed slot', () => {
  const docs = [...mk(3, 'software/a', 10), ...mk(2, 'software/a', 3)];
  const { rings } = packRings(docs, ['software/a']);
  const fill = (r) => rings[r].sectors.find(s => s.domain === 'software/a').half * 2;
  assert.ok(fill(3) > fill(2), 'the busier ring fills more of the slot');
});

test('a domain absent from a ring simply has no sector that ring (no neighbour fill)', () => {
  const docs = [...mk(3, 'software/a', 10), ...mk(2, 'software/a', 5), ...mk(2, 'software/b', 5)];
  const order = ['software/a', 'software/b'];
  const { rings } = packRings(docs, order);
  assert.equal(rings[3].sectors.find(s => s.domain === 'software/b'), undefined);
  // b's slot centre is fixed; on ring 2 it appears, on ring 3 it doesn't — a's centre
  // is unchanged either way (no reflow).
  const aC2 = rings[2].sectors.find(s => s.domain === 'software/a').center;
  const aC3 = rings[3].sectors.find(s => s.domain === 'software/a').center;
  assert.equal(aC2, aC3);
});

test('arc_per_doc is floored so a lone doc is not a huge wedge', () => {
  const docs = mk(3, 'software/a', 2);
  const { arcPerDocDeg } = packRings(docs, ['software/a']);
  assert.ok(arcPerDocDeg <= 12 + 1e-9);
});

test('domain ordinal order (by slot) is the global order', () => {
  const docs = [
    ...mk(3, 'software/a', 5), ...mk(3, 'software/b', 5),
    ...mk(2, 'software/a', 5), ...mk(2, 'software/b', 5),
  ];
  const { slots } = packRings(docs, ['software/a', 'software/b']);
  assert.deepEqual(slots.map(s => s.domain), ['software/a', 'software/b']);
});

// ── Task 7: small-N + orchestrator ──────────────────────────────────────────────

test('small entity uses simple layout: strongest doc is INNERMOST (no inversion)', () => {
  const docs = [2, 3, 4, 6, 10].map((n, i) => ({ id: i, n_entities: n, domain_path: 'software/a/x' }));
  const out = layoutStar({ documents: docs, co_entities: [], entity: { id: 'e' } });
  assert.equal(out.mode, 'small');
  const strongest = out.documents.find(d => d.id === 0);
  const weakest = out.documents.find(d => d.id === 4);
  assert.ok(strongest.radius < weakest.radius, 'strongest orbits closest');
});

test('tiny entity does not produce a giant wedge', () => {
  const docs = [{ id: 'a', n_entities: 2, domain_path: 'software/x/y' }];
  const out = layoutStar({ documents: docs, co_entities: [], entity: { id: 'e' } });
  assert.equal(out.mode, 'small');
});

test('large entity uses ring mode', () => {
  const docs = Array.from({ length: 60 }, (_, i) => ({ id: i, n_entities: (i % 12) + 2, domain_path: 'software/a/x' }));
  const out = layoutStar({ documents: docs, co_entities: [], entity: { id: 'e' } });
  assert.equal(out.mode, 'rings');
  assert.equal(out.rings.length, 4);
});

// ── Task 8: colour + co-entity band ─────────────────────────────────────────────

test('sector colour: mid path if present, else first present leaf', () => {
  const palette = { 'software/a/x': '#111111', 'software/a/y': '#222222' };
  assert.equal(sectorColor('software/a', palette), '#111111');
  const palette2 = { 'software/a': '#999999', 'software/a/x': '#111111' };
  assert.equal(sectorColor('software/a', palette2), '#999999');
  assert.equal(sectorColor('misc', palette), null);
});

test('co-entity band: stronger co-entities sit nearer the inner band edge', () => {
  const docAngle = new Map([['d1', 0], ['d2', Math.PI]]);
  const co = [
    { id: 'c1', shared_doc_ids: ['d1'], _entityDocCount: 10, shared: 8 },
    { id: 'c2', shared_doc_ids: ['d1'], _entityDocCount: 10, shared: 1 },
  ];
  const placed = coEntityBand(co, docAngle, { rInner: 620, rOuter: 720 });
  const c1 = placed.find(p => p.id === 'c1'), c2 = placed.find(p => p.id === 'c2');
  assert.ok(c1.radius < c2.radius, 'stronger -> nearer inner edge');
});
