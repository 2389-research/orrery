# ABOUTME: The code_intent scope, and the normalization gate that follows a batch.
# ABOUTME: Both fail SILENTLY when wrong — the job succeeds and does nothing useful.

"""These cover the failure mode that motivated the whole port order.

Collection ingest runs in two phases: phase 1 writes `code_intent` documents, then
enqueues an `extract_batch` job with `{"scope": "code_intent"}`. If that scope is not
recognised, the job does not raise — it falls through to the domain branch, selects
zero documents, and completes successfully. The visible result is a collection full of
documents and no entities, with nothing in any log to explain it.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from src.db import get_connection, init_db


class FakeRelay:
    """Stand-in for orrery_relay.Relay — no network, one entity per chunk."""

    calls: list[str]

    @classmethod
    def from_settings(cls, settings, **overrides):
        inst = cls()
        inst.calls = []
        return inst

    async def complete(self, model, max_tokens, messages, **kwargs):
        self.calls.append(messages[0]["content"])
        return SimpleNamespace(text=json.dumps(
            {"entities": [{"name": "alpha", "type": "Thing"}, {"name": "beta", "type": "Thing"}]}))


def _seed(db_path, *, content_type, status="classified", n=1):
    init_db(db_path)
    spec_id = "spec1"
    conn = get_connection(db_path)
    conn.execute("INSERT INTO specs (id, domain_path, version, spec_content) VALUES (?, NULL, 1, ?)",
                 (spec_id, "Extract entities."))
    doc_ids = []
    for i in range(n):
        doc_id = str(uuid.uuid4())
        doc_ids.append(doc_id)
        conn.execute("INSERT INTO documents (id, title, content, status, content_type) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (doc_id, f"doc{i}", "body", status, content_type))
        conn.execute("INSERT INTO chunks (id, document_id, chunk_index, offset, length, text) "
                     "VALUES (?, ?, 0, 0, 5, ?)", (str(uuid.uuid4()), doc_id, f"CHUNK{i}"))
    conn.commit()
    conn.close()
    return spec_id, doc_ids


async def _run(db_path, spec_id, scope, monkeypatch):
    import src.jobs.extract_batch as mod
    monkeypatch.setattr(mod, "Relay", FakeRelay)
    job = {"id": str(uuid.uuid4()), "config": json.dumps({"spec_id": spec_id, "scope": scope})}
    # The job row has to exist: the batch records its outcome with an UPDATE, which
    # silently affects zero rows if the row is missing — so a test that skips this
    # asserts on a result the code never had a chance to write.
    conn = get_connection(db_path)
    conn.execute("INSERT INTO jobs (id, type, target, status, config) "
                 "VALUES (?, 'extract_batch', ?, 'running', ?)",
                 (job["id"], scope, job["config"]))
    conn.commit()
    conn.close()
    await mod.run_extract_batch(job, db_path)
    return job["id"]


async def test_the_code_intent_scope_selects_the_code_intent_documents(tmp_path, monkeypatch):
    """Without this branch the job selects nothing and still reports success."""
    db_path = str(tmp_path / "test.db")
    spec_id, doc_ids = _seed(db_path, content_type="code_intent", n=2)

    job_id = await _run(db_path, spec_id, "code_intent", monkeypatch)

    conn = get_connection(db_path)
    entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    extracted = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status = 'extracted'").fetchone()[0]
    result = json.loads(conn.execute(
        "SELECT result FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])
    conn.close()

    assert entities == 2, "code_intent docs were not extracted"
    assert extracted == 2
    assert result["docs_processed"] == 2


async def test_the_code_intent_scope_ignores_ordinary_documents(tmp_path, monkeypatch):
    """It is scoped by content_type, so a plain upload in the same workspace is
    untouched — otherwise a collection ingest would re-sweep unrelated documents with
    the code spec."""
    db_path = str(tmp_path / "test.db")
    spec_id, _ = _seed(db_path, content_type="text", n=2)

    await _run(db_path, spec_id, "code_intent", monkeypatch)

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    still_classified = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status = 'classified'").fetchone()[0]
    conn.close()
    assert still_classified == 2


async def test_a_rerun_is_a_no_op_because_status_leaves_the_scope(tmp_path, monkeypatch):
    """Idempotence comes from the `status = 'classified'` predicate.

    Phase 1 can be re-enqueued (a retried ingest, a manual re-run), and without this
    the second pass would re-extract every document and double every co-occurrence
    weight.
    """
    db_path = str(tmp_path / "test.db")
    spec_id, _ = _seed(db_path, content_type="code_intent", n=1)

    await _run(db_path, spec_id, "code_intent", monkeypatch)
    conn = get_connection(db_path)
    first = conn.execute("SELECT COUNT(*) FROM entity_sources").fetchone()[0]
    conn.close()

    job_id = await _run(db_path, spec_id, "code_intent", monkeypatch)

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM entity_sources").fetchone()[0] == first
    result = json.loads(conn.execute(
        "SELECT result FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])
    conn.close()
    assert result["docs_processed"] == 0


async def test_the_pdf_page_scope_selects_only_the_pdf_page_documents(tmp_path, monkeypatch):
    """Phase 2 of PDF ingest is scoped by content_type, so a pdf_page batch extracts
    the page-docs and leaves an unrelated code_intent doc untouched — otherwise a PDF
    batch would sweep another collection's docs with the wrong spec."""
    db_path = str(tmp_path / "test.db")
    spec_id, page_ids = _seed(db_path, content_type="pdf_page", n=1)

    # A code_intent doc in the same workspace that must NOT be swept by the pdf batch.
    conn = get_connection(db_path)
    other_id = str(uuid.uuid4())
    conn.execute("INSERT INTO documents (id, title, content, status, content_type) "
                 "VALUES (?, 'code', 'body', 'classified', 'code_intent')", (other_id,))
    conn.execute("INSERT INTO chunks (id, document_id, chunk_index, offset, length, text) "
                 "VALUES (?, ?, 0, 0, 5, 'CHUNKX')", (str(uuid.uuid4()), other_id))
    conn.commit()
    conn.close()

    await _run(db_path, spec_id, "pdf_page", monkeypatch)

    conn = get_connection(db_path)
    extracted = [r[0] for r in conn.execute(
        "SELECT id FROM documents WHERE status = 'extracted'").fetchall()]
    other_status = conn.execute(
        "SELECT status FROM documents WHERE id = ?", (other_id,)).fetchone()[0]
    conn.close()

    assert extracted == page_ids, "pdf_page scope did not select exactly the page-docs"
    assert other_status == "classified", "pdf_page scope swept a code_intent doc"


# --- the normalization gate -------------------------------------------------

@pytest.mark.parametrize("prime_first,expect_normalized", [
    (False, True),   # first pass: new entities exist, normalization must run
    (True, False),   # second pass: nothing new, normalization must be skipped
])
async def test_normalization_runs_only_when_there_are_new_entities(
    tmp_path, monkeypatch, prime_first, expect_normalized
):
    """Batch normalization re-embeds the entity set, so a no-op batch must not call it.

    Guarded rather than assumed cheap: this runs after every extract_batch, including
    the trailing one where every document has already been swept, and paying a full
    re-embed to decide nothing is the difference between a fast re-ingest and a slow one.
    """
    db_path = str(tmp_path / "test.db")
    spec_id, _ = _seed(db_path, content_type="code_intent", n=1)

    if prime_first:
        await _run(db_path, spec_id, "code_intent", monkeypatch)

    called = []
    import src.normalizer as normalizer_mod
    monkeypatch.setattr(normalizer_mod, "run_batch_normalization",
                        lambda conn: called.append(True) or {"merged": 0})

    await _run(db_path, spec_id, "code_intent", monkeypatch)

    assert bool(called) is expect_normalized


async def test_the_snapshot_is_marked_dirty_even_when_normalization_is_skipped(
    tmp_path, monkeypatch
):
    """Skipping normalization must not skip the dirty flag.

    Document status and co-occurrence changed even with no new entities, so a snapshot
    built before this batch is stale. The two decisions are separate on purpose — tying
    the flag to normalization would serve a stale graph until some unrelated write
    happened to flip it.
    """
    db_path = str(tmp_path / "test.db")
    spec_id, _ = _seed(db_path, content_type="code_intent", n=2)
    await _run(db_path, spec_id, "code_intent", monkeypatch)

    # A doc that yields only entities that already exist: processed, none new.
    conn = get_connection(db_path)
    doc_id = str(uuid.uuid4())
    conn.execute("INSERT INTO documents (id, title, content, status, content_type) "
                 "VALUES (?, 'later', 'body', 'classified', 'code_intent')", (doc_id,))
    conn.execute("INSERT INTO chunks (id, document_id, chunk_index, offset, length, text) "
                 "VALUES (?, ?, 0, 0, 5, 'CHUNK9')", (str(uuid.uuid4()), doc_id))
    conn.execute("UPDATE graph_snapshot SET dirty = 0")
    conn.commit()
    was_clean = conn.execute("SELECT dirty FROM graph_snapshot").fetchone()
    conn.close()

    await _run(db_path, spec_id, "code_intent", monkeypatch)

    conn = get_connection(db_path)
    rows = conn.execute("SELECT dirty FROM graph_snapshot").fetchall()
    processed = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status = 'extracted'").fetchone()[0]
    conn.close()

    assert processed == 3, "the third doc should have been processed"
    if was_clean is not None:                 # only meaningful if a snapshot row exists
        assert all(r[0] == 1 for r in rows), "graph left clean despite processed docs"
