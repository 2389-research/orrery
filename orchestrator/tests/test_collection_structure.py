"""GET /collections/{collection_id}/structure — collection -> modules -> files(+entities) for the
collection drill-in viz (`frontend/public/viz/collection.html`).

Key behaviours under test:
- Only the top `max_files` files by entity count are returned as render nodes
  (the main-graph top-N-by-degree strategy), while `total_files` still reports
  the full count. A 7k-file repo must not ship every file.
- Connected collections come from the (cheap) collection_edges table + the precomputed graph
  snapshot's collection routes — not a per-request entity_sources self-join.
"""


def _seed_repo_with_files(store, collection_id, n_files, module="pkg"):
    """Create `collection_id` with one module and `n_files` files; file k (1..n) is
    given k distinct entity mentions so its entity_count == k (deterministic rank)."""
    c = store.conn
    store.collections.create(collection_id, collection_id, collection_id, f"/collections/{collection_id}")
    # Module-level doc.
    c.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (f"{collection_id}-m", module))
    store.collections.link_document(f"{collection_id}-m", collection_id, role="group")
    for k in range(1, n_files + 1):
        fid = f"{collection_id}-f{k}"
        c.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (fid, f"file{k}"))
        store.collections.link_document(fid, collection_id, role="leaf", parent_path=module)
        for j in range(k):  # k entity mentions → entity_count == k
            eid = f"{collection_id}-e{k}-{j}"
            c.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, 'Concept')",
                      (eid, f"ent-{k}-{j}"))
            c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)", (eid, fid))
    c.commit()


def test_structure_caps_files_to_top_n_by_entity_count(test_client, test_store):
    _seed_repo_with_files(test_store, "r1", n_files=5)

    body = test_client.get("/collections/r1/structure?max_files=3").json()

    assert body["total_files"] == 5
    assert body["rendered_files"] == 3

    files = [f for m in body["modules"] for f in m["files"]]
    assert len(files) == 3
    # The three most entity-dense files (5, 4, 3) survive; the sparse ones drop.
    assert sorted((f["entity_count"] for f in files), reverse=True) == [5, 4, 3]
    titles = {f["title"] for f in files}
    assert titles == {"file5", "file4", "file3"}


def test_structure_returns_all_files_when_under_cap(test_client, test_store):
    _seed_repo_with_files(test_store, "r1", n_files=4)

    body = test_client.get("/collections/r1/structure?max_files=400").json()

    assert body["total_files"] == 4
    assert body["rendered_files"] == 4
    files = [f for m in body["modules"] for f in m["files"]]
    assert len(files) == 4
    # Module entity_count is the sum over its rendered files (1+2+3+4).
    mod = next(m for m in body["modules"] if m["path"] == "pkg")
    assert mod["entity_count"] == 10
    # The module doc id is exposed so the viz can open its summary in the doc reader.
    assert mod["id"] == "r1-m"


def test_structure_connected_repos_from_manifest_edges(test_client, test_store):
    _seed_repo_with_files(test_store, "r1", n_files=2)
    _seed_repo_with_files(test_store, "r2", n_files=2)
    # Manifest-import edge r1 -> r2 (collection_edges is the cheap, precomputed table).
    # `type` is explicit because it is NOT NULL and part of the key in this schema —
    # a manifest dependency IS a typed relation, so there is nothing to infer.
    test_store.conn.execute(
        "INSERT INTO collection_edges (source, target, type) VALUES ('r1', 'r2', 'uses')")
    test_store.conn.commit()

    body = test_client.get("/collections/r1/structure").json()
    conn = {c["id"]: c for c in body["connected_collections"]}
    assert "r2" in conn
    assert "import" in conn["r2"]["via"]


def test_structure_connected_repos_from_shared_entities(test_client, test_store):
    """The other half of `connected_repos`, which had no coverage — and so silently
    broke when the payload went v5.

    These links are read off the graph snapshot. In v4 they were a top-level
    `repo_routes` key; in v5 they are typed entries in the single `edges` collection.
    The route kept reading the old key, which just yielded [] — so the endpoint still
    answered, still 200'd, and quietly returned only `import` links. The v5 golden
    can't catch that: this endpoint is not part of the migrated contract.
    """
    _seed_repo_with_files(test_store, "r1", n_files=2)
    _seed_repo_with_files(test_store, "r2", n_files=2)
    # One entity mentioned in a doc of EACH repo — no manifest edge between them, so
    # `shared_entity` is the only thing that can connect them here.
    c = test_store.conn
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('shared', 'shared', 'Concept')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('shared', 'r1-f1')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('shared', 'r2-f1')")
    c.commit()

    from src.pipeline.graph_snapshot import rebuild_snapshot
    rebuild_snapshot(test_store)

    body = test_client.get("/collections/r1/structure").json()
    conn = {r["id"]: r for r in body["connected_collections"]}
    assert "r2" in conn, "a repo sharing an entity must show up as connected"
    assert "shared_entity" in conn["r2"]["via"]
    assert "import" not in conn["r2"]["via"], "no manifest edge was seeded"


def test_root_bucket_is_the_collections_own_document(test_client, test_store):
    """Files at the checkout root belong to the collection's OWN document.

    codesum gives root-level files `parent_path = '.'` (its REPO_PATH sentinel), and
    group buckets are keyed by module title, which '.' never matches — so those files
    fall into a "(root)" bucket. That bucket used to be synthesized as
    `{"id": None, "summary": ""}` while the role='root' document sat unused in the very
    same query result. Unlike every real group, the viz could not open it in the doc
    reader and showed no summary.
    """
    c = test_store.conn
    test_store.collections.create("r1", "r1", "r1", "/collections/r1")
    # The collection's own summary document.
    c.execute("INSERT INTO documents (id, title, content) VALUES ('r1-root', 'r1', 'what r1 is')")
    test_store.collections.link_document("r1-root", "r1", role="root")
    # A file at the checkout root — parent_path is the '.' sentinel, matching no group.
    c.execute("INSERT INTO documents (id, title) VALUES ('r1-f', 'README.md')")
    test_store.collections.link_document("r1-f", "r1", role="leaf", parent_path=".")
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e', 'ent', 'Concept')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('e', 'r1-f')")
    c.commit()

    body = test_client.get("/collections/r1/structure").json()
    root = next(m for m in body["modules"] if m["path"] == "(root)")
    assert [f["title"] for f in root["files"]] == ["README.md"]
    assert root["id"] == "r1-root", "the root bucket must be openable in the doc reader"
    assert root["summary"] == "what r1 is"


def test_structure_topn_tiebreak_is_deterministic(test_client, test_store):
    # All files tie on entity count → the cap must keep the SAME files every call
    # (deterministic tie-break by doc id), not an arbitrary subset.
    c = test_store.conn
    test_store.collections.create("r1", "r1", "r1", "/collections/r1")
    c.execute("INSERT INTO documents (id, title) VALUES ('r1-m', 'pkg')")
    test_store.collections.link_document("r1-m", "r1", role="group")
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e', 'ent', 'Concept')")
    for k in range(6):
        fid = f"r1-f{k}"
        c.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (fid, f"file{k}"))
        test_store.collections.link_document(fid, "r1", role="leaf", parent_path="pkg")
        c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES ('e', ?)", (fid,))
    c.commit()

    def kept():
        body = test_client.get("/collections/r1/structure?max_files=3").json()
        return sorted(f["id"] for m in body["modules"] for f in m["files"])

    first = kept()
    assert len(first) == 3
    assert first == kept()                      # stable across calls
    assert first == ["r1-f0", "r1-f1", "r1-f2"]  # ties broken by ascending doc id


def test_structure_excludes_invalidated_docs(test_client, test_store):
    # Two leaf docs; soft-delete one. The resolver's invalid_at gate must keep it out
    # of both the file count and the rendered modules after the slice-resolver fold.
    _seed_repo_with_files(test_store, "r1", n_files=2)
    test_store.conn.execute(
        "UPDATE documents SET invalid_at = '2020-01-01 00:00:00' WHERE id = ?", ("r1-f1",))
    test_store.conn.commit()

    body = test_client.get("/collections/r1/structure").json()

    assert body["total_files"] == 1
    file_ids = {f["id"] for m in body["modules"] for f in m["files"]}
    assert "r1-f1" not in file_ids
    assert "r1-f2" in file_ids


def test_structure_404_for_unknown_repo(test_client):
    assert test_client.get("/collections/nope/structure").status_code == 404
