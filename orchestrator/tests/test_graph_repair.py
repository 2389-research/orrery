import sqlite3
import pytest
from src.db import init_db
from src.pipeline.graph_repair import propose_correction, get_pending_issues, ALLOWED_ACTIONS


def _seed(test_db):
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1', 'panopticon', 'Product')")
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e2', 'websim', 'Product')")
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e3', 'web sim', 'Product')")
    conn.commit()
    return conn


def test_propose_invalidate_inserts_pending_row(test_db):
    conn = _seed(test_db)
    res = propose_correction(conn, action="invalidate", entity="panopticon",
                             rationale="metaphor, not a real product", proposer="agent-x")
    assert res["status"] == "pending"
    row = conn.execute("SELECT * FROM graph_issues WHERE id = ?", (res["issue_id"],)).fetchone()
    assert row["action"] == "invalidate"
    assert row["target_entity_id"] == "e1"
    assert row["target_entity_name"] == "panopticon"
    assert row["proposer"] == "agent-x"


def test_propose_merge_requires_and_resolves_second_entity(test_db):
    conn = _seed(test_db)
    res = propose_correction(conn, action="merge", entity="web sim", target_b="websim",
                             rationale="spacing variant")
    row = conn.execute("SELECT * FROM graph_issues WHERE id = ?", (res["issue_id"],)).fetchone()
    assert row["target_entity_id"] == "e3"
    assert row["target_b_entity_id"] == "e2"


def test_propose_merge_self_raises(test_db):
    conn = _seed(test_db)
    # entity and target_b resolve to the same id (case-insensitive) -> reject.
    with pytest.raises(ValueError, match="itself"):
        propose_correction(conn, action="merge", entity="websim", target_b="WebSim")


def test_propose_retype_records_proposed_type(test_db):
    conn = _seed(test_db)
    res = propose_correction(conn, action="retype", entity="panopticon", proposed_type="Concept")
    row = conn.execute("SELECT proposed_type FROM graph_issues WHERE id = ?", (res["issue_id"],)).fetchone()
    assert row["proposed_type"] == "Concept"


def test_propose_rename_records_proposed_name(test_db):
    conn = _seed(test_db)
    res = propose_correction(conn, action="rename", entity="websim", proposed_name="WebSim")
    row = conn.execute("SELECT proposed_name FROM graph_issues WHERE id = ?", (res["issue_id"],)).fetchone()
    assert row["proposed_name"] == "WebSim"


def test_propose_unknown_entity_raises(test_db):
    conn = _seed(test_db)
    with pytest.raises(ValueError, match="not found"):
        propose_correction(conn, action="invalidate", entity="does-not-exist")


def test_propose_bad_action_raises(test_db):
    conn = _seed(test_db)
    with pytest.raises(ValueError, match="action"):
        propose_correction(conn, action="frobnicate", entity="panopticon")


def test_propose_merge_without_target_b_raises(test_db):
    conn = _seed(test_db)
    with pytest.raises(ValueError, match="target_b"):
        propose_correction(conn, action="merge", entity="web sim")


def test_propose_retype_without_type_raises(test_db):
    conn = _seed(test_db)
    with pytest.raises(ValueError, match="proposed_type"):
        propose_correction(conn, action="retype", entity="panopticon")


def test_propose_rename_without_name_raises(test_db):
    conn = _seed(test_db)
    with pytest.raises(ValueError, match="proposed_name"):
        propose_correction(conn, action="rename", entity="websim")


def test_get_pending_issues_returns_only_pending(test_db):
    conn = _seed(test_db)
    r = propose_correction(conn, action="invalidate", entity="panopticon")
    conn.execute("UPDATE graph_issues SET status='rejected' WHERE id=?", (r["issue_id"],))
    propose_correction(conn, action="invalidate", entity="websim")
    conn.commit()
    pending = get_pending_issues(conn)
    assert len(pending) == 1
    assert pending[0]["target_entity_name"] == "websim"


def _seed_entity_with_edge(conn):
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1','panopticon','Product')")
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e2','ebay','Organization')")
    conn.execute("INSERT INTO relationships (id, from_entity, to_entity, type, weight) VALUES ('r1','e1','e2','co_occurs',3)")
    conn.commit()


def test_apply_invalidation_round_trips(test_db):
    import sqlite3
    from src.pipeline.graph_repair import apply_invalidation, rollback_invalidation
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    r = apply_invalidation(conn, "e1", reason="metaphor", actor="human")
    assert r["edges_invalidated"] == 1
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0] is not None
    assert conn.execute("SELECT invalid_at FROM relationships WHERE id='r1'").fetchone()[0] is not None
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e2'").fetchone()[0] is None  # neighbor untouched
    back = rollback_invalidation(conn, "e1")
    assert back["edges_restored"] == 1
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0] is None
    assert conn.execute("SELECT invalid_at FROM relationships WHERE id='r1'").fetchone()[0] is None
    # rollback is audited
    assert conn.execute(
        "SELECT COUNT(*) FROM normalization_log WHERE action='rollback_invalidate'"
    ).fetchone()[0] == 1


def test_rollback_restores_only_own_edges(test_db):
    """A shared edge invalidated by a PRIOR independent invalidation must not be resurrected
    when a later invalidate+rollback touches the same neighbor."""
    import sqlite3
    from src.pipeline.graph_repair import apply_invalidation, rollback_invalidation
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)  # e1-e2 via r1
    # Prior, independent invalidation of shared neighbor e2 → invalidates r1.
    apply_invalidation(conn, "e2", reason="prior")
    assert conn.execute("SELECT invalid_at FROM relationships WHERE id='r1'").fetchone()[0] is not None
    # Now invalidate e1: r1 is already invalid, so e1's apply does NOT own it.
    r = apply_invalidation(conn, "e1", reason="metaphor")
    assert r["edges_invalidated"] == 0
    back = rollback_invalidation(conn, "e1")
    assert back["edges_restored"] == r["edges_invalidated"]  # 0, matches
    # e1 restored, but r1 + e2 stay invalid (owned by e2's invalidation, not resurrected).
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0] is None
    assert conn.execute("SELECT invalid_at FROM relationships WHERE id='r1'").fetchone()[0] is not None
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e2'").fetchone()[0] is not None


def test_apply_commit_false_defers(test_db):
    """commit=False must not persist: a rollback (simulated crash) before the atomic
    commit leaves the graph and log untouched — the substrate resolve_correction relies on."""
    import sqlite3
    from src.pipeline.graph_repair import apply_invalidation
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    apply_invalidation(conn, "e1", reason="x", commit=False)
    conn.rollback()
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0] is None
    assert conn.execute("SELECT invalid_at FROM relationships WHERE id='r1'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM normalization_log").fetchone()[0] == 0


def test_resolve_approve_retype_is_atomic(test_db):
    """resolve commits the apply + status together, exactly once (no double-apply on the log)."""
    import sqlite3
    from src.pipeline.graph_repair import propose_correction, resolve_correction
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    iid = propose_correction(conn, action="retype", entity="panopticon", proposed_type="Concept")["issue_id"]
    resolve_correction(conn, iid, "approve", reviewer="human")
    assert conn.execute("SELECT status FROM graph_issues WHERE id=?", (iid,)).fetchone()[0] == "accepted"
    assert conn.execute("SELECT type FROM entities WHERE id='e1'").fetchone()[0] == "Concept"
    assert conn.execute("SELECT COUNT(*) FROM normalization_log WHERE action='retype'").fetchone()[0] == 1


def test_the_index_is_marked_stale_only_after_the_write_is_durable(test_db, monkeypatch):
    """The ORDERING, which is the whole point and which no end-state assertion covers.

    Every other staleness test checks the flag afterwards, and the flag ends up False
    whether the mark runs before or after the commit — so moving it back above
    `conn.commit()` would keep them all green while reintroducing the bug: a rebuild
    triggered by a premature mark reads pre-commit rows and then publishes itself ready,
    leaving a stale index believed fresh.

    Asserts from a SECOND connection, because that is what a rebuild actually is — a
    different connection, which under WAL sees only committed data. If the mark fires
    first, the correction is not visible there yet and this fails.
    """
    from src.db import get_connection
    from src.pipeline import graph_repair

    # get_connection, not sqlite3.connect: it applies WAL and busy_timeout, and this
    # test turns on exactly those semantics — a SECOND connection seeing the committed
    # row is what a rebuild racing the mark would see. Raw connections would test a
    # journal mode the running system never uses.
    conn = get_connection(test_db)
    _seed_entity_with_edge(conn)

    seen = {}

    def _probe(conn):   # signature mirrors _mark_search_index_stale(conn) (#113)
        other = get_connection(test_db)
        try:
            seen["invalid_at"] = other.execute(
                "SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0]
        finally:
            other.close()

    monkeypatch.setattr(graph_repair, "_mark_search_index_stale", _probe)
    graph_repair.apply_invalidation(conn, "e1", reason="ordering")

    assert seen, "the mark must actually run"
    assert seen["invalid_at"] is not None, (
        "the index was marked stale before the soft delete was committed — a rebuild "
        "racing that mark would read the entity as still active and publish a stale "
        "index as ready")
    conn.close()


def _resolve_and_report_index_flag(test_db, action, **kwargs):
    """Approve a correction through the PRODUCTION path and report the index flag after.

    `resolve_correction` runs every apply_* with `commit=False` and owns the single
    commit itself, so the apply_* functions cannot mark the index — they would be marking
    it against writes that have not landed yet, letting a concurrent rebuild publish
    pre-correction rows as fresh. The owner of the transaction has to mark it after the
    commit, and these are the only tests covering that arrangement: every other staleness
    test calls apply_* directly with the default `commit=True`.
    """
    from src.db import get_connection
    from src.pipeline.graph_repair import propose_correction, resolve_correction
    from src.pipeline.search import pipeline as search_pipeline
    from src.pipeline.search import retrieval

    conn = get_connection(test_db)
    key = retrieval._ws_key(conn)
    try:
        _seed_entity_with_edge(conn)
        iid = propose_correction(conn, action=action, entity="panopticon", **kwargs)["issue_id"]
        search_pipeline._indexes_ready.add(key)   # simulate a built index for this workspace
        resolve_correction(conn, iid, "approve", reviewer="human")
        return key in search_pipeline._indexes_ready   # False == marked stale
    finally:
        search_pipeline._indexes_ready.discard(key)   # module state; don't leak to later tests
        conn.close()


def test_resolving_a_retype_marks_the_search_index_stale(test_db):
    """`retype` never marked the index at all, and `type` is carried on every indexed
    entity — one half of the enumeration bug that unconditional marking removes."""
    assert _resolve_and_report_index_flag(test_db, "retype", proposed_type="Concept") is False


def test_resolving_a_rename_marks_the_search_index_stale(test_db):
    """The other half, and the worse one: `canonical_name` is the text the entity index
    actually embeds, so a rename leaves every vector pointing at the old wording."""
    assert _resolve_and_report_index_flag(
        test_db, "rename", proposed_name="panopticon-renamed") is False


def test_apply_retype_and_rename_log(test_db):
    import sqlite3
    from src.pipeline.graph_repair import apply_retype, apply_rename
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    apply_retype(conn, "e1", "Concept", actor="human", reason="x")
    assert conn.execute("SELECT type FROM entities WHERE id='e1'").fetchone()[0] == "Concept"
    apply_rename(conn, "e2", "eBay", actor="human", reason="x")
    assert conn.execute("SELECT canonical_name FROM entities WHERE id='e2'").fetchone()[0] == "eBay"
    logs = conn.execute("SELECT action, before_value, after_value FROM normalization_log ORDER BY action").fetchall()
    assert ("rename","ebay","eBay") in [(a,b,c) for a,b,c in logs]
    assert ("retype","Product","Concept") in [(a,b,c) for a,b,c in logs]


def test_resolve_reject_sets_status_only(test_db):
    import sqlite3
    from src.pipeline.graph_repair import propose_correction, resolve_correction
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    iid = propose_correction(conn, action="invalidate", entity="panopticon")["issue_id"]
    resolve_correction(conn, iid, "reject", reviewer="human")
    row = conn.execute("SELECT status, reviewer FROM graph_issues WHERE id=?", (iid,)).fetchone()
    assert row[0] == "rejected" and row[1] == "human"
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0] is None  # no mutation


def test_resolve_approve_invalidate_mutates(test_db):
    import sqlite3
    from src.pipeline.graph_repair import propose_correction, resolve_correction
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    iid = propose_correction(conn, action="invalidate", entity="panopticon", rationale="metaphor")["issue_id"]
    resolve_correction(conn, iid, "approve", reviewer="human")
    assert conn.execute("SELECT status FROM graph_issues WHERE id=?", (iid,)).fetchone()[0] == "accepted"
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='e1'").fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM normalization_log WHERE action='invalidate'").fetchone()[0] == 1


def test_resolve_approve_merge_applies(test_db):
    import sqlite3
    from src.pipeline.graph_repair import propose_correction, resolve_correction
    conn = sqlite3.connect(test_db)
    _seed_merge_fixture(conn)
    iid = propose_correction(conn, action="merge", entity="web sim", target_b="websim", rationale="dupe")["issue_id"]
    res = resolve_correction(conn, iid, "approve", reviewer="human")
    assert res["applied"] is True
    # survivor = websim (2 sources) beats web sim (tie → target_b) — loser web sim soft-deleted
    assert conn.execute("SELECT invalid_at FROM entities WHERE canonical_name='web sim'").fetchone()[0] is not None
    assert conn.execute("SELECT status FROM graph_issues WHERE id=?", (iid,)).fetchone()[0] == "accepted"


def _seed_merge_fixture(conn):
    # survivor s (2 sources), loser l (1 source), neighbor x. l and s BOTH co-occur with x in chunk c1
    # (shared chunk → must be counted ONCE for the merged s–x edge, not double).
    conn.executescript("""
      INSERT INTO entities (id,canonical_name,type) VALUES
        ('s','websim','Product'),('l','web sim','Product'),('x','harper reed','Person');
      INSERT INTO documents (id,title,status) VALUES ('d','D','extracted');
      INSERT INTO chunks (id,document_id,chunk_index,text) VALUES
        ('c1','d',0,'websim web sim harper reed'),('c2','d',1,'websim only'),('c3','d',2,'web sim harper reed');
      INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES
        ('s','d','c1'),('s','d','c2'),('l','d','c1'),('l','d','c3'),
        ('x','d','c1'),('x','d','c3');
      INSERT INTO relationships (id,from_entity,to_entity,type,weight,source_chunk) VALUES
        ('e_sx','s','x','co_occurs',1,'c1'),   -- s–x from c1
        ('e_lx','l','x','co_occurs',1,'c3');    -- l–x from c3
    """)
    conn.commit()


def test_apply_merge_collapses_and_recomputes_weight(test_db):
    import sqlite3
    from src.pipeline.graph_repair import apply_merge
    conn = sqlite3.connect(test_db)
    _seed_merge_fixture(conn)
    apply_merge(conn, loser_id="l", survivor_id="s", actor="human", reason="spacing")
    # loser soft-deleted, not hard-deleted
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='l'").fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE id='l'").fetchone()[0] == 1
    # loser's sources moved to survivor
    assert conn.execute("SELECT COUNT(*) FROM entity_sources WHERE entity_id='l'").fetchone()[0] == 0
    # exactly ONE active s–x edge, weight = distinct chunks where (s or l) co-occurs with x = {c1,c3} = 2
    rows = conn.execute("SELECT weight, source_chunk FROM relationships WHERE type='co_occurs' AND invalid_at IS NULL "
                        "AND ((from_entity='s' AND to_entity='x') OR (from_entity='x' AND to_entity='s'))").fetchall()
    assert len(rows) == 1 and rows[0][0] == 2
    # uniform projection: the recomputed corrections-path edge carries source_chunk NULL
    assert rows[0][1] is None
    # no active edge references the loser
    assert conn.execute("SELECT COUNT(*) FROM relationships WHERE (from_entity='l' OR to_entity='l') AND invalid_at IS NULL").fetchone()[0] == 0
    # merge_map alias set
    assert conn.execute("SELECT to_entity_id FROM merge_map WHERE from_name='web sim'").fetchone()[0] == 's'


def test_rollback_merge_restores_exactly(test_db):
    import sqlite3
    from src.pipeline.graph_repair import apply_merge, rollback_merge
    conn = sqlite3.connect(test_db)
    _seed_merge_fixture(conn)
    before_sources = conn.execute("SELECT entity_id,chunk_id FROM entity_sources ORDER BY 1,2").fetchall()
    before_edges = conn.execute("SELECT id,from_entity,to_entity,weight FROM relationships ORDER BY id").fetchall()
    apply_merge(conn, loser_id="l", survivor_id="s")
    rollback_merge(conn, loser_id="l")
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='l'").fetchone()[0] is None
    assert conn.execute("SELECT entity_id,chunk_id FROM entity_sources ORDER BY 1,2").fetchall() == before_sources
    assert conn.execute("SELECT id,from_entity,to_entity,weight FROM relationships ORDER BY id").fetchall() == before_edges
    assert conn.execute("SELECT COUNT(*) FROM merge_map WHERE from_name='web sim'").fetchone()[0] == 0


def test_rollback_merge_same_name_losers_uses_logged_survivor(test_db):
    """Two same-named losers ('web sim') merged into different survivors. Rolling back the FIRST
    must restore its own survivor's edges via the logged survivor id — not a merge_map name lookup
    (which the second merge's alias would have clobbered) → wrong survivor."""
    import sqlite3
    from src.pipeline.graph_repair import apply_merge, rollback_merge
    conn = sqlite3.connect(test_db)
    conn.executescript("""
      INSERT INTO entities (id,canonical_name,type) VALUES
        ('s1','survivor one','Product'),('l1','web sim','Product'),('n1','neigh one','Person'),
        ('s2','survivor two','Product'),('l2','web sim','Concept'),('n2','neigh two','Person');
      INSERT INTO documents (id,title,status) VALUES ('d','D','extracted');
      INSERT INTO chunks (id,document_id,chunk_index,text) VALUES
        ('k1','d',0,'a'),('k2','d',1,'b'),('k3','d',2,'c'),('k4','d',3,'d');
      INSERT INTO entity_sources (entity_id,document_id,chunk_id) VALUES
        ('s1','d','k1'),('l1','d','k1'),('n1','d','k1'),
        ('s2','d','k3'),('l2','d','k3'),('n2','d','k3');
      INSERT INTO relationships (id,from_entity,to_entity,type,weight,source_chunk) VALUES
        ('r_s1n1','s1','n1','co_occurs',1,'k1'),('r_l1n1','l1','n1','co_occurs',1,'k1'),
        ('r_s2n2','s2','n2','co_occurs',1,'k3'),('r_l2n2','l2','n2','co_occurs',1,'k3');
    """)
    conn.commit()
    apply_merge(conn, loser_id="l1", survivor_id="s1")   # merge_map['web sim'] -> s1
    apply_merge(conn, loser_id="l2", survivor_id="s2")   # INSERT OR REPLACE clobbers -> s2
    s2_edges_before = conn.execute(
        "SELECT id,from_entity,to_entity,weight FROM relationships "
        "WHERE from_entity='s2' OR to_entity='s2' ORDER BY id").fetchall()
    rollback_merge(conn, loser_id="l1")
    # l1 restored, and its OWN survivor s1's pre-merge edges are back (r_s1n1 + r_l1n1)
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='l1'").fetchone()[0] is None
    restored = conn.execute("SELECT id FROM relationships WHERE id IN ('r_s1n1','r_l1n1')").fetchall()
    assert len(restored) == 2
    # s2's merge is untouched by rolling back l1
    assert conn.execute("SELECT invalid_at FROM entities WHERE id='l2'").fetchone()[0] is not None
    assert conn.execute(
        "SELECT id,from_entity,to_entity,weight FROM relationships "
        "WHERE from_entity='s2' OR to_entity='s2' ORDER BY id").fetchall() == s2_edges_before


def test_merge_preserves_and_restores_prior_alias(test_db):
    """The loser name may already alias to another entity (from ingest normalization). apply_merge
    re-points it to the survivor; rollback_merge restores the ORIGINAL alias target, not delete."""
    import sqlite3
    from src.pipeline.graph_repair import apply_merge, rollback_merge
    conn = sqlite3.connect(test_db)
    _seed_merge_fixture(conn)
    conn.execute("INSERT INTO merge_map (from_name, to_entity_id) VALUES ('web sim', 'x')")  # prior alias
    conn.commit()
    apply_merge(conn, loser_id="l", survivor_id="s")
    assert conn.execute("SELECT to_entity_id FROM merge_map WHERE from_name='web sim'").fetchone()[0] == 's'
    rollback_merge(conn, loser_id="l")
    # prior alias restored exactly (not deleted)
    assert conn.execute("SELECT to_entity_id FROM merge_map WHERE from_name='web sim'").fetchone()[0] == 'x'


def test_apply_merge_double_apply_raises(test_db):
    """A second apply on an already soft-deleted loser must refuse (bad snapshot otherwise)."""
    import sqlite3
    import pytest
    from src.pipeline.graph_repair import apply_merge
    conn = sqlite3.connect(test_db)
    _seed_merge_fixture(conn)
    apply_merge(conn, loser_id="l", survivor_id="s")
    with pytest.raises(ValueError, match="already merged"):
        apply_merge(conn, loser_id="l", survivor_id="s")


def test_apply_invalidation_refuses_already_invalid(test_db):
    """A second invalidate on an already soft-deleted node must refuse (mirrors apply_merge)."""
    import sqlite3
    import pytest
    from src.pipeline.graph_repair import apply_invalidation
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    apply_invalidation(conn, "e1", reason="metaphor")
    with pytest.raises(ValueError, match="already invalidated"):
        apply_invalidation(conn, "e1", reason="again")


def test_apply_merge_refuses_invalid_survivor(test_db):
    """Cannot merge a loser into a soft-deleted survivor."""
    import sqlite3
    import pytest
    from src.pipeline.graph_repair import apply_invalidation, apply_merge
    conn = sqlite3.connect(test_db)
    _seed_merge_fixture(conn)
    apply_invalidation(conn, "s", reason="gone")  # survivor soft-deleted
    with pytest.raises(ValueError, match="survivor already invalidated"):
        apply_merge(conn, loser_id="l", survivor_id="s")


def test_resolve_unknown_action_raises_and_leaves_pending(test_db):
    """A graph_issues row with an unrecognized action must be rejected on approve,
    not silently accepted-without-apply."""
    import sqlite3
    import uuid
    import pytest
    from src.pipeline.graph_repair import resolve_correction
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO graph_issues (id, action, target_entity_id, target_entity_name, status) "
        "VALUES (?, 'frobnicate', 'e1', 'panopticon', 'pending')", (iid,))
    conn.commit()
    with pytest.raises(ValueError, match="unknown action"):
        resolve_correction(conn, iid, "approve", reviewer="human")
    # the bad row stays pending (not accepted)
    assert conn.execute("SELECT status FROM graph_issues WHERE id=?", (iid,)).fetchone()[0] == "pending"


def test_resolve_entity_by_name_skips_invalidated(test_db):
    """The by-name resolution path must exclude soft-deleted entities so proposing/resolving
    doesn't target an already-invalidated node."""
    import sqlite3
    import pytest
    from src.pipeline.graph_repair import apply_invalidation, propose_correction
    conn = sqlite3.connect(test_db)
    _seed_entity_with_edge(conn)
    apply_invalidation(conn, "e1", reason="metaphor")  # 'panopticon' now invalid
    with pytest.raises(ValueError, match="not found"):
        propose_correction(conn, action="retype", entity="panopticon", proposed_type="Concept")


def test_apply_schema_columns_exist(test_db):
    import sqlite3
    conn = sqlite3.connect(test_db)
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(relationships)").fetchall()}
    lcols = {r[1] for r in conn.execute("PRAGMA table_info(normalization_log)").fetchall()}
    conn.close()
    assert {"invalid_at", "invalid_reason", "updated_at"} <= ecols
    assert {"invalid_at", "invalid_reason"} <= rcols
    assert {"action", "before_value", "after_value", "actor", "reason",
            "model_verdict", "model_confidence", "reviewer"} <= lcols


def test_graph_issues_table_exists(test_db):
    import sqlite3
    conn = sqlite3.connect(test_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(graph_issues)").fetchall()}
    conn.close()
    assert {
        "id", "action", "target_entity_id", "target_entity_name",
        "target_b_entity_id", "target_b_name", "proposed_type", "proposed_name",
        "rationale", "proposer", "status", "judge_verdict", "judge_confidence",
        "judge_rationale", "reviewer", "created_at", "resolved_at",
    } <= cols
