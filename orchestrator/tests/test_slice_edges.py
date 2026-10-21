# ABOUTME: Tests for the in-slice co-occurrence adjacency engine (graph slices #119, PR 2).
import pytest

from src.pipeline.slice import Slice, ensure_slice_temp_table
from src.pipeline.slice_edges import in_slice_adjacency, in_slice_cooccurrences

NEW, OLD = "2026-10-01 00:00:00", "2026-01-01 00:00:00"


def _doc(conn, doc_id, created, silo_id=None):
    conn.execute("INSERT INTO documents (id, title, created_at, silo_id) VALUES (?,?,?,?)",
                 (doc_id, doc_id, created, silo_id))


def _chunk(conn, chunk_id, doc_id, entities):
    conn.execute("INSERT INTO chunks (id, document_id) VALUES (?,?)", (chunk_id, doc_id))
    for e in entities:
        conn.execute("INSERT INTO entity_sources (entity_id, document_id, chunk_id) VALUES (?,?,?)",
                     (e, doc_id, chunk_id))


@pytest.fixture
def conn(test_store):
    c = test_store.conn
    for e in "ABC":
        c.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?,?,?)", (e, e, "t"))
    _doc(c, "d_in", NEW); _chunk(c, "ch_in1", "d_in", ["A", "B"])
    _doc(c, "d_in2", NEW); _chunk(c, "ch_in2", "d_in2", ["B", "C"])
    _doc(c, "d_out", OLD); _chunk(c, "ch_out", "d_out", ["A", "C"])
    c.commit()
    return c


def _build(conn):
    ensure_slice_temp_table(conn, Slice(since="2026-06-01 00:00:00"))
    return conn


def _edges(adj):
    return {(k, n): w for k, v in adj.items() for n, w in v}


def test_adjacency_only_in_slice_edges(conn):
    adj = in_slice_adjacency(_build(conn), ["A", "B", "C"])
    e = _edges(adj)
    assert e[("A", "B")] == 1 and e[("B", "C")] == 1
    assert ("A", "C") not in e and ("C", "A") not in e


def test_symmetric(conn):
    adj = in_slice_adjacency(_build(conn), ["A", "B", "C"])
    assert {n for n, _ in adj["B"]} == {"A", "C"}
    assert [n for n, _ in adj["A"]] == ["B"]
    assert [n for n, _ in adj["C"]] == ["B"]


def test_frontier_only_and_chunking(conn):
    adj = in_slice_adjacency(_build(conn), ["A"])
    assert _edges(adj) == {("A", "B"): 1, ("B", "A"): 1}
    # many ids exceed the variable limit; must still work
    ids = ["A"] + [f"x{i}" for i in range(2000)]
    assert ("A", "B") in _edges(in_slice_adjacency(conn, ids))


def test_weight_counts_distinct_chunks(conn):
    _chunk(conn, "ch_in3", "d_in", ["A", "B"]); conn.commit()
    e = _edges(in_slice_adjacency(_build(conn), ["A"]))
    assert e[("A", "B")] == 2


def test_human_invalidated_pair_excluded(conn):
    conn.execute("INSERT INTO relationships (id, from_entity, to_entity, type, weight, invalid_at) "
                 "VALUES ('r1','B','A','co_occurs',1,'2026-10-02')")
    conn.commit()
    e = _edges(in_slice_adjacency(_build(conn), ["A", "B", "C"]))
    assert ("A", "B") not in e and ("B", "A") not in e
    assert e[("B", "C")] == 1


def test_emits_cooccurrence_gate(conn):
    conn.execute("INSERT INTO collections (id) VALUES ('c1')")
    conn.execute("INSERT INTO document_collections (document_id, collection_id, emits_cooccurrence) "
                 "VALUES ('d_in','c1',0)")
    conn.commit()
    e = _edges(in_slice_adjacency(_build(conn), ["A", "B", "C"]))
    assert ("A", "B") not in e and e[("B", "C")] == 1


def test_agent_report_silo_excluded(conn):
    conn.execute("INSERT INTO watched_sources (id, type, provenance_kind) VALUES ('w1','vault','agent_report')")
    conn.execute("UPDATE documents SET silo_id='w1' WHERE id='d_in'")
    conn.commit()
    e = _edges(in_slice_adjacency(_build(conn), ["A", "B", "C"]))
    assert ("A", "B") not in e and e[("B", "C")] == 1


def test_cooccurrences_sorted_limited(conn):
    _chunk(conn, "ch_in3", "d_in2", ["B", "C"]); conn.commit()  # B-C weight 2, A-B weight 1
    _build(conn)
    assert in_slice_cooccurrences(conn, "B") == [("C", 2), ("A", 1)]
    assert in_slice_cooccurrences(conn, "B", limit=1) == [("C", 2)]


def test_missing_slice_table_raises_clear_error(conn):
    conn.execute("DROP TABLE IF EXISTS _slice_chunks")
    with pytest.raises(RuntimeError, match="_slice_chunks"):
        in_slice_adjacency(conn, ["A"])
