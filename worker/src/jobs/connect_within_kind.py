# ABOUTME: one-time backfill — merge exact-name+type duplicate entities within a provenance kind.
# ABOUTME: uses _merge_entities: logged (normalization_log + merge_map re-attach), not snapshot-reversible.

import json
from collections import defaultdict

from ..db import get_connection, mark_graph_dirty, recompute_cooccurrence
from ..normalizer import _entity_kind_set, _merge_entities


async def run_connect_within_kind(job: dict, db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        groups: dict[tuple, list[str]] = defaultdict(list)
        for r in conn.execute(
            "SELECT id, canonical_name, type FROM entities WHERE invalid_at IS NULL ORDER BY id"
        ).fetchall():
            kinds = _entity_kind_set(conn, r["id"])
            if len(kinds) != 1:   # empty (no sources) or multi-kind: leave alone
                continue
            if None in kinds:     # real silo with unset kind: unknown != unknown, never group
                continue
            groups[(r["canonical_name"], r["type"], next(iter(kinds)))].append(r["id"])

        groups_merged = entities_merged = 0
        survivors: list[str] = []
        for (name, _type, _kind), ids in groups.items():
            if len(ids) < 2:
                continue
            survivor = ids[0]  # lowest id: deterministic
            for loser in ids[1:]:
                _merge_entities(conn, from_id=loser, from_name=name, to_id=survivor,
                                to_name=name, method="kind_backfill", similarity=1.0)
                entities_merged += 1
            groups_merged += 1
            survivors.append(survivor)

        if survivors:
            recompute_cooccurrence(conn, survivors)
        # Always flag dirty: a prior crashed run may have committed merges without the
        # recompute, and a re-run then finds only singletons and would never self-heal.
        mark_graph_dirty(conn)
        result = {"groups_merged": groups_merged, "entities_merged": entities_merged}
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(result), job["id"]))
        conn.commit()
    finally:
        conn.close()
