# ABOUTME: The handful of primitive graph reads every surface is built from, each
# ABOUTME: carrying the active-graph filter and bounded IN clauses so callers cannot forget.

"""Primitive graph reads.

The read path repeats about five ideas over and over: look a node up, count its degree,
get its container memberships, get its neighbours, list a container's contents.
Everything from the neighborhood endpoint to the MCP server is one of those with a
different scope.

The reason to have them in one place is **correctness, not tidiness**. Each of these
queries has two obligations that are easy to forget and invisible when forgotten:

- **Active by default.** Every query touching `entities` must repeat
  `invalid_at IS NULL`, or an entity removed through the corrections flow comes back.
  The opt-out is real and load-bearing: the dedup path passes `include_invalid=True` on
  purpose, so re-ingest re-attaches to an invalidated node instead of resurrecting it as
  a duplicate.
- **Bounded IN clauses.** Ids are chunked under SQLite's bound-parameter limit
  (SQLITE_MAX_VARIABLE_NUMBER, historically 999). Hand-rolled versions of these queries
  build one clause over an unbounded id list, which works until a domain gets big enough
  and then raises.

A copy-paste obligation gets forgotten; a shared reader cannot.
"""

from __future__ import annotations

from collections import defaultdict

# Keep well under SQLITE_MAX_VARIABLE_NUMBER, leaving room for other bound params.
_CHUNK = 900

ACTIVE = "invalid_at IS NULL"


def _chunks(seq, size=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ── 1. node lookup ──────────────────────────────────────────────────────────

def entities_by_ids(conn, ids, *, include_invalid=False) -> dict[str, dict]:
    """id -> {id, canonical_name, type} for the ids that exist and are active."""
    out: dict[str, dict] = {}
    clause = "" if include_invalid else f" AND {ACTIVE}"
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT id, canonical_name, type FROM entities WHERE id IN ({ph}){clause}",
            list(chunk),
        ):
            out[r["id"]] = {"id": r["id"], "canonical_name": r["canonical_name"],
                            "type": r["type"]}
    return out


def entity_by_name(conn, name, *, include_invalid=False) -> dict | None:
    """Case-insensitive single lookup, deterministic on duplicate names.

    Replaces the `list(limit=500)` + client-side linear scan that the route layer and
    the MCP server each improvised: an entity past position 500 was reported "not
    found", and which 500 you got depended on the default ordering. Resolving in SQL has
    no cap and no ordering dependency.
    """
    clause = "" if include_invalid else f" AND {ACTIVE}"
    row = conn.execute(
        f"SELECT id, canonical_name, type FROM entities "
        f"WHERE lower(canonical_name) = lower(?){clause} ORDER BY id LIMIT 1",
        (name,),
    ).fetchone()
    return None if row is None else {"id": row["id"], "canonical_name": row["canonical_name"],
                                     "type": row["type"]}


# ── 2. degree ───────────────────────────────────────────────────────────────

def degrees_of(conn, ids) -> dict[str, int]:
    """id -> mention count. One GROUP BY, never a per-row correlated subquery — the
    latter turns a page of entities into a page of table scans.

    Filters like every other primitive here. Callers usually pass ids that came from
    `entities_by_ids` and are already active, but a primitive that is "active by
    default" cannot be the one place that quietly is not: a caller passing raw ids
    would get a degree for a soft-deleted entity and have no way to tell.
    """
    out: dict[str, int] = {i: 0 for i in ids}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT es.entity_id, COUNT(*) c FROM entity_sources es "
            f"JOIN entities e ON e.id = es.entity_id AND e.{ACTIVE} "
            f"WHERE es.entity_id IN ({ph}) GROUP BY es.entity_id",
            list(chunk),
        ):
            out[r["entity_id"]] = r["c"]
    return out


# ── 3. memberships ──────────────────────────────────────────────────────────

def domain_memberships(conn, ids=None) -> dict[str, dict[str, float]]:
    """id -> {domain_path: normalized weight}.

    `ids=None` covers the whole graph in one pass, which is what a full payload build
    needs; passing ids scopes it for a drill-in view.
    """
    raw: dict[str, dict[str, int]] = defaultdict(dict)

    def _collect(sql, params):
        for r in conn.execute(sql, params):
            raw[r[0]][r[1]] = r[2]

    base = (f"SELECT es.entity_id, dd.domain_path, COUNT(*) AS weight "
            f"FROM entity_sources es "
            f"JOIN document_domains dd ON es.document_id = dd.document_id "
            f"JOIN entities e ON e.id = es.entity_id AND e.{ACTIVE} ")
    if ids is None:
        _collect(base + "GROUP BY es.entity_id, dd.domain_path", ())
    else:
        for chunk in _chunks(ids):
            ph = ",".join("?" * len(chunk))
            _collect(base + f"WHERE es.entity_id IN ({ph}) "
                            f"GROUP BY es.entity_id, dd.domain_path", list(chunk))

    out: dict[str, dict[str, float]] = {}
    for eid, weights in raw.items():
        total = sum(weights.values())
        if total > 0:
            out[eid] = {p: round(w / total, 3) for p, w in weights.items()}
    return out


# ── 4. neighbours ───────────────────────────────────────────────────────────

def co_entities(conn, entity_id, *, doc_ids=None, limit=30) -> list[dict]:
    """Entities sharing a document with `entity_id`, strongest first.

    `shared_doc_ids` is returned because callers place a co-entity relative to the
    documents it shares, and fall back to an arbitrary arrangement without it.
    """
    def _run(scope_ids):
        params: list = [entity_id]
        scope = ""
        if scope_ids is not None:
            scope = f" AND es2.document_id IN ({','.join('?' * len(scope_ids))})"
            params += list(scope_ids)
        params.append(entity_id)
        # `src` filters the SOURCE entity too: es2 is checked, but without this an
        # invalidated entity would still return active neighbours.
        return conn.execute(
            f"""SELECT e.id, e.canonical_name, e.type, COUNT(*) AS weight,
                       GROUP_CONCAT(es2.document_id) AS docs
                FROM entity_sources es1
                JOIN entities src ON src.id = es1.entity_id AND src.{ACTIVE}
                JOIN entity_sources es2 ON es1.document_id = es2.document_id
                JOIN entities e ON e.id = es2.entity_id AND e.{ACTIVE}
                WHERE es1.entity_id = ?{scope} AND es2.entity_id != ?
                GROUP BY e.id""",
            params,
        ).fetchall()

    # Chunk rather than truncate: slicing doc_ids to one bound-parameter batch silently
    # drops every document past the first 900, so a large scope returns incomplete
    # weights AND incomplete shared_doc_ids. Merge per chunk, then rank once.
    merged: dict[str, dict] = {}
    batches = [None] if doc_ids is None else list(_chunks(doc_ids))
    if doc_ids is not None and not batches:
        return []
    for batch in batches:
        for r in _run(batch):
            acc = merged.setdefault(r["id"], {"id": r["id"], "canonical_name": r["canonical_name"],
                                              "type": r["type"], "weight": 0, "shared_doc_ids": []})
            acc["weight"] += r["weight"]
            if r["docs"]:
                acc["shared_doc_ids"].extend(r["docs"].split(","))

    ranked = sorted(merged.values(), key=lambda c: (-c["weight"], c["canonical_name"]))
    return ranked[:limit]


# ── 5. container contents ───────────────────────────────────────────────────

def entities_in_domain(conn, domain_path, *, limit=50, recursive=False) -> list[dict]:
    """Active entities mentioned in a domain's documents, by degree."""
    match = "dd.domain_path LIKE ? || '/%' OR dd.domain_path = ?" if recursive else "dd.domain_path = ?"
    params = [domain_path, domain_path] if recursive else [domain_path]
    rows = conn.execute(
        f"""SELECT e.id, e.canonical_name, e.type, COUNT(DISTINCT es.document_id) AS degree
            FROM entity_sources es
            JOIN document_domains dd ON dd.document_id = es.document_id
            JOIN entities e ON e.id = es.entity_id AND e.{ACTIVE}
            WHERE {match}
            GROUP BY e.id ORDER BY degree DESC, e.canonical_name LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [{"id": r["id"], "canonical_name": r["canonical_name"], "type": r["type"],
             "degree": r["degree"]} for r in rows]


def domain_neighbours(conn, domain_path, *, limit=10) -> list[dict]:
    """Domains sharing entities with `domain_path`, strongest first.

    Served from the materialized `domain_edges` table when it has been populated, and
    computed live otherwise — a correct slow answer beats an empty fast one, and a graph
    that has never been built must still be able to answer.
    """
    rows = conn.execute(
        """SELECT CASE WHEN source = ? THEN target ELSE source END AS path, weight
           FROM domain_edges WHERE source = ? OR target = ?
           ORDER BY weight DESC, path LIMIT ?""",
        (domain_path, domain_path, domain_path, limit),
    ).fetchall()
    if rows:
        return [{"path": r["path"], "weight": r["weight"]} for r in rows]
    return _domain_neighbours_live(conn, domain_path, limit=limit)


# ── 6. provenance (silo + kind) ──────────────────────────────────────────────
#
# Both resolve `kind` LIVE via the `silo_kind` view (documents.silo_id -> silo_kind ->
# provenance_kind on the silo row) — never a per-node materialized copy, so a source
# re-classified after ingest shows up immediately (spec: per-source silos + provenance,
# task 11a). A dominant-by-count pick, not a set: entities/collections are silo-scoped
# by normalization going forward (task 50), so almost every one has exactly one silo
# among its sources; the rare one spanning several (pre-#50 data, or a deliberate
# cross-silo merge) is represented by its heaviest. Ties favor a NAMED silo over the
# null pool, then break lexicographically, so the pick is deterministic.
#
# Consequence for `?silo=` filtering (routes/graph.py): a multi-silo node's graph
# node collapses to its DOMINANT silo, so `GET /graph?silo=<its non-dominant silo>`
# will not surface it, even though that silo genuinely contributed sources to it.
# `get_entity` is unaffected — it lists every source's own silo/kind individually,
# not a single collapsed pick.

def _pick_dominant(rows, id_key: str) -> dict[str, dict]:
    best: dict[str, tuple] = {}
    for r in rows:
        rid, sid, kind, w = r[id_key], r["silo_id"], r["kind"], r["weight"]
        key = (w, sid is not None, sid or "")
        if rid not in best or key > best[rid][0]:
            best[rid] = (key, sid, kind)
    return {rid: {"silo_id": v[1], "kind": v[2]} for rid, v in best.items()}


def entity_silos(conn, ids=None, in_slice: bool = False) -> dict[str, dict]:
    """entity_id -> {"silo_id", "kind"} — the entity's dominant silo, live-resolved.

    `ids=None` covers the whole graph in one pass (what the snapshot builder needs);
    passing ids scopes it to a drill-in read (neighborhood/subgraph).
    `in_slice=True` counts only documents in TEMP TABLE `_slice_docs` (caller created it).
    """
    sj = "JOIN _slice_docs sd ON sd.doc_id = es.document_id " if in_slice else ""
    base = (f"SELECT es.entity_id, d.silo_id, sk.kind, COUNT(*) AS weight "
            f"FROM entity_sources es "
            f"{sj}"
            f"JOIN documents d ON d.id = es.document_id{'' if in_slice else f' AND d.{ACTIVE}'} "
            f"JOIN entities e ON e.id = es.entity_id AND e.{ACTIVE} "
            f"LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id ")
    if ids is None:
        rows = conn.execute(base + "GROUP BY es.entity_id, d.silo_id").fetchall()
    else:
        rows = []
        for chunk in _chunks(ids):
            ph = ",".join("?" * len(chunk))
            rows += conn.execute(
                base + f"WHERE es.entity_id IN ({ph}) GROUP BY es.entity_id, d.silo_id",
                list(chunk),
            ).fetchall()
    return _pick_dominant(rows, "entity_id")


def collection_silos(conn, ids=None) -> dict[str, dict]:
    """collection_id -> {"silo_id", "kind"} — dominant by document count.

    In practice uniform: every document in a collection resolves to the SAME silo_id
    (the collection's own id, or its external source's id if it has one) via
    `resolve_silo_id`'s precedence, so "dominant" only matters for a collection whose
    documents predate that precedence being enforced.
    """
    base = (f"SELECT dc.collection_id, d.silo_id, sk.kind, COUNT(*) AS weight "
            f"FROM document_collections dc "
            f"JOIN documents d ON d.id = dc.document_id AND d.{ACTIVE} "
            f"LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id ")
    if ids is None:
        rows = conn.execute(base + "GROUP BY dc.collection_id, d.silo_id").fetchall()
    else:
        rows = []
        for chunk in _chunks(ids):
            ph = ",".join("?" * len(chunk))
            rows += conn.execute(
                base + f"WHERE dc.collection_id IN ({ph}) GROUP BY dc.collection_id, d.silo_id",
                list(chunk),
            ).fetchall()
    return _pick_dominant(rows, "collection_id")


def silo_kinds_of(conn, silo_ids) -> dict[str, str | None]:
    """silo_id -> kind, batch-resolved live via the `silo_kind` view.

    For callers that already know a node's own `silo_id` (a document — one column,
    no aggregation needed) and just need `kind` alongside it. A `None` silo_id (the
    null-silo pool) has no view row and is skipped; callers get a missing key, which
    `.get(...)` turns into `None` — the correct answer for "no silo".
    """
    ids = [s for s in dict.fromkeys(silo_ids) if s is not None]
    out: dict[str, str | None] = {}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT silo_id, kind FROM silo_kind WHERE silo_id IN ({ph})", list(chunk)):
            out[r["silo_id"]] = r["kind"]
    return out


def _domain_neighbours_live(conn, domain_path, *, limit=10) -> list[dict]:
    """The scoped form of the whole-graph co-occurrence edge set.

    `weight` is the same quantity the full edge set reports, so a caller can show one
    domain's neighbours without pulling every edge in the graph. `shared_entities` is
    the more interpretable number (how many entities they have in common) and is
    returned alongside rather than instead of it.
    """
    rows = conn.execute(
        f"""SELECT dd2.domain_path AS path,
                   COUNT(*) AS weight,
                   COUNT(DISTINCT es1.entity_id) AS shared_entities
            FROM document_domains dd1
            JOIN entity_sources es1 ON dd1.document_id = es1.document_id
            JOIN entities e ON e.id = es1.entity_id AND e.{ACTIVE}
            JOIN entity_sources es2 ON es1.entity_id = es2.entity_id
                                   AND es1.document_id != es2.document_id
            JOIN document_domains dd2 ON es2.document_id = dd2.document_id
            WHERE dd1.domain_path = ? AND dd2.domain_path != ?
            GROUP BY dd2.domain_path
            ORDER BY weight DESC, dd2.domain_path
            LIMIT ?""",
        (domain_path, domain_path, limit),
    ).fetchall()
    return [{"path": r["path"], "weight": r["weight"],
             "shared_entities": r["shared_entities"]} for r in rows]
