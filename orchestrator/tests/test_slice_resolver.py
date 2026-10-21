# ABOUTME: Slice resolver turns facets into an in-slice chunk set (#119).
import pytest
from src.db import init_db
from src.repositories.sqlite_store import SQLiteDataStore
from src.pipeline.slice import Slice, parse_slice, resolve_slice_chunks, ensure_slice_temp_table


def _seed(store):
    c = store.conn
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d_old','o','2026-10-01 00:00:00')")
    c.execute("INSERT INTO documents (id,title,created_at) VALUES ('d_new','n','2026-10-19 00:00:00')")
    c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d_old','alpha',1,1.0)")
    c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d_new','beta',1,1.0)")
    c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES ('c_old','d_old',0,'x')")
    c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES ('c_new','d_new',0,'y')")
    c.commit()


@pytest.fixture
def store(tmp_path):
    p = str(tmp_path / "t.db")
    init_db(p)
    s = SQLiteDataStore(p)
    yield s
    s.close()


def test_time_facet(store):
    _seed(store)
    assert resolve_slice_chunks(store.conn, Slice(since="2026-10-15 00:00:00")) == {"c_new"}


def test_domain_facet(store):
    _seed(store)
    assert resolve_slice_chunks(store.conn, Slice(domains=("alpha",))) == {"c_old"}


def test_facets_and_combined(store):
    _seed(store)
    assert resolve_slice_chunks(store.conn, Slice(domains=("beta",), since="2026-10-15 00:00:00")) == {"c_new"}
    assert resolve_slice_chunks(store.conn, Slice(domains=("alpha",), since="2026-10-15 00:00:00")) == set()


def test_parse_slice_none_when_empty():
    assert parse_slice({}) is None


def test_parse_slice_domain():
    s = parse_slice({"domain": "alpha"})
    assert s is not None and s.domains == ("alpha",)


def test_parse_window_preset_sets_since():
    s = parse_slice({"window": "1w"})
    assert s is not None and s.since is not None and s.until is None


def test_temp_table_matches_resolved_set(store):
    _seed(store)
    slc = Slice(since="2026-10-15 00:00:00")
    name = ensure_slice_temp_table(store.conn, slc)
    n = store.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    assert n == len(resolve_slice_chunks(store.conn, slc)) == 1


from src.pipeline.time_scope import TimeScopeError


def test_unknown_window_raises():
    with pytest.raises(TimeScopeError):
        parse_slice({"window": "1W"})
    with pytest.raises(TimeScopeError):
        parse_slice({"window": "3d"})


def test_window_with_since_raises():
    with pytest.raises(TimeScopeError):
        parse_slice({"window": "1w", "since": "-3d"})
    with pytest.raises(TimeScopeError):
        parse_slice({"window": "1w", "as_of": "2026-10-10"})


def test_window_all_no_time_bound_keeps_other_facets():
    s = parse_slice({"window": "all", "domain": "alpha"})
    assert s is not None and s.since is None and s.until is None
    assert s.domains == ("alpha",)
    assert parse_slice({"window": "all"}) is None


def test_temp_table_leaves_no_open_txn(store):
    _seed(store)
    ensure_slice_temp_table(store.conn, Slice(since="2026-10-15 00:00:00"))
    assert not store.conn.in_transaction


def _seed_rich(store):
    c = store.conn
    c.execute("INSERT INTO watched_sources (id,type,provenance_kind) VALUES ('s1','vault','human_vault')")
    c.execute("INSERT INTO watched_sources (id,type,provenance_kind) VALUES ('s2','vault','agent_report')")
    for col in ("col1", "col2", "col3"):
        c.execute("INSERT INTO collections (id,name,path) VALUES (?,?,?)", (col, col, col))
    docs = [
        # id, created_at, silo, invalid_at
        ("a", "2026-10-10 00:00:00", "s1", None),
        ("b", "2026-10-12 00:00:00", "s1", None),
        ("c", "2026-10-12 00:00:00", "s2", None),
        ("e", "2026-10-14 00:00:00", "s1", None),   # created == until boundary
        ("f", "2026-10-12 00:00:00", "s1", "2026-10-20 00:00:00"),  # deleted after until
        ("g", "2026-10-12 00:00:00", "s1", "2026-10-13 00:00:00"),  # deleted before until
    ]
    for i, ca, silo, inv in docs:
        c.execute("INSERT INTO documents (id,title,created_at,silo_id,invalid_at) VALUES (?,?,?,?,?)",
                  (i, i, ca, silo, inv))
        c.execute("INSERT INTO chunks (id,document_id,chunk_index,text) VALUES (?,?,0,'t')", ("ch_" + i, i))
    for d in ("alpha/x", "beta/y", "gamma"):
        c.execute("INSERT OR IGNORE INTO domains (id,path) VALUES (?,?)", (d, d))
    dom = {"a": "alpha/x", "b": "alpha/x", "c": "alpha/x", "e": "beta/y", "f": "alpha/x", "g": "alpha/x"}
    for d, p in dom.items():
        c.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES (?,?,1,1.0)", (d, p))
    colm = {"a": "col1", "b": "col1", "c": "col1", "e": "col2", "f": "col1", "g": "col1"}
    for d, k in colm.items():
        c.execute("INSERT INTO document_collections (document_id,collection_id) VALUES (?,?)", (d, k))
    c.commit()


def test_collection_facet(store):
    _seed_rich(store)
    assert resolve_slice_chunks(store.conn, Slice(collections=("col2",))) == {"ch_e"}
    assert resolve_slice_chunks(store.conn, Slice(collections=("col3",))) == set()


def test_kind_facet_uses_silo_kind_view(store):
    _seed_rich(store)
    assert resolve_slice_chunks(store.conn, Slice(kind="agent_report")) == {"ch_c"}
    assert resolve_slice_chunks(store.conn, Slice(kind="human_vault")) == {"ch_a", "ch_b", "ch_e"}


def test_invalid_at_excluded_without_until(store):
    _seed_rich(store)
    got = resolve_slice_chunks(store.conn, Slice(silo="s1"))
    assert "ch_f" not in got and "ch_g" not in got
    assert got == {"ch_a", "ch_b", "ch_e"}


def test_until_exclusive_and_asof_gate(store):
    _seed_rich(store)
    got = resolve_slice_chunks(store.conn, Slice(until="2026-10-14 00:00:00"))
    assert "ch_e" not in got          # created_at == until is excluded
    assert "ch_f" in got              # deleted after until: still live as-of until
    assert "ch_g" not in got          # deleted before until
    assert got == {"ch_a", "ch_b", "ch_c", "ch_f"}


def test_all_facets_combined(store):
    _seed_rich(store)
    slc = Slice(since="2026-10-11 00:00:00", until="2026-10-14 00:00:00",
                domains=("alpha", "beta"), collections=("col1", "col2"),
                silo="s1", kind="human_vault")
    # b (in window, s1, alpha, col1) and f (as-of live); a too old, c wrong silo/kind,
    # e at boundary, g deleted before until
    assert resolve_slice_chunks(store.conn, slc) == {"ch_b", "ch_f"}
