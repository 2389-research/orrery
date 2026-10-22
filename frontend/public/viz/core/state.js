/**
 * World state — domains, entities, clusters, activity.
 * Single source of truth for all renderers.
 */

import { WORLD_W, WORLD_H, sqrt, random, hypot, assignDomainColors, blendColors, hexRGB, rnd, TAU } from './utils.js';

// Hydration swaps rather than grows: the render set stays at exactly the size the
// server chose (max_render_nodes), so the draw cost and sprite budget never drift.
// A newly-resolved node displaces one of the weakest currently-rendered nodes,
// picked at random from the lowest-degree pool so the same node isn't always the
// victim. Eviction is not lossy — every node stays in `nodeIndex` and can be
// hydrated again.
const EVICT_POOL_SIZE = 100;

export class WorldState {
  constructor() {
    this.domains = new Map();    // path → domain node
    this.entities = new Map();   // id → entity node
    this.collections = new Map();      // id → collection node (binned inside its domain)
    this.domainEntities = new Map(); // path → entity nodes (reverse index for 1-hop hover)
    this.collectionEntities = new Map();   // collectionId → entity nodes (reverse index for 1-hop hover)
    this.nodeIndex = new Map();      // id → raw record for EVERY node, not just the render set
    this.nodeIdByName = new Map();   // lowercased name → id, over that same full set
    this.renderCap = 0;              // held at the server's render-set size
    this._evictPool = [];            // lowest-degree ids, candidates to displace
    this.clusters = [];          // [{id, domainPaths, centroidX, centroidY, radius}]
    this.tradeRoutes = [];       // [{source, target, weight}]
    this.collectionRoutes = [];        // collection↔collection shared-entity edges
    this.collectionEdges = [];         // collection↔collection manifest co-usage edges
    this.collectionNeighbors = new Map(); // collectionId → Set(connected ids), built once at load
    this.domainColors = {};      // path → hex
    this.tick = 0;

    // Selection
    this.hoveredId = null;
    this.pinnedId = null;

    // Raw data
    this.graphData = null;

    // Ambient pulse system
    this.pulses = [];
    this.nextPulseTime = 0;
    this.pulseInterval = 600;
  }

  get activeId() {
    return this.pinnedId ?? this.hoveredId;
  }

  /** Load from /graph API response */
  loadGraphData(data) {
    this.graphData = data;
    this.domains.clear();
    this.entities.clear();
    this.collections.clear();

    // One `layout.positions` map holds every node type, so a domain is any positioned
    // id that is not a collection. Collections are nodes[] entries of type='collection'.
    const layout = data.layout || {};
    const layoutPositions = layout.positions || {};
    const collectionRecords = (data.nodes || [])
      .filter(n => n.type === 'collection')
      .map(n => ({
        id: n.id, name: n.label, path: n.path, document_count: n.degree || 0,
        domain: (n.memberships || []).find(m => m.container_type === 'domain')?.id ?? null,
      }));
    const collectionIds = new Set(collectionRecords.map(r => r.id));
    const domainPositions = Object.fromEntries(
      Object.entries(layoutPositions).filter(([id]) => !collectionIds.has(id)));
    const collectionPositions = layoutPositions;
    const palette = layout.palette || null;
    const simmeringDomains = data.meta?.activity?.simmering_domains || [];

    // Per-domain facts come from `taxonomy`, looked up by path. Domains are enumerated
    // from the positions map, NOT from taxonomy: taxonomy describes every domain with
    // content (156 on the office graph) while only the positioned ones can be drawn
    // (145), so enumerating the wider set would yield domains with no coordinates.
    const taxByPath = new Map((data.taxonomy || []).map(t => [t.path, t]));
    const docCountOf = p => taxByPath.get(p)?.document_count || 0;
    const specVersionOf = p => taxByPath.get(p)?.spec_version || 0;
    const isSubdomainOf = p => !!taxByPath.get(p)?.is_subdomain;

    // A sliced payload (a virtual-noosphere view, or ad-hoc time/domain/collection/silo
    // facets) carries `meta.slice`. `layout.positions` is the FULL noosphere's domain
    // basis (so a domain keeps its UMAP home across slices — the point of the layout),
    // but `taxonomy.document_count` is the in-slice count, 0 for domains with no docs in
    // the slice. Without this gate every slice would still instantiate all ~N positioned
    // domains at base radius, so the galaxies look identical. In a slice we draw only the
    // domains ACTIVE in-slice (docCount > 0); positions are untouched, so the structure is
    // preserved and the galaxy visibly changes between slices. The unsliced full graph is
    // unaffected (it draws every positioned domain, as before).
    const slice = data.meta?.slice;
    const slicedView = !!(slice && (slice.since || slice.until ||
      (slice.domains && slice.domains.length) ||
      (slice.collections && slice.collections.length) || slice.silo || slice.kind));

    // Compute colors
    const domainList = Object.keys(domainPositions).map(path => ({
      path,
      spec_version: specVersionOf(path) || null,
    }));
    this.domainColors = assignDomainColors(domainList);

    // Also store per-domain API colors as fallback
    if (palette) {
      for (const [path, color] of Object.entries(palette)) {
        if (!this.domainColors[path]) {
          this.domainColors[path] = color;
        }
      }
    }

    // Create domain nodes in world space
    const PADDING = 0.08;
    for (const [path, pos] of Object.entries(domainPositions)) {
      const vc = docCountOf(path);
      const isSub = isSubdomainOf(path);
      const specVersion = specVersionOf(path);
      const hasSpec = specVersion > 0;

      // UMAP → world space
      const wx = (PADDING + pos.x * (1 - 2 * PADDING)) * WORLD_W;
      const wy = (PADDING + pos.y * (1 - 2 * PADDING)) * WORLD_H;

      // Compute maturity
      const allPaths = Object.keys(domainPositions);
      const subdoms = allPaths.filter(p => p.startsWith(path + '/') && p !== path);
      let maturity = 0;
      if (subdoms.length === 0) {
        maturity = hasSpec ? 1.0 : 0.0;
      } else {
        const subWithSpec = subdoms.filter(p => specVersionOf(p) > 0).length;
        const subCoverage = subWithSpec / subdoms.length;
        maturity = hasSpec ? 0.5 + subCoverage * 0.5 : subCoverage * 0.4;
      }

      const isSimmering = simmeringDomains.includes(path);
      const radius = isSub ? 60 + sqrt(vc) * 20 : 120 + sqrt(vc) * 35;

      this.domains.set(path, {
        id: 'dom:' + path,
        kind: 'domain',
        path,
        label: isSub ? path.split('/').pop() : path,
        color: this.domainColors[path] || '#81d4fa',
        radius,
        worldX: wx,
        worldY: wy,
        x: wx, y: wy,  // current position (after repulsion)
        docCount: vc,
        maturity,
        specVersion,
        entityCount: 0, // populated after entities are loaded
        simmering: isSimmering,
        isSubdomain: isSub,
        phase: random() * TAU,
        rot: random() * TAU,
        birthScale: 0,
        activityGlow: 0,
      });
    }

    // Enforce minimum separation
    this._enforceMinSeparation(400, 200);

    // Under a slice, drop domains with no in-slice docs — but AFTER separation, so each
    // surviving domain keeps the exact coordinates it would have in the full graph.
    // `_enforceMinSeparation` perturbs positions based on which neighbours are present,
    // so filtering BEFORE it would drift a surviving domain's home between slices (the
    // opposite of what the slice view wants: a stable map you can compare across days).
    // The full/unsliced view keeps every positioned domain. Entities only ever reference
    // domains that still have in-slice docs (both derive from document_domains ⋈ the
    // in-slice doc set), so this prune never orphans a rendered entity.
    if (slicedView) {
      for (const [path, d] of this.domains) {
        if (d.docCount === 0) this.domains.delete(path);
      }
    }

    // Compute clusters
    this._computeClusters(600);

    // Collection nodes FIRST (so entities can be positioned relative to theirs).
    // Placed by their OWN semantic position, mapped to world coords like domains.
    const COLL_PAD = 0.08;
    for (const coll of collectionRecords) {
      const pos = collectionPositions[coll.id];
      let rwx, rwy;
      if (pos) {
        rwx = (COLL_PAD + pos.x * (1 - 2 * COLL_PAD)) * WORLD_W;
        rwy = (COLL_PAD + pos.y * (1 - 2 * COLL_PAD)) * WORLD_H;
      } else {
        const dom = coll.domain ? this.domains.get(coll.domain) : null;
        rwx = dom ? dom.x : WORLD_W / 2;
        rwy = dom ? dom.y : WORLD_H / 2;
      }
      this.collections.set(coll.id, {
        id: 'collection:' + coll.id, collectionId: coll.id, kind: 'collection',
        label: coll.name, name: coll.name, domainPath: coll.domain || null,
        color: '#e0a030', radius: 16 + Math.min(sqrt(coll.document_count || 0) * 3, 10),
        worldX: rwx, worldY: rwy, x: rwx, y: rwy, docCount: coll.document_count || 0,
        phase: random() * TAU, birthScale: 0,
      });
    }

    // Create entity nodes — positioned around the COLLECTION they belong to
    // (collectionWeights), since a code entity lives in a specific collection. Falls
    // back to the domain centroid only if it has no resolvable collection.
    const entityRecords = (data.nodes || []).filter(n => n.type === 'entity');
    for (const ent of entityRecords) {
      const node = this._makeEntityNode(ent);
      if (node) this.entities.set(node.id, node);
    }

    // Index EVERY node the payload describes, not only the rendered top-N, so a
    // search hit outside the render set stays resolvable and can be hydrated on
    // demand (see hydrateEntity — without this, hits on the un-rendered tail, ~91k of
    // 94k on the office graph, were a silent no-op).
    //
    const nodeIndexSource = data.node_index || {};
    this.nodeIndex.clear();
    this.nodeIdByName.clear();
    for (const [id, rec] of Object.entries(nodeIndexSource)) {
      this.nodeIndex.set(id, rec.id ? rec : { ...rec, id });
      const nm = (rec.label || '').toLowerCase();
      // First id wins on a name collision — ~8% of names are shared on the office
      // graph, which is also why the search broadcast really wants to carry ids.
      if (nm && !this.nodeIdByName.has(nm)) this.nodeIdByName.set(nm, id);
    }

    // Hold the render set at whatever the server sized it to; hydration swaps.
    this.renderCap = this.entities.size;
    this._evictPool = [];

    // Entity repulsion — push overlapping entities apart
    this._spreadEntities(30, 50);

    // Count entities per domain + build reverse indexes (domain/collection → entities)
    // so 1-hop neighborhood highlighting is an O(1) lookup on hover, not a
    // per-frame scan over all entities.
    this.domainEntities.clear();
    this.collectionEntities.clear();
    for (const [, e] of this.entities) {
      for (const path of Object.keys(e.domainWeights || {})) {
        const dom = this.domains.get(path);
        if (dom) dom.entityCount++;
        let arr = this.domainEntities.get(path);
        if (!arr) this.domainEntities.set(path, arr = []);
        arr.push(e);
      }
      for (const rid of Object.keys(e.collectionWeights || {})) {
        if (!this.collections.has(rid)) continue;
        let arr = this.collectionEntities.get(rid);
        if (!arr) this.collectionEntities.set(rid, arr = []);
        arr.push(e);
      }
    }

    // (collection nodes are created above, before entities, so entities can be
    // positioned relative to theirs)
    // All route kinds arrive in one typed `edges` collection, separated by type +
    // scope rather than by having their own top-level key.
    const edgesOf = (type, scope) => (data.edges || [])
      .filter(e => e.type === type && (scope === undefined || e.scope === scope))
      .map(e => ({ source: e.source, target: e.target, weight: e.weight }));

    this.collectionRoutes = edgesOf('cooccurrence', 'collection');
    // Every ASSERTED collection edge, not just `uses`. Filtering to 'uses' dropped
    // `chain_next`, so a tracker run's trajectory neighbours vanished from the
    // adjacency index and its lines stopped drawing. (Drawing the two kinds
    // *differently* is separate viz work; the payload keeps them distinct either way.)
    this.collectionEdges = (data.edges || [])
      .filter(e => e.scope === 'collection' && e.type !== 'cooccurrence')
      .map(e => ({ source: e.source, target: e.target, weight: e.weight, type: e.type }));

    // Collection adjacency index (both directions), built once — so neighborhood
    // hover reads O(1) instead of rescanning the edge arrays per lookup.
    this.collectionNeighbors = new Map();
    const _adj = (a, b) => {
      let s = this.collectionNeighbors.get(a);
      if (!s) this.collectionNeighbors.set(a, s = new Set());
      s.add(b);
    };
    for (const e of [...this.collectionRoutes, ...this.collectionEdges]) {
      if (e.source == null || e.target == null) continue;
      _adj(e.source, e.target); _adj(e.target, e.source);
    }

    // Trade routes
    // Sorted strongest-first so the renderer can draw just the top-N as the
    // domain backbone (there can be ~9k routes; the weak tail is near-invisible
    // at ~0.06 alpha but was the dominant stroke cost when zoomed in).
    this.tradeRoutes = edgesOf('cooccurrence', 'domain').map(r => ({
      source: r.source,
      target: r.target,
      weight: r.weight,
      pulseStart: null,
      pulseDuration: 900,
    })).sort((a, b) => b.weight - a.weight);

    // Reset ambient pulses on data reload
    this.pulses = [];
    this.nextPulseTime = 0;
  }

  /**
   * Read an entity record from either vocabulary into the fields placement needs.
   *
   * Nodes carry `label` / `subtype` / `degree` / `memberships[]`. Only `domain` and
   * `collection` memberships are bucketed: routing every non-collection container into the domain
   * map would put an unknown container's ids into `domainEntities`, keyed by something
   * `this.domains` has never heard of.
   */
  _entityFields(n) {
    const dw = {}, rw = {};
    for (const m of (n.memberships || [])) {
      if (m.container_type === 'collection') rw[m.id] = m.weight;
      else if (m.container_type === 'domain') dw[m.id] = m.weight;
    }
    return { id: n.id, name: n.label, subtype: n.subtype, degree: n.degree || 0, dw, rw };
  }

  /**
   * Build a render node for one entity record, or null if it cannot be placed.
   *
   * Shared by the bulk load and by hydrateEntity so a lazily-resolved node lands
   * in exactly the same spot it would have had if it were in the render set.
   * Requires domains and collections to be positioned already.
   */
  _makeEntityNode(ent) {
    const { id: entId, name, subtype, degree, dw, rw } = this._entityFields(ent);
    const useCollection = Object.keys(rw).some(rid => this.collections.has(rid));

    let sx = 0, sy = 0, tw = 0;
    if (useCollection) {
      for (const [rid, w] of Object.entries(rw)) {
        const coll = this.collections.get(rid);
        if (!coll) continue;
        const cw = w * w * w;       // cube for symmetry breaking
        sx += coll.x * cw; sy += coll.y * cw; tw += cw;
      }
    } else {
      for (const [dname, w] of Object.entries(dw)) {
        const dom = this.domains.get(dname);
        if (!dom) continue;
        const cw = w * w * w;
        sx += dom.x * cw; sy += dom.y * cw; tw += cw;
      }
    }
    if (tw === 0) return null;
    sx /= tw; sy /= tw;

    // Color keeps the domain palette (blended) for visual continuity.
    const domColors = [], domWeights = [];
    for (const [dname, w] of Object.entries(dw)) {
      const dom = this.domains.get(dname);
      if (dom) { domColors.push(dom.color); domWeights.push(w); }
    }
    const vc = degree;
    const color = domColors.length > 1 ? blendColors(domColors, domWeights) : (domColors[0] || '#e0a030');
    const jx = sx + rnd(-30, 30), jy = sy + rnd(-30, 30);

    return {
      id: entId,
      kind: 'entity',
      name,
      label: name,
      type: subtype,
      color,
      radius: 2 + Math.min(sqrt(vc) * 0.8, 8),
      worldX: jx,
      worldY: jy,
      x: jx,
      y: jy,
      vx: 0, vy: 0,
      sourceCount: vc,
      domainWeights: dw,
      collectionWeights: rw,
      phase: random() * TAU,
      birthScale: 0,
      stability: Math.min(0.9, 0.3 * Math.log(vc + 1) / Math.log(30)),
      activityGlow: 0,
    };
  }

  /** Rebuild the pool of lowest-degree nodes that are safe to displace. */
  _refillEvictPool() {
    const cands = [];
    for (const [id, e] of this.entities) {
      if (id === this.pinnedId || id === this.hoveredId) continue;
      if (e.activityGlow > 0) continue;   // mid-animation; dropping it would flicker
      cands.push(e);
    }
    cands.sort((a, b) => a.sourceCount - b.sourceCount);
    this._evictPool = cands.slice(0, EVICT_POOL_SIZE).map(e => e.id);
  }

  /**
   * Drop one of the weakest rendered nodes to make room for `protectId`.
   *
   * Random within the low-degree pool so repeated hydration doesn't keep hammering
   * the single weakest node. Refills once if the pool is exhausted or fully
   * protected; returns false only if nothing is safe to evict, in which case the
   * caller accepts one node over cap rather than refusing to resolve a search hit.
   */
  _evictWeakest(protectId) {
    for (let attempt = 0; attempt < 2; attempt++) {
      while (this._evictPool.length) {
        const i = (random() * this._evictPool.length) | 0;
        const id = this._evictPool.splice(i, 1)[0];
        if (id === protectId || id === this.pinnedId || id === this.hoveredId) continue;
        const victim = this.entities.get(id);
        if (!victim || victim.activityGlow > 0) continue;
        this.entities.delete(id);
        // Keep the hover reverse-indexes free of dangling nodes. Domain
        // `entityCount` is deliberately NOT decremented: it sizes the domain glyph
        // and reflects the server's count, so it must stay stable as nodes swap.
        for (const path of Object.keys(victim.domainWeights || {})) {
          const arr = this.domainEntities.get(path);
          const at = arr ? arr.indexOf(victim) : -1;
          if (at >= 0) arr.splice(at, 1);
        }
        for (const rid of Object.keys(victim.collectionWeights || {})) {
          const arr = this.collectionEntities.get(rid);
          const at = arr ? arr.indexOf(victim) : -1;
          if (at >= 0) arr.splice(at, 1);
        }
        return true;
      }
      this._refillEvictPool();
    }
    return false;
  }

  /**
   * Resolve an entity by id or name, adding a render node for it if the payload
   * described it but did not include it in the render set.
   *
   * A hydrated node is deliberately left out of `domainEntities` / `collectionEntities`
   * and out of each domain's `entityCount`: hydrating must make a node
   * addressable without shifting domain sizes or hover neighborhoods. It also
   * skips `_spreadEntities`, so it can overlap a neighbour — acceptable for a
   * transient search hit, and far better than the node being unreachable.
   */
  hydrateEntity(idOrName) {
    if (!idOrName) return null;
    const key = String(idOrName);

    const direct = this.entities.get(key);
    if (direct) return direct;

    const lower = key.toLowerCase();
    const id = this.nodeIndex.has(key) ? key : this.nodeIdByName.get(lower);
    if (id) {
      const already = this.entities.get(id);
      if (already) return already;
      const node = this._makeEntityNode(this.nodeIndex.get(id));
      if (node) {
        // Swap, don't grow — make room before inserting.
        if (this.renderCap && this.entities.size >= this.renderCap) this._evictWeakest(id);
        node.hydrated = true;
        this.entities.set(node.id, node);
        return node;
      }
      // Placement failed (no resolvable domain or collection). FALL THROUGH rather than
      // returning null: an entity with the same label may already be in the render set,
      // and returning null here hid it — the index said "exists" and we answered "no",
      // so a search hit for an already-visible star silently failed to light it up.
    }

    // Payload without a `positions` map (older snapshot): fall back to scanning
    // the render set by label, which is what callers used to do inline.
    for (const [, e] of this.entities) {
      // A node with no label would throw here and abort the glow for the WHOLE result
      // set — and this fallback runs on every miss, including typos.
      if ((e.label || '').toLowerCase() === lower) return e;
    }
    return null;
  }

  /** Push domains apart to enforce minimum separation */
  _enforceMinSeparation(minDist, iterations) {
    const doms = [...this.domains.values()];
    for (let iter = 0; iter < iterations; iter++) {
      for (let i = 0; i < doms.length; i++) {
        for (let j = i + 1; j < doms.length; j++) {
          const a = doms[i], b = doms[j];
          const dx = b.x - a.x, dy = b.y - a.y;
          const dist = hypot(dx, dy) || 1;
          if (dist < minDist) {
            const push = (minDist - dist) / 2;
            const nx = dx / dist, ny = dy / dist;
            a.x -= nx * push * 0.5;
            a.y -= ny * push * 0.5;
            b.x += nx * push * 0.5;
            b.y += ny * push * 0.5;
          }
        }
      }
    }
    // Update worldX/Y to match
    for (const d of doms) {
      d.worldX = d.x;
      d.worldY = d.y;
    }
  }

  /** Group nearby domains into clusters */
  _computeClusters(threshold) {
    const doms = [...this.domains.values()];
    const assigned = new Set();
    this.clusters = [];

    for (let i = 0; i < doms.length; i++) {
      if (assigned.has(i)) continue;
      const members = [i];
      assigned.add(i);

      for (let j = i + 1; j < doms.length; j++) {
        if (assigned.has(j)) continue;
        const dx = doms[i].x - doms[j].x;
        const dy = doms[i].y - doms[j].y;
        if (hypot(dx, dy) < threshold) {
          members.push(j);
          assigned.add(j);
        }
      }

      // Compute centroid and radius
      let cx = 0, cy = 0;
      for (const mi of members) {
        cx += doms[mi].x;
        cy += doms[mi].y;
      }
      cx /= members.length;
      cy /= members.length;

      let maxR = 0;
      for (const mi of members) {
        const dist = hypot(doms[mi].x - cx, doms[mi].y - cy);
        maxR = Math.max(maxR, dist + doms[mi].radius * 2);
      }

      this.clusters.push({
        id: `cluster-${this.clusters.length}`,
        domainPaths: members.map(mi => doms[mi].path),
        centroidX: cx,
        centroidY: cy,
        radius: Math.max(maxR, 200),
        members: members.map(mi => doms[mi]),
      });
    }
  }

  /** Push overlapping entities apart using spatial grid for performance */
  _spreadEntities(minDist, iterations) {
    const ents = [...this.entities.values()];
    if (ents.length < 2) return;

    const cellSize = minDist * 2;

    for (let iter = 0; iter < iterations; iter++) {
      // Build spatial grid
      const grid = {};
      for (const e of ents) {
        const cx = Math.floor(e.x / cellSize);
        const cy = Math.floor(e.y / cellSize);
        const key = `${cx},${cy}`;
        (grid[key] ??= []).push(e);
      }

      // Check neighbors in adjacent cells
      for (const e of ents) {
        const cx = Math.floor(e.x / cellSize);
        const cy = Math.floor(e.y / cellSize);

        for (let dx = -1; dx <= 1; dx++) {
          for (let dy = -1; dy <= 1; dy++) {
            const neighbors = grid[`${cx + dx},${cy + dy}`];
            if (!neighbors) continue;

            for (const other of neighbors) {
              if (other === e) continue;
              const ddx = e.x - other.x;
              const ddy = e.y - other.y;
              const dist = hypot(ddx, ddy) || 0.1;
              if (dist < minDist) {
                const push = (minDist - dist) / 2 * 0.3;
                const nx = ddx / dist, ny = ddy / dist;
                e.x += nx * push;
                e.y += ny * push;
                other.x -= nx * push;
                other.y -= ny * push;
              }
            }
          }
        }
      }
    }

    // Update worldX/Y
    for (const e of ents) {
      e.worldX = e.x;
      e.worldY = e.y;
    }
  }

  /** Update per-frame state */
  update(dt) {
    this.tick++;
    const now = performance.now();

    // Grow birthScale
    for (const d of this.domains.values()) {
      if (d.birthScale < 1) d.birthScale = Math.min(1, d.birthScale + 0.015);
      // Decay activity — matches reference: 0.0015 * dt
      if (d.activityGlow > 0) d.activityGlow = Math.max(0, d.activityGlow - 0.0015 * dt);
      d.rot += 0.0003;
    }

    for (const e of this.entities.values()) {
      if (e.birthScale < 1) e.birthScale = Math.min(1, e.birthScale + 0.012);
      if (e.activityGlow > 0) e.activityGlow = Math.max(0, e.activityGlow - 0.0015 * dt);
    }

    for (const r of this.collections.values()) {
      if (r.birthScale < 1) r.birthScale = Math.min(1, r.birthScale + 0.02);
    }

    // Update active pulses
    this.pulses = this.pulses.filter(p => p.progress < 1);
    for (const p of this.pulses) {
      p.progress += p.speed;
      // Boost destination domain on arrival
      if (p.progress >= 0.82 && !p.arrivedFlag) {
        p.arrivedFlag = true;
        const tgtDom = this.domains.get(p.target);
        if (tgtDom) tgtDom.activityGlow = Math.min(1, tgtDom.activityGlow + 0.5);
      }
    }

    // Pulses are only spawned by search glow — no ambient firing
  }

  /** Trigger search glow — cascaded timing per animation spec */
  triggerSearchGlow(entityNames) {
    const hitEntities = [];
    const hitDomainPaths = new Set();

    // Find hit entities (preserve search ranking order). Resolves through the
    // full node index, so a hit on the un-rendered tail still lights up.
    for (const name of entityNames) {
      const e = this.hydrateEntity(name);
      if (!e) continue;
      hitEntities.push(e);
      if (e.domainWeights) {
        for (const path of Object.keys(e.domainWeights)) hitDomainPaths.add(path);
      }
    }

    if (!hitEntities.length) return;

    // Rank domains by hit density
    const domScores = {};
    for (const path of hitDomainPaths) {
      domScores[path] = hitEntities.filter(e =>
        e.domainWeights && path in e.domainWeights
      ).length;
    }
    const topDomains = [...hitDomainPaths]
      .sort((a, b) => (domScores[b] || 0) - (domScores[a] || 0))
      .slice(0, 5);
    hitDomainPaths.clear();
    for (const p of topDomains) hitDomainPaths.add(p);

    // t=0: Flash entities (subtle, staggered)
    hitEntities.forEach((e, i) => {
      setTimeout(() => {
        e.activityGlow = Math.min(1.0, e.activityGlow + 0.5);
      }, i * 40);
    });

    // t=150ms: Entity → domain pulses (energy flows up to parent domains)
    setTimeout(() => {
      const seen = new Set(); // one pulse per entity-domain pair
      hitEntities.slice(0, 8).forEach((e, i) => {
        if (!e.domainWeights) return;
        for (const path of Object.keys(e.domainWeights)) {
          if (!hitDomainPaths.has(path)) continue;
          const key = `${e.id}:${path}`;
          if (seen.has(key)) continue;
          seen.add(key);
          const dom = this.domains.get(path);
          if (!dom) continue;
          setTimeout(() => {
            this.pulses.push({
              source: path,  // we store domain path but use entity position for rendering
              target: path,
              fromX: e.x, fromY: e.y,
              toX: dom.x, toY: dom.y,
              col: hexRGB(dom.color),
              progress: 0,
              speed: 0.007 + random() * 0.005,
              arrivedFlag: false,
            });
          }, i * 50);
        }
      });
    }, 150);

    // t=500ms: Domains brighten on pulse arrival
    setTimeout(() => {
      for (const path of hitDomainPaths) {
        const dom = this.domains.get(path);
        if (dom) dom.activityGlow = Math.min(1.0, dom.activityGlow + 0.45);
      }
    }, 500);

    // t=700ms: Fire route pulses between hit domains (after entity→domain arrives)
    setTimeout(() => {
      // Score routes by hit density
      const domainHitCount = {};
      for (const e of hitEntities) {
        if (e.domainWeights) {
          for (const [path, w] of Object.entries(e.domainWeights)) {
            domainHitCount[path] = (domainHitCount[path] || 0) + w;
          }
        }
      }

      const candidates = [];
      for (const route of this.tradeRoutes) {
        if (hitDomainPaths.has(route.source) && hitDomainPaths.has(route.target)) {
          const score = (domainHitCount[route.source] || 0) + (domainHitCount[route.target] || 0);
          candidates.push({ route, score });
        }
      }
      // Sort by score, shuffle ties to avoid alphabetical spatial bias
      candidates.sort((a, b) => b.score - a.score || (random() - 0.5));

      // Cap at 4 pulses — direction flows from more-hit domain outward
      candidates.slice(0, 4).forEach((c, i) => {
        // Pulse flows from the domain with more search hits to the one with fewer
        const scoreA = domainHitCount[c.route.source] || 0;
        const scoreB = domainHitCount[c.route.target] || 0;
        const from = scoreA >= scoreB ? c.route.source : c.route.target;
        const to = scoreA >= scoreB ? c.route.target : c.route.source;
        const srcDom = this.domains.get(from);
        if (srcDom) {
          setTimeout(() => {
            this.pulses.push({
              source: from,
              target: to,
              col: hexRGB(srcDom.color),
              progress: 0,
              speed: 0.008,
              arrivedFlag: false,
            });
          }, i * 150);
        }
      });
    }, 700);

    // t=1200ms: Arrival — destination domains get secondary pulse
    setTimeout(() => {
      for (const path of hitDomainPaths) {
        const dom = this.domains.get(path);
        if (dom) dom.activityGlow = Math.min(1.0, dom.activityGlow + 0.4);
      }
    }, 1200);
  }
}
