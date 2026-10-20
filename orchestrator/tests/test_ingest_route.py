import os
import io
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("AWS_ACCESS_KEY", "test-key")
os.environ.setdefault("AWS_SECRET_KEY", "test-secret")

from src.db import init_db
from src.config import Settings
from src.repositories.sqlite_store import SQLiteDataStore
from src.repositories.factory import set_test_store


MOCK_CLASSIFICATION = {
    "primary_domain": "techniques/wet-blending",
    "secondary_domains": [],
    "new_domains": [],
    "confidence": 0.9,
}

MOCK_ENTITIES = [
    {"name": "citadel", "type": "Brand"},
    {"name": "abaddon black", "type": "Paint"},
]


def make_test_store(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    store = SQLiteDataStore(db_path)
    set_test_store(store)
    return store


def make_test_settings(tmp_path):
    return Settings(
        aws_access_key="test-key",
        aws_secret_key="test-secret",
        db_path=str(tmp_path / "test.db"),
        documents_dir=str(tmp_path / "documents"),
    )


@pytest.mark.asyncio
async def test_ingest_stores_document(tmp_path):
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=MOCK_ENTITIES), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        result = await _ingest_document(store, "Test Doc", "Hello world content", None)

    assert "document_id" in result
    assert result["title"] == "Test Doc"
    assert result["content_type"] == "text"

    row = store.conn.execute("SELECT * FROM documents WHERE id = ?", (result["document_id"],)).fetchone()
    assert row is not None
    assert row["title"] == "Test Doc"
    set_test_store(None)
    store.close()


@pytest.mark.asyncio
async def test_ingest_marks_search_index_stale(tmp_path):
    """New docs must become searchable without a manual rebuild (#113 follow-up).

    Ingest embeds the new rows but that only updates the DB; the in-memory FAISS index
    for this workspace must be invalidated so the next search rebuilds. Before the fix the
    per-workspace ready flag stayed set and the new docs were invisible until a correction
    or POST /search/rebuild."""
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)
    from src.pipeline.search import pipeline as sp
    from src.pipeline.search import retrieval
    key = retrieval._ws_key(store.conn)
    sp._indexes_ready.add(key)   # simulate an index already built for this workspace

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=MOCK_ENTITIES), \
         patch("src.pipeline.search.retrieval.embed_new_entities", return_value=0), \
         patch("src.pipeline.search.retrieval.embed_new_chunks", return_value=0), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        await _ingest_document(store, "Doc", "hello world content", None)

    assert key not in sp._indexes_ready, "ingest must invalidate this workspace's search index"
    sp._indexes_ready.discard(key)
    set_test_store(None)
    store.close()


@pytest.mark.asyncio
async def test_ingest_creates_chunks(tmp_path):
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)

    long_content = "word " * 1000

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=[]), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        result = await _ingest_document(store, "Chunky Doc", long_content, None)

    chunks = store.conn.execute(
        "SELECT * FROM chunks WHERE document_id = ?", (result["document_id"],)
    ).fetchall()
    assert len(chunks) > 1
    set_test_store(None)


@pytest.mark.asyncio
async def test_ingest_assigns_classification(tmp_path):
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=[]), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        result = await _ingest_document(store, "Classified Doc", "Some content about painting", None)

    assert "techniques/wet-blending" in result["domains"]
    set_test_store(None)


@pytest.mark.asyncio
async def test_ingest_always_extracts_with_general_spec(tmp_path):
    """Ingest always extracts entities using the built-in general spec."""
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=MOCK_ENTITIES), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        result = await _ingest_document(store, "Extract Doc", "Content with entities", None)

    assert result["entity_count"] == 2

    entities = store.conn.execute("SELECT * FROM entities").fetchall()
    assert len(entities) == 2
    set_test_store(None)


@pytest.mark.asyncio
async def test_ingest_does_not_queue_simmer_general(tmp_path):
    """General spec is built-in, so no simmer_general job should be queued."""
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=MOCK_ENTITIES), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        result = await _ingest_document(store, "Doc", "Content", None)

    # No simmer_general jobs — general spec is always available
    jobs = store.conn.execute("SELECT * FROM jobs WHERE type = 'simmer_general'").fetchall()
    assert len(jobs) == 0
    set_test_store(None)


@pytest.mark.asyncio
async def test_ingest_dedup_by_content_hash(tmp_path):
    """Second ingest with same content returns existing doc."""
    store = make_test_store(tmp_path)
    settings = make_test_settings(tmp_path)

    with patch("src.routes.ingest.get_settings", return_value=settings), \
         patch("src.routes.ingest.classify_document", new_callable=AsyncMock, return_value=MOCK_CLASSIFICATION), \
         patch("src.routes.ingest.extract_document", new_callable=AsyncMock, return_value=MOCK_ENTITIES), \
         patch("src.routes.ingest.Relay"):
        from src.routes.ingest import _ingest_document
        result1 = await _ingest_document(store, "Doc 1", "Same content", None)
        result2 = await _ingest_document(store, "Doc 2", "Same content", None)

    assert result1["document_id"] == result2["document_id"]
    assert result2["entity_count"] == 0  # dedup returns 0

    docs = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert docs == 1
    set_test_store(None)


# --- /ingest error paths ---

def test_ingest_unsupported_extension_returns_415(test_client, tmp_path):
    with patch("src.routes.ingest.get_settings", return_value=make_test_settings(tmp_path)):
        response = test_client.post(
            "/ingest",
            files={"file": ("archive.zip", b"PK\x03\x04fake", "application/zip")},
        )
    assert response.status_code == 415


def test_ingest_file_too_large_returns_413(test_client, tmp_path):
    from src.routes.ingest import _MAX_UPLOAD_BYTES
    big = b"x" * (_MAX_UPLOAD_BYTES + 1)
    with patch("src.routes.ingest.get_settings", return_value=make_test_settings(tmp_path)):
        response = test_client.post(
            "/ingest",
            files={"file": ("big.txt", big, "text/plain")},
        )
    assert response.status_code == 413


def test_ingest_mislabeled_pdf_returns_415(test_client, tmp_path):
    # File has .pdf extension but wrong magic bytes — should be 415 not 422
    with patch("src.routes.ingest.get_settings", return_value=make_test_settings(tmp_path)):
        response = test_client.post(
            "/ingest",
            files={"file": ("sneaky.pdf", b"PK\x03\x04not-a-pdf", "application/pdf")},
        )
    assert response.status_code == 415


def test_ingest_corrupt_pdf_returns_422(test_client, tmp_path):
    # Valid %PDF magic but garbled body — parse failure, not type mismatch
    with patch("src.routes.ingest.get_settings", return_value=make_test_settings(tmp_path)):
        response = test_client.post(
            "/ingest",
            files={"file": ("bad.pdf", b"%PDF-1.4 garbage data \x00\xff", "application/pdf")},
        )
    assert response.status_code == 422


def test_ingest_blank_pdf_returns_422(test_client, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    with patch("src.routes.ingest.get_settings", return_value=make_test_settings(tmp_path)):
        response = test_client.post(
            "/ingest",
            files={"file": ("scan.pdf", buf.getvalue(), "application/pdf")},
        )
    assert response.status_code == 422
    assert "no extractable text" in response.json()["detail"]
