/**
 * Star View renderer — single entity with documents and co-occurring entities.
 * The deepest zoom level: a local 2-hop graph.
 */

import { sin, cos, TAU, PI, hexRGB, rgba, clamp, min, max, sqrt } from '../core/utils.js';
import { docGlowSprite, coEntityGlowSprite, DOC_R0, CO_R0 } from './sprites.js';

// Is a world-space point outside the visible box (with the box's own margin)? Used
// to skip drawing off-screen nodes — the big win once a star has many documents.
function _culled(view, x, y) {
  return view && (x < view.left || x > view.right || y < view.top || y > view.bottom);
}

const TYPE_COLORS = {
  Person: '#378ADD', Organization: '#7F77DD', Product: '#1D9E75',
  Technology: '#BA7517', Event: '#D85A30', Concept: '#9c9a92',
  Location: '#5DCAA5', Process: '#6BBACC', Tool: '#BA7517',
  Repo: '#e0a030',
};

function typeColor(type) { return TYPE_COLORS[type] || '#9c9a92'; }

/** Draw the central entity star */
export function drawCentralStar(ctx, entity, tick) {
  const tc = typeColor(entity.type);
  const [r, g, b] = hexRGB(tc);
  const act = entity.activityGlow || 0;
  const boost = 1 + act * 2.0;
  const breathe = 0.95 + 0.05 * sin(tick * 0.0015);
  const sz = entity.radius;

  // Wide halo
  const g1 = ctx.createRadialGradient(entity.x, entity.y, 0, entity.x, entity.y, sz * 5);
  g1.addColorStop(0, rgba(r, g, b, 0.08 * boost));
  g1.addColorStop(0.3, rgba(r, g, b, 0.03 * boost));
  g1.addColorStop(1, rgba(r, g, b, 0));
  ctx.fillStyle = g1;
  ctx.beginPath();
  ctx.arc(entity.x, entity.y, sz * 5, 0, TAU);
  ctx.fill();

  // Inner glow
  const g2 = ctx.createRadialGradient(entity.x, entity.y, 0, entity.x, entity.y, sz * 1.8);
  g2.addColorStop(0, rgba(r, g, b, 0.25 * boost));
  g2.addColorStop(0.5, rgba(r, g, b, 0.10 * boost));
  g2.addColorStop(1, rgba(r, g, b, 0));
  ctx.fillStyle = g2;
  ctx.beginPath();
  ctx.arc(entity.x, entity.y, sz * 1.8, 0, TAU);
  ctx.fill();

  // Core star
  ctx.shadowBlur = sz * 2;
  ctx.shadowColor = `rgba(255,255,255,${0.4 * boost})`;
  const g3 = ctx.createRadialGradient(entity.x, entity.y, 0, entity.x, entity.y, sz * 0.6);
  g3.addColorStop(0, `rgba(255,255,255,${min(1, 0.95 * boost * breathe)})`);
  g3.addColorStop(0.4, rgba(r, g, b, 0.8 * boost));
  g3.addColorStop(1, rgba(r, g, b, 0));
  ctx.fillStyle = g3;
  ctx.beginPath();
  ctx.arc(entity.x, entity.y, sz * 0.6, 0, TAU);
  ctx.fill();
  ctx.shadowBlur = 0;

  // Spikes
  const spikeLen = sz * 2.5 * breathe;
  const spikeA = 0.25 * boost;
  for (let s = 0; s < 4; s++) {
    const a = s * PI / 2 + tick * 0.0002;
    const x2 = entity.x + cos(a) * spikeLen;
    const y2 = entity.y + sin(a) * spikeLen;
    const spg = ctx.createLinearGradient(entity.x, entity.y, x2, y2);
    spg.addColorStop(0, `rgba(255,255,255,${spikeA})`);
    spg.addColorStop(0.3, rgba(r, g, b, spikeA * 0.5));
    spg.addColorStop(1, rgba(r, g, b, 0));
    ctx.strokeStyle = spg;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(entity.x, entity.y);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  // Label
  ctx.fillStyle = `rgba(220,215,200,${0.65 * boost})`;
  ctx.font = `400 22px 'Courier New', monospace`;
  ctx.textAlign = 'center';
  ctx.fillText(entity.name, entity.x, entity.y + sz * 3.5);
  ctx.font = "14px 'Courier New', monospace";
  const [tr, tg, tb] = hexRGB(tc);
  ctx.fillStyle = rgba(tr, tg, tb, 0.45);
  ctx.fillText(`${entity.type} · ${entity.sourceCount} docs`, entity.x, entity.y + sz * 3.5 + 20);
}

/** Draw document nodes orbiting the entity.
 *  `activeId` = pinned-or-hovered node; `hl` = Set of highlighted node ids (the active
 *  node + everything it connects to) or null. When something is active, docs OUTSIDE the
 *  highlight set dim right down and the active doc brightens — the collection-view model. */
export function drawDocuments(ctx, docs, tick, activeId, view, hl) {
  const glow = docGlowSprite();
  for (const doc of docs) {
    const isActive = activeId === doc.id;
    const inHl = !hl || hl.has(doc.id);
    const dim = hl && !inHl;
    const act = doc.activityGlow || 0;
    let alpha = clamp(0.7 + act * 0.4, 0, 1);
    if (isActive) alpha = 1; else if (dim) alpha *= 0.14;

    // Orbital drift — always update _px/_py (used by hit-test + connections) even
    // when the node itself is culled, so those stay accurate.
    const ox = sin(tick * 0.0002 * doc.orbitSpeed + doc.orbitPhase) * doc.orbitDrift;
    const oy = cos(tick * 0.00025 * doc.orbitSpeed + doc.orbitPhase * 1.2) * doc.orbitDrift * 0.7;
    const px = doc.x + ox;
    const py = doc.y + oy;
    doc._px = px;
    doc._py = py;
    if (_culled(view, px, py)) continue;

    // Document node — warm amber glow, pre-baked into an offscreen sprite. The active
    // doc gets a bigger, hotter halo so it reads as the focus.
    const sz = doc.radius * (isActive ? 1.5 : 1);
    const half = (glow.width * 0.5) * (sz / DOC_R0) * (isActive ? 1.4 : 1);
    ctx.globalAlpha = min(1, alpha);
    ctx.drawImage(glow, px - half, py - half, half * 2, half * 2);
    if (isActive) { ctx.drawImage(glow, px - half, py - half, half * 2, half * 2); } // double-pass = hotter core
    ctx.globalAlpha = 1;

    // Core
    ctx.fillStyle = `rgba(255,228,175,${(isActive ? 1 : 0.8) * alpha})`;
    ctx.beginPath();
    ctx.arc(px, py, sz * 0.5, 0, TAU);
    ctx.fill();

    // Tiny document icon — two horizontal lines (skip when dimmed to cut noise)
    if (!dim) {
      ctx.strokeStyle = `rgba(255,220,160,${0.5 * alpha})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px - sz * 0.3, py - sz * 0.15);
      ctx.lineTo(px + sz * 0.3, py - sz * 0.15);
      ctx.moveTo(px - sz * 0.3, py + sz * 0.15);
      ctx.lineTo(px + sz * 0.2, py + sz * 0.15);
      ctx.stroke();
    }

    // Label — the active doc only (its attached entities get their own labels).
    if (isActive) {
      const label = doc.title.length > 30 ? doc.title.slice(0, 28) + '…' : doc.title;
      ctx.fillStyle = `rgba(255,235,190,0.98)`;
      ctx.font = `16px 'Courier New', monospace`;
      ctx.textAlign = 'center';
      ctx.fillText(label, px, py + sz * 2 + 10);
    }
  }
}

/** Draw co-occurring entities */
export function drawCoEntities(ctx, coEntities, tick, activeId, view, hl) {
  for (const e of coEntities) {
    const isActive = activeId === e.id;
    const inHl = !hl || hl.has(e.id);         // in the active node's highlight set
    const lit = hl && inHl;                    // highlighted because the active node links to it
    const dim = hl && !inHl;
    const tc = typeColor(e.type);
    const [rc, gc, bc] = hexRGB(tc);
    const act = e.activityGlow || 0;
    let alpha = clamp(0.5 + act * 0.5, 0, 1);
    if (isActive) alpha = 1.3; else if (lit) alpha = 1.1; else if (dim) alpha *= 0.12;

    // Slow drift — keep _px/_py current even when culled (hit-test + connections).
    const ox = sin(tick * 0.00015 * e.orbitSpeed + e.orbitPhase) * e.orbitDrift;
    const oy = cos(tick * 0.0002 * e.orbitSpeed + e.orbitPhase * 1.4) * e.orbitDrift * 0.6;
    const px = e.x + ox;
    const py = e.y + oy;
    e._px = px;
    e._py = py;
    if (_culled(view, px, py)) continue;

    // Glow — pre-baked sprite. Highlighted co-entities get a bigger, double-passed halo.
    const glow = coEntityGlowSprite(tc);
    const boost = (isActive || lit) ? 1.4 : 1;
    const half = (glow.width * 0.5) * (e.radius / CO_R0) * boost;
    ctx.globalAlpha = min(1, alpha);
    ctx.drawImage(glow, px - half, py - half, half * 2, half * 2);
    if (isActive || lit) { ctx.drawImage(glow, px - half, py - half, half * 2, half * 2); }
    ctx.globalAlpha = 1;

    // Crisp core when highlighted (matches the collection node core).
    if (!dim) {
      ctx.fillStyle = `rgba(255,240,220,${isActive ? 0.98 : lit ? 0.85 : 0.6})`;
      ctx.beginPath();
      ctx.arc(px, py, Math.max(1.3, e.radius * 0.4), 0, TAU);
      ctx.fill();
    }

    // Label — the active co-entity, AND every co-entity a hovered/pinned doc is attached
    // to (so the doc's connected entities reveal their names). Resting cloud stays clean.
    const showLabel = isActive || lit;
    if (showLabel) {
      ctx.fillStyle = `rgba(255,244,224,${isActive ? 0.95 : 0.85})`;
      ctx.font = `${isActive ? 16 : 13}px 'Courier New', monospace`;
      ctx.textAlign = 'center';
      ctx.fillText(e.name, px, py - e.radius * 2 - 4);
      if (isActive) {
        ctx.font = "11px 'Courier New', monospace";
        ctx.fillStyle = rgba(rc, gc, bc, 0.55);
        ctx.fillText(`${e.type} · ${e.weight} shared`, px, py - e.radius * 2 + 10);
      }
    }
  }
}

/** Draw a small glowing "mini star" — brighter/sharper than a co-entity dot,
 *  but smaller and dimmer than the central star. Used for repo peripheral
 *  nodes (in both the star view and the repo view's connected-repo ring).
 *  `opts`: { color, brightness (0-1, relative boost vs. central star),
 *  subLabel (optional second line under the name) }. */
export function drawMiniStar(ctx, node, tick, hoveredId, opts = {}) {
  const color = opts.color || '#e0a030';
  const brightness = opts.brightness ?? 0.55;
  const [r, g, b] = hexRGB(color);
  const hov = hoveredId === node.id;
  const act = node.activityGlow || 0;
  const boost = (1 + act * 2.0) * brightness * (hov ? 1.35 : 1);
  const breathe = 0.95 + 0.05 * sin(tick * 0.0015 + (node.orbitPhase || 0));
  const sz = node.radius;

  // Slow drift, same feel as co-entities
  const ox = sin(tick * 0.00015 * (node.orbitSpeed || 0.5) + (node.orbitPhase || 0)) * (node.orbitDrift || 0);
  const oy = cos(tick * 0.0002 * (node.orbitSpeed || 0.5) + (node.orbitPhase || 0) * 1.4) * (node.orbitDrift || 0) * 0.6;
  const px = node.x + ox;
  const py = node.y + oy;
  node._px = px;
  node._py = py;

  // Wide halo
  const g1 = ctx.createRadialGradient(px, py, 0, px, py, sz * 4);
  g1.addColorStop(0, rgba(r, g, b, 0.10 * boost));
  g1.addColorStop(0.3, rgba(r, g, b, 0.04 * boost));
  g1.addColorStop(1, rgba(r, g, b, 0));
  ctx.fillStyle = g1;
  ctx.beginPath();
  ctx.arc(px, py, sz * 4, 0, TAU);
  ctx.fill();

  // Inner glow
  const g2 = ctx.createRadialGradient(px, py, 0, px, py, sz * 1.6);
  g2.addColorStop(0, rgba(r, g, b, 0.3 * boost));
  g2.addColorStop(0.5, rgba(r, g, b, 0.12 * boost));
  g2.addColorStop(1, rgba(r, g, b, 0));
  ctx.fillStyle = g2;
  ctx.beginPath();
  ctx.arc(px, py, sz * 1.6, 0, TAU);
  ctx.fill();

  // Core
  ctx.shadowBlur = sz * 1.2;
  ctx.shadowColor = `rgba(255,255,255,${0.3 * boost})`;
  const g3 = ctx.createRadialGradient(px, py, 0, px, py, sz * 0.55);
  g3.addColorStop(0, `rgba(255,255,255,${min(1, 0.85 * boost * breathe)})`);
  g3.addColorStop(0.4, rgba(r, g, b, 0.75 * boost));
  g3.addColorStop(1, rgba(r, g, b, 0));
  ctx.fillStyle = g3;
  ctx.beginPath();
  ctx.arc(px, py, sz * 0.55, 0, TAU);
  ctx.fill();
  ctx.shadowBlur = 0;

  // Spikes — shorter/fewer than the central star, but present (what makes
  // this read as a "star" rather than a co-entity glow dot)
  const spikeLen = sz * 1.6 * breathe;
  const spikeA = 0.22 * boost;
  for (let s = 0; s < 4; s++) {
    const a = s * PI / 2 + tick * 0.00015;
    const x2 = px + cos(a) * spikeLen;
    const y2 = py + sin(a) * spikeLen;
    const spg = ctx.createLinearGradient(px, py, x2, y2);
    spg.addColorStop(0, `rgba(255,255,255,${spikeA})`);
    spg.addColorStop(0.3, rgba(r, g, b, spikeA * 0.5));
    spg.addColorStop(1, rgba(r, g, b, 0));
    ctx.strokeStyle = spg;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, py);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  // Label
  const label = opts.label || node.name;
  ctx.fillStyle = `rgba(240,200,120,${(hov ? 0.9 : 0.5) * min(1, boost)})`;
  ctx.font = `${hov ? 14 : 12}px 'Courier New', monospace`;
  ctx.textAlign = 'center';
  ctx.fillText(label, px, py - sz * 2 - 6);
  const subLabel = opts.subLabel;
  if (subLabel) {
    ctx.font = "10px 'Courier New', monospace";
    ctx.fillStyle = rgba(r, g, b, 0.45);
    ctx.fillText(subLabel, px, py - sz * 2 + 8);
  }
}

/** Draw the full 2-hop connection graph.
 *  center ↔ docs (always),  doc ↔ co-entity (via shared_doc_ids),
 *  Hover highlights the full chain: hover doc → light up its co-entities,
 *  hover co-entity → light up its shared docs.
 */
export function drawConnections(ctx, centerX, centerY, docs, coEntities, activeId, centralEntityId, docIndex, view) {
  if (!activeId) return;   // no resting spokes — links appear only for the active node
  const actDoc = docIndex ? docIndex.get(activeId) : docs.find(d => d.id === activeId);
  const actCo = coEntities.find(e => e.id === activeId);
  const HILITE = 'rgba(255,222,150,0.6)';   // collection-view bright link

  if (actDoc) {
    // center → the active doc
    ctx.strokeStyle = HILITE;
    ctx.lineWidth = 1.8;
    ctx.setLineDash([3, 7]);
    ctx.beginPath(); ctx.moveTo(centerX, centerY); ctx.lineTo(actDoc._px, actDoc._py); ctx.stroke();
    ctx.setLineDash([]);
    // active doc → each co-entity it is attached to (in that entity's own colour)
    for (const co of coEntities) {
      if (!co.sharedDocIds || !co.sharedDocIds.includes(actDoc.id)) continue;
      const [rc, gc, bc] = hexRGB(typeColor(co.type));
      ctx.strokeStyle = rgba(rc, gc, bc, 0.5);
      ctx.lineWidth = 1.4;
      ctx.setLineDash([2, 6]);
      ctx.beginPath(); ctx.moveTo(actDoc._px, actDoc._py); ctx.lineTo(co._px, co._py); ctx.stroke();
      ctx.setLineDash([]);
    }
  } else if (actCo) {
    // center → the active co-entity
    ctx.strokeStyle = HILITE;
    ctx.lineWidth = 1.4;
    ctx.setLineDash([3, 7]);
    ctx.beginPath(); ctx.moveTo(centerX, centerY); ctx.lineTo(actCo._px, actCo._py); ctx.stroke();
    ctx.setLineDash([]);
    // active co-entity → each doc it shares
    const [rc, gc, bc] = hexRGB(typeColor(actCo.type));
    for (const docId of (actCo.sharedDocIds || [])) {
      const doc = docIndex ? docIndex.get(docId) : docs.find(d => d.id === docId);
      if (!doc) continue;
      ctx.strokeStyle = rgba(rc, gc, bc, 0.32);
      ctx.lineWidth = 1.1;
      ctx.setLineDash([2, 6]);
      ctx.beginPath(); ctx.moveTo(actCo._px, actCo._py); ctx.lineTo(doc._px, doc._py); ctx.stroke();
      ctx.setLineDash([]);
    }
  } else if (activeId === centralEntityId) {
    // center → every doc, faint, so the core's reach reads without drowning the field
    ctx.strokeStyle = 'rgba(255,200,120,0.10)';
    ctx.lineWidth = 0.6;
    ctx.setLineDash([3, 8]);
    for (const doc of docs) {
      ctx.beginPath(); ctx.moveTo(centerX, centerY); ctx.lineTo(doc._px, doc._py); ctx.stroke();
    }
    ctx.setLineDash([]);
  }
}
