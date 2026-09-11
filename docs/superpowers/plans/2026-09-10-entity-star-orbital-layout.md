# Entity Star Orbital Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the entity double-click page's fixed single-ring layout with a two-axis polar layout — radius = connection strength (four rings), angle = domain sectors packed per-ring so arcs breathe — that scales to large entities.

**Architecture:** The layout math becomes a pure ESM module (`core/star-layout.js`), unit-tested with `node --test`, so `star.html` stays a thin renderer that imports it (per CLAUDE.md: the viz is self-contained modules, not React). The `get_star_graph` API method gains three additive fields (`n_entities` and `domain_path` per document, plus a `palette` computed the same way the galaxy computes it). The canvas renderer (`renderers/star.js`) gains sector drawing and colours by domain instead of the dead `TYPE_COLORS` table.

**Tech Stack:** Python 3.12 (FastAPI orchestrator, pytest, file-backed SQLite fixtures), vanilla ESM + Canvas2D (viz), `node --test` for the layout unit tests.

**Spec:** `docs/superpowers/specs/2026-09-10-entity-star-orbital-layout-design.md` — read it first; this plan implements it exactly, including the constants (`MIN_DOCS_FOR_RINGS=20`, `MAX_FILL=340°`, `ARC_PER_DOC_MAX=12°`, misc threshold 10% / cap 12, ring radii `150/280/410/560`).

---

## File Structure

**New:**
- `frontend/public/viz/core/star-layout.js` — pure layout math, no DOM/canvas. All the ring/sector/small-N/colour/co-entity functions. The one place the spec's algorithm lives.
- `frontend/public/viz/core/star-layout.test.mjs` — `node --test` unit tests for star-layout.js (spec tests a–j).
- `orchestrator/tests/test_star_graph_orbital.py` — API tests for the new payload fields + palette-agrees-with-galaxy.

**Modified:**
- `orchestrator/src/pipeline/graph_snapshot.py` — add `domain_palette(conn)` helper next to `assign_domain_colors`; the single source both the galaxy and the star page use (prevents the two palettes drifting).
- `orchestrator/src/pipeline/graph_v5.py:104-108` — replace the inline palette build with a `domain_palette(conn)` call (must produce byte-identical output).
- `orchestrator/src/repositories/sqlite_store.py:558-614` — `get_star_graph`: add `n_entities` + `domain_path` per doc, add `palette`, raise `co_limit` default to 150.
- `orchestrator/src/routes/entities.py:36` — raise the route's `co_limit` default to 150.
- `frontend/public/viz/star.html:237-360` — `buildStarView` delegates document + co-entity placement to `star-layout.js`; applies the server `palette`.
- `frontend/public/viz/renderers/star.js` — draw domain sectors; colour documents/central star by domain (not `TYPE_COLORS`); draw the co-entity band.

**Constants live in `star-layout.js`** as named exports so the tests and the renderer share one definition.

---

## Task 1: `domain_palette` helper — one palette source for galaxy and star

The spec requires the star page's colours to match the galaxy's exactly. `assign_domain_colors` is set-dependent, so both must compute over the same domain set (`min_doc_count=1`). Extract the galaxy's inline palette build into a shared helper so they cannot drift.

**Files:**
- Modify: `orchestrator/src/pipeline/graph_snapshot.py` (add helper after `assign_domain_colors`, ~line 108)
- Modify: `orchestrator/src/pipeline/graph_v5.py:104-108` (use the helper)
- Test: `orchestrator/tests/test_star_graph_orbital.py` (new file, first test)

- [ ] **Step 1: Write the failing test**

Create `orchestrator/tests/test_star_graph_orbital.py`:

```python
# ABOUTME: The star page must colour domains identically to the galaxy, and carry the
# ABOUTME: per-doc domain + entity-count the orbital layout needs. These guard both.

from src.pipeline.graph_snapshot import domain_palette, assign_domain_colors


def _seed_domains(conn, paths_with_counts):
    for path, n in paths_with_counts:
        conn.execute(
            "INSERT INTO domains (id, path, parent_path, document_count) VALUES (?,?,?,?)",
            (path, path, "/".join(path.split("/")[:-1]) or None, n))
    conn.commit()


def test_domain_palette_matches_min_doc_count_1_set(test_store):
    c = test_store.conn
    _seed_domains(c, [("software/ai-agents", 5), ("software/testing-qa", 3),
                      ("software/zero-doc", 0)])
    pal = domain_palette(c)
    # Built over the min_doc_count>=1 set, exactly as the galaxy does.
    expected = assign_domain_colors([{"path": "software/ai-agents"},
                                     {"path": "software/testing-qa"}])
    assert pal["software/ai-agents"] == expected["software/ai-agents"]
    assert pal["software/testing-qa"] == expected["software/testing-qa"]
    # The zero-doc domain is excluded, so it does not shift the others' hues.
    assert "software/zero-doc" not in pal
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py::test_domain_palette_matches_min_doc_count_1_set -v`
Expected: FAIL — `ImportError: cannot import name 'domain_palette'`.

- [ ] **Step 3: Add the helper**

In `orchestrator/src/pipeline/graph_snapshot.py`, immediately after `assign_domain_colors` returns (after its `return color_map`), add:

```python
def domain_palette(conn) -> dict[str, str]:
    """The domain→hex map, computed over the SAME domain set the galaxy uses.

    `assign_domain_colors` is set-dependent (branch_level, slice_size and each
    family's golden-ratio index all derive from the input list), so any consumer
    that wants to agree with the galaxy MUST feed it the identical set:
    domains with document_count >= 1, ordered by path. This is that single source —
    used by the galaxy payload (graph_v5) and the star page (get_star_graph) so the
    two cannot drift.
    """
    rows = conn.execute(
        "SELECT path FROM domains WHERE document_count >= 1 ORDER BY path"
    ).fetchall()
    domains = [{"path": r["path"]} for r in rows]
    palette = assign_domain_colors(domains)
    # Region fallback, identical to the galaxy's (graph_v5): a top-level region key
    # so a node with no exact-path colour still gets its family hue.
    for d in domains:
        region = d["path"].split("/")[0]
        if region not in palette:
            palette[region] = palette.get(d["path"], "#81d4fa")
    return palette
```

Note: `conn.execute(...).fetchall()` rows are `sqlite3.Row` (the store sets `row_factory`), so `r["path"]` works. If a caller passes a plain connection without the row factory, use `r[0]` — but every store connection has it.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py::test_domain_palette_matches_min_doc_count_1_set -v`
Expected: PASS.

- [ ] **Step 5: Point the galaxy at the helper (must not change its output)**

In `orchestrator/src/pipeline/graph_v5.py`, replace the inline build at `:104-108`:

```python
    palette = assign_domain_colors(domains)
    for d in domains:
        region = d["path"].split("/")[0]
        if region not in palette:
            palette[region] = palette.get(d["path"], "#81d4fa")
```

with:

```python
    from .graph_snapshot import domain_palette
    palette = domain_palette(conn)
```

`domains` here is already `store.domains.list(min_doc_count=1)` mapped to `{"path":...}` dicts, so the helper (which queries the same `document_count >= 1` set ordered by path) produces the identical map. Leave the `assign_domain_colors` import in graph_v5 if other code uses it; otherwise it can stay — do not chase unused-import cleanup here.

- [ ] **Step 6: Verify the galaxy payload is unchanged**

Run: `cd orchestrator && python -m pytest tests/ -k "graph and not mcp" -v`
Expected: PASS — no graph payload test regresses. If a snapshot/palette test exists it must still pass, proving the extraction is faithful.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/src/pipeline/graph_snapshot.py orchestrator/src/pipeline/graph_v5.py orchestrator/tests/test_star_graph_orbital.py
git commit -m "feat(graph): shared domain_palette helper (galaxy + star agree)"
```

---

## Task 2: `get_star_graph` — add `n_entities`, `domain_path`, `palette`

**Files:**
- Modify: `orchestrator/src/repositories/sqlite_store.py:558-614`
- Test: `orchestrator/tests/test_star_graph_orbital.py`

- [ ] **Step 1: Write the failing tests**

Append to `orchestrator/tests/test_star_graph_orbital.py`:

```python
def _seed_entity_with_docs(conn):
    # entity e1 in two docs; d1 has 3 entities total (share 1/3), d2 has 10 (share 1/10)
    _seed_domains(conn, [("software/ai-agents", 2), ("software/testing-qa", 1)])
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1','openai','integration')")
    for i in range(1, 3):
        conn.execute("INSERT INTO documents (id,title,content,content_hash) VALUES (?,?,?,?)",
                     (f"d{i}", f"doc {i}", "x", f"h{i}"))
    conn.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d1','software/ai-agents',1,0.9)")
    conn.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d2','software/testing-qa',1,0.9)")
    # d1: e1 + 2 others = 3 entities ; d2: e1 + 9 others = 10 entities
    others = [f"o{i}" for i in range(11)]
    for oid in others:
        conn.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'concept')", (oid, oid))
    conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES ('e1','d1')")
    conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES ('e1','d2')")
    for oid in others[:2]:
        conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES (?, 'd1')", (oid,))
    for oid in others[2:11]:
        conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES (?, 'd2')", (oid,))
    conn.commit()


def test_documents_carry_domain_path_and_active_entity_count(test_store):
    _seed_entity_with_docs(test_store.conn)
    g = test_store.relationships.get_star_graph("e1")
    by_id = {d["id"]: d for d in g["documents"]}
    assert by_id["d1"]["domain_path"] == "software/ai-agents"
    assert by_id["d1"]["n_entities"] == 3      # e1 + 2 others
    assert by_id["d2"]["domain_path"] == "software/testing-qa"
    assert by_id["d2"]["n_entities"] == 10


def test_n_entities_counts_only_active(test_store):
    from src.pipeline.graph_repair import apply_invalidation
    _seed_entity_with_docs(test_store.conn)
    apply_invalidation(test_store.conn, "o0", reason="test")   # o0 is in d1
    g = test_store.relationships.get_star_graph("e1")
    by_id = {d["id"]: d for d in g["documents"]}
    assert by_id["d1"]["n_entities"] == 2      # e1 + o1 ; o0 invalidated


def test_palette_present_and_agrees_with_galaxy(test_store):
    from src.pipeline.graph_snapshot import domain_palette
    _seed_entity_with_docs(test_store.conn)
    g = test_store.relationships.get_star_graph("e1")
    oracle = domain_palette(test_store.conn)
    for leaf in ("software/ai-agents", "software/testing-qa"):
        assert g["palette"][leaf] == oracle[leaf]      # star == galaxy, not re-derived
```

- [ ] **Step 2: Run and watch fail**

Run: `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py -v`
Expected: three FAILs — `KeyError: 'domain_path'`, `KeyError: 'n_entities'`, `KeyError: 'palette'`.

- [ ] **Step 3: Implement**

In `orchestrator/src/repositories/sqlite_store.py`, edit `get_star_graph`.

Replace the Documents block so each document carries its primary domain and active-entity count:

```python
        # Documents — with primary domain and the active-entity count per doc (the
        # share denominator the orbital layout needs). domain_path comes from a
        # correlated SUBQUERY, not a JOIN: (document_id, domain_path) is the PK but
        # is_primary is an unconstrained flag, so a JOIN could multiply a doc row if a
        # doc ever had two primaries. The subquery + LIMIT 1 can never multiply rows.
        # domain_path is None when a doc has no primary domain → client greys it.
        doc_rows = self._conn.execute("""
            SELECT DISTINCT d.id, d.title, d.content_type,
                   (SELECT dd.domain_path FROM document_domains dd
                     WHERE dd.document_id = d.id AND dd.is_primary = 1 LIMIT 1) AS domain_path
            FROM entity_sources es
            JOIN documents d ON es.document_id = d.id
            WHERE es.entity_id = ? AND d.invalid_at IS NULL
            ORDER BY d.title
        """, (entity_id,)).fetchall()
        doc_ids = [r["id"] for r in doc_rows]

        # n_entities per doc: distinct ACTIVE entities extracted from that doc.
        n_entities = {}
        if doc_ids:
            ph = ",".join("?" * len(doc_ids))
            for r in self._conn.execute(f"""
                SELECT es.document_id, COUNT(DISTINCT es.entity_id) AS n
                FROM entity_sources es
                JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL
                WHERE es.document_id IN ({ph})
                GROUP BY es.document_id
            """, doc_ids):
                n_entities[r["document_id"]] = r["n"]

        documents = [{
            "id": r["id"], "title": r["title"],
            "content_type": r["content_type"] or "text",
            "domain_path": r["domain_path"],
            "n_entities": n_entities.get(r["id"], 1),   # >=1: the entity itself
        } for r in doc_rows]
```

Then before the `return`, build the palette and add it:

```python
        from ..pipeline.graph_snapshot import domain_palette
        palette = domain_palette(self._conn)
        # Filter to the leaf domains actually present on this star.
        present = {r["domain_path"] for r in doc_rows if r["domain_path"]}
        palette = {p: palette[p] for p in present if p in palette}
```

And add `"palette": palette` to the returned dict:

```python
        return {
            "entity": {...},          # unchanged
            "documents": documents,
            "co_entities": co_entities,
            "palette": palette,
        }
```

Import note: `domain_palette` is imported inside the method to avoid a module-level import cycle (`graph_snapshot` imports from repositories elsewhere). Follow the codebase's existing in-method-import pattern.

- [ ] **Step 4: Run and watch pass**

Run: `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py -v`
Expected: all PASS. Also run the existing star test to confirm no regression:
`python -m pytest tests/test_star_graph_invalid_at.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/repositories/sqlite_store.py orchestrator/tests/test_star_graph_orbital.py
git commit -m "feat(star-graph): per-doc domain_path + active n_entities + palette"
```

---

## Task 3: Raise the `co_limit` default to 150

The band needs more than 30 co-entities to be worth spreading; openai has 2,130.

**Files:**
- Modify: `orchestrator/src/repositories/sqlite_store.py:558` (`get_star_graph` signature)
- Modify: `orchestrator/src/routes/entities.py:36` (route default)
- Modify: `frontend/public/viz/star.html:150` (client fetch)
- Test: `orchestrator/tests/test_star_graph_orbital.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_co_limit_defaults_to_150(test_store):
    import inspect
    sig = inspect.signature(test_store.relationships.get_star_graph)
    assert sig.parameters["co_limit"].default == 150
```

- [ ] **Step 2: Run and watch fail**

Run: `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py::test_co_limit_defaults_to_150 -v`
Expected: FAIL — default is 30.

- [ ] **Step 3: Change the three defaults**

- `sqlite_store.py:558`: `def get_star_graph(self, entity_id, co_limit=150):`
- `entities.py:36`: `def get_star_graph(entity_id: str, co_limit: int = 150, auth: AuthStore = Depends(get_auth_store)):`
- `star.html:150`: change `?co_limit=30` to `?co_limit=150`.

- [ ] **Step 4: Run and watch pass**

Run: `cd orchestrator && python -m pytest tests/test_star_graph_orbital.py::test_co_limit_defaults_to_150 -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/repositories/sqlite_store.py orchestrator/src/routes/entities.py frontend/public/viz/star.html
git commit -m "feat(star-graph): raise co_limit default 30 -> 150"
```

---

## Task 4: `star-layout.js` — strength + tie-safe ring assignment

Start the pure layout module with the two most-tested pieces. Use `node --test` (Node 25 ships it; no dependency to add).

**Files:**
- Create: `frontend/public/viz/core/star-layout.js`
- Create: `frontend/public/viz/core/star-layout.test.mjs`

- [ ] **Step 1: Write the failing tests**

Create `frontend/public/viz/core/star-layout.test.mjs`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { documentStrength, assignRings, MIN_DOCS_FOR_RINGS } from './star-layout.js';

test('strength is 1 / active entity count', () => {
  assert.equal(documentStrength({ n_entities: 5 }), 0.2);
  assert.equal(documentStrength({ n_entities: 1 }), 1);
  assert.equal(documentStrength({ n_entities: 0 }), 1);   // guard: never divide by 0
});

test('ring assignment hits percentile targets when shares are distinct', () => {
  // 10 docs, all distinct shares -> 1 in ring0 (10%), 2 in ring1 (to 30%),
  // 3 in ring2 (to 60%), 4 in ring3.
  const docs = Array.from({ length: 10 }, (_, i) => ({ id: i, n_entities: i + 2 }));
  const rings = assignRings(docs);
  const counts = [0, 0, 0, 0];
  for (const d of rings) counts[d.ring]++;
  assert.deepEqual(counts, [1, 2, 3, 4]);
});

test('a tie group is never split across rings', () => {
  // 8 docs share one strong value (would straddle the 10% and 30% targets),
  // 2 weaker distinct values after. The whole tie group must share one ring.
  const docs = [
    ...Array.from({ length: 8 }, (_, i) => ({ id: `t${i}`, n_entities: 6 })), // share 1/6
    { id: 'w1', n_entities: 20 },
    { id: 'w2', n_entities: 40 },
  ];
  const rings = assignRings(docs);
  const tieRings = new Set(rings.filter(d => String(d.id).startsWith('t')).map(d => d.ring));
  assert.equal(tieRings.size, 1, 'all 8 tied docs in one ring');
});

test('strongest doc seats in ring 0 when it is small enough (no inversion at scale)', () => {
  const docs = Array.from({ length: 100 }, (_, i) => ({ id: i, n_entities: i + 2 }));
  const rings = assignRings(docs);
  const strongest = rings.find(d => d.id === 0);   // n_entities=2 -> highest share
  assert.equal(strongest.ring, 0);
});
```

- [ ] **Step 2: Run and watch fail**

Run: `cd frontend/public/viz/core && node --test star-layout.test.mjs`
Expected: FAIL — cannot find module `./star-layout.js`.

- [ ] **Step 3: Implement the module start**

Create `frontend/public/viz/core/star-layout.js`:

```js
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

/** Connection strength of a document to the entity: its share of the doc's active
 *  entity set. Guarded so a malformed 0 never divides. */
export function documentStrength(doc) {
  const n = doc.n_entities || 1;
  return 1 / n;
}

/** Assign each doc a ring 0..3 by the tie-safe cumulative rule. Returns the same
 *  doc objects with a `.ring` and `.strength` field added (new array).
 *
 *  Reduce to DISTINCT share values (strongest first); walk them adding each value's
 *  doc count to a running total; test cum/total AFTER adding: ring 0 if <=0.10,
 *  1 if <=0.30, 2 if <=0.60, else 3. Chosen per distinct value, so a tie group is
 *  never split (spec "Rings" section). */
export function assignRings(docs, { targets = RING_TARGETS } = {}) {
  const withStrength = docs.map(d => ({ ...d, strength: documentStrength(d) }));
  const total = withStrength.length || 1;

  // distinct values, strongest first
  const byValue = new Map();
  for (const d of withStrength) {
    const k = d.strength;
    if (!byValue.has(k)) byValue.set(k, []);
    byValue.get(k).push(d);
  }
  const values = [...byValue.keys()].sort((a, b) => b - a);

  let cum = 0;
  const ringOf = new Map();   // value -> ring
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
```

- [ ] **Step 4: Run and watch pass**

Run: `cd frontend/public/viz/core && node --test star-layout.test.mjs`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/core/star-layout.js frontend/public/viz/core/star-layout.test.mjs
git commit -m "feat(viz): star-layout strength + tie-safe ring assignment"
```

---

## Task 5: `star-layout.js` — mid-level roll-up, domain order, adaptive misc

**Files:**
- Modify: `frontend/public/viz/core/star-layout.js`
- Modify: `frontend/public/viz/core/star-layout.test.mjs`

- [ ] **Step 1: Write the failing tests**

Append to the test file:

```js
import { midLevel, orderDomains } from './star-layout.js';

test('midLevel rolls a leaf path up to two segments', () => {
  assert.equal(midLevel('software/testing-qa/unit-testing/llm-judge'), 'software/testing-qa');
  assert.equal(midLevel('software'), 'software');
  assert.equal(midLevel(null), MISC_LABEL);   // no domain -> misc
});

test('orderDomains: named grow until misc <= 10%, misc last', () => {
  // 100 docs: two big domains (45, 45), ten tiny (1 each).
  const docs = [
    ...Array.from({ length: 45 }, () => ({ domain_path: 'software/a/x' })),
    ...Array.from({ length: 45 }, () => ({ domain_path: 'software/b/y' })),
    ...Array.from({ length: 10 }, (_, i) => ({ domain_path: `software/tiny${i}/z` })),
  ];
  const { order, miscCount } = orderDomains(docs);
  // named domains cover 90% at 2, so misc (10 docs = 10%) is allowed; adding more
  // named would drop misc below threshold — rule stops as soon as misc <= 10%.
  assert.equal(order[order.length - 1], MISC_LABEL);
  assert.ok(order.length <= MISC_CAP + 1);
});

test('orderDomains: single-domain entity -> one sector, no misc', () => {
  const docs = Array.from({ length: 30 }, () => ({ domain_path: 'software/only/z' }));
  const { order } = orderDomains(docs);
  assert.deepEqual(order, ['software/only']);
});

test('orderDomains is stable: ties broken by path, misc always last', () => {
  const docs = [
    { domain_path: 'software/b/z' }, { domain_path: 'software/a/z' },
    { domain_path: 'software/a/z' }, { domain_path: 'software/b/z' },
  ];
  const { order } = orderDomains(docs);   // equal counts -> alphabetical
  assert.deepEqual(order, ['software/a', 'software/b']);
});
```

- [ ] **Step 2: Run and watch fail**

Run: `cd frontend/public/viz/core && node --test star-layout.test.mjs`
Expected: FAIL — `midLevel`/`orderDomains` not exported.

- [ ] **Step 3: Implement**

Append to `star-layout.js`:

```js
/** Roll a leaf domain path up to its mid-level (2 segments). null/'' -> misc. */
export function midLevel(domainPath) {
  if (!domainPath) return MISC_LABEL;
  const parts = domainPath.split('/');
  return parts.length >= 2 ? parts.slice(0, 2).join('/') : domainPath;
}

/** Decide the sector order and how many are named.
 *
 *  Roll every doc to its mid-level; count docs per mid-level. Sort by count desc,
 *  then path asc (deterministic). Grow the named set from the top until the
 *  remaining tail ("misc") is <= MISC_THRESHOLD of all docs, capped at MISC_CAP.
 *  Returns { order: [...midpaths, 'misc'?], named: Set, miscCount }.
 *  `misc` is appended only if any docs fall outside the named set. */
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
```

- [ ] **Step 4: Run and watch pass**

Run: `cd frontend/public/viz/core && node --test star-layout.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/core/star-layout.js frontend/public/viz/core/star-layout.test.mjs
git commit -m "feat(viz): star-layout domain roll-up + adaptive misc ordering"
```

---

## Task 6: `star-layout.js` — per-ring sector packing (the breathing arcs)

**Files:**
- Modify: `frontend/public/viz/core/star-layout.js`
- Modify: `frontend/public/viz/core/star-layout.test.mjs`

- [ ] **Step 1: Write the failing tests**

Append:

```js
import { packRings, TAU } from './star-layout.js';

// helper: build docs with explicit ring + mid domain
function mk(ring, mid, n = 1) {
  return Array.from({ length: n }, (_, i) => ({
    id: `${mid}-${ring}-${i}`, ring, domain_path: `${mid}/leaf`,
  }));
}

test('busiest ring occupies exactly MAX_FILL; others scale proportionally', () => {
  const docs = [...mk(3, 'software/a', 100), ...mk(2, 'software/a', 50)];
  const order = ['software/a'];
  const { rings } = packRings(docs, order);
  const deg = r => (r.arcSpan * 180 / Math.PI);
  assert.ok(Math.abs(deg(rings[3]) - 340) < 1e-6);          // busiest = MAX_FILL
  assert.ok(Math.abs(deg(rings[2]) - 170) < 1e-6);          // half the docs -> half
});

test('each ring arc is centred on -90 degrees (top)', () => {
  const docs = mk(3, 'software/a', 10);
  const { rings } = packRings(docs, ['software/a']);
  const mid = (rings[3].startAngle + rings[3].endAngle) / 2;
  assert.ok(Math.abs(mid - (-Math.PI / 2)) < 1e-6);
});

test('a domain absent from a ring yields zero arc; neighbours fill the ring span', () => {
  // ring3 has domain a only; ring2 has a and b
  const docs = [...mk(3, 'software/a', 10), ...mk(2, 'software/a', 5), ...mk(2, 'software/b', 5)];
  const order = ['software/a', 'software/b'];
  const { rings } = packRings(docs, order);
  const ring3 = rings[3];
  assert.equal(ring3.sectors.find(s => s.domain === 'software/b'), undefined);
  // ring2's two sectors' arcs sum to the ring's total span
  const sum = rings[2].sectors.reduce((a, s) => a + s.arc, 0);
  assert.ok(Math.abs(sum - rings[2].arcSpan) < 1e-9);
});

test('arc_per_doc is floored so a lone doc is not a huge wedge', () => {
  const docs = mk(3, 'software/a', 2);   // Nmax=2 -> 340/2=170deg/doc without floor
  const { rings, arcPerDocDeg } = packRings(docs, ['software/a']);
  assert.ok(arcPerDocDeg <= 12 + 1e-9, 'floored at ARC_PER_DOC_MAX');
});

test('domain ordinal position is identical across rings', () => {
  const docs = [
    ...mk(3, 'software/a', 5), ...mk(3, 'software/b', 5),
    ...mk(2, 'software/a', 5), ...mk(2, 'software/b', 5),
  ];
  const order = ['software/a', 'software/b'];
  const { rings } = packRings(docs, order);
  const seq = r => r.sectors.map(s => s.domain);
  assert.deepEqual(seq(rings[3]), seq(rings[2]));   // same order both rings
});
```

- [ ] **Step 2: Run and watch fail**

Run: `cd frontend/public/viz/core && node --test star-layout.test.mjs`
Expected: FAIL — `packRings`/`TAU` not exported.

- [ ] **Step 3: Implement**

Append to `star-layout.js`:

```js
export const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

/** Pack documents into per-ring domain sectors (the breathing arcs).
 *
 *  For each ring, arc width per doc is uniform in DEGREES across all rings:
 *  arc_per_doc = min(MAX_FILL / Nmax, ARC_PER_DOC_MAX), where Nmax is the busiest
 *  ring's doc count. Each ring's total span = its doc count * arc_per_doc, centred
 *  on -90deg. Within a ring, domains are laid left-to-right in the fixed `order`
 *  (misc last); docs within a domain get equal angular slots.
 *
 *  Returns { rings: [{ ring, docCount, arcSpan, startAngle, endAngle,
 *                       sectors: [{ domain, arc, startAngle, endAngle, docs:[{...,angle}] }] }],
 *            arcPerDocDeg }.  Rings with 0 docs have arcSpan 0 and empty sectors. */
export function packRings(docs, order, {
  maxFillDeg = MAX_FILL_DEG, arcPerDocMaxDeg = ARC_PER_DOC_MAX_DEG,
} = {}) {
  const NRINGS = RING_RADII.length;
  const perRing = Array.from({ length: NRINGS }, () => []);
  for (const d of docs) if (d.ring != null) perRing[d.ring].push(d);

  const nmax = Math.max(1, ...perRing.map(r => r.length));
  const arcPerDocDeg = Math.min(maxFillDeg / nmax, arcPerDocMaxDeg);
  const arcPerDoc = arcPerDocDeg * DEG;

  const orderIndex = new Map(order.map((m, i) => [m, i]));
  const rings = perRing.map((ringDocs, ring) => {
    const arcSpan = ringDocs.length * arcPerDoc;
    const startAngle = -Math.PI / 2 - arcSpan / 2;

    // group this ring's docs by mid-level, in the fixed global order
    const byDomain = new Map();
    for (const d of ringDocs) {
      const m = d.domain_path ? midLevelIfNamed(d, order) : MISC_LABEL;
      if (!byDomain.has(m)) byDomain.set(m, []);
      byDomain.get(m).push(d);
    }
    const domainsHere = [...byDomain.keys()].sort(
      (a, b) => (orderIndex.get(a) ?? 1e9) - (orderIndex.get(b) ?? 1e9));

    let cursor = startAngle;
    const sectors = domainsHere.map(domain => {
      const dd = byDomain.get(domain);
      const arc = dd.length * arcPerDoc;
      const s0 = cursor, s1 = cursor + arc;
      const placed = dd.map((d, i) => ({
        ...d, angle: s0 + arc * ((i + 0.5) / dd.length),
      }));
      cursor = s1;
      return { domain, arc, startAngle: s0, endAngle: s1, docs: placed };
    });
    return { ring, docCount: ringDocs.length, arcSpan, startAngle,
             endAngle: startAngle + arcSpan, sectors };
  });
  return { rings, arcPerDocDeg };
}

/** A doc's sector key: its mid-level if that mid-level is a named sector, else misc. */
function midLevelIfNamed(doc, order) {
  const m = midLevel(doc.domain_path);
  return order.includes(m) ? m : MISC_LABEL;
}
```

- [ ] **Step 4: Run and watch pass**

Run: `cd frontend/public/viz/core && node --test star-layout.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/core/star-layout.js frontend/public/viz/core/star-layout.test.mjs
git commit -m "feat(viz): star-layout per-ring sector packing (breathing arcs)"
```

---

## Task 7: `star-layout.js` — small-N layout + `layoutStar` orchestrator

**Files:**
- Modify: `frontend/public/viz/core/star-layout.js`
- Modify: `frontend/public/viz/core/star-layout.test.mjs`

- [ ] **Step 1: Write the failing tests** (the small-N regression, spec tests h/i)

```js
import { layoutStar } from './star-layout.js';

test('small entity uses simple layout: strongest doc is INNERMOST (no inversion)', () => {
  // 5 docs, distinct shares; strongest (n=2) must be at the smallest radius.
  const docs = [2, 3, 4, 6, 10].map((n, i) => ({ id: i, n_entities: n, domain_path: 'software/a/x' }));
  const out = layoutStar({ documents: docs, co_entities: [], entity: { id: 'e' } });
  assert.equal(out.mode, 'small');
  const strongest = out.documents.find(d => d.id === 0);   // n=2, highest share
  const weakest = out.documents.find(d => d.id === 4);      // n=10
  assert.ok(strongest.radius < weakest.radius, 'strongest orbits closest');
});

test('tiny entity does not produce a giant wedge', () => {
  const docs = [{ id: 'a', n_entities: 2, domain_path: 'software/x/y' }];
  const out = layoutStar({ documents: docs, co_entities: [], entity: { id: 'e' } });
  assert.equal(out.mode, 'small');   // 1 doc -> simple path, never a 340deg sector
});

test('large entity uses ring mode', () => {
  const docs = Array.from({ length: 60 }, (_, i) => ({ id: i, n_entities: (i % 12) + 2, domain_path: 'software/a/x' }));
  const out = layoutStar({ documents: docs, co_entities: [], entity: { id: 'e' } });
  assert.equal(out.mode, 'rings');
  assert.ok(out.rings.length === 4);
});
```

- [ ] **Step 2: Run and watch fail** (`layoutStar` not exported)

- [ ] **Step 3: Implement**

Append:

```js
export const RING_INNER = RING_RADII[0];
export const RING_OUTER = RING_RADII[RING_RADII.length - 1];

/** Below MIN_DOCS_FOR_RINGS: one crescent, radius = strength directly (strongest
 *  innermost — this is what fixes the small-N radius inversion), angle grouped by
 *  mid-level domain. Same fields the ring mode produces so the renderer is uniform. */
function layoutSmall(docs) {
  const withS = docs.map(d => ({ ...d, strength: documentStrength(d) }));
  const strengths = withS.map(d => d.strength);
  const smin = Math.min(...strengths), smax = Math.max(...strengths);
  const span = (smax - smin) || 1;
  // radius: strongest -> RING_INNER, weakest -> RING_OUTER
  const radiusOf = s => RING_INNER + (RING_OUTER - RING_INNER) * (1 - (s - smin) / span);

  // group by mid-level, contiguous arcs across a MAX_FILL crescent centred on -90
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
 *  added in Task 8's coEntityBand and merged by star.html. */
export function layoutStar(graph, opts = {}) {
  const docs = graph.documents || [];
  if (docs.length < (opts.minDocs ?? MIN_DOCS_FOR_RINGS)) {
    return layoutSmall(docs);
  }
  const ringed = assignRings(docs);
  const { order, named } = orderDomains(docs);
  const { rings, arcPerDocDeg } = packRings(ringed, order, opts);
  // flatten positioned docs for hit-testing/rendering convenience
  const positioned = [];
  for (const r of rings) for (const s of r.sectors)
    for (const d of s.docs) positioned.push({ ...d, radius: RING_RADII[r.ring] });
  return { mode: 'rings', documents: positioned, rings, sectorOrder: order,
           namedDomains: named, arcPerDocDeg };
}
```

- [ ] **Step 4: Run and watch pass** — full file: `node --test star-layout.test.mjs`

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/core/star-layout.js frontend/public/viz/core/star-layout.test.mjs
git commit -m "feat(viz): star-layout small-N crescent + layoutStar entry point"
```

---

## Task 8: `star-layout.js` — sector colour + co-entity band

**Files:**
- Modify: `frontend/public/viz/core/star-layout.js`
- Modify: `frontend/public/viz/core/star-layout.test.mjs`

- [ ] **Step 1: Write the failing tests**

```js
import { sectorColor, coEntityBand } from './star-layout.js';

test('sector colour: mid path if present, else first present leaf', () => {
  const palette = { 'software/a/x': '#111111', 'software/a/y': '#222222' };
  // mid 'software/a' not a key -> first present leaf under it, sorted
  assert.equal(sectorColor('software/a', palette), '#111111');
  const palette2 = { 'software/a': '#999999', 'software/a/x': '#111111' };
  assert.equal(sectorColor('software/a', palette2), '#999999');   // mid present wins
  assert.equal(sectorColor('misc', palette), null);               // misc -> grey (renderer)
});

test('co-entity band: stronger co-entities sit nearer the inner band edge', () => {
  const docAngle = new Map([['d1', 0], ['d2', Math.PI]]);
  const co = [
    { id: 'c1', shared_doc_ids: ['d1'], _entityDocCount: 10, shared: 8 },  // strong
    { id: 'c2', shared_doc_ids: ['d1'], _entityDocCount: 10, shared: 1 },  // weak
  ];
  const placed = coEntityBand(co, docAngle, { rInner: 620, rOuter: 720 });
  const c1 = placed.find(p => p.id === 'c1'), c2 = placed.find(p => p.id === 'c2');
  assert.ok(c1.radius < c2.radius, 'stronger -> nearer inner edge');
  assert.ok(c1.radius >= 620 && c2.radius <= 720);
});
```

- [ ] **Step 2: Run and watch fail**

- [ ] **Step 3: Implement**

Append:

```js
export const MISC_COLOR = '#6d7280';   // neutral grey for the misc sector + no-domain docs

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
      if (docAngleById.has(id)) { const a = docAngleById.get(id); sx += Math.cos(a); sy += Math.sin(a); k++; }
    }
    const angle = k ? Math.atan2(sy, sx) : (i * 2.399963);   // golden-angle fallback
    const denom = co._entityDocCount || 1;
    const strength = Math.min(1, (co.shared ?? (co.shared_doc_ids || []).length) / denom);
    const jitter = ((i % 5) - 2) * 4;   // ±8px, deterministic
    const radius = rInner + (rOuter - rInner) * (1 - strength) + jitter;
    return { ...co, angle, radius, strength };
  });
}
```

- [ ] **Step 4: Run and watch pass** — full file green.

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/core/star-layout.js frontend/public/viz/core/star-layout.test.mjs
git commit -m "feat(viz): star-layout sector colour + co-entity band"
```

---

## Task 9: Wire `star.html` to `star-layout.js`

Replace the hand-rolled placement in `buildStarView` with calls into the tested module. This is integration glue — validated visually in Task 11, not unit-tested.

**Files:**
- Modify: `frontend/public/viz/star.html` (imports at top; `buildStarView` ~237-360)

- [ ] **Step 1: Import the module**

At the top of the `<script type="module">` (near the other imports ~line 58-60), add:

```js
import * as SL from './core/star-layout.js';
```

- [ ] **Step 2: Compute `_entityDocCount`, `shared`, and preserve `labelThreshold`**

The band needs each co-entity's `shared/docs_of_entity`. `docs_of_entity` = `docList.length`; `shared` = `co.shared_doc_ids.length`. The renderer also decides **resting labels** from `e.labelThreshold` (`renderers/star.js:176`: `hov || e.weight >= e.labelThreshold`), so this field MUST survive — dropping it hides every co-entity label except on hover, violating the spec's "co-entities carry text". Preserve the existing threshold logic (`star.html:286` today). In `buildStarView`, before placing co-entities:

```js
  const entityDocCount = docList.length || 1;
  const filteredCo0 = coList.filter(c => c.type !== 'Domain');
  const sortedCo = [...filteredCo0].sort((a, b) => b.weight - a.weight);
  // top ~30% by weight get resting labels (same rule as before this change)
  const labelThreshold = sortedCo.length > 8
    ? (sortedCo[Math.floor(sortedCo.length * 0.3)]?.weight || 1)
    : 0;
  for (const co of filteredCo0) {
    co._entityDocCount = entityDocCount;
    co.shared = (co.shared_doc_ids || []).length;
    co.labelThreshold = labelThreshold;
  }
```

- [ ] **Step 3: Replace document + co-entity placement with `layoutStar` + `coEntityBand`**

In `buildStarView`, replace the manual doc-ring loop (`const docRing = 180; for (...)`) and the co-entity loop (`const coRing = 380; ...`) with:

```js
  const layout = SL.layoutStar(graph);
  const cx = 2500, cy = 2500;

  // documents
  const docAngle = new Map();
  for (const d of layout.documents) {
    const dx = cx + Math.cos(d.angle) * d.radius, dy = cy + Math.sin(d.angle) * d.radius;
    const doc = { id: d.id, kind: 'document', title: d.title || 'Untitled',
                  content_type: d.content_type || 'text', domain_path: d.domain_path,
                  radius: 8, x: dx, y: dy, _px: dx, _py: dy, angle: d.angle,
                  orbitPhase: Math.random() * SL.TAU, orbitSpeed: 0.5 + Math.random() * 0.5,
                  orbitDrift: 4 + Math.random() * 6, activityGlow: 0 };
    docs.push(doc); docById[d.id] = doc; docIndex.set(d.id, doc);
    docAngle.set(d.id, d.angle);
  }

  // co-entities: graduated band beyond the outer ring (filteredCo0 from Step 2)
  const band = SL.coEntityBand(filteredCo0, docAngle,
      { rInner: SL.RING_OUTER + 60, rOuter: SL.RING_OUTER + 60 + Math.min(200, 40 + filteredCo0.length) });
  for (const co of band) {
    const x = cx + Math.cos(co.angle) * co.radius, y = cy + Math.sin(co.angle) * co.radius;
    coEntities.push({ id: co.id, kind: 'co_entity', name: co.canonical_name, type: co.type,
      weight: co.weight, sharedDocIds: co.shared_doc_ids || [], strength: co.strength,
      labelThreshold: co.labelThreshold,          // preserved so resting labels show
      radius: 5 + Math.sqrt(co.weight || 1) * 2, x, y, _px: x, _py: y,
      orbitPhase: Math.random() * SL.TAU, orbitSpeed: 0.4 + Math.random() * 0.6,
      orbitDrift: 5 + Math.random() * 8, activityGlow: 0 });
  }

  // stash the ring/sector structure + palette for the renderer
  window.__STAR_LAYOUT__ = layout;
  window.__STAR_PALETTE__ = graph.palette || {};
```

Keep the existing central-entity setup, the repo/collection block (unchanged — collections stay empty), and the bounding-box computation that follows.

- [ ] **Step 4: Manual smoke — the page loads without error**

Serve the viz (Task 11 has the full recipe) and open the star page for a small and a large entity. Open DevTools console. Expected: no errors, dots rendered in rings (large) or one crescent (small). Sector *drawing* comes in Task 10 — at this step docs are positioned but sectors aren't yet drawn.

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/star.html
git commit -m "feat(viz): star.html positions docs + co-entities via star-layout"
```

---

## Task 10: Renderer — draw sectors, colour by domain, draw the co-entity band

Canvas code, tuned visually. Colour comes from the domain palette, replacing the dead `TYPE_COLORS` path.

**Files:**
- Modify: `frontend/public/viz/renderers/star.js`

- [ ] **Step 1: Colour documents + central star by domain**

Add near the top of `star.js`:

```js
import { sectorColor, MISC_COLOR } from '../core/star-layout.js';

// palette + layout are stashed on window by star.html's buildStarView
function palette() { return (typeof window !== 'undefined' && window.__STAR_PALETTE__) || {}; }
function docColor(doc) {
  const p = palette();
  if (doc.domain_path && p[doc.domain_path]) return p[doc.domain_path];
  return MISC_COLOR;
}
```

In `drawDocuments`, use `docColor(doc)` for each document's fill instead of the current `typeColor`-derived colour.

- [ ] **Step 2: Draw the sectors (behind the docs)**

Add a `drawSectors(ctx, layout, cx, cy, view)` exported function that, for `layout.mode === 'rings'`, iterates `layout.rings[].sectors[]` and for each sector draws:
- a filled wedge (two radial edges at `sector.startAngle`/`endAngle`, arced between the ring's inner and outer radius from `RING_RADII`) tinted with `sectorColor(sector.domain, palette()) || MISC_COLOR` at low alpha (~0.06);
- the two radial boundary lines at higher alpha (~0.25), so adjacent sectors' differing widths read as the "cut into one another" stepped edges;
- the faint full-circle ring guides (reuse `collection.html:409-413` styling).

Label each *named* sector once, on its widest ring: find `argmax_ring sector.arc` for that domain across `layout.rings`, place the mid-level name at that ring's mid-angle just outside the ring radius. Skip the label for `misc` (draw its grey wedge but a small dim "misc" tag).

Wire it into `star.html` with two literal edits:
- Extend the renderer import at `star.html:60` to include `drawSectors`:
  ```js
  import { drawCentralStar, drawDocuments, drawCoEntities, drawConnections, drawMiniStar, drawSectors } from './renderers/star.js';
  ```
- In the render loop, insert the call **before** `drawDocuments(ctx, docs, tick, hoveredId, view);` (currently `star.html:601`), passing the stashed layout:
  ```js
      drawSectors(ctx, window.__STAR_LAYOUT__, 2500, 2500, view);
      drawDocuments(ctx, docs, tick, hoveredId, view);
  ```
So docs sit on top of their sectors. For `layout.mode === 'small'`, `drawSectors` draws only the faint ring guides (no sectors). Guard `drawSectors` against a null/undefined layout (first frame before `buildStarView` runs).

- [ ] **Step 3: Draw the co-entity band ring guide (optional, faint)**

At the band radius, an optional faint guide circle so the outer band reads as a band. Keep subtle.

- [ ] **Step 4: Manual visual check** — see Task 11. Iterate on alpha/label placement live with the user.

- [ ] **Step 5: Commit**

```bash
git add frontend/public/viz/renderers/star.js frontend/public/viz/star.html
git commit -m "feat(viz): draw domain sectors + colour star page by domain"
```

---

## Task 11: Run it for visual review

Get the updated orchestrator API + viz in front of the user on a large, medium, and small entity.

**Files:** none (deploy/serve only)

- [ ] **Step 1: Run the full test suites once**

```bash
cd orchestrator && python -m pytest tests/test_star_graph_orbital.py tests/test_star_graph_invalid_at.py -v
cd ../frontend/public/viz/core && node --test star-layout.test.mjs
```
Expected: all green.

- [ ] **Step 2: Restart the orchestrator so it serves the new `get_star_graph`**

The running compose stack mounts `orchestrator/src`, so a restart picks up the API change:

```bash
docker restart noospheric-orrery-orchestrator-1
until curl -sf localhost:8100/entities -H "X-Workspace-Id: default" >/dev/null; do sleep 2; done
```

- [ ] **Step 3: Confirm the payload has the new fields**

```bash
# pick a large entity id (openai/integration) from the default workspace
curl -s "localhost:8100/entities/3a18e649-8d6c-4888-86dc-8dbc8fb78f40/star-graph?co_limit=150" \
  -H "X-Workspace-Id: default" | python3 -c "import sys,json; g=json.load(sys.stdin); d=g['documents'][0]; print('doc keys:', sorted(d)); print('has palette:', 'palette' in g, 'n_palette', len(g.get('palette',{}))); print('co_entities', len(g['co_entities']))"
```
Expected: doc keys include `domain_path` and `n_entities`; palette present; ~150 co-entities.

- [ ] **Step 4: Serve the viz and open the star page**

The viz static files are served from the mounted `frontend/public/viz`. Open the star page directly (host swaps to it from the galaxy on double-click; for review, load it standalone) for three entities — large (openai `3a18e649…`), medium (docker), small (a <20-doc entity) — via:

```
http://localhost:3100/... (the app's entity route)   # or the standalone viz URL used in prior sessions
```

Confirm in the browser: large entity shows breathing sectors + rings; small entity shows one crescent with the strongest doc innermost; colours match the galaxy; 0 console errors.

- [ ] **Step 5: Hand to the user for feedback**

Report the three URLs and the entity ids used. Collect feedback on: sector boundary styling, tint alpha, label placement, ring radii, co-entity band thickness — these are the live-tuning knobs (`MAX_FILL_DEG`, `RING_RADII`, band width). Iterate on Task 10 styling as directed.

---

## Notes for the executor

- **Follow CLAUDE.md:** orchestrator tests use the file-backed `test_store`/`test_client` fixtures (never `:memory:`). The viz stays self-contained ESM — do not pull in a bundler or framework for `star-layout.js`.
- **The pure module is the contract.** All layout decisions live in `star-layout.js` and are unit-tested; `star.html` and `star.js` must not re-implement any of the math. If you find yourself computing an angle or radius in the renderer, it belongs in the module with a test.
- **Constants are exported from `star-layout.js`.** The renderer imports `RING_RADII`, `MISC_COLOR`, etc. — never hardcode a second copy.
- **Palette agreement is the one cross-cutting invariant:** `domain_palette` is the single source; the API test asserts the star payload equals it. Don't let a second palette computation creep into `get_star_graph`.
