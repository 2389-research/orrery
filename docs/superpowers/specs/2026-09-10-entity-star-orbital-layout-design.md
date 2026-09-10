# Entity Star Page — Orbital Layout by Connection Strength

**Status:** Design
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
- Because `share = 1/ndoc` and `ndoc` clusters on small integers, shares are **quantized and tied**: openai's top 10% holds only 3 distinct share values (40 docs tie at exactly 0.167). **Ties are intended** — those docs are equally about the entity and belong at the same radius; the domain angle separates them. The design must not treat ties as a defect (an earlier draft did, wrongly).

### Domain structure — real and universal

Every document has a primary domain (`document_domains.is_primary = 1`); **0%** were unclassified across every entity checked. Domains are meaningfully concentrated:

| Entity | docs | distinct leaf domains | top-3 leaf cover |
|---|---|---|---|
| openai | 631 | 69 | 54% |
| gemini | 641 | 39 | 68% |
| fastapi | 209 | 32 | 54% |
| docker | 132 | 49 | 37% |
| pydantic | 40 | 17 | 40% |

69 leaf domains is too many to draw as sectors, so sectors roll up to the **mid-level path** (`software/testing-qa`, `software/ai-agents`): openai collapses to ~26 mid-level groups. The design draws the **top 8 by document count as named sectors and buckets the tail into one "other" sector**.

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

### Rings (radius)

Four orbit rings at fixed **percentile cutoffs of strength**: top 10% / next 20% / next 30% / next 40%, inner → outer. Compute the cutoffs from the entity's own share distribution, assign each document to a ring by its share, and draw the rings at fixed radii (`R1 < R2 < R3 < R4`).

- Rings are the skeleton and are drawn as faint concentric guide circles (as `collection.html:409-413` draws depth rings).
- A ring with zero documents in its percentile band is **bare** — no error, no stretching. Bare rings and partial crescents are desired output, not defects.
- Ties (many docs at one share) land in the same ring by construction; angle separates them.

### Domain sectors (angle) — non-uniform, per-ring

Within **each ring independently**, the documents present are grouped by their mid-level domain, and each domain gets a contiguous arc whose angular width ∝ the number of documents that domain has **in that ring**. This is what makes the arcs breathe: a domain that is fat on the inner ring can pinch to nothing on the outer ring, and its neighbours expand into the freed angle.

Two invariants keep it from reading as chaos:
1. **Global domain order is fixed** across all rings (by total document count, then domain path for determinism), so a domain keeps the same angular *neighbourhood* between rings even as its width changes — the sector still reads as a sector.
2. **A fixed arc-per-document scale**, shared by all rings, sets angular width. The busiest ring fills most of the circle (capped at ~340°, leaving a small gap so the ring's start/end is visible); sparser rings occupy proportionally less — a thin ring is a small crescent, not a stretched full circle.

Within a domain's arc on a ring, documents are laid out at equal angular slots (the collection page's `(_leaf + 0.5)/total` mechanic, `collection.html:259`), so spacing is uniform inside the arc.

### Documents outside a domain's own ring

A document's radius is its strength ring; its angle is its domain's arc *on that ring*. Because arc widths are computed per ring, a domain's arc on ring 1 and its arc on ring 3 need not be the same angular span — that is the intended breathing. The domain's fixed order anchors it so the eye still tracks it across rings.

### Co-entities and collections

- **Co-entities** are placed at the circular mean of the angles of their shared documents (as `star.html:295-301` already does), so a co-entity sits in the domain neighbourhood it actually relates to, at a radius just beyond the outermost ring. Strength between the entity and a co-entity uses `shared_docs / docs_of_entity` (a Jaccard-style share) rather than raw `co_occurs` weight, which mostly measures how popular the *other* entity is.
- **Collections** (repos the entity belongs to) keep their current outer-periphery ring, unchanged.

## Sector notation

- Each named sector's boundary is drawn as sweeping radial lines at its arc edges, spanning the rings it occupies, in the domain's colour (Total War / 40k styling). Because widths vary per ring, a sector boundary is a stepped/blocky radial edge, not a straight spoke — this is the "cut into and out of one another" look.
- A faint domain-colour tint fills the sector's populated cells.
- The sector is labelled once, on its widest ring, with the mid-level domain name.
- The "other" sector is drawn in a neutral grey with no per-domain tint.

## Colour — fixes the current monochrome bug

`star.js`'s `TYPE_COLORS` table keys on `Person/Organization/Product/…`, but the real entity-type vocabulary is `capability/interface/pattern/concept/data_model/integration/dependency/…` — **zero overlap**, so `typeColor()` returns default grey for every node today (the reason the current page is monochrome). The redesign colours documents and sectors by **domain**, using the same palette the galaxy uses. The star page must receive a domain→colour map:

- The galaxy computes colours via `assignDomainColors()` (`utils.js:87`) plus the payload's `layout.palette`. The star payload currently carries no palette.
- **Decision:** `get_star_graph` returns a `palette` map (`domain_path → hex`) for the domains present, computed the same way the galaxy payload computes it, so the star page and the galaxy agree on a domain's colour. `star.html` applies it exactly as `state.js:93-96` does.

## API changes (`get_star_graph`)

The layout needs per-document domain and entity-count, neither of which the payload carries today. Current shape:

```
documents:   [{id, title, content_type}]
co_entities: [{id, canonical_name, type, weight, shared_doc_ids}]
```

Changes:

1. **Add to each document:** `domain_path` (the primary domain, full leaf path — the client rolls up to mid-level and keeps the leaf for the exact palette colour) and `n_entities` (distinct entities extracted from that document, i.e. the share denominator). Both come from joins already adjacent to the existing query.
2. **Add `palette`:** `{domain_path → hex}` for the domains present.
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

- **Layout unit tests** (pure functions, no canvas): extract the ring-assignment and per-ring sector-packing into testable functions and assert: (a) percentile cutoffs put the right doc counts in each ring; (b) a domain absent from a ring yields zero arc there and its neighbours' arcs sum correctly; (c) global domain order is stable across rings; (d) ties in strength land in one ring; (e) a single-domain entity produces one full sector; (f) the busiest ring is capped at the max fill angle and sparser rings scale down proportionally.
- **API test** (`get_star_graph`): assert each document carries `domain_path` and `n_entities`, that `palette` covers every domain present, that `co_limit` is honoured and defaults to the new value, and that an entity with unclassified docs (should be none in practice, but defensively) still returns a usable payload.
- **Regression:** an entity with one document and no co-entities renders without divide-by-zero (empty rings, single arc).
- Follow the existing `orchestrator/tests/` file-backed SQLite fixture convention (never `:memory:`, per CLAUDE.md).

## Open questions for review

1. Ring radii and the max-fill cap (~340°) are proposed constants; confirm they should be tunable query params (like `collection.html`'s `?scale=`) rather than hardcoded.
2. Whether the "other" sector should be omitted below a threshold (e.g. if the top 8 already cover >95%).
3. Whether co-entity strength (`shared/docs_of_entity`) should also drive a co-entity's radius, or only documents get the orbital treatment while co-entities stay a single outer band.
