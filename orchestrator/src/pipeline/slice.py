# ABOUTME: A graph slice (facet filter) and its resolver to an in-slice chunk set (#119).
from __future__ import annotations
from dataclasses import dataclass
from .time_scope import resolve_scope, TimeScopeError

# window preset -> relative `since` understood by time_scope (-Nd)
_WINDOW_PRESETS = {"today": "-1d", "2d": "-2d", "1w": "-7d", "2w": "-14d", "1m": "-30d", "all": None}


@dataclass(frozen=True)
class Slice:
    since: str | None = None            # resolved SQLite-UTC string
    until: str | None = None
    domains: tuple[str, ...] | None = None
    collections: tuple[str, ...] | None = None
    silo: str | None = None
    kind: str | None = None


def parse_slice(params: dict) -> "Slice | None":
    """Build a Slice from request/registry params, or None if no facet present.
    `window` is shorthand that maps to `since`. Raises TimeScopeError on bad time input."""
    window = params.get("window")
    since, until, as_of = params.get("since"), params.get("until"), params.get("as_of")
    if window is not None:
        if window not in _WINDOW_PRESETS:
            raise TimeScopeError(
                f"unknown window {window!r}; expected one of {sorted(_WINDOW_PRESETS)}")
        if since or until or as_of:
            raise TimeScopeError(
                "window cannot be combined with since/until/as_of (ambiguous)")
        since = _WINDOW_PRESETS[window]
    scope = resolve_scope(since=since, until=until, as_of=as_of)

    def _tup(name, v):
        if v is None:
            return None
        items = tuple(v) if isinstance(v, (list, tuple)) else (v,)
        if not all(isinstance(i, str) for i in items):
            raise ValueError(f"{name} must be a string or a list of strings")
        return items

    def _scalar(name, v):
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{name} must be a string")
        return v

    s = Slice(
        since=scope["since"], until=scope["until"],
        domains=_tup("domain", params.get("domain") if params.get("domain") is not None else params.get("domains")),
        collections=_tup("collection", params.get("collection") if params.get("collection") is not None else params.get("collections")),
        silo=_scalar("silo", params.get("silo")), kind=_scalar("kind", params.get("kind")),
    )
    return s if any([s.since, s.until, s.domains, s.collections, s.silo, s.kind]) else None


def _slice_facets(slc: "Slice") -> tuple[list[str], list[str], list]:
    """(joins, clauses, params) over `documents d` — the ONE place the facet predicate is
    built, shared by the chunk-level and document-level resolvers.
    `until` doubles as an as-of cutoff (invalid_at > until OR IS NULL), matching #118."""
    clauses, params, joins = [], [], []
    if slc.until:
        clauses.append("(d.invalid_at > ? OR d.invalid_at IS NULL)")
        params.append(slc.until)
    else:
        clauses.append("d.invalid_at IS NULL")
    if slc.since:
        clauses.append("d.created_at >= ?")
        params.append(slc.since)
    if slc.until:
        clauses.append("d.created_at < ?")
        params.append(slc.until)
    if slc.silo:
        if slc.silo == "none":
            clauses.append("d.silo_id IS NULL")
        else:
            clauses.append("d.silo_id = ?")
            params.append(slc.silo)
    if slc.domains:
        joins.append("JOIN document_domains dd ON dd.document_id = d.id")
        clauses.append("(" + " OR ".join("dd.domain_path LIKE ? || '%'" for _ in slc.domains) + ")")
        params.extend(slc.domains)
    if slc.collections:
        ph = ",".join("?" * len(slc.collections))
        joins.append("JOIN document_collections dc ON dc.document_id = d.id")
        clauses.append(f"dc.collection_id IN ({ph})")
        params.extend(slc.collections)
    if slc.kind:
        joins.append("JOIN silo_kind sk ON sk.silo_id = d.silo_id")
        clauses.append("sk.kind = ?")
        params.append(slc.kind)
    return joins, clauses, params


def _slice_chunk_query(slc: "Slice") -> tuple[str, list]:
    """SELECT ch.id FROM chunks JOIN documents ... WHERE <facets AND-combined>."""
    joins, clauses, params = _slice_facets(slc)
    sql = ("SELECT DISTINCT ch.id FROM chunks ch JOIN documents d ON ch.document_id = d.id "
           + " ".join(joins) + " WHERE " + " AND ".join(clauses))
    return sql, params


def _slice_doc_query(slc: "Slice") -> tuple[str, list]:
    """SELECT d.id FROM documents ... WHERE <facets AND-combined>."""
    joins, clauses, params = _slice_facets(slc)
    sql = ("SELECT DISTINCT d.id FROM documents d "
           + " ".join(joins) + " WHERE " + " AND ".join(clauses))
    return sql, params


def resolve_slice_chunks(conn, slc: "Slice") -> set:
    """The set of in-slice chunk ids (chunks whose document matches every facet)."""
    sql, params = _slice_chunk_query(slc)
    return {r[0] for r in conn.execute(sql, params).fetchall()}


def ensure_slice_temp_table(conn, slc: "Slice") -> str:
    """Materialise the in-slice chunk ids into TEMP TABLE `_slice_chunks(chunk_id)` and
    return its name. Later consumers JOIN against it (reusable, dodges the SQL var limit)."""
    conn.execute("DROP TABLE IF EXISTS _slice_chunks")
    conn.execute("CREATE TEMP TABLE _slice_chunks (chunk_id TEXT PRIMARY KEY)")
    sql, params = _slice_chunk_query(slc)
    conn.execute(f"INSERT OR IGNORE INTO _slice_chunks (chunk_id) {sql}", params)
    conn.commit()  # don't leave an open txn pinning a stale WAL read snapshot (TEMP table survives)
    return "_slice_chunks"


def resolve_slice_docs(conn, slc: "Slice") -> set:
    """The set of in-slice document ids (documents matching every facet)."""
    sql, params = _slice_doc_query(slc)
    return {r[0] for r in conn.execute(sql, params).fetchall()}


def ensure_slice_doc_temp_table(conn, slc: "Slice") -> str:
    """Materialise the in-slice document ids into TEMP TABLE `_slice_docs(doc_id)` and
    return its name. PR 1's measures (mention_count, domain sizes, routes) are
    document-level, so the aggregator joins this rather than the chunk table."""
    conn.execute("DROP TABLE IF EXISTS _slice_docs")
    conn.execute("CREATE TEMP TABLE _slice_docs (doc_id TEXT PRIMARY KEY)")
    sql, params = _slice_doc_query(slc)
    conn.execute(f"INSERT OR IGNORE INTO _slice_docs (doc_id) {sql}", params)
    conn.commit()  # same reason as the chunk table: no open txn pinning a stale WAL snapshot
    return "_slice_docs"
