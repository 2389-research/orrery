# ABOUTME: Opt-in synthetic-scale benchmark for the on-demand graph slice path (#119 §6).
# ABOUTME: Measures resolver + aggregate time per window; reports, never gates. RUN_BENCH=1 to run.
import os
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.db import init_db
from src.pipeline.graph_v5 import build_graph_v5
from src.pipeline.slice import Slice, resolve_slice_docs
from src.repositories.sqlite_store import SQLiteDataStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BENCH") != "1",
    reason="opt-in benchmark; set RUN_BENCH=1 to run",
)

N_DOCS, N_DAYS, N_ENTITIES = 3000, 30, 400
DOMAINS = [f"{r}/{c}/{t}" for r, c, t in [
    ("software", "backend", "apis"), ("software", "frontend", "ui"),
    ("software", "infra", "deploy"), ("business", "strategy", "planning"),
    ("business", "ops", "process"), ("product", "design", "ux"),
    ("research", "ml", "models"), ("people", "team", "culture")]]


def _build_corpus(db_path: str, now: datetime) -> None:
    rng = random.Random(1234)
    init_db(db_path)
    import sqlite3
    conn = sqlite3.connect(db_path)
    ts = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    for i, p in enumerate(DOMAINS):
        conn.execute("INSERT INTO domains (id, path, parent_path, document_count) VALUES (?,?,?,?)",
                     (f"dom{i}", p, p.rsplit("/", 1)[0], N_DOCS // len(DOMAINS)))
    conn.executemany("INSERT INTO entities (id, canonical_name, type) VALUES (?,?,?)",
                     [(f"e{i}", f"Entity {i}", rng.choice(["concept", "tool", "person"]))
                      for i in range(N_ENTITIES)])
    # Zipf-ish popularity so some entities are hubs and many are rare.
    weights = [1.0 / (i + 1) ** 0.8 for i in range(N_ENTITIES)]
    ent_ids = [f"e{i}" for i in range(N_ENTITIES)]
    docs, dds, chunks, srcs = [], [], [], []
    for d in range(N_DOCS):
        created = now - timedelta(seconds=rng.uniform(0, N_DAYS * 86400))
        did = f"doc{d}"
        docs.append((did, f"Transcript {d}", "x", ts(created)))
        dom = rng.choice(DOMAINS)
        dds.append((did, dom, 1, 0.9))
        # a topical cluster of entities per doc so there is real co-occurrence
        topic = rng.sample(ent_ids, 3) + rng.choices(ent_ids, weights, k=4)
        for c in range(rng.randint(2, 5)):
            cid = f"{did}c{c}"
            chunks.append((cid, did, c, c * 1000, 1000, "chunk text"))
            for e in set(rng.sample(topic, rng.randint(2, 5))):
                srcs.append((e, did, cid, "bench"))
    conn.executemany("INSERT INTO documents (id, title, content, created_at, status) "
                     "VALUES (?,?,?,?, 'complete')", [(a, b, c, t) for a, b, c, t in docs])
    conn.executemany("INSERT INTO document_domains VALUES (?,?,?,?)", dds)
    conn.executemany("INSERT INTO chunks (id, document_id, chunk_index, offset, length, text) "
                     "VALUES (?,?,?,?,?,?)", chunks)
    conn.executemany("INSERT INTO entity_sources (entity_id, document_id, chunk_id, extraction_pass) "
                     "VALUES (?,?,?,?)", srcs)
    conn.commit()
    conn.close()
    print(f"\ncorpus: {len(docs)} docs, {len(chunks)} chunks, {len(srcs)} entity_sources, "
          f"{N_ENTITIES} entities, {len(DOMAINS)} domains, {N_DAYS} days")


def test_slice_scaling_benchmark(tmp_path):
    db_path = str(tmp_path / "bench.db")
    now = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    _build_corpus(db_path, now)
    print(f"generation: {time.perf_counter() - t0:.1f}s")
    store = SQLiteDataStore(db_path)
    try:
        fmt = lambda dt: (dt).strftime("%Y-%m-%d %H:%M:%S")
        windows = {"day": 1, "week": 7, "month": 30}
        # warm: layout is computed lazily/cached on first build; exclude that from the timings
        build_graph_v5(store, slice=Slice(since=fmt(now - timedelta(days=1))))
        rows, node_counts = [], {}
        for name, days in windows.items():
            slc = Slice(since=fmt(now - timedelta(days=days)))
            t = time.perf_counter()
            docs = resolve_slice_docs(store.conn, slc)
            resolve_ms = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            payload = build_graph_v5(store, slice=slc)
            agg_ms = (time.perf_counter() - t) * 1000
            assert "node_index" in payload and "edges" in payload
            node_counts[name] = len(payload["node_index"])
            rows.append((name, len(docs), resolve_ms, agg_ms, node_counts[name], len(payload["edges"])))
        print(f"{'window':<7}{'docs':>6}{'resolve_ms':>12}{'aggregate_ms':>14}{'nodes':>8}{'edges':>8}")
        for n, d, r, a, nn, ne in rows:
            print(f"{n:<7}{d:>6}{r:>12.1f}{a:>14.1f}{nn:>8}{ne:>8}")
        assert node_counts["month"] >= node_counts["week"]
    finally:
        store.close()
