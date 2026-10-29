import json

H = {"X-Workspace-Id": "default"}


def _seed_doc(store, doc_id="d1", invalid_at=None):
    store.conn.execute("INSERT OR IGNORE INTO documents (id, title, invalid_at) VALUES (?,?,?)",
                       (doc_id, doc_id, invalid_at))
    store.conn.commit()


def _seed(store):
    _seed_doc(store, "d1")
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


def test_offset_bearing_bounds_are_read_as_office_local(test_client, test_store):
    _seed(test_store)
    for since, until in (("2026-10-01T00:00:00Z", "2026-10-01T23:59:59+00:00"),
                         ("2026-10-01T00:00:00-0700", "2026-10-01T23:59:59.5Z")):
        r = test_client.get("/stretches", params={"since": since, "until": until}, headers=H)
        assert r.status_code == 200
        assert [s["stretch_id"] for s in r.json()] == ["s1", "s2"]


def test_mic_and_collection_filter(test_client, test_store):
    _seed(test_store)
    assert [s["stretch_id"] for s in test_client.get("/stretches", params={"mic": "micB"}, headers=H).json()] == ["s3"]
    assert len(test_client.get("/stretches", params={"collection": "silo1"}, headers=H).json()) == 3
    assert test_client.get("/stretches", params={"collection": "nope"}, headers=H).json() == []


def test_limit_and_offset_are_clamped(test_client, test_store):
    _seed(test_store)
    r = test_client.get("/stretches", params={"limit": 0, "offset": -5}, headers=H)
    assert r.status_code == 200 and len(r.json()) == 1  # limit floored to 1, offset to 0
    r = test_client.get("/stretches", params={"limit": 10**9}, headers=H)
    assert r.status_code == 200 and len(r.json()) == 3


def test_excludes_stretch_of_invalid_doc(test_client, test_store):
    _seed(test_store)
    test_store.conn.execute("UPDATE documents SET invalid_at = '2026-10-03 00:00:00' WHERE id='d1'")
    test_store.conn.commit()
    assert test_client.get("/stretches", headers=H).json() == []


def test_excludes_stretch_with_missing_doc(test_client, test_store):
    _seed(test_store)
    test_store.conn.execute("DELETE FROM documents WHERE id='d1'")
    test_store.conn.commit()
    assert test_client.get("/stretches", headers=H).json() == []


def test_document_delete_removes_stretches(test_client, test_store):
    _seed(test_store)
    _seed_doc(test_store, "d2")
    test_store.conn.execute(
        'INSERT INTO stretches (stretch_id, doc_id, start, "end") VALUES ("x1","d2","2026-10-01","2026-10-01")')
    test_store.conn.commit()
    test_store.documents.delete("d1")
    left = [r[0] for r in test_store.conn.execute("SELECT stretch_id FROM stretches").fetchall()]
    assert left == ["x1"]


def test_virtual_noosphere_slice_limits_stretches(test_client, test_store, tmp_path, monkeypatch):
    from src.routes import workspace_routes
    monkeypatch.setattr(workspace_routes, "_registry_path",
                        lambda create=True: str(tmp_path / "registry.json"))
    _seed(test_store)  # d1: s1,s2,s3 (created now -> in a 1w window)
    _seed_doc(test_store, "d_old")
    test_store.conn.execute("UPDATE documents SET created_at='2020-01-01 00:00:00' WHERE id='d_old'")
    test_store.conn.execute(
        'INSERT INTO stretches (stretch_id, doc_id, start, "end") VALUES ("old","d_old","2026-10-01","2026-10-01")')
    test_store.conn.commit()
    assert len(test_client.get("/stretches", headers=H).json()) == 4
    vid = test_client.post("/views", json={"name": "wk", "parent": "default",
                                           "slice": {"window": "1w"}}).json()["id"]
    r = test_client.get("/stretches", headers={"X-Workspace-Id": vid}).json()
    assert set(r) == {"stretches", "scope"}
    assert sorted(s["stretch_id"] for s in r["stretches"]) == ["s1", "s2", "s3"]


def test_strip_userinfo():
    from src.routes.graph import _strip_userinfo
    assert _strip_userinfo("https://user:tok@github.com/o/r.git") == "https://github.com/o/r.git"
    assert _strip_userinfo("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert _strip_userinfo("git@github.com:o/r.git") == "git@github.com:o/r.git"
    assert _strip_userinfo(None) is None
