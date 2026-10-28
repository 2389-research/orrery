import json

H = {"X-Workspace-Id": "default"}


def _seed(store):
    rows = [
        ("s3", "d1", "micB", "2026-10-02T09:00:00", "2026-10-02T09:10:00"),
        ("s1", "d1", "micA", "2026-10-01T09:00:00", "2026-10-01T09:10:00"),
        ("s2", "d1", "micA", "2026-10-01T15:00:00", "2026-10-01T15:10:00"),
    ]
    for sid, doc, mic, st, en in rows:
        store.conn.execute(
            'INSERT INTO stretches (stretch_id, doc_id, mic_id, silo_id, start, "end", summary,'
            " concepts, ideas, nouns) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, doc, mic, "silo1", st, en, f"sum {sid}",
             json.dumps([{"tag": "t", "statement": "x"}]),
             json.dumps([{"kind": "k", "text": "y"}]), json.dumps(["n"])))
    store.conn.commit()


def test_window_filter_order_and_parsed_json(test_client, test_store):
    _seed(test_store)
    r = test_client.get("/stretches", params={"since": "2026-10-01T00:00:00",
                                              "until": "2026-10-01T23:59:59"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert [s["stretch_id"] for s in body] == ["s1", "s2"]
    assert body[0]["concepts"] == [{"tag": "t", "statement": "x"}]
    assert isinstance(body[0]["ideas"], list) and body[0]["nouns"] == ["n"]
    assert set(body[0]) == {"stretch_id", "doc_id", "mic_id", "start", "end", "summary",
                            "concepts", "ideas", "nouns"}


def test_unfiltered_ordered_by_start(test_client, test_store):
    _seed(test_store)
    ids = [s["stretch_id"] for s in test_client.get("/stretches", headers=H).json()]
    assert ids == ["s1", "s2", "s3"]


def test_overlap_semantics(test_client, test_store):
    _seed(test_store)
    # window starting mid-stretch s1 still includes it (end >= since)
    r = test_client.get("/stretches", params={"since": "2026-10-01T09:05:00",
                                              "until": "2026-10-01T10:00:00"}, headers=H)
    assert [s["stretch_id"] for s in r.json()] == ["s1"]


def test_mic_and_collection_filter(test_client, test_store):
    _seed(test_store)
    assert [s["stretch_id"] for s in test_client.get("/stretches", params={"mic": "micB"}, headers=H).json()] == ["s3"]
    assert len(test_client.get("/stretches", params={"collection": "silo1"}, headers=H).json()) == 3
    assert test_client.get("/stretches", params={"collection": "nope"}, headers=H).json() == []
