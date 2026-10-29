import json

import pytest

from src.db import get_connection, init_db
from src.jobs import generate_commentary as gc
from src.jobs.generate_commentary import (
    _select_missing_batch,
    commentary_enrolled_dbs,
    run_commentary_sweep,
)


def _settings():
    from types import SimpleNamespace
    return SimpleNamespace(db_path="/nonexistent/orrery.db", summarization_model="m")


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
    r1 = await run_commentary_sweep([test_db], relay, _settings(), batch_size=3)
    assert r1["made"] == 3 and r1["workspace"] == test_db
    assert relay.calls == 3

    # Pass 2: the remaining 1 (only_missing skips the 3 already done).
    r2 = await run_commentary_sweep([test_db], relay, _settings(), batch_size=3)
    assert r2["made"] == 1

    # Pass 3: caught up — nothing to do, workspace reported as None.
    r3 = await run_commentary_sweep([test_db], relay, _settings(), batch_size=3)
    assert r3["made"] == 0 and r3["workspace"] is None

    conn = get_connection(test_db)
    assert len(_commentary_ids(conn)) == 4   # every node commented exactly once
    conn.close()


def test_enrolled_dbs_filters_to_opted_in_workspaces(tmp_path):
    base = tmp_path / "orrery.db"
    ws = tmp_path / "workspaces"
    (ws / "aaa").mkdir(parents=True); (ws / "bbb").mkdir(parents=True); (ws / "ccc").mkdir(parents=True)
    db = lambda i: str(ws / i / "orrery.db")
    (ws / "registry.json").write_text(json.dumps([
        {"id": "aaa", "name": "A", "commentary_sweep": True},
        {"id": "bbb", "name": "B"},                          # no flag → not enrolled
        {"id": "ccc", "name": "C", "commentary_sweep": False},
    ]))
    got = commentary_enrolled_dbs([db("aaa"), db("bbb"), db("ccc")], str(base))
    assert got == [db("aaa")]


def test_enrolled_dbs_no_registry_is_noop(tmp_path):
    # No registry at all → nothing enrolled → sweep never fans out on its own.
    assert commentary_enrolled_dbs([str(tmp_path / "workspaces" / "x" / "orrery.db")],
                                   str(tmp_path / "orrery.db")) == []


@pytest.mark.asyncio
async def test_sweep_picks_first_workspace_with_missing_nodes(tmp_path):
    empty_db = str(tmp_path / "empty.db"); init_db(empty_db)
    work_db = str(tmp_path / "work.db"); init_db(work_db)
    conn = get_connection(work_db); _seed(conn, n_collections=1, n_domains=0); conn.close()
    relay = FakeRelay()

    r = await run_commentary_sweep([empty_db, work_db], relay, _settings(), batch_size=5)
    assert r["workspace"] == work_db and r["made"] == 1


class FailingRelay(FakeRelay):
    """Every call fails (a broken summarization route)."""

    async def complete_structured(self, **kwargs):
        self.calls += 1
        raise RuntimeError("remote route down")


@pytest.fixture(autouse=True)
def _clear_cooldown():
    gc._sweep_cooldown.clear()
    yield
    gc._sweep_cooldown.clear()


def _two_seeded(tmp_path):
    broken_db = str(tmp_path / "broken.db"); init_db(broken_db)
    healthy_db = str(tmp_path / "healthy.db"); init_db(healthy_db)
    for p in (broken_db, healthy_db):
        conn = get_connection(p); _seed(conn, n_collections=1, n_domains=0); conn.close()
    return broken_db, healthy_db


@pytest.mark.asyncio
async def test_failing_workspace_costs_one_batch_then_cools_down(tmp_path):
    # The poll loop awaits the sweep, so a failing pass must stay ONE batch (not one per
    # broken workspace) — and the broken workspace must not be re-picked next pass, or it
    # starves every enrolled workspace after it.
    broken_db, healthy_db = _two_seeded(tmp_path)
    bad = FailingRelay()

    r1 = await run_commentary_sweep([broken_db, healthy_db], bad, _settings(), batch_size=5)
    assert r1["workspace"] == broken_db and r1["made"] == 0 and r1["failed"] == 1
    assert bad.calls == 1  # one batch only; healthy_db was not attempted this pass

    good = FakeRelay()
    r2 = await run_commentary_sweep([broken_db, healthy_db], good, _settings(), batch_size=5)
    assert r2["workspace"] == healthy_db and r2["made"] == 1


@pytest.mark.asyncio
async def test_cooled_down_workspace_is_retried_after_cooldown(tmp_path):
    broken_db, _ = _two_seeded(tmp_path)
    await run_commentary_sweep([broken_db], FailingRelay(), _settings(), batch_size=5)
    assert broken_db in gc._sweep_cooldown

    # Lapse the cooldown: the workspace is retried, and a success clears its entry.
    gc._sweep_cooldown[broken_db] = 0.0  # back-date the entry: the cooldown has lapsed
    r = await run_commentary_sweep([broken_db], FakeRelay(), _settings(), batch_size=5)
    assert r["workspace"] == broken_db and r["made"] == 1
    assert broken_db not in gc._sweep_cooldown
