# Entity Segmentum View — place first, derive territory after

**Status:** Design (v2)
**Date:** 2026-09-10
**Supersedes:** `2026-09-10-entity-star-orbital-layout-design.md` (arc-packing v1).
**Scope (client):** replaces the layout in `frontend/public/viz/core/star-layout.js` and the sector drawing in `frontend/public/viz/renderers/star.js`; `star.html` wiring.
**Scope (backend):** none new. v1's `get_star_graph` additions (`domain_path`, `n_entities`, `palette`, `co_limit=150`) already deliver everything this consumes.

## Why v2

v1 made domain sectors a **layout primitive** — it allocated angular arcs per domain, then dropped documents into them. Live feedback (rigid pie-slices, dead space reserved for domains absent from a ring, colour marking only a doc-sliver, no organic packing, no nesting) confirmed this is the wrong spine. It is failure mode #1 below.

v2 inverts it, per the guiding invariant:

> **No stage after placement may move a point radially, and no stage before outlining may reference sectors.**

Documents are placed as points in a polar field: **radius = relevance to the core entity** (a hard encoding), **angle = emergent from connectivity to everything else** (a local force layout). Conceptual-domain **sectors are computed after placement** by rasterizing domain occupancy onto a fine polar lattice and stroking the boundary of each domain's occupied region. Borders follow the documents; nesting ("a small domain inside a big one's territory") falls out for free as an enclave, with no special-casing.

Two decisions the human fixed, which shape this doc:
1. **Distance uses what we already compute.** No new relevance metric — the existing per-document entity-share (`1 / n_entities`, already in the payload) drives radius, via rank+sqrt (below). Exactness does not matter.
2. **No global-galaxy layout is used.** Angle comes from connectivity *within the local set only*. Absolute orientation is arbitrary; only relative adjacency (connected things land near each other) matters. This removes v1's proposed global-position API field — the view is fully self-contained from the existing star payload.

---

## Terminology

| Term | Meaning |
|---|---|
| Core entity | The entity at the centre of the view (the double-clicked node). |
| Doc | A document connected to the core. One point on the map. |
| Relevance `rel(d)` | How tied doc `d` is to the core. Here: the entity-share `1 / n_entities(d)` already returned by `get_star_graph`. Range (0, 1]. Its coarseness/ties are handled by rank scaling, not by needing a finer metric. |
| Peripheral entity | A co-entity of the core (payload `co_entities`), rendered as a fixed anchor body the docs are attracted toward. |
| Domain | The mid-level roll-up of a doc's `domain_path` (v1's `midLevel`), one hard label per doc. |
| Lattice | Fine polar grid (A angular × J radial). Used only to draw borders; invisible; unrelated to relevance. |
| Sector | The union of lattice cells assigned to one domain. Emergent — never an input. |
| Contested cell | A cell where the top-two domain votes are within `contestRatio`. |
| Enclave | A connected component of a domain's cells disjoint from that domain's largest component. |

---

## Inputs (all already in the `get_star_graph` payload)

- `docs[]`: `id`, `title`, `domain_path` (→ mid-level domain), `n_entities` (→ `rel`), `content_type`.
- `co_entities[]`: `id`, `canonical_name`, `type`, `weight`, `shared_doc_ids[]`. These give the doc↔peripheral-entity edges (a doc is linked to every co-entity whose `shared_doc_ids` contains it) and, transitively, doc↔doc proximity (docs sharing a co-entity attract).
- `palette`: `{leaf domain_path → hex}`, galaxy-agreeing.
- `config`: parameter object (defaults below).

No `globalPositions`. No new payload fields.

## Output

- `points[]`: per doc — final `(r, theta)` / `(x, y)`, continuous radius (no bands), render size after aggregation, and its domain.
- `sectors[]`: per domain — closed boundary loops (ordered `{type:'arc'|'radial', ...polar}` primitives), member cells, label anchor `(r, theta)`, enclave list.
- `contested[]`, `diagnostics` (per-stage stats for the acceptance checks).

---

## Stage 0 — Resolve inputs

- **Hard domain per doc:** `midLevel(domain_path)`; no primary domain → `misc`. (Adaptive top-N + `misc` bucketing from v1 is retained for *labelling/colour*, not for placement.)
- **Radius mapping (rank + sqrt, equal-area):**
  - `rel(d) = 1 / n_entities(d)`.
  - Sort docs by `rel` descending; `p(d)` = rank percentile in [0,1] (most relevant → 0). Ties broken by doc id so percentiles are distinct and stable — this is what turns the quantized share (many docs at exactly 1/6, 1/7…) into a smooth radius instead of clotting at a few radii.
  - `r(d) = R0 + (R1 − R0) · sqrt(p(d))`. sqrt keeps visual density roughly uniform across rings (circumference grows with r).
  - Absolute `rel` is surfaced in hover/detail, never as radius text.
- **Edge set for angle (core excluded):** doc↔peripheral-entity edges from `shared_doc_ids`. Edges to the core itself drive nothing after radius is set (radius already *is* relationship-to-core; reusing it for angle would smear clusters radially).

## Stage 1 — Seed angles (local, deterministic, no global reference)

There is no global orientation to inherit, so seed cheaply and let connectivity do the work:

1. Give each **peripheral entity** an initial angle by phyllotaxis (`i · 137.5°`) over co-entities sorted by descending `weight` then id — deterministic, evenly spread, no clumping.
2. Each **doc**'s seed angle = weighted circular mean of its connected peripheral entities' seed angles (weights = shared-doc count). Docs with no co-entity edge: circular mean of their domain siblings' seeds; if none, `hash(id) → angle`.
3. Add `hash(id) → ±2°` jitter to break exact overlaps.

No `Math.random` anywhere. Absolute orientation is meaningless by design; relative adjacency is what Stage 2 refines.

## Stage 2 — Simulate (hand-rolled, radially projected)

A small deterministic force sim — **hand-rolled, not d3-force**, because (a) the viz is dependency-free ESM with a CSP that blocks arbitrary CDNs, and (b) d3-force's `jiggle()` uses `Math.random`, breaking determinism. The needed forces are few:

- **Link attraction** along the Stage-0 edge set. Peripheral entities are **fixed anchors** at their Stage-1 angle and radius `R1 + margin` (not simulated bodies); docs are pulled toward the anchors and toward docs they share a co-entity with. Strength ∝ normalized edge weight.
- **Collision** at `dotRadius + padding`.
- **Weak tether** for low-signal docs only (edge degree ≤ 1): toward domain centroid, strength ≤ 0.1× link (must lose to any real edge).
- **Radial projection every tick:** reset each doc to `(r_target, atan2(y, x))` — keep only tangential motion. Radius is exact at all times; the sim is effectively 1-D angular. This is the invariant's teeth.
- Deterministic termination: fixed `maxTicks` / `alphaMin`. Target ≤ 300 ms for 1,500 docs (projection makes ticks cheap); may run in a worker with the seeded positions rendered immediately.

## Stage 3 — Rasterize occupancy

1. Lattice `A × J`: `A = clamp(round(3·√n), 64, 160)`, `J = 24`. A rendering knob, never a data statement.
2. Each doc casts a gaussian-weighted vote for its domain into nearby cells; distance in map units `d² = (Δθ·r_mid)² + Δr²`.
3. **Adaptive bandwidth** `h = c · median-nearest-neighbour distance`, c ≈ 1.5, clamped [0.5, 3] cell diagonals — must shrink as density grows so borders sharpen rather than bleed (fixed bandwidth is failure mode #2).
4. Cell winner = argmax domain vote above `voteFloor`, else empty. Runner-up ≥ `contestRatio`·winner → mark **contested** (assigned to winner for region math, flagged for render).

## Stage 4 — Region cleanup (angular axis wraps, radial does not)

1. **Despeckle:** an assigned cell with zero same-domain 4-neighbours → empty.
2. **Islands:** per-domain flood-fill; components `< minRegionCells` dissolve into the majority bordering domain (or empty); components `≥ minRegionCells` survive as enclaves.
3. **Holes:** a foreign/empty region fully enclosed by domain A — keep if it contains docs (a real enclave), absorb if empty and small.
4. Cap enclaves per domain at `maxEnclaves`, dissolving smallest-first; log dissolutions in diagnostics (they flag label/structure disagreement worth surfacing).

## Stage 5 — Boundary extraction + draw

1. Each occupied cell contributes 4 lattice edges (inner arc, outer arc, 2 radials). Per domain, delete every edge shared by two same-domain cells — exact coordinate matching, no geometry ops.
2. Link surviving edges into closed loops (each surviving vertex has degree 2 per domain; walk them). Assert every loop closes.
3. **Corner fillets:** small arc (≈0.35 cell) at each corner to kill the pixel staircase, ≤ half the shorter adjacent edge.
4. Render:
   - Fill: per-domain single path, ~8–12% alpha, domain colour (v1's `sectorColor` rule: mid-path hex, else first present leaf; `misc` grey).
   - Border: filleted loops, ~1 px, ~80% alpha.
   - Contested: `contestedStyle: 'gap' | 'hatch'`, default `'gap'`.
   - Labels: one per domain, largest component only, at its outermost arc midpoint, offset out. Never label enclaves.
   - Docs: dots above fills; radius `base · clamp(√(300/n), 0.5, 1)`.
   - Colour docs by domain (kills the v1 monochrome bug — `TYPE_COLORS` matched nothing).
5. **Aggregation at scale:** cluster docs within `mergeRadius` (screen px) into one brighter dot + count badge, expanding on zoom/click. Rendering/hover only — votes still use individual positions.

## Interaction (carried from v1, still required)

- **Hover a sector or a doc → highlight that domain across the whole map, dim the rest.** With derived territories this is: hover angle+radius → lattice cell → its domain; or a hovered doc → its domain. Already proven workable.
- Double-click a doc → existing detail panel. Unchanged.

---

## Parameters (one config object; no inline magic numbers)

| Name | Default | Notes |
|---|---|---|
| R0 / R1 | 60 / min(canvas)/2 − 60 | inner/outer map radii |
| A / J | 3·√n clamp[64,160] / 24 | lattice |
| bandwidth c | 1.5 × median NN | clamp [0.5,3] cell diagonals |
| voteFloor | isolated doc claims ~1–3 cells | tune |
| contestRatio | 0.8 | |
| minRegionCells | 4 | |
| maxEnclaves | 3 / domain | |
| tether | 0.1 × link | degree ≤ 1 only |
| maxTicks / alphaMin | 300 / 0.001 | |
| mergeRadius | 8 px | |
| contestedStyle | 'gap' | |

## Non-goals

- Sectors never influence placement — no force, constraint, or seed references domain territory.
- No radial motion after Stage 0; crowding resolves tangentially (collision) or by aggregation.
- No per-band containers, row snapping, or angular-span allocation — **delete v1's `packRings`/slot/arc code entirely.**
- No global-graph positions consumed.
- Peripheral entities are anchors, not simulated bodies.

## Failure modes (each has bitten an attempt)

1. **Sector-first layout** (v1) — forbidden by the invariant. A doc in a "wrong" sector is a labelling/graph fact, not a layout bug.
2. **Fixed kernel bandwidth** → border bleed at high n. Adaptive (3.3).
3. **Nondeterminism** from force-seed sensitivity → seeded everything, hashed jitter/tiebreaks, hand-rolled sim.
4. **Radial clotting** from linear `rel→r` → rank+sqrt (Stage 0).
5. **Gaussian pile-up / blank wedges** from seeding a whole domain at one bearing → per-doc entity-mean seeds spread naturally; collision finishes.

## Acceptance criteria

1. **Radius honesty:** rendered radius = Stage-0 target within 0.5 px, at n ∈ {50, 300, 1200}.
2. **Determinism:** two runs on identical input → byte-identical points and sector loops.
3. **Orientation coherence:** ≥ 70% of docs land within ±45° of their Stage-1 seed (sim refines, not scrambles).
4. **Border sanity at scale:** at n=1200, contested ≤ 15% of assigned cells; no domain over `maxEnclaves`; every extracted loop closes (asserted).
5. **Blank-space bound:** at n ≥ 300, no empty annulus > 2 radial rings and no empty wedge > 30° (given docs span domains).
6. **Performance:** seed→outline ≤ 500 ms at n=1500; re-rasterize alone ≤ 50 ms.
7. **Visual regression:** headless snapshots at n ∈ {50, 300, 1200} on fixed fixtures (playwright, as used in v1 review).

## Staged delivery (build + validate each stage on the previous stage's points)

1. **Stage 0–1:** render seeded points only, no sim. Verify radius mapping (rank+sqrt) and that seeds spread; no global reference.
2. **Stage 2:** points spread tangentially; assert radius unchanged per tick in dev builds.
3. **Stage 3–4:** debug lattice overlay (cell assignments as flat colours, contested highlighted).
4. **Stage 5:** borders, fillets, labels, contested styling, domain doc colours, hover.
5. **Aggregation + zoom last.**

Debug in pipeline order: is the point somewhere strange (0–2) before asking whether the border drew wrong (3–5).

## Testing

- Pure stages (0,1,3,4,5) unit-tested with `node --test` (as v1's `star-layout.test.mjs`): radius mapping monotonic + within-bounds; seed determinism; rasterize winner/contested logic; despeckle/island/hole transitions on hand-built lattices; edge-cancellation produces closed loops on a known shape.
- Stage 2: assert radius invariance across ticks and orientation-coherence bound on a fixture.
- Snapshot/visual regression via the headless harness from v1 review.
- Backend unchanged; v1's `test_star_graph_orbital.py` still applies.

## What is kept vs deleted

- **Kept:** the whole backend (`domain_palette`, per-doc `domain_path`/`n_entities`, `palette`, `co_limit=150`); `midLevel`, `orderDomains` (for label/colour bucketing), `sectorColor`, `documentStrength`, the palette wiring, hover plumbing, the headless render harness.
- **Deleted:** `assignRings`, `packRings`, the ring/slot/arc packing and its tests, `drawSectors`'s wedge drawing. Replaced by Stages 0–5.
