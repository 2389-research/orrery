"""`GET /documents` accepts a TimeScope window and `order` (#116, trimmed).

No time params ⇒ identical to today (newest-first, bare list). With a window, only
documents whose `created_at` falls in it are returned, and the resolved scope is echoed.
"""

import pytest


def _seed_doc(store, doc_id, created_at):
    store.conn.execute(
        "INSERT INTO documents (id, title, status, created_at) VALUES (?, ?, 'complete', ?)",
        (doc_id, f"title-{doc_id}", created_at))
    store.conn.commit()


@pytest.fixture
def seeded(test_store):
    _seed_doc(test_store, "old", "2026-10-01 09:00:00")
    _seed_doc(test_store, "mid", "2026-10-10 09:00:00")
    _seed_doc(test_store, "new", "2026-10-19 09:00:00")
    return test_store


def test_no_params_is_unchanged_bare_list(seeded, test_client):
    body = test_client.get("/documents").json()
    assert isinstance(body, list), "no-params response must stay a bare list (back-compat)"
    assert {d["id"] for d in body} == {"old", "mid", "new"}
    # newest-first, as today
    assert [d["id"] for d in body] == ["new", "mid", "old"]


def test_since_until_window(seeded, test_client):
    body = test_client.get("/documents", params={"since": "2026-10-05", "until": "2026-10-15"}).json()
    assert isinstance(body, dict), "a scoped response carries the echoed scope"
    assert {d["id"] for d in body["documents"]} == {"mid"}
    assert body["scope"] == {"since": "2026-10-05 00:00:00", "until": "2026-10-15 00:00:00"}


def test_order_asc(seeded, test_client):
    body = test_client.get("/documents", params={"since": "2026-09-01", "order": "asc"}).json()
    assert [d["id"] for d in body["documents"]] == ["old", "mid", "new"]


def test_until_is_exclusive(seeded, test_client):
    # 'new' is at 09:00; until at exactly 'new' date midnight excludes it, includes up to mid.
    body = test_client.get("/documents", params={"until": "2026-10-19"}).json()
    assert {d["id"] for d in body["documents"]} == {"old", "mid"}
