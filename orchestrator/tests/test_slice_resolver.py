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
