# ABOUTME: Tests for the horton-orrery pull-API reader — fake fetch, no network.
# ABOUTME: The chunk fixture is shaped after the real horton-orrery /v1/chunks record.

from src.jobs.horton_reader import chunk_to_doc, drain_chunks, get_chunk, list_chunks_page


def sample_chunk():
    return {
        "id": "horton:chunk/AA:BB:CC:DD:EE:01/2026-10-26T19:00:00Z",
        "revision": 2,
        "content_hash": "sha256:b9ce16ea",
        "body": "[14:05:00] synthetic two\n[14:07:00] synthetic late",
        "day": "2026-10-26",
        "folder": "horton/AA:BB:CC:DD:EE:01/2026-10-26",
        "complete": True,
        "gaps": [],
        "line_count": 2,
        "lines": [{"ts": "2026-10-26T19:05:00Z", "text": "synthetic two", "date_confidence": "exact"}],
        "mic": {"id": "AA:BB:CC:DD:EE:01", "name": "desk2", "mixed_mics": False,
                "names_in_window": ["desk", "desk2"]},
        "window": {"start": "2026-10-26T19:00:00Z", "end": "2026-10-26T19:15:00Z", "size": "15m"},
        "source": "mqtt",
    }


def test_list_chunks_page_returns_envelope_fields():
    seen = {}

    def fake(path, params=None):
        seen["path"], seen["params"] = path, params
        return {"items": [{"id": "a"}], "next_cursor": "c1", "has_more": True}

    items, cur, more = list_chunks_page(fake, "c0", limit=5)
    assert seen["path"] == "/v1/chunks" and seen["params"] == {"after": "c0", "limit": 5}
    assert [i["id"] for i in items] == ["a"] and cur == "c1" and more is True


def test_drain_chunks_accumulates_until_has_more_false():
    seq = iter([{"items": [{"id": "a"}], "next_cursor": "c1", "has_more": True},
                {"items": [{"id": "b"}], "next_cursor": "c2", "has_more": False}])

    def fake(path, params=None):
        return next(seq)

    items, cur, more = drain_chunks(fake, None)
    assert [i["id"] for i in items] == ["a", "b"]
    assert cur == "c2"
    assert more is False


def test_drain_chunks_spin_guard_stops_on_nonadvancing_cursor():
    def fake(path, params=None):
        return {"items": [], "next_cursor": params and params.get("after"), "has_more": True}

    items, cur, more = drain_chunks(fake, "x")
    assert more is True


def test_get_chunk_percent_encodes_the_id():
    seen = {}

    def fake(path, params=None):
        seen["path"] = path
        return {"id": "ok"}

    get_chunk(fake, "horton:chunk/AA:BB/2026-10-26T19:00:00Z")
    assert "%3A" in seen["path"] and "%2F" in seen["path"]
    assert seen["path"].startswith("/v1/chunks/") and "/" not in seen["path"][len("/v1/chunks/"):]


def test_chunk_to_doc_maps_body_and_event_time():
    c = sample_chunk()
    doc = chunk_to_doc(c)
    assert set(doc) == {"source_path", "title", "content", "parent_path", "metadata", "mic_id", "day"}
    assert doc["source_path"] == c["id"]
    assert doc["content"] == c["body"]
    assert doc["parent_path"] == c["folder"]
    assert doc["mic_id"] == "AA:BB:CC:DD:EE:01" and doc["day"] == "2026-10-26"
    assert doc["title"] == "desk2 · 14:05"
    m = doc["metadata"]
    assert m["window_start"] == c["window"]["start"] and m["window_end"] == c["window"]["end"]
    assert m["revision"] == 2 and m["content_hash"] == c["content_hash"]
    assert m["mic"] == {"id": c["mic"]["id"], "name": "desk2",
                        "names_in_window": ["desk", "desk2"], "mixed_mics": False}


def test_chunk_to_doc_title_falls_back_to_mic_id_when_no_name():
    c = {**sample_chunk(), "mic": {"id": "AA:BB", "mixed_mics": False}}
    assert chunk_to_doc(c)["title"].startswith("AA:BB")


def test_chunk_to_doc_title_time_falls_back_to_window_start():
    c = {**sample_chunk(), "body": "no prefix here"}
    assert chunk_to_doc(c)["title"].endswith("19:00")


def test_chunk_to_doc_skips_empty_body():
    assert chunk_to_doc({**sample_chunk(), "body": "   "}) is None
