# ABOUTME: Tests for the shared session featurizer (summary prompts, write_doc, domain helpers)
# ABOUTME: extracted from ingest_ccvault so ingest_meridian reuses the same code.

import json

from src.db import get_connection
from src.jobs.session_featurizer import parent_of, write_doc


def test_write_doc_persists_metadata_json(test_db):
    conn = get_connection(test_db)
    did = write_doc(conn, "c1", "body", "session_intent", "group", None, "classified",
                    "T", "d/x", {"cwd": "/r"})
    row = conn.execute("SELECT metadata FROM documents WHERE id=?", (did,)).fetchone()
    assert json.loads(row["metadata"])["cwd"] == "/r"
    conn.close()


def test_write_doc_metadata_null_when_absent(test_db):
    conn = get_connection(test_db)
    did = write_doc(conn, "c1", "body", "session_intent", "group", None, "classified", "T", "d/x")
    row = conn.execute("SELECT metadata FROM documents WHERE id=?", (did,)).fetchone()
    assert row["metadata"] is None
    conn.close()


def test_parent_of():
    assert parent_of("a/b/c") == "a/b"
    assert parent_of("a") is None
    assert parent_of(None) is None
