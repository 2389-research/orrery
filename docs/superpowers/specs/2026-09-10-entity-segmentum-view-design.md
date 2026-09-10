# Entity Segmentum View — place first, derive territory after

**Status:** Design (v2, rev 2)
**Date:** 2026-09-10
**Supersedes:** `2026-09-10-entity-star-orbital-layout-design.md` (arc-packing v1).
**Scope (client):** replaces the layout in `frontend/public/viz/core/star-layout.js` and the sector drawing in `frontend/public/viz/renderers/star.js`; `star.html` wiring.
**Scope (backend):** none new. v1's `get_star_graph` additions (`domain_path`, `n_entities`, `palette`, `co_limit=150`) deliver everything this consumes.

## Why v2, and what rev 2 changed

v1 made domain sectors a **layout primitive** — it allocated angular arcs per domain and dropped docs into them (rigid pie-slices, dead space, no organic packing). That is failure mode #1.

v2 inverts it: **place documents first; derive sectors after.** A document is a point whose **radius encodes relevance to the core entity** and whose **angle emerges from connectivity**; **domain territories are computed after placement** by rasterizing which cells the docs of each domain occupy and stroking the boundary. Borders follow docs; nesting falls out for free as an enclave.

**rev 2** folds in two findings that arrived together — the human's design intuition and an empirical prototype (Stages 0–3 run on real entities) that independently reached the same conclusion:

1. **Domains are the anchors, exactly as in the main galaxy map.** In the galaxy, entities have *no* position of their own (`state.js:67-70`); domains have positions (`domain_layout`) and pull their members. This view mirrors that: each **domain** sits at an angle and softly pulls its member documents. (v2 rev 1 wrongly anchored on co-entities.)
2. **Radius is a *band*, not a hard lock.** The prototype showed that pinning radius exactly to relevance **shatters territories at scale** (openai/gemini: ~5 coherent domains, ~9 fractured into confetti) because docs of other domains sit between a domain's members *radially*, slicing it — no seeding can fix this. The fix, which the human proposed independently: let a doc move within a **band** around its relevance radius (suspended, pushed and pulled), so a domain's docs can consolidate radially as well as angularly.

Revised guiding invariant:

> Radius is **constrained to a relevance band**, never frozen and never free. Domains may act as **point attractors**, but a domain's **drawn territory is emergent** — no stage may place a doc inside a pre-drawn sector region.

Two human decisions still hold: distance uses the metric we already compute (entity-share); **no global-galaxy layout is consumed** for placement (rev 2 anchor angles come from *local* domain co-occurrence; a later global-blend option is architected as a swap, see Stage 1).

---

## Terminology

| Term | Meaning |
|---|---|
| Core entity | The double-clicked entity at the centre. |
| Doc | A document connected to the core. One point. |
| Relevance `rel(d)` | `1 / n_entities(d)` (already in the payload). Range (0,1]. Coarseness/ties handled by rank scaling, not a finer metric. |
| Relevance band | The radial range a doc may occupy: `[r_target − BAND/2, r_target + BAND/2]`. A soft spring holds it near `r_target`; forces move it within the band. |
| Domain | Mid-level roll-up of `domain_path` (v1 `midLevel`); missing → `misc`. One hard label per doc. |
| Domain anchor | A point at `(R_anchor, θ_domain)` that softly pulls its member docs. `θ_domain` from the anchor-angle strategy (Stage 1). Domains are the attractors — the main-map mechanic. |
| Peripheral entity | A co-entity; used only to compute doc↔doc connectivity (docs sharing a co-entity are linked). Not itself an anchor in rev 2. |
| Lattice / cell | Fine polar grid (A×J) used only to draw borders. Emergent, invisible, unrelated to relevance. |
| Sector | Union of lattice cells a domain's docs occupy. Derived — never an input. |
| Enclave | A domain component disjoint from that domain's largest component. |

---

## Inputs (all already in the `get_star_graph` payload; no new fields)

- `docs[]`: `id`, `title`, `domain_path` (→ domain), `n_entities` (→ `rel`), `content_type`.
- `co_entities[]`: `id`, `weight`, `shared_doc_ids[]` — used to derive **doc↔doc** edges (two docs are linked, weight = number of co-entities they share) and **domain↔domain** co-occurrence (Stage 1).
- `palette`: `{leaf domain_path → hex}`, galaxy-agreeing.
- `config`: **client-side** parameter object (defaults below) — NOT part of the payload.

## Output

- `points[]`: per doc — final `(r, θ)`/`(x, y)` (r within its band), domain, render size after aggregation.
- `sectors[]`: per domain — closed boundary loops, member cells, label anchor, enclave list, and a `coherence` score (largest-component / total cells) used by the draw guard.
- `contested[]`, `diagnostics` (per-stage stats for acceptance).

---

## Stage 0 — Resolve inputs

- **Hard domain per doc:** `midLevel(domain_path)`, else `misc`. (v1 `orderDomains` adaptive top-N + `misc` retained for **label/colour** only.)
- **Relevance → target radius + band (rank + sqrt):**
  - `rel(d) = 1 / n_entities(d)`.
  - `p(d)` = rank percentile by `rel` desc, **ties broken by doc id** (distinct, stable) — turns the quantized share into a smooth radius instead of clotting.
  - `r_target(d) = R0 + (R1 − R0) · sqrt(p(d))`; band = `[r_target − BAND/2, r_target + BAND/2]`, clamped to `[R0, R1]`.
  - Note: rank+sqrt gives each doc a distinct target, but at the dense outer edge adjacent targets differ by less than a dot; **collision + the band + aggregation** prevent overlap, not radius spacing alone. Absolute `rel` shows in hover.
- **Edge sets:** doc↔doc from shared co-entities (weight = shared co-entity count); domain↔domain co-occurrence (weight = docs shared between two domains) for Stage 1. Edges to the core drive nothing after radius is set.

## Stage 1 — Domain anchors (local co-occurrence) + seed

**Anchor-angle strategy is pluggable** (`config.anchorAngles`), so #3 is a later swap, not a rewrite:

- **`'local'` (rev 2 default):** build the domain↔domain co-occurrence graph (edge weight = docs the two domains share among the core's docs); take the **Fiedler vector** (2nd-smallest eigenvector of the graph Laplacian); sort domains by it; map sorted rank → angle evenly around the circle. Co-occurring domains land adjacent. Deterministic (`numpy`-free: a small dependency-free Jacobi/QR or power-iteration deflation on ≤ ~30×30 domain matrices; the co-entity variant validated this embedding empirically). Single-domain or disconnected graph → fall back to sorted-by-doc-count even spacing.
- **`'global'` / `'blend'` (architected, not built):** read `θ_domain` from `domain_layout` bearings; blend keeps global angle with local presence/pull. The strategy returns `{domain → θ}`; nothing downstream cares how it was computed.

**Seed:** each domain anchor at `(R_anchor = R1 + margin, θ_domain)`. Each doc's seed angle = its domain anchor's angle + `hash(id) → ±8°` spread (so a domain's docs don't start as one point). **Antipodal/degenerate guard is unnecessary here** (a doc seeds from its single domain anchor, not a mean of opposing vectors — this removes the reviewer's Stage-1 cancellation gap by construction). Seed radius = `r_target`.

## Stage 2 — Simulate (hand-rolled, soft radial band)

Hand-rolled deterministic sim (not d3-force: the viz is dependency-free native ESM served statically with no bundler to pull npm d3, and d3's `jiggle()` uses `Math.random`, breaking determinism). Forces:

- **Domain-anchor pull:** each doc toward its domain anchor `(R_anchor, θ_domain)`. This is the main-map mechanic and the primary angular organizer.
- **Doc↔doc attraction** along shared-co-entity edges, strength ∝ normalized weight — refines within-neighbourhood position and lets a doc resist its domain pull when its real ties point elsewhere.
- **Collision** at `dotRadius + padding`; **zero-distance degenerate pairs separated deterministically by doc-id order** (closes the reviewer's determinism gap).
- **Soft radial band spring** (replaces v2 rev 1's hard projection): a strong spring toward `r_target`, but the doc may sit anywhere in its band; after each tick **clamp r into the band** (not onto `r_target`). This is what lets a domain consolidate radially and stops the territory-slicing the prototype found — while keeping radius an honest, bounded encoding of relevance.
- Deterministic termination (`maxTicks`/`alphaMin`); ≤ ~300 ms for 1,500 docs; may run in a worker.

## Stage 3 — Rasterize occupancy

1. Lattice `A×J`: `A = clamp(round(3·√n), 64, 160)`, `J = 24`.
2. Each doc casts gaussian votes for its domain into nearby cells; `d² = (Δθ·r_mid)² + Δr²`.
3. **Adaptive bandwidth** `h = c · median-NN distance`, c ≈ 1.5, clamp [0.5,3] cell diagonals (fixed bandwidth = failure mode #2).
4. Cell winner = argmax vote above `voteFloor`, else empty; runner-up ≥ `contestRatio`·winner → **contested** (assigned to winner for region math, flagged for render).

## Stage 4 — Region cleanup (θ wraps; radial does not)

1. **Despeckle:** assigned cell with zero same-domain 4-neighbours → empty.
2. **Islands:** per-domain flood-fill; components `< minRegionCells` dissolve into majority bordering domain (or empty); `≥ minRegionCells` survive as enclaves.
3. **Holes:** enclosed foreign/empty region — keep if it has docs, absorb if empty and small.
4. Cap enclaves at `maxEnclaves`, smallest-first; log dissolutions in diagnostics.

## Stage 5 — Boundary extraction + draw

1. Each occupied cell contributes 4 lattice edges; per domain, delete edges shared by two same-domain cells (exact coordinate match). **θ-seam:** normalize angular coordinates modulo `A` so the 0/2π edges of a seam-straddling domain match and cancel (closes the reviewer's spurious-12-o'clock-line gap).
2. Link surviving edges into closed loops. **Degree-4 pinch (diagonal touch) disambiguation:** at a vertex where two same-domain cells meet only at a corner with two foreign cells, split the crossing by a fixed rule (connect the pair that keeps each region on its own side — consistent clockwise turn), so the walk stays degree-2 and **every loop closes** (asserted). (Closes the reviewer's marching-squares gap.)
3. **Corner fillets:** ≈0.35-cell arcs, ≤ half the shorter adjacent edge.
4. **Draw guard (confetti suppression):** for each domain compute `coherence = largest-component-cells / total-cells`. Draw fill+border+label only for domains with `coherence ≥ coherenceFloor` OR `total-cells ≥ minSectorCells`; domains that rasterize to confetti (the prototype's fractured secondaries on huge entities) get **their docs coloured but no border/label** — no meaningless islands. Diagnostics record which domains were suppressed.
5. Render: per-domain fill (~8–12% alpha, `sectorColor` rule); filleted borders (~1px, ~80%); contested `'gap'|'hatch'` (default `'gap'`); one label per surviving domain on its largest component; docs as dots coloured by domain (kills the v1 monochrome bug); dot radius `base·clamp(√(300/n),0.5,1)`.
6. **Aggregation at scale:** cluster docs within `mergeRadius` (screen px) → one brighter dot + count badge, expanding on zoom. Render/hover only; votes use individual positions.

## Interaction (from v1, required)

- **Hover a sector or doc → highlight that domain across the map, dim the rest** (angle+radius → cell → domain, or hovered doc → its domain). Proven workable.
- Double-click a doc → existing detail panel. Unchanged.

---

## Parameters (one config object; no inline magic numbers)

| Name | Default | Notes |
|---|---|---|
| R0 / R1 | 60 / min(canvas)/2 − 60 | inner/outer map radii |
| BAND | 90 | radial freedom around `r_target` |
| R_anchor margin | +40 beyond R1 | domain anchor radius |
| anchorAngles | 'local' | 'local' \| 'global' \| 'blend' (strategy) |
| A / J | 3·√n clamp[64,160] / 24 | lattice |
| bandwidth c | 1.5 × median NN | clamp [0.5,3] cell diagonals |
| voteFloor | isolated doc claims ~1–3 cells | tune |
| contestRatio | 0.8 | |
| minRegionCells | 4 | island dissolution |
| maxEnclaves | 3 / domain | |
| coherenceFloor / minSectorCells | 0.35 / 12 | draw guard |
| seed spread | ±8° | hashed per doc |
| maxTicks / alphaMin | 300 / 0.001 | |
| mergeRadius | 8 px | |
| contestedStyle | 'gap' | |

## Non-goals

- Sector **regions** never influence placement; domain **point anchors** may pull docs (the main-map mechanic) — these are different, and the distinction is the rev-2 invariant.
- Radius is never frozen and never free — always within the relevance band.
- No per-band containers / row snapping / angular-span allocation — **delete v1 `assignRings`/`packRings`/slot code.**
- No global-galaxy positions consumed under `anchorAngles: 'local'`.

## Failure modes (each has bitten an attempt)

1. **Sector-first layout** (v1) — forbidden; a doc in a "wrong" territory is a labelling/graph fact.
2. **Fixed kernel bandwidth** → border bleed. Adaptive (3.3).
3. **Nondeterminism** → seeded, hashed jitter/tiebreaks, hand-rolled sim, id-tiebreak on degenerate collisions.
4. **Radial clotting** from linear `rel→r` → rank+sqrt (Stage 0).
5. **Territory slicing at scale** (measured): hard radial pin fractures multi-domain large entities regardless of seed. Fixed by the soft **band** (Stage 2) + the **draw guard** (Stage 5.4). Angular clustering is necessary but not sufficient; radial freedom is the other half.

## Acceptance criteria

1. **Radius honesty (band):** every doc's `r` is within its `[r_target ± BAND/2]` band at n ∈ {50, 300, 1200} — bounded, not exact.
2. **Determinism:** two runs on identical input → byte-identical points and sector loops.
3. **Orientation coherence:** ≥ 70% of docs within ±45° of their domain anchor angle after the sim (the sim refines, not scrambles), measured on medium entities; large entities reported, not gated (radial band changes the achievable bar).
4. **Territory coherence + honest suppression:** every *drawn* sector has `coherence ≥ coherenceFloor`; domains below it are suppressed (docs coloured, no border) and counted in diagnostics. At n=1200 contested ≤ 15% of assigned cells; no domain over `maxEnclaves`; every extracted loop closes (asserted). This is the criterion v2 rev 1 could not meet and the band+guard target.
5. **Blank-space bound:** at n ≥ 300, no empty annulus > 2 rings, no empty wedge > 30° (given docs span domains).
6. **Performance:** seed→outline ≤ 500 ms at n=1500; re-rasterize alone ≤ 50 ms.
7. **Visual regression:** headless snapshots at n ∈ {50, 300, 1200} (playwright harness from v1 review).

## Staged delivery (validate each stage on the previous stage's points)

1. **Stage 0–1:** render seeded points; verify radius band + domain-anchor seeding; debug overlay of anchor angles.
2. **Stage 2:** points spread within bands; assert `r ∈ band` per tick in dev builds; check orientation coherence.
3. **Stage 3–4:** debug lattice overlay (assignments + contested).
4. **Stage 5:** borders, fillets, labels, contested styling, **draw guard**, domain doc colours, hover.
5. **Aggregation + zoom last.**

Debug in pipeline order: is the point somewhere strange (0–2) before asking whether the border drew wrong (3–5).

## Testing

- Pure stages (0,1,3,4,5) unit-tested with `node --test`: radius mapping monotonic + banded; Fiedler ordering deterministic on a hand-built domain graph (+ single-domain/disconnected fallback); rasterize winner/contested; despeckle/island/hole transitions; edge-cancellation closes loops incl. a θ-seam case and a degree-4 pinch case; draw-guard suppresses a confetti fixture and keeps a coherent one.
- Stage 2: assert `r ∈ band` across ticks; orientation-coherence bound on a fixture; determinism (two runs identical).
- Snapshot/visual regression via the headless harness.
- Backend unchanged; v1 `test_star_graph_orbital.py` still applies.

## Kept vs deleted

- **Kept:** all backend (`domain_palette`, per-doc `domain_path`/`n_entities`, `palette`, `co_limit=150`); `midLevel`, `orderDomains`, `sectorColor`, `documentStrength`; palette wiring; hover plumbing; the headless render harness.
- **Deleted:** `assignRings`, `packRings`, ring/slot/arc packing + tests, `drawSectors` wedge drawing. Replaced by Stages 0–5.
