# ABOUTME: POST /ingest/pdf route — two-phase like /ingest/repo. The route only checks
# ABOUTME: the %PDF magic bytes and enqueues the ingest_pdf worker job (no rasterization),
# ABOUTME: so these tests need only bytes starting with %PDF, not a real multi-page PDF.

_PDF = b"%PDF-1.4 minimal test bytes"


def test_ingest_pdf_creates_collection_and_enqueues(test_client, tmp_path):
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(_PDF)
    r = test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "s"})
    assert r.status_code == 202
    body = r.json(); assert "job_id" in body and "collection_id" in body


def test_ingest_pdf_rejects_non_pdf(test_client, tmp_path):
    f = tmp_path / "x.pdf"; f.write_bytes(b"not a pdf")
    assert test_client.post("/ingest/pdf", json={"path": str(f), "name": "x"}).status_code == 400


def test_ingest_pdf_rejects_missing_file(test_client, tmp_path):
    assert test_client.post("/ingest/pdf", json={"path": str(tmp_path / "nope.pdf"), "name": "z"}).status_code == 400


def test_ingest_pdf_rejects_bad_vision_mode(test_client, tmp_path):
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(_PDF)
    assert test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "s", "vision_mode": "bogus"}).status_code == 422


def test_ingest_pdf_duplicate_name_conflicts(test_client, tmp_path):
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(_PDF)
    test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "dup"})
    assert test_client.post("/ingest/pdf", json={"path": str(pdf), "name": "dup"}).status_code == 409
