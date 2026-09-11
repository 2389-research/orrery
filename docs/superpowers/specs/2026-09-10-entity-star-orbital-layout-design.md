# Entity Star Page — Orbital Layout by Connection Strength

**Status:** SUPERSEDED by `2026-09-10-entity-segmentum-view-design.md` (2026-09-10). The
arc-packing approach here made sectors a layout primitive; live feedback showed that
produces rigid, dead-space pie-slices. v2 inverts it: place documents first, derive
sectors after by rasterizing domain occupancy. The **backend work in this doc still
stands** (`get_star_graph` payload additions, `domain_palette`); only the client
layout is replaced.
**Date:** 2026-09-10
**Scope:** `frontend/public/viz/star.html`, `frontend/public/viz/renderers/star.js`, `orchestrator/src/repositories/sqlite_store.py::get_star_graph`, `orchestrator/src/routes/entities.py`.

## Problem

The entity double-click page (`star.html`) was designed when entities were small. It places documents on a single fixed ring (`docRing = 180`), co-entities on another (`coRing = 380`), and repos on a third (`repoRing = 480`) — radii that never change with count. A large entity (`openai` has **631 documents**) crushes hundreds of labels onto an 180px-radius circle: at 631 docs that is ~1.8px of arc per doc against ~60px labels. The result is the illegible blob in the current screenshots. The page does not scale, does not use the available space, and encodes no structure.

The **collection page** (`collection.html`) scales well because it exploits the directory hierarchy: each depth is an orbit ring, siblings share a ring, and a subtree's angular span is proportional to how many files it holds (`collection.html:251-271`, Reingold–Tilford radial tree). Entities have no such hierarchy — the "less defined levels" problem.

## Goal

Give the entity page a **two-axis polar layout** that scales to large entities and makes the space carry meaning, taking direct inspiration from the collection page:

- **Radius = connection strength.** Documents most strongly about the entity orbit closest.
- **Angle = domain.** Documents group into domain sectors, so the layout has readable structure the way the collection page's directory branches do.

The visual target is a galactic-sector map (Warhammer 40k / Total War): concentric orbit rings overlaid with non-uniform domain sectors whose arcs swell and pinch ring by ring, cutting into and out of one another.

## What the data supports (measured, not assumed)

All figures measured against `~/orrery-data/_lightmode_data/workspaces/default/orrery.db`.

### Connection strength — the metric

Literal mention frequency ("openai mentioned 50×") is **not in the database**: 98.6% of `(entity, document)` pairs have exactly one `entity_sources` row, and 96% of documents are single-chunk, so there is no per-document count to read without re-reading document text.

The servable metric is **document entity-share**: for a document that mentions the entity, its share = `1 / (number of distinct entities extracted from that document)`. A doc about openai with 5 entities gives openai a 0.20 share; a 400-entity eval gives it 0.0025. This is one `GROUP BY` (measured **19 ms** for openai's 631 docs, no index tuning) and reuses the codebase's existing normalized-weight idiom (`graph_reads.py::domain_memberships` already computes `count/total` shares per domain).

Properties that shape the design:
- Share ranges **0.200 → 0.00069** for openai (291× spread) — a real, discriminating signal, unlike raw `co_occurs` weight (median 1 across 2,130 co-entities).
- Because `share = 1/ndoc` and `ndoc` clusters on small integers, shares are **quantized and tied**: openai's top 10% holds only 3 distinct share values (40 docs tie at exactly 0.167). **Ties are intended** — those docs are equally about the entity and belong at the same radius; the domain angle separates them. The design must not treat ties as a defect (an earlier draft did, wrongly). This directly constrains ring assignment below: a ring boundary may never split a tied share value.
- `n_entities` (the share denominator) counts only **active** entities (`invalid_at IS NULL`), matching the soft-delete-aware reads the rest of the graph uses. The same `invalid_at IS NULL` filter that `get_star_graph` already applies to co-entities and documents applies here.

### Domain structure — real and universal

Every document has a primary domain (`document_domains.is_primary = 1`); **0%** were unclassified across every entity checked. Domains are meaningfully concentrated:

| Entity | docs | distinct leaf domains | top-3 leaf cover |
|---|---|---|---|
| openai | 631 | 69 | 54% |
| gemini | 641 | 39 | 68% |
| fastapi | 209 | 32 | 54% |
| docker | 132 | 49 | 37% |
| pydantic | 40 | 17 | 40% |

69 leaf domains is too many to draw as sectors, so sectors roll up to the **mid-level path** (`software/testing-qa`, `software/ai-agents`): openai collapses to ~26 mid-level groups. The tail of small domains is always bucketed into one neutral-grey sector labelled **"misc"** (drawn last in order).

**The named-sector count is adaptive, not a hard top-8**, because a fixed cutoff leaves misc as a dominant grey wedge on some entities (measured 17% for docker, 26% for a thin-spread entity — competing visually with the real sectors — versus 0–11% for concentrated ones like gemini/openai). Rule: draw the largest domains as named sectors, adding more until misc falls **≤ 10%** of the entity's documents, up to a hard cap of **12** named sectors. If misc is still > 10% at 12 sectors, draw it as-is: that entity genuinely lacks concentrated domain structure, and a large misc is the honest depiction. No document is ever dropped or recoloured — misc keeps its own grey.

### Strength and domain are correlated — and that is the feature

Radius (strength) and angle (domain) are not orthogonal, but they do not collapse either. A domain concentrates at a **characteristic radius**, and different domains dominate different rings. Per-ring arc widths for openai (each cell = that domain's share of the ring's populated arc):

```
                 inner ring →→→ outer ring
devops-infra       38%   34%   12%    1%    fat inner wedge, pinches to nothing
ai-agents          33%   33%   22%   12%
testing-qa          3%    5%   44%   53%    absent inner, dominates outer
```

`devops-infra` (API-usage monitoring) owns the inner arc; `testing-qa` (llm-judge evals) owns the outer arc; they trade places moving outward. gemini is sharper (devops 57% inner, testing-qa 76% outer). This is exactly the Segmentum-Obscurus-enveloping-the-Eye-of-Terror shape from the reference, and it is legible: a fat inner arc means "docs where the entity is the point"; a fat outer arc means "docs where the entity is incidental (evals citing everything)." The shape of the crescents is the meaning.

## Layout model

Rings are the primary structure; domain arcs are packed within each ring independently.

### Small entities use a simpler layout (below `MIN_DOCS_FOR_RINGS = 20`)

The four-ring percentile scheme is meaningful only when there are enough documents to fill rings. Below the threshold it degenerates in two measured ways, so **an entity with fewer than 20 documents skips the ring/sector machinery entirely** and uses a basic single-tier layout:

- **radius = strength directly** (continuous linear map of share to `[R_inner, R_outer]`, strongest innermost) — no percentile bands. This is what fixes the inversion described below and guarantees "most-connected orbits closest" for exactly the small entities where a human can read every dot.
- **angle = mid-level domain**, docs grouped into contiguous arcs and evenly slotted, same colour rule as the full layout — a single crescent, the look that already works (reference image 11).

Why the threshold exists (both are real, measured):
- **Radius inversion.** The ring rule below assigns by cumulative fraction tested *after* adding a share value, so when the strongest document is itself > 10% of the entity's docs (unavoidable at ≤ 10 docs), it is pushed *out* of ring 1 — `inter` (5 docs) seats its strongest doc on ring 2 with ring 1 bare; `community-detection` (2 docs) lands it on ring 3. The strongest doc drawn farthest out is backwards. The tie-safe rule is correct at scale; it is simply the wrong tool at small N.
- **Arc blow-up.** `arc_per_doc = MAX_FILL / Nmax` (below) explodes when the busiest ring is tiny: `Nmax = 1` makes a single document a 340° coloured wedge. The small-N layout sidesteps this; the floor below is a second guard.

`MIN_DOCS_FOR_RINGS` is a tunable constant. Note that a *medium* entity with many domains relative to docs (e.g. 27 docs across 15 domains) sits above the threshold but still shows weak breathing — too few docs per (ring × domain) cell — and reads as scattered wedges rather than crescents. This is accepted: such an entity genuinely lacks concentrated structure, and the wedges are still far better than the current blob. No extra mechanism for it.

### Rings (radius)

Four orbit rings at **percentile targets of strength**: top 10% / next 20% / next 30% / next 40%, inner → outer. The rings are drawn at fixed radii (`R1 < R2 < R3 < R4`).

**Assignment rule (tie-safe), stated precisely so a planner cannot build it two ways:**
1. Collect the entity's documents and their shares. Reduce to the **distinct share values**, sorted descending (strongest first).
2. Walk the distinct values strongest-first. For each value, add its document count to the running total *first*, then test the running fraction `cum/total`: the value's ring is 1 if `cum/total ≤ 0.10`, 2 if `≤ 0.30`, 3 if `≤ 0.60`, else 4. (Tested **after** adding, so the value that crosses a boundary falls into the outer band, and a tie group is kept whole.) The ring is chosen per **distinct value**, so **every document sharing a value lands in the same ring** — satisfying the ties invariant and test (d).
3. Because a whole tie group is placed together, a band can be over- or under-filled when a large tie group straddles a target (e.g. if 15% of docs share one value, ring 1 holds ~15%, not exactly 10%). This is intended: the percentages are targets, not hard quotas, and the tie group is never split. The assignment is deterministic given a stable sort (sort distinct values descending; the input doc order does not matter).

- Rings are the skeleton and are drawn as faint concentric guide circles (as `collection.html:409-413` draws depth rings).
- A ring with zero documents in its band is **bare** — no error, no stretching. Bare rings and partial crescents are desired output, not defects.
- **Ring radii are load-bearing, not cosmetic.** Because share = 1/n and n skews large, documents concentrate on the outer two rings (measured: 79–84% of docs on R3+R4 for openai/gemini, with R1 near-empty). Evenly-spaced radii would waste the inner half of the canvas and crowd the outer dots. The radii must therefore bias outward — either fixed values weighted toward R3/R4 (a proposed starting set: `R1=150, R2=280, R3=410, R4=560`), or spacing that adapts to each ring's population so a near-empty inner ring sits closer in. Note that because `arc_per_doc` is uniform in *degrees*, per-doc gap in *pixels* grows with radius (openai R1 ≈ 3px/doc, R4 ≈ 11px/doc), so pushing the busy outer rings to larger radii directly buys dot legibility.

### Domain sectors (angle) — non-uniform, per-ring

Within **each ring independently**, the documents present are grouped by their mid-level domain, and each domain gets a contiguous arc whose angular width ∝ the number of documents that domain has **in that ring**. This is what makes the arcs breathe: a domain that is fat on the inner ring can pinch to nothing on the outer ring, and its neighbours expand into the freed angle.

Two invariants keep it from reading as chaos:

1. **Global domain order is fixed** across all rings. Order domains by total document count (across the whole entity, not per ring), breaking ties by domain path for determinism; the "misc" tail sector is always last. Every ring lays its present domains out in this same order, so a domain keeps its **ordinal position** (e.g. always the third arc) between rings even as its width breathes. Its *absolute* angle drifts as spans change — that drift is the intended cut-in/cut-out, not a bug — but the eye tracks it by order, colour, and neighbours.

2. **Arc-per-document scale is derived from the busiest ring, so nothing can overflow.** Let `MAX_FILL` be the largest fraction of the circle any ring may occupy (proposed **340°**, leaving a ~20° gap so the arc's start/end is visible). Let `Nmax` be the document count of the busiest ring. Then `arc_per_doc = MAX_FILL / Nmax`, applied uniformly to **every** ring. Consequences, stated so the earlier ambiguity is closed:
   - The busiest ring occupies exactly `MAX_FILL`; every other ring occupies `MAX_FILL × (N_ring / Nmax)` — strictly proportional, a smaller crescent.
   - No ring can exceed `MAX_FILL`, so there is no upper clamp and no "ring overflows the cap" case. `arc_per_doc` is derived, not an independent constant; `MAX_FILL` is the single tunable for density.
   - **Lower floor:** `arc_per_doc` is floored at `ARC_PER_DOC_MAX` (proposed **12°**) so a ring with very few documents does not render each one as an enormous wedge. This only ever engages above the small-N threshold (a busiest ring of, say, 4 docs in an otherwise larger entity); at `arc_per_doc` below the floor the busiest ring simply occupies less than `MAX_FILL`. Below `MIN_DOCS_FOR_RINGS` the simpler layout runs instead and this does not apply.

**Angular anchoring:** each ring's populated arc is **centred on the 12-o'clock axis** (−90°). A ring occupying angle `A = N_ring × arc_per_doc` spans `[−90° − A/2, −90° + A/2]`, and domains fill it left-to-right in the fixed global order. A sparse ring is therefore a small crescent centred at top; a full ring wraps toward the bottom. This is the deterministic rule the co-entity placement below depends on.

Within a domain's arc on a ring, documents are laid out at equal angular slots (the collection page's `(_leaf + 0.5)/total` mechanic, `collection.html:259`), so spacing is uniform inside the arc.

### Documents outside a domain's own ring

A document's radius is its strength ring; its angle is its domain's arc *on that ring*, under the centred anchor above. Because arc widths and the ring's total span are computed per ring, a domain's arc on ring 1 and on ring 3 differ in both width and absolute angle — that is the intended breathing. Fixed ordinal order is what lets the eye still track a domain across rings.

### Co-entities and collections

- **Co-entities** are placed at the circular mean of the angles of their shared documents (as `star.html:295-301` already does). The shared-doc angles are well-defined by the centred per-ring anchor above, so a co-entity sits in the domain neighbourhood it actually relates to. They occupy a **thick band beyond the outermost document ring**, not a single thin circle: the band spans `[R_band_inner, R_band_outer]`, and a co-entity's radius within it is set by its strength (`shared_docs / docs_of_entity`, a Jaccard-style share — stronger co-entities toward the inner edge, nearer the entity) plus a small deterministic jitter so equal-strength nodes at similar angles do not stack. The band widens with count (thicker when there are more co-entities) so the outer band never becomes the new pile. This is deliberately **not** the four-ring document scheme — it is one graduated band. It reuses the `co_occurs` shared-doc data the payload already carries; raw `co_occurs` weight is not used for radius (it mostly measures how popular the *other* entity is).
  - Context: the collection page never shows entities at rest — it materialises them on demand, fanned out from the clicked document (`collection.html:306`). The star page instead keeps co-entities in the resting view, so the graduated band above is what prevents the ~150 (capped) co-entities from re-blobbing. This is strictly better than the current single-radius placement, which is the pile in reference image 9.
- **Collections** (repos the entity belongs to) are **out of scope** for this work. `get_star_graph` returns no `collections` today, so the repo ring is presently always empty and `star.html` defaults it; it stays that way. Populating it is a separate change.

## Sector notation

- Each named sector's boundary is drawn as sweeping radial lines at its arc edges, spanning the rings it occupies, in the domain's colour (Total War / 40k styling). Because widths vary per ring, a sector boundary is a stepped/blocky radial edge, not a straight spoke — this is the "cut into and out of one another" look.
- A faint domain-colour tint fills the sector's populated cells.
- The sector is labelled once, on its widest ring, with the mid-level domain name.
- The "misc" tail sector is drawn in a neutral grey with no per-domain tint, labelled "misc".
- **Documents are unlabelled dots; only sectors and co-entities carry text.** This is load-bearing for legibility: even under this layout a huge entity's outer ring is dense (openai's ring 4 ≈ 11px of arc per doc, gemini ≈ 10px), which reads fine as dots but would re-blob instantly if each document were labelled. Document identity surfaces on hover/click, as it does today — never as resting labels.

## Colour — fixes the current monochrome bug

`star.js`'s `TYPE_COLORS` table keys on `Person/Organization/Product/…`, but the real entity-type vocabulary is `capability/interface/pattern/concept/data_model/integration/dependency/…` — **zero overlap**, so `typeColor()` returns default grey for every node today (the reason the current page is monochrome). The redesign colours documents and sectors by **domain**, using the same palette the galaxy uses. The star page must receive a domain→colour map, and getting it right requires care because the palette function is **set-dependent**:

- `assign_domain_colors()` (`graph_snapshot.py:62`) derives `branch_level`, `top_level_names`, `slice_size`, and each path's golden-ratio index from the **entire** domain list it is given. Run over a star's subset of domains, it returns **different** hexes for the same `domain_path` than the galaxy does. So "compute for the domains present" would silently disagree with the galaxy.
- **Decision:** `get_star_graph` computes the palette over the **same domain set the galaxy uses** — `store.domains.list(min_doc_count=1)` mapped to `{"path": …}` dicts, exactly as `graph_v5.py:74-77` then `:104` — and **filters** the resulting map to the leaf domains present on this star. This exact argument matters: `assign_domain_colors` is set-dependent, and `store.domains.list()` (default `min_doc_count=0`) would include zero-doc domains the galaxy excludes, shifting the golden-ratio index of every later leaf in a family and silently diverging the hexes. Same input set (`min_doc_count=1`) → identical hexes → the star and galaxy agree. The palette is keyed by **leaf `domain_path`**, which always resolves (a document's primary domain is a real row). Measured: 58 of 61 mid-level roll-up paths are themselves domain rows, but 3 are not (e.g. `technology/databases`), so a mid-level path is **not** a reliable palette key.
- **Sector colour rule (always resolvable):** a sector's colour is the palette hex of its mid-level path if present in the filtered map, else the hex of the **first present leaf under it** (a sector exists only because ≥1 of its leaves is present on the star, so a present leaf always exists; take them in sorted order for determinism). Does not require synthetic domain rows.
- On the star page the `palette` is the **primary** colour source, not a fallback. The galaxy runs client-side `assignDomainColors()` over its full domain list and treats `layout.palette` only as a fill-in (`state.js:93-96`); the star page has no full domain list to run that over, so it reads colours directly from the server `palette`. If a document's leaf `domain_path` is missing from the map (defensive), fall back to a neutral grey.

## API changes (`get_star_graph`)

The layout needs per-document domain and entity-count, neither of which the payload carries today. Current shape:

```
documents:   [{id, title, content_type}]
co_entities: [{id, canonical_name, type, weight, shared_doc_ids}]
```

Changes:

1. **Add to each document:** `domain_path` (the primary domain, full leaf path — the client rolls up to mid-level for the sector and keeps the leaf for the exact document dot colour) and `n_entities` (count of **active** entities extracted from that document, `COUNT(DISTINCT entity_id)` filtered `invalid_at IS NULL` — the share denominator, matching the soft-delete-aware graph). Both come from joins already adjacent to the existing query.
2. **Add `palette`:** `{leaf domain_path → hex}` covering every leaf domain present, computed over the full graph domain set then filtered (see Colour). Sector colours are derived client-side from this by the sector-colour rule.
3. **Raise the co-entity cap.** `co_limit` defaults to **30** in both the route (`entities.py:36`) and the client fetch (`star.html:150`); openai has 2,130 co-entities, so the page already silently shows 30. Raise the default (proposed **150**) and keep it a query parameter. Documents were never capped and remain uncapped.
4. **Strength is computed client-side** from `n_entities` (the client owns layout math), so no strength field is added to the payload. The server provides the raw denominator only.

Backward compatibility: the new fields are additive. The `weight`/`shared_doc_ids` fields stay for the co-entity strength calculation.

## Non-goals

- No pipeline or extraction change. Strength is derived from existing `entity_sources` / `document_domains` rows.
- No re-ingestion. Mention-frequency is explicitly out of scope (not in the data; would require re-reading documents).
- The collection page is not modified; it is the reference, not a target.
- Light-mode work stays parked.
- The galaxy (`index.html`) and its renderers are untouched.

## Testing

- **Layout unit tests** (pure functions, no canvas): extract the ring-assignment and per-ring sector-packing into testable functions and assert: (a) ring assignment hits the percentile targets when shares are distinct; (b) a domain absent from a ring yields zero arc there and its neighbours' arcs sum to the ring's total span; (c) global domain order (by total doc count, path tiebreak, "misc" last) is identical across rings; (d) a tie group is never split across rings even when it straddles a target, and the band is allowed to over/under-fill as a result; (e) a single-domain entity produces one arc; (f) the busiest ring occupies exactly `MAX_FILL` (when above the `arc_per_doc` floor) and every other ring occupies `MAX_FILL × N_ring/Nmax`; (g) each ring's arc is centred on −90°.
- **Small-N and degenerate tests:** (h) an entity below `MIN_DOCS_FOR_RINGS` uses the simple layout, and its strongest document is placed at the inner radius, not an outer one (the inversion regression); (i) a 1- or 2-doc entity does not produce a 340°/170° wedge (either the small-N path or the `arc_per_doc` floor engages); (j) the misc-sector rule: named-sector count grows until misc ≤ 10% or hits the 12 cap, misc keeps its own grey, and no document is dropped or recoloured.
- **API test** (`get_star_graph`): assert each document carries `domain_path` and `n_entities` (the latter counting only active entities); that `palette` covers every leaf domain present and that its hexes equal the **galaxy's** for the same paths — compare against a palette built the way `graph_v5` builds it (`assign_domain_colors` over `store.domains.list(min_doc_count=1)`), **not** re-derived from the star's own subset or from `min_doc_count=0`, so a divergence in the input set actually fails the test; that `co_limit` is honoured and defaults to the new value; and that an entity whose docs somehow lack a primary domain still returns a usable payload (grey fallback).
- **Regression:** an entity with one document and no co-entities renders without divide-by-zero (empty rings, single arc).
- Follow the existing `orchestrator/tests/` file-backed SQLite fixture convention (never `:memory:`, per CLAUDE.md).

## Resolved during review

- **Small entities** (< `MIN_DOCS_FOR_RINGS`, proposed 20) use a simpler single-tier layout (radius = strength directly), fixing both the radius inversion and the giant-wedge blow-up. (User decision.)
- **Tail sector** is labelled "misc", grey, with an adaptive named-sector count (grow until misc ≤ 10%, cap 12) so it never dominates. (User decision.)
- **Co-entities** stay in the resting view as a thick, strength-graduated, jittered band beyond the document rings — not the four-ring scheme, not a single thin circle. (User decision.)

## Open questions for review

1. Tunable constants — `MIN_DOCS_FOR_RINGS` (20), `MAX_FILL` (340°), `ARC_PER_DOC_MAX` floor (12°), the misc threshold (10%) and cap (12), and the ring radii — all have proposed defaults above. Confirm whether any should be query params like `collection.html`'s `?scale=`, or all hardcoded. Not blocking; defaults exist.
2. Ring radii: fixed outward-biased set (`R1=150, R2=280, R3=410, R4=560`) versus population-adaptive spacing. The plan can start fixed and revisit; flagged because the data shows spacing materially affects space usage.
