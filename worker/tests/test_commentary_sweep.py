import pytest

from src.db import get_connection, init_db
from src.jobs.generate_commentary import (
    _select_missing_batch,
    run_commentary_sweep,
)


class FakeRelay:
    """Records calls and returns a valid 3-comment payload for every node."""

    def __init__(self):
        self.calls = 0

    async def complete_structured(self, **kwargs):
        self.calls += 1
        return {"comments": [
            {"kind": "description", "text": "It is a node.", "pose": "reading"},
            {"kind": "omnissiah", "text": "Its preservation serves.", "pose": "galxy"},
            {"kind": "humor", "text": "It exists. Noted.", "pose": "pointing"},
        ]}


def _seed(conn, n_collections=2, n_domains=2):
    for i in range(n_collections):
        conn.execute(
            "INSERT INTO collections (id, name, path, root_path, document_count) VALUES (?,?,?,?,?)",
            (f"c{i}", f"coll{i}", f"coll{i}", f"/x{i}", 5 + i))
    for i in range(n_domains):
        conn.execute(
            "INSERT INTO domains (id, path, parent_path, document_count) VALUES (?,?,?,?)",
            (f"d{i}", f"software/tool{i}", "software", 3 + i))
    conn.commit()


def _commentary_ids(conn):
    return {(r["node_type"], r["node_id"])
            for r in conn.execute("SELECT node_type, node_id FROM node_commentary")}


def test_select_missing_batch_is_bounded_across_types(test_db):
    conn = get_connection(test_db)
    _seed(conn, n_collections=2, n_domains=2)
    # 4 nodes exist; a batch of 3 must return exactly 3.
    assert len(_select_missing_batch(conn, ("domain", "collection"), 3)) == 3
    # batch larger than the backlog returns all of it, not more.
    assert len(_select_missing_batch(conn, ("domain", "collection"), 99)) == 4
    conn.close()


@pytest.mark.asyncio
async def test_sweep_drains_in_bounded_batches_and_is_idempotent(test_db):
    conn = get_connection(test_db)
    _seed(conn, n_collections=2, n_domains=2)   # 4 nodes total, all missing commentary
    conn.close()
    relay = FakeRelay()

    # Pass 1: one bounded batch of 3.
    r1 = await run_commentary_sweep([test_db], relay, "m", batch_size=3)
    assert r1["made"] == 3 and r1["workspace"] == test_db
    assert relay.calls == 3

    # Pass 2: the remaining 1 (only_missing skips the 3 already done).
    r2 = await run_commentary_sweep([test_db], relay, "m", batch_size=3)
    assert r2["made"] == 1

    # Pass 3: caught up — nothing to do, workspace reported as None.
    r3 = await run_commentary_sweep([test_db], relay, "m", batch_size=3)
    assert r3["made"] == 0 and r3["workspace"] is None

    conn = get_connection(test_db)
    assert len(_commentary_ids(conn)) == 4   # every node commented exactly once
    conn.close()


@pytest.mark.asyncio
async def test_sweep_picks_first_workspace_with_missing_nodes(tmp_path):
    empty_db = str(tmp_path / "empty.db"); init_db(empty_db)
    work_db = str(tmp_path / "work.db"); init_db(work_db)
    conn = get_connection(work_db); _seed(conn, n_collections=1, n_domains=0); conn.close()
    relay = FakeRelay()

    r = await run_commentary_sweep([empty_db, work_db], relay, "m", batch_size=5)
    assert r["workspace"] == work_db and r["made"] == 1
