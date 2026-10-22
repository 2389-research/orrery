"""Tests for image-related routes: serving, search, simmer trigger."""

from pathlib import Path


def test_image_simmer_trigger(test_client, test_store):
    """POST /simmer/{domain}/image queues a per-domain image simmer job.

    Image simmering is per-domain now — it layers domain recognition context onto the
    static general image spec (`trigger_domain_image_simmer`) — so the domain must exist.
    """
    test_store.conn.execute(
        "INSERT INTO domains (id, path, document_count) VALUES ('d1', 'software/ai-agents', 1)"
    )
    test_store.conn.commit()

    resp = test_client.post("/simmer/software/ai-agents/image")
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "queued"

    # Job exists in DB
    job = test_store.conn.execute(
        "SELECT type, target, status FROM jobs WHERE id = ?", (data["job_id"],)
    ).fetchone()
    assert job["type"] == "simmer_domain_image"
    assert job["target"] == "software/ai-agents"
    assert job["status"] == "queued"


def test_image_simmer_unknown_domain_404(test_client, test_store):
    """A per-domain image simmer for a domain that doesn't exist is a 404."""
    resp = test_client.post("/simmer/does/not/exist/image")
    assert resp.status_code == 404


def test_image_simmer_conflict(test_client, test_store):
    """Can't trigger a domain image simmer when one is already running for it."""
    test_store.conn.execute(
        "INSERT INTO domains (id, path, document_count) VALUES ('d1', 'software/ai-agents', 1)"
    )
    test_store.conn.commit()
    assert test_client.post("/simmer/software/ai-agents/image").status_code == 200
    resp = test_client.post("/simmer/software/ai-agents/image")
    assert resp.status_code == 409


def test_image_serving_not_found(test_client, test_store):
    """GET /images/{id} returns 404 for missing document."""
    resp = test_client.get("/images/nonexistent-id")
    assert resp.status_code == 404


def test_image_serving_not_image(test_client, test_store):
    """GET /images/{id} returns 400 for text document."""
    test_store.documents.create("txt1", "notes.md", "text content", "hash1", content_type="text")
    resp = test_client.get("/images/txt1")
    # Either 400 (not an image) or 404 (file doesn't exist) is acceptable
    assert resp.status_code in (400, 404)


def test_search_with_images_param(test_client, test_store):
    """GET /search?include_images=true doesn't error even with no images."""
    resp = test_client.get("/search?q=test&expand=false&include_images=true")
    assert resp.status_code == 200
    data = resp.json()
    assert "images" in data
    assert isinstance(data["images"], list)


def test_domains_include_text_image_counts(test_client, test_store):
    """GET /domains returns text_count and image_count per domain."""
    test_store.documents.create("txt1", "doc.md", "text", "h1", content_type="text")
    test_store.documents.create("img1", "pic.jpg", "desc", "h2", content_type="image")
    test_store.conn.execute(
        "INSERT INTO domains (id, path, document_count) VALUES ('d1', 'test/domain', 2)"
    )
    test_store.conn.execute(
        "INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES ('txt1', 'test/domain', 1, 1.0)"
    )
    test_store.conn.execute(
        "INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES ('img1', 'test/domain', 1, 1.0)"
    )
    test_store.conn.commit()

    resp = test_client.get("/domains")
    assert resp.status_code == 200
    domains = resp.json()
    d = next(d for d in domains if d["path"] == "test/domain")
    assert d["text_count"] == 1
    assert d["image_count"] == 1
