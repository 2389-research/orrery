# ABOUTME: scripts/carve_noosphere.py must produce a noosphere that stands on its own —
# ABOUTME: right magnitudes, resolvable provenance, no dangling edges, and idempotent.

"""The carve had no coverage, and six defects lived in it at once.

Every one of them was invisible to a test that only checked "did rows arrive":
`merge_map` was queried on a column that does not exist, `entity_sources` re-inserted
on every run, `document_count` came over at the PARENT's magnitude (3557 stored for a
domain holding 2 carved docs), the silo rows provenance_kind resolves through were
left behind, `collection_edges` was dropped, and an empty selection still ran the
destructive writes. So these tests assert the *invariants of a standalone noosphere*
rather than row counts, and `test_carving_twice_equals_carving_once` is the one that
catches the whole class of double-apply bugs.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SILO_WEB = "f39e1b88-4a3f-410a-8c39-46adf2b2627c"      # must match WEBSITE_SILOS


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def carve_mod():
    return _load("carve_noosphere", "scripts/carve_noosphere.py")


def _schema(path):
    from src.db import init_db
    init_db(str(path))


def _parent(path):
    """A miniature parent graph: two website pages, a repo, a vault note, and enough
    derived rows that every closure rule has something to get wrong."""
    _schema(path)
    c = sqlite3.connect(str(path))
    c.executescript(f"""
    INSERT INTO watched_sources (id, type, uri, enabled, provenance_kind) VALUES
      ('{SILO_WEB}', 'vault', '/site', 1, 'human_vault'),
      ('repo-orrery',  'repo',  '/git/orrery', 1, 'neutral_summary'),
      ('vault-notes',  'vault', '/vault', 1, 'human_vault'),
      ('other-source', 'vault', '/unrelated', 1, 'human_vault');

    -- in-subject documents
    INSERT INTO documents (id, title, content, content_hash, silo_id) VALUES
      ('d-post',  'content/posts/why-orrery/index.md', 'x', 'h1', '{SILO_WEB}'),
      ('d-prod',  'content/products/orrery.md',        'x', 'h2', '{SILO_WEB}'),
      ('d-repo',  'orrery repo summary',               'x', 'h3', 'repo-orrery'),
      ('d-note',  'orrery design notes',               'x', 'h4', 'vault-notes'),
      -- out of subject: must NOT travel
      ('d-other', 'unrelated musings',                 'x', 'h5', 'other-source');

    INSERT INTO domains (id, path, parent_path, document_count) VALUES
      ('dom1', 'software/ai-agents', 'software', 9999),
      ('dom2', 'software/frontend',  'software', 4242);

    INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES
      ('d-post', 'software/ai-agents', 1, 0.9),
      ('d-prod', 'software/ai-agents', 1, 0.9),
      ('d-repo', 'software/frontend',  1, 0.8),
      ('d-other','software/frontend',  1, 0.8);

    INSERT INTO collections (id, name, path, document_count, kind, provenance_kind) VALUES
      ('coll-orrery', 'orrery', '/git/orrery', 8888, 'git_repo', 'neutral_summary'),
      ('coll-far',    'far',    '/git/far',    77,   'git_repo', 'neutral_summary');

    INSERT INTO document_collections (document_id, collection_id, role) VALUES
      ('d-repo', 'coll-orrery', 'root');

    INSERT INTO collection_edges (source, target, type, weight) VALUES
      ('coll-orrery', 'coll-far', 'uses', 1.0);       -- one end absent -> must drop

    INSERT INTO entities (id, canonical_name, type) VALUES
      ('e1', 'Orrery', 'Product'),
      ('e2', 'UMAP',   'Technique'),
      ('e-out', 'Elsewhere', 'Concept');

    INSERT INTO chunks (id, document_id, chunk_index) VALUES
      ('ch-post', 'd-post', 0), ('ch-repo', 'd-repo', 0), ('ch-other', 'd-other', 0);

    INSERT INTO entity_sources (entity_id, document_id, chunk_id) VALUES
      ('e1', 'd-post', 'ch-post'), ('e2', 'd-post', 'ch-post'),
      ('e1', 'd-repo', 'ch-repo'), ('e-out', 'd-other', 'ch-other');

    INSERT INTO relationships (id, from_entity, to_entity, type, weight) VALUES
      ('r-in',  'e1', 'e2',    'co_occurs',  40),    -- parent-scale weight
      ('r-kept','e1', 'e2',    'relates_to', 40),    -- asserted: keep the given weight
      ('r-out', 'e1', 'e-out', 'co_occurs',  9);     -- one end absent -> must drop

    INSERT INTO merge_map (from_name, to_entity_id) VALUES
      ('orrery-project', 'e1'),
      ('somewhere-else', 'e-out');

    INSERT INTO domain_layout (domain_path, x, y) VALUES
      ('software/ai-agents', 0.25, 0.75), ('software/frontend', 0.6, 0.1);
    """)
    c.commit()
    c.close()


@pytest.fixture
def parent(tmp_path):
    p = tmp_path / "parent.db"
    _parent(p)
    return p


@pytest.fixture
def child(tmp_path):
    p = tmp_path / "child.db"
    _schema(p)
    return p


def _q(path, sql, *a):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


# ── the subject travels, and only the subject ──────────────────────────────────

def test_carves_the_subject_and_nothing_else(carve_mod, parent, child):
    carve_mod.carve(parent, child, "root", False)
    assert {r[0] for r in _q(child, "SELECT id FROM documents")} == {
        "d-post", "d-prod", "d-repo", "d-note"}
    assert {r[0] for r in _q(child, "SELECT id FROM entities")} == {"e1", "e2"}


def test_no_edge_dangles(carve_mod, parent, child):
    """An edge pointing at an entity the noosphere lacks renders as a route to
    nowhere, so both endpoint filters must actually bite."""
    carve_mod.carve(parent, child, "root", False)
    assert sorted(r[0] for r in _q(child, "SELECT id FROM relationships")) == ["r-in", "r-kept"]
    assert _q(child, """SELECT COUNT(*) FROM relationships r
                         WHERE r.from_entity NOT IN (SELECT id FROM entities)
                            OR r.to_entity   NOT IN (SELECT id FROM entities)""")[0][0] == 0
    assert _q(child, """SELECT COUNT(*) FROM collection_edges e
                         WHERE e.source NOT IN (SELECT id FROM collections)
                            OR e.target NOT IN (SELECT id FROM collections)""")[0][0] == 0


def test_collection_edges_travel_when_both_ends_do(carve_mod, parent, child):
    """`uses` / `chain_next` are what distinguish a trajectory from a bag of repos;
    graph_v5 emits them and the viz draws them."""
    c = sqlite3.connect(str(parent))
    c.execute("INSERT INTO collections (id, name, path, document_count) VALUES ('coll-two','two','/git/two',1)")
    c.execute("INSERT INTO document_collections (document_id, collection_id, role) VALUES ('d-prod','coll-two','root')")
    c.execute("INSERT INTO collection_edges (source, target, type) VALUES ('coll-orrery','coll-two','uses')")
    c.commit(); c.close()

    carve_mod.carve(parent, child, "root", False)
    assert ("coll-orrery", "coll-two") in {(r[0], r[1]) for r in
                                           _q(child, "SELECT source, target FROM collection_edges")}


# ── magnitudes: the bug that made every carved galaxy look uniform ─────────────

def test_document_count_is_recounted_not_inherited(carve_mod, parent, child):
    """Copied verbatim, `document_count` is the PARENT's — a domain holding 2 carved
    docs reported 3557 in the real carve, so every body rendered near-max radius.
    graph_v5 reads this column directly and state.js sizes from it."""
    carve_mod.carve(parent, child, "root", False)
    counts = dict(_q(child, "SELECT path, document_count FROM domains"))
    assert counts["software/ai-agents"] == 2          # d-post + d-prod, not 9999
    assert counts["software/frontend"] == 1           # d-repo only (d-other stayed)
    assert dict(_q(child, "SELECT id, document_count FROM collections"))["coll-orrery"] == 1


# ── provenance has to resolve, or #50/#79 gating silently inverts ─────────────

def test_provenance_kind_resolves_for_every_carried_document(carve_mod, parent, child):
    """documents.silo_id travels verbatim; provenance_kind is read through the
    `silo_kind` view. Without the silo rows every doc comes out kind NULL, the kind
    filters return nothing, and an agent_report silo loses co-occurrence gating."""
    carve_mod.carve(parent, child, "root", False)
    unresolved = _q(child, """SELECT COUNT(*) FROM documents d
                               LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id
                               WHERE d.silo_id IS NOT NULL AND sk.kind IS NULL""")[0][0]
    assert unresolved == 0
    kinds = dict(_q(child, """SELECT d.id, sk.kind FROM documents d
                               JOIN silo_kind sk ON sk.silo_id = d.silo_id"""))
    assert kinds["d-note"] == "human_vault" and kinds["d-repo"] == "neutral_summary"


def test_carried_sources_arrive_disabled_and_others_are_untouched(carve_mod, parent, child):
    """A carve is a snapshot, not a feed — but a blanket UPDATE would also stop syncs
    the destination already owned."""
    c = sqlite3.connect(str(child))
    c.execute("INSERT INTO watched_sources (id, type, uri, enabled) VALUES ('pre-existing','vault','/mine',1)")
    c.commit(); c.close()

    carve_mod.carve(parent, child, "root", False)
    enabled = dict(_q(child, "SELECT id, enabled FROM watched_sources"))
    assert enabled["pre-existing"] == 1, "the destination's own sync must keep running"
    assert all(v == 0 for k, v in enabled.items() if k != "pre-existing")
    assert "other-source" not in enabled, "only silos the carried docs reference travel"


# ── normalization decisions ────────────────────────────────────────────────────

def test_merge_map_travels_for_kept_entities(carve_mod, parent, child):
    """merge_map is (from_name -> to_entity_id). The carve queried a `canonical_id`
    column that exists nowhere, so the branch was dead and aliases never came over —
    later ingest would re-split what the parent had merged."""
    carve_mod.carve(parent, child, "root", False)
    assert dict(_q(child, "SELECT from_name, to_entity_id FROM merge_map")) == {"orrery-project": "e1"}


def test_umap_positions_travel(carve_mod, parent, child):
    """Without these the carved galaxy re-lays out and loses the geometry it had
    inside the parent."""
    carve_mod.carve(parent, child, "root", False)
    assert dict(_q(child, "SELECT domain_path, x FROM domain_layout"))["software/ai-agents"] == 0.25


# ── idempotency: the resume-after-failure case ────────────────────────────────

def test_carving_twice_equals_carving_once(carve_mod, parent, child):
    """entity_sources has NO primary key, so INSERT OR IGNORE has nothing to ignore
    against: a second run doubled every entity's source_count and degree, inflating
    domain radii and route weights. Re-running must be a no-op."""
    carve_mod.carve(parent, child, "root", False)
    tables = ("documents", "chunks", "entities", "entity_sources", "relationships",
              "document_domains", "domains", "collections", "document_collections",
              "collection_edges", "merge_map", "domain_layout", "watched_sources")
    before = {t: _q(child, f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}

    carve_mod.carve(parent, child, "root", False)
    after = {t: _q(child, f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}
    assert after == before


def test_snapshot_is_cleared_and_dirty(carve_mod, parent, child):
    """Serving the parent's snapshot from the child would show the parent's graph."""
    carve_mod.carve(parent, child, "root", False)
    payload, dirty = _q(child, "SELECT payload, dirty FROM graph_snapshot WHERE id='current'")[0]
    assert payload is None and dirty == 1


# ── the destructive-on-empty case ─────────────────────────────────────────────

def test_empty_selection_refuses_instead_of_damaging_the_destination(carve_mod, tmp_path, child):
    """The subject predicates name one specific corpus, so --from another DB selects
    nothing — and the writes at the end (clearing the snapshot, disabling sources)
    would still have run."""
    empty = tmp_path / "empty.db"
    _schema(empty)
    c = sqlite3.connect(str(child))
    c.execute("INSERT INTO watched_sources (id, type, uri, enabled) VALUES ('mine','vault','/mine',1)")
    c.execute("INSERT OR REPLACE INTO graph_snapshot (id, payload, dirty) VALUES ('current','{\"meta\":1}',0)")
    c.commit(); c.close()

    with pytest.raises(SystemExit):
        carve_mod.carve(empty, child, "root", False)

    assert _q(child, "SELECT enabled FROM watched_sources WHERE id='mine'")[0][0] == 1
    assert _q(child, "SELECT payload FROM graph_snapshot WHERE id='current'")[0][0] is not None


def test_cooccurrence_weights_are_recounted_not_inherited(carve_mod, parent, child):
    """relationships.weight counts the CHUNKS a pair shares. Copied verbatim it is the
    parent's count (40 here for a pair sharing exactly 1 carved chunk), and the carved
    noosphere's own star pages order by SUM(r.weight)."""
    carve_mod.carve(parent, child, "root", False)
    w = dict(_q(child, "SELECT id, weight FROM relationships"))
    assert w["r-in"] == 1, "co_occurs weight must reflect the carved corpus, not 40"
    assert w["r-kept"] == 40, "an asserted relationship keeps the weight it was given"


def test_cooccurrence_edges_with_nothing_left_to_support_them_are_dropped(carve_mod, parent, child):
    """A co_occurs edge recounting to 0 asserts a co-occurrence this corpus does not
    contain — its shared chunks all stayed behind."""
    c = sqlite3.connect(str(parent))
    # e1 and e2 both travel, but this pair shares no CARRIED chunk
    c.execute("INSERT INTO entities (id, canonical_name, type) VALUES ('e3','Lonely','Concept')")
    c.execute("INSERT INTO entity_sources (entity_id, document_id, chunk_id) VALUES ('e3','d-post',NULL)")
    c.execute("INSERT INTO relationships (id, from_entity, to_entity, type, weight) "
              "VALUES ('r-zero','e1','e3','co_occurs',12)")
    c.commit(); c.close()

    carve_mod.carve(parent, child, "root", False)
    assert "r-zero" not in {r[0] for r in _q(child, "SELECT id FROM relationships")}
