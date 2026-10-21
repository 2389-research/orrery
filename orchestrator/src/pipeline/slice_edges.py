# ABOUTME: Live in-slice entity<->entity co-occurrence edges, recomputed from entity_sources (#119).
"""co_occurs rows in `relationships` carry no chunk provenance (source_chunk is NULL), so
they cannot be read-filtered by a slice; the in-slice edges are recomputed here instead,
mirroring `db.recompute_cooccurrence` restricted to in-slice chunks.

PRECONDITION: the caller has created TEMP TABLE `_slice_chunks` on `conn` via
`pipeline.slice.ensure_slice_temp_table`. `_slice_chunks` membership is the SOLE
document-validity filter (PR 1 convention), so no `documents.invalid_at` gate here.
"""
from __future__ import annotations
import sqlite3

# SQLite's default variable cap is 999; the frontier list is used twice per query.
_CHUNK = 450

_SQL = """
SELECT s1.entity_id AS a, s2.entity_id AS b, COUNT(DISTINCT s1.chunk_id) AS w
FROM entity_sources s1
JOIN _slice_chunks sc ON sc.chunk_id = s1.chunk_id
JOIN entity_sources s2
  ON s1.chunk_id = s2.chunk_id AND s1.entity_id < s2.entity_id
JOIN entities e1 ON e1.id = s1.entity_id AND e1.invalid_at IS NULL
JOIN entities e2 ON e2.id = s2.entity_id AND e2.invalid_at IS NULL
JOIN documents d1 ON d1.id = s1.document_id
WHERE s1.chunk_id IS NOT NULL
  AND COALESCE((SELECT MIN(emits_cooccurrence) FROM document_collections
                WHERE document_id = s1.document_id), 1) = 1
  AND COALESCE((SELECT MIN(emits_cooccurrence) FROM document_collections
                WHERE document_id = s2.document_id), 1) = 1
  AND COALESCE((SELECT kind FROM silo_kind WHERE silo_id = d1.silo_id), '') != 'agent_report'
  AND NOT EXISTS (SELECT 1 FROM relationships r
                  WHERE r.type = 'co_occurs' AND r.invalid_at IS NOT NULL
                    AND ((r.from_entity = s1.entity_id AND r.to_entity = s2.entity_id)
                      OR (r.from_entity = s2.entity_id AND r.to_entity = s1.entity_id)))
  AND (s1.entity_id IN ({ph}) OR s2.entity_id IN ({ph}))
GROUP BY a, b
"""


def in_slice_adjacency(conn, entity_ids: list[str]) -> dict[str, list[tuple[str, int]]]:
    """Symmetric in-slice co-occurrence adjacency for edges touching `entity_ids`.
    Each edge appears under BOTH endpoints; weight = distinct shared in-slice chunks.
    Requires `_slice_chunks` (see module docstring)."""
    ids = list(dict.fromkeys(entity_ids))
    pairs: dict[tuple[str, str], int] = {}
    for i in range(0, len(ids), _CHUNK):
        part = ids[i:i + _CHUNK]
        ph = ",".join("?" * len(part))
        try:
            rows = conn.execute(_SQL.format(ph=ph), part + part).fetchall()
        except sqlite3.OperationalError as e:
            if "_slice_chunks" in str(e):
                raise RuntimeError(
                    "in_slice_adjacency requires _slice_chunks: call ensure_slice_temp_table() first") from e
            raise
        for r in rows:
            pairs[(r[0], r[1])] = r[2]  # idempotent across chunks: same pair, same weight
    adj: dict[str, list[tuple[str, int]]] = {}
    for (a, b), w in pairs.items():
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))
    return adj


def in_slice_cooccurrences(conn, entity_id: str, limit: int | None = None) -> list[tuple[str, int]]:
    """`entity_id`'s in-slice neighbours, heaviest first (ties by id), top `limit`."""
    nbrs = in_slice_adjacency(conn, [entity_id]).get(entity_id, [])
    nbrs.sort(key=lambda t: (-t[1], t[0]))
    return nbrs[:limit] if limit is not None else nbrs
