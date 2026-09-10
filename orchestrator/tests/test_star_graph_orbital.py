# ABOUTME: The star page must colour domains identically to the galaxy, and carry the
# ABOUTME: per-doc domain + entity-count the orbital layout needs. These guard both.

from src.pipeline.graph_snapshot import domain_palette, assign_domain_colors


def _seed_domains(conn, paths_with_counts):
    for path, n in paths_with_counts:
        conn.execute(
            "INSERT INTO domains (id, path, parent_path, document_count) VALUES (?,?,?,?)",
            (path, path, "/".join(path.split("/")[:-1]) or None, n))
    conn.commit()


def test_domain_palette_matches_min_doc_count_1_set(test_store):
    c = test_store.conn
    _seed_domains(c, [("software/ai-agents", 5), ("software/testing-qa", 3),
                      ("software/zero-doc", 0)])
    pal = domain_palette(c)
    # Built over the min_doc_count>=1 set, exactly as the galaxy does.
    expected = assign_domain_colors([{"path": "software/ai-agents"},
                                     {"path": "software/testing-qa"}])
    assert pal["software/ai-agents"] == expected["software/ai-agents"]
    assert pal["software/testing-qa"] == expected["software/testing-qa"]
    # The zero-doc domain is excluded, so it does not shift the others' hues.
    assert "software/zero-doc" not in pal


def _seed_entity_with_docs(conn):
    # entity e1 in two docs; d1 has 3 entities total (share 1/3), d2 has 10 (share 1/10)
    _seed_domains(conn, [("software/ai-agents", 2), ("software/testing-qa", 1)])
    conn.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e1','openai','integration')")
    for i in range(1, 3):
        conn.execute("INSERT INTO documents (id,title,content,content_hash) VALUES (?,?,?,?)",
                     (f"d{i}", f"doc {i}", "x", f"h{i}"))
    conn.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d1','software/ai-agents',1,0.9)")
    conn.execute("INSERT INTO document_domains (document_id,domain_path,is_primary,confidence) VALUES ('d2','software/testing-qa',1,0.9)")
    # d1: e1 + 2 others = 3 entities ; d2: e1 + 9 others = 10 entities
    others = [f"o{i}" for i in range(11)]
    for oid in others:
        conn.execute("INSERT INTO entities (id,canonical_name,type) VALUES (?,?,'concept')", (oid, oid))
    conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES ('e1','d1')")
    conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES ('e1','d2')")
    for oid in others[:2]:
        conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES (?, 'd1')", (oid,))
    for oid in others[2:11]:
        conn.execute("INSERT INTO entity_sources (entity_id,document_id) VALUES (?, 'd2')", (oid,))
    conn.commit()


def test_documents_carry_domain_path_and_active_entity_count(test_store):
    _seed_entity_with_docs(test_store.conn)
    g = test_store.relationships.get_star_graph("e1")
    by_id = {d["id"]: d for d in g["documents"]}
    assert by_id["d1"]["domain_path"] == "software/ai-agents"
    assert by_id["d1"]["n_entities"] == 3      # e1 + 2 others
    assert by_id["d2"]["domain_path"] == "software/testing-qa"
    assert by_id["d2"]["n_entities"] == 10


def test_n_entities_counts_only_active(test_store):
    from src.pipeline.graph_repair import apply_invalidation
    _seed_entity_with_docs(test_store.conn)
    apply_invalidation(test_store.conn, "o0", reason="test")   # o0 is in d1
    g = test_store.relationships.get_star_graph("e1")
    by_id = {d["id"]: d for d in g["documents"]}
    assert by_id["d1"]["n_entities"] == 2      # e1 + o1 ; o0 invalidated


def test_palette_present_and_agrees_with_galaxy(test_store):
    _seed_entity_with_docs(test_store.conn)
    g = test_store.relationships.get_star_graph("e1")
    oracle = domain_palette(test_store.conn)
    for leaf in ("software/ai-agents", "software/testing-qa"):
        assert g["palette"][leaf] == oracle[leaf]      # star == galaxy, not re-derived


def test_co_limit_defaults_to_150(test_store):
    import inspect
    sig = inspect.signature(test_store.relationships.get_star_graph)
    assert sig.parameters["co_limit"].default == 150
