/**
 * Pure layout math for the entity star page. No DOM, no canvas — everything here is
 * unit-tested with `node --test` (star-layout.test.mjs). star.html imports these and
 * only does canvas work with the results. See
 * docs/superpowers/specs/2026-09-10-entity-star-orbital-layout-design.md.
 */

// ── tunable constants (one definition, shared by tests + renderer) ──────────────
export const MIN_DOCS_FOR_RINGS = 20;     // below this: simple single-tier layout
export const RING_TARGETS = [0.10, 0.30, 0.60];  // cumulative-fraction ring cuts
export const RING_RADII = [150, 280, 410, 560];  // outward-biased (docs skew outer)
export const MAX_FILL_DEG = 340;          // busiest ring's max angular fill
export const ARC_PER_DOC_MAX_DEG = 12;    // floor: a lone doc never a huge wedge
export const MISC_THRESHOLD = 0.10;       // grow named sectors until misc <= this
export const MISC_CAP = 12;               // ...but never more than this many named
export const MISC_LABEL = 'misc';
export const MISC_COLOR = '#6d7280';      // neutral grey for misc sector + no-domain docs
export const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

export const RING_INNER = RING_RADII[0];
export const RING_OUTER = RING_RADII[RING_RADII.length - 1];

/** Connection strength of a document to the entity: its share of the doc's active
 *  entity set. Guarded so a malformed 0 never divides. */
export function documentStrength(doc) {
  const n = doc.n_entities || 1;
  return 1 / n;
}

/** Assign each doc a ring 0..3 by the tie-safe cumulative rule. Returns the same
 *  doc objects with `.ring` and `.strength` added (new array).
 *
 *  Reduce to DISTINCT share values (strongest first); walk them adding each value's
 *  doc count to a running total; test cum/total AFTER adding: ring 0 if <=0.10,
 *  1 if <=0.30, 2 if <=0.60, else 3. Chosen per distinct value, so a tie group is
 *  never split (spec "Rings" section). */
export function assignRings(docs, { targets = RING_TARGETS } = {}) {
  const withStrength = docs.map(d => ({ ...d, strength: documentStrength(d) }));
  const total = withStrength.length || 1;

  const byValue = new Map();   // distinct value -> docs
  for (const d of withStrength) {
    if (!byValue.has(d.strength)) byValue.set(d.strength, []);
    byValue.get(d.strength).push(d);
  }
  const values = [...byValue.keys()].sort((a, b) => b - a);   // strongest first

  let cum = 0;
  const ringOf = new Map();
  for (const v of values) {
    cum += byValue.get(v).length;
    const frac = cum / total;
    let ring = targets.length;                 // default: outermost
    for (let i = 0; i < targets.length; i++) {
      if (frac <= targets[i]) { ring = i; break; }
    }
    ringOf.set(v, ring);
  }
  return withStrength.map(d => ({ ...d, ring: ringOf.get(d.strength) }));
}

/** Roll a leaf domain path up to its mid-level (2 segments). null/'' -> misc. */
export function midLevel(domainPath) {
  if (!domainPath) return MISC_LABEL;
  const parts = domainPath.split('/');
  return parts.length >= 2 ? parts.slice(0, 2).join('/') : domainPath;
}

/** Decide the sector order and how many are named.
 *
 *  Roll every doc to its mid-level; count docs per mid-level. Sort by count desc,
 *  then path asc (deterministic). Grow the named set from the top until the tail
 *  ("misc") is <= threshold of all docs, capped at cap. Returns
 *  { order: [...midpaths, 'misc'?], named: Set, miscCount }. `misc` is appended
 *  only if any docs fall outside the named set. */
export function orderDomains(docs, { threshold = MISC_THRESHOLD, cap = MISC_CAP } = {}) {
  const total = docs.length || 1;
  const counts = new Map();
  for (const d of docs) {
    const m = midLevel(d.domain_path);
    counts.set(m, (counts.get(m) || 0) + 1);
  }
  const sorted = [...counts.entries()].sort(
    (a, b) => (b[1] - a[1]) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));

  const named = [];
  let covered = 0;
  for (const [mid, n] of sorted) {
    if (named.length >= cap) break;
    named.push(mid);
    covered += n;
    if ((total - covered) / total <= threshold) break;
  }
  const namedSet = new Set(named);
  const miscCount = total - covered;
  const order = [...named];
  if (miscCount > 0) order.push(MISC_LABEL);
  return { order, named: namedSet, miscCount };
}

/** A doc's sector key: its mid-level if that mid-level is a named sector, else misc. */
function midLevelIfNamed(doc, order) {
  const m = midLevel(doc.domain_path);
  return order.includes(m) ? m : MISC_LABEL;
}

/** Pack documents into per-ring domain sectors (the breathing arcs).
 *
 *  Arc width per doc is uniform in DEGREES across all rings:
 *  arc_per_doc = min(MAX_FILL / Nmax, ARC_PER_DOC_MAX), Nmax = busiest ring's docs.
 *  Each ring's total span = its doc count * arc_per_doc, centred on -90deg. Within a
 *  ring, domains are laid left-to-right in the fixed `order` (misc last); docs within
 *  a domain get equal angular slots.
 *
 *  Returns { rings: [{ ring, docCount, arcSpan, startAngle, endAngle,
 *              sectors: [{ domain, arc, startAngle, endAngle, docs:[{...,angle}] }] }],
 *            arcPerDocDeg }. Rings with 0 docs have arcSpan 0 and empty sectors. */
export function packRings(docs, order, {
  maxFillDeg = MAX_FILL_DEG, arcPerDocMaxDeg = ARC_PER_DOC_MAX_DEG,
} = {}) {
  const NRINGS = RING_RADII.length;

  // cell[domain][ring] = docs in that (domain, ring), plus per-ring totals.
  const cell = new Map();
  const perRingCount = Array(NRINGS).fill(0);
  for (const d of docs) {
    if (d.ring == null) continue;
    perRingCount[d.ring]++;
    const m = midLevelIfNamed(d, order);
    if (!cell.has(m)) cell.set(m, Array.from({ length: NRINGS }, () => []));
    cell.get(m)[d.ring].push(d);
  }

  // Present domains, in the fixed global order (misc last). Each domain gets a FIXED
  // angular slot whose width = its busiest ring's doc count, so a domain occupies the
  // SAME absolute angle on every ring (a continuous radial sector) and its FILL
  // breathes within that slot — full on its strong ring, a short centred stub on a
  // weak one. arc_per_doc scales the sum of slot widths to <= MAX_FILL.
  const domains = order.filter(m => cell.has(m));
  const slotDocs = new Map();
  let totalSlotDocs = 0;
  for (const m of domains) {
    const mx = Math.max(0, ...cell.get(m).map(a => a.length));
    slotDocs.set(m, mx);
    totalSlotDocs += mx;
  }
  const arcPerDocDeg = Math.min(maxFillDeg / Math.max(1, totalSlotDocs), arcPerDocMaxDeg);
  const arcPerDoc = arcPerDocDeg * DEG;

  // Fixed global slots, contiguous, centred on -90deg (the slot BAND is centred).
  const totalSpan = totalSlotDocs * arcPerDoc;
  let cursor = -Math.PI / 2 - totalSpan / 2;
  const slots = [];
  const slotByDomain = new Map();
  for (const m of domains) {
    const w = slotDocs.get(m) * arcPerDoc;
    const slot = { domain: m, start: cursor, end: cursor + w, center: cursor + w / 2, width: w };
    slots.push(slot); slotByDomain.set(m, slot);
    cursor += w;
  }

  // Per ring: each present domain's FILLED arc, centred in its fixed slot.
  const rings = [];
  for (let r = 0; r < NRINGS; r++) {
    const sectors = [];
    for (const m of domains) {
      const dd = cell.get(m)[r];
      if (!dd.length) continue;
      const slot = slotByDomain.get(m);
      const arc = dd.length * arcPerDoc;
      const s0 = slot.center - arc / 2;
      const placed = dd.map((d, i) => ({ ...d, angle: s0 + arc * ((i + 0.5) / dd.length) }));
      sectors.push({ domain: m, center: slot.center, half: arc / 2,
                     startAngle: s0, endAngle: s0 + arc, docs: placed });
    }
    rings.push({ ring: r, docCount: perRingCount[r], sectors });
  }
  return { rings, slots, arcPerDocDeg };
}

/** Below MIN_DOCS_FOR_RINGS: one crescent, radius = strength directly (strongest
 *  innermost — fixes the small-N radius inversion), angle grouped by mid-level. Same
 *  doc fields the ring mode produces so the renderer is uniform. */
function layoutSmall(docs) {
  const withS = docs.map(d => ({ ...d, strength: documentStrength(d) }));
  const strengths = withS.map(d => d.strength);
  const smin = Math.min(...strengths), smax = Math.max(...strengths);
  const span = (smax - smin) || 1;
  // strongest -> RING_INNER, weakest -> RING_OUTER
  const radiusOf = s => RING_INNER + (RING_OUTER - RING_INNER) * (1 - (s - smin) / span);

  const { order } = orderDomains(docs);
  const orderIndex = new Map(order.map((m, i) => [m, i]));
  withS.sort((a, b) =>
    (orderIndex.get(midLevelIfNamed(a, order)) ?? 1e9) -
    (orderIndex.get(midLevelIfNamed(b, order)) ?? 1e9));
  const arc = MAX_FILL_DEG * DEG;
  const start = -Math.PI / 2 - arc / 2;
  const positioned = withS.map((d, i) => ({
    ...d, radius: radiusOf(d.strength),
    angle: start + arc * ((i + 0.5) / withS.length),
  }));
  return { mode: 'small', documents: positioned, sectorOrder: order };
}

/** Entry point. Chooses small vs ring mode and returns positioned documents plus
 *  (ring mode) the ring/sector structure the renderer draws. Co-entity placement is
 *  computed separately by coEntityBand and merged by star.html. */
export function layoutStar(graph, opts = {}) {
  const docs = graph.documents || [];
  if (docs.length < (opts.minDocs ?? MIN_DOCS_FOR_RINGS)) {
    return layoutSmall(docs);
  }
  const ringed = assignRings(docs);
  const { order, named } = orderDomains(docs);
  const { rings, slots, arcPerDocDeg } = packRings(ringed, order, opts);
  const positioned = [];
  for (const r of rings) for (const s of r.sectors)
    for (const d of s.docs) positioned.push({ ...d, radius: RING_RADII[r.ring], domain: s.domain });
  return { mode: 'rings', documents: positioned, rings, slots, sectorOrder: order,
           namedDomains: named, arcPerDocDeg };
}

/** A sector's colour: the mid-path hex if the palette has it, else the hex of the
 *  first (sorted) present leaf under that mid-path. Returns null for misc so the
 *  renderer uses MISC_COLOR. */
export function sectorColor(midPath, palette) {
  if (midPath === MISC_LABEL) return null;
  if (palette[midPath]) return palette[midPath];
  const leaves = Object.keys(palette).filter(p => p.startsWith(midPath + '/')).sort();
  return leaves.length ? palette[leaves[0]] : null;
}

/** Place co-entities in a graduated band beyond the document rings. Angle = circular
 *  mean of the angles of the docs they share with the entity; radius = band inner
 *  edge for the strongest (shared/entityDocCount), outer for the weakest, plus a small
 *  deterministic jitter so equal-strength neighbours don't stack. */
export function coEntityBand(coEntities, docAngleById, { rInner, rOuter }) {
  return coEntities.map((co, i) => {
    let sx = 0, sy = 0, k = 0;
    for (const id of (co.shared_doc_ids || [])) {
      if (docAngleById.has(id)) {
        const a = docAngleById.get(id);
        sx += Math.cos(a); sy += Math.sin(a); k++;
      }
    }
    const angle = k ? Math.atan2(sy, sx) : (i * 2.399963);   // golden-angle fallback
    const denom = co._entityDocCount || 1;
    const strength = Math.min(1, (co.shared ?? (co.shared_doc_ids || []).length) / denom);
    const jitter = ((i % 5) - 2) * 4;   // ±8px, deterministic
    const radius = rInner + (rOuter - rInner) * (1 - strength) + jitter;
    return { ...co, angle, radius, strength };
  });
}
