"""The graph payload contract (v5).

Through the migration this file compared the payload against an independent v4 builder
(the oracle) — every ablation phase had to be provably equivalent before the next
began. The adapter and the oracle are both gone now, so that question is answered and
closed; what remains is protecting the payload from drift.

That job is done by a GOLDEN: build the payload for a fixed seeded graph and compare it
to a committed JSON snapshot. Regenerate deliberately with

    UPDATE_GRAPH_GOLDEN=1 pytest tests/test_graph_v5_contract.py

and review the diff — an unreviewed regeneration defeats the point.

The behavioural tests below survive from the migration because each pins something a
golden alone would not explain: honest counts, order that carries meaning, and the
fields behind the maturity visual. Every one of them exists because an empty or absent
value would otherwise have passed silently — that happened four times during the
migration (active_simmers, spec_version, repo_edges, and the un-rendered node tail).

See docs/superpowers/specs/2026-08-05-graph-contract-design.md.
"""

import json
import os
import pathlib

import pytest

from src.pipeline.graph_snapshot import build_graph_payload
from src.pipeline.graph_v5 import build_graph_v5

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "graph_v5_golden.json"

LAYERS = {"meta", "taxonomy", "nodes", "node_index", "edges", "layout"}


def _seed(store, *, entities=6, docs=4, with_repo=True, unplaceable=1):
    """A graph with the shapes that actually stress the payload.

    Deliberately includes an entity with NO domain membership: those are dropped from
    the render set, and without one here the count-honesty path is never exercised.
    """
    c = store.conn
    for i in range(docs):
        # Explicit, fixed created_at: the payload now carries first_seen/last_seen
        # (entity recency), so the golden must be deterministic rather than defaulting
        # each doc to CURRENT_TIMESTAMP.
        c.execute("INSERT INTO documents (id, title, content_type, created_at) VALUES (?, ?, ?, ?)",
                  (f"d{i}", f"doc {i}", "image" if i == 0 else "text", f"2026-01-{i+1:02d} 00:00:00"))
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha', 2)")
    c.execute("INSERT INTO domains (path, document_count) VALUES ('alpha/beta/deep', 2)")
    for i in range(docs):
        path = "alpha" if i % 2 == 0 else "alpha/beta/deep"
        c.execute("INSERT INTO document_domains (document_id, domain_path, is_primary, confidence)"
                  " VALUES (?, ?, 1, 1.0)", (f"d{i}", path))

    if with_repo:
        c.execute("INSERT INTO collections (id, name, path, root_path, document_count)"
                  " VALUES ('r1', 'repo-one', 'repo-one', '/tmp/r1', ?)", (docs,))
        for i in range(docs):
            # role/emits_cooccurrence, not the fork's legacy `level`: this schema never
            # carried that column, and these are leaf documents — they must contribute
            # co-occurrence for the collection-scoped edges below to exist at all.
            c.execute("INSERT INTO document_collections "
                      "(document_id, collection_id, role, emits_cooccurrence, parent_path)"
                      " VALUES (?, 'r1', 'leaf', 1, NULL)", (f"d{i}",))

    for i in range(entities):
        c.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, ?)",
                  (f"e{i}", f"entity {i}", "Concept"))
        # spread mentions so degrees differ and the ranking is meaningful
        for j in range(i % docs + 1):
            c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)",
                      (f"e{i}", f"d{j}"))

    for i in range(unplaceable):
        # mentioned only in a document with no domain assignment
        c.execute("INSERT INTO documents (id, title) VALUES (?, ?)", (f"orphan{i}", "orphan"))
        c.execute("INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, 'Concept')",
                  (f"u{i}", f"unplaceable {i}"))
        c.execute("INSERT INTO entity_sources (entity_id, document_id) VALUES (?, ?)",
                  (f"u{i}", f"orphan{i}"))
    c.commit()


# ── drift protection ────────────────────────────────────────────────────────

def _golden_view(payload):
    """The payload minus `layout.positions`.

    Coordinates come out of UMAP, which is seeded (`random_state=42`) but NOT stable
    across umap/numpy builds — so pinning them makes the golden fail on any machine
    that resolves different wheels than the one that generated it, which is what
    happened the first time this ran in CI. Everything else about layout is
    deterministic and stays pinned; the positions get their own structural assertion
    below.
    """
    out = json.loads(json.dumps(payload, sort_keys=True))
    layout = out.get("layout")
    if isinstance(layout, dict):
        layout.pop("positions", None)
    return out


def test_payload_matches_the_golden(test_store):
    """Catches any unintended change to the payload's shape or contents."""
    _seed(test_store, entities=12)
    payload = build_graph_payload(test_store)

    if os.environ.get("UPDATE_GRAPH_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(_golden_view(payload), indent=1, sort_keys=True) + "\n")
        pytest.skip(f"golden regenerated at {GOLDEN} — review the diff")

    assert GOLDEN.exists(), (
        f"{GOLDEN} is missing; regenerate with UPDATE_GRAPH_GOLDEN=1 and review it")
    assert _golden_view(payload) == json.loads(GOLDEN.read_text())


def test_every_node_that_needs_a_position_has_one(test_store):
    """Stands in for the coordinates the golden cannot pin: assert the SHAPE of
    layout.positions rather than the values, which are UMAP-build dependent."""
    _seed(test_store)
    v5 = build_graph_v5(test_store)
    positions = v5["layout"]["positions"]
    collection_ids = {n["id"] for n in v5["nodes"] if n["type"] == "collection"}

    assert collection_ids <= set(positions), "every repo must be positioned"
    for node_id, xy in positions.items():
        assert set(xy) == {"x", "y"}, f"{node_id} has an odd position record"
        assert 0.0 <= xy["x"] <= 1.0 and 0.0 <= xy["y"] <= 1.0, \
            f"{node_id} is outside the unit square the frame declares"


def test_the_payload_is_exactly_the_declared_layers(test_store):
    """No stray top-level keys — the v4 vocabulary crept in as extra keys, which is
    what the layered shape exists to prevent."""
    _seed(test_store)
    assert set(build_graph_payload(test_store)) == LAYERS


def test_empty_graph_builds_cleanly(test_store):
    """A fresh noosphere must still produce every layer rather than blowing up."""
    assert set(build_graph_payload(test_store)) == LAYERS


def test_graph_without_repos_builds_cleanly(test_store):
    """The base product has no repo layer at all; v5 must not require one."""
    _seed(test_store, with_repo=False)
    payload = build_graph_payload(test_store)
    assert set(payload) == LAYERS
    assert not [n for n in payload["nodes"] if n["type"] == "collection"]


# ── honest counts ───────────────────────────────────────────────────────────

def test_pruning_declares_nodes_it_could_not_place(test_store):
    """The improvement v5 was worth having for. Entities with no domain membership are
    skipped, and v4 also left them out of its own total, so a consumer could not tell.
    Now `meta.pruning.excluded` names them and the arithmetic closes.

    Observed in production, not hypothetical: a text/image corpus had 1,411 entities of
    which only 1,342 were indexed.
    """
    _seed(test_store, unplaceable=3)
    v5 = build_graph_v5(test_store)
    excluded = {x["reason"]: x["count"] for x in v5["meta"]["pruning"]["excluded"]}
    assert excluded["no_domain_membership"] == 3

    live = test_store.conn.execute(
        "SELECT COUNT(*) FROM entities WHERE invalid_at IS NULL").fetchone()[0]
    # Entity-specific arithmetic reads counts_by_type: meta.counts covers every emitted
    # node type (entities + repos + documents), so it is not the entity total.
    ents = v5["meta"]["counts_by_type"]["entity"]
    assert ents["total"] + excluded["no_domain_membership"] == live
    assert ents["total"] == len(v5["node_index"])
    assert not any(k.startswith("u") for k in v5["node_index"])


def test_render_cap_is_honoured_and_reported(test_store):
    _seed(test_store, entities=12)
    v5 = build_graph_v5(test_store, max_render_nodes=5)
    render = [n for n in v5["nodes"] if n["type"] == "entity"]
    assert len(render) == 5
    # The cap is an ENTITY cap, so it is counts_by_type that reports it; meta.counts
    # covers every emitted node type and must agree with what `nodes` actually carries.
    assert v5["meta"]["counts_by_type"]["entity"]["included"] == 5
    assert v5["meta"]["counts"]["nodes_included"] == len(v5["nodes"])
    assert v5["meta"]["pruning"]["max_render_nodes"] == 5
    # the index still describes every placeable entity
    assert len(v5["node_index"]) == v5["meta"]["counts_by_type"]["entity"]["total"] > 5


# ── order that carries meaning ──────────────────────────────────────────────

def test_render_set_is_the_degree_ranked_prefix_of_the_index(test_store):
    """The renderer treats `nodes` as "the strongest N", so it must be a PREFIX of the
    ranking rather than an arbitrary subset."""
    _seed(test_store, entities=12)
    v5 = build_graph_v5(test_store, max_render_nodes=5)
    render = [n for n in v5["nodes"] if n["type"] == "entity"]
    assert [n["id"] for n in render] == list(v5["node_index"])[:5]
    degrees = [n["degree"] for n in v5["node_index"].values()]
    assert degrees == sorted(degrees, reverse=True)


def test_edge_types_partition_the_collection(test_store):
    """One typed collection replaced three top-level keys, so every edge must be
    classifiable — nothing invented, nothing stranded."""
    _seed(test_store)
    test_store.conn.execute("INSERT INTO collections (id, name, path, root_path, document_count)"
                            " VALUES ('r2', 'repo-two', 'repo-two', '/tmp/r2', 1)")
    # Seed the stored type explicitly. The previous version of this test inserted a
    # type-LESS row and asserted it came out as "uses", which codified the bug below:
    # the builder never selected `type` at all.
    test_store.conn.execute("INSERT INTO collection_edges (source, target, type, weight)"
                            " VALUES ('r1', 'r2', 'repo_uses', 4)")
    test_store.conn.commit()

    edges = build_graph_v5(test_store)["edges"]
    buckets = {
        ("cooccurrence", "domain"): [],
        ("cooccurrence", "collection"): [],
        ("uses", "collection"): [],
    }
    for e in edges:
        buckets[(e["type"], e["scope"])].append(e)
    assert sum(len(v) for v in buckets.values()) == len(edges)
    # and the `uses` bucket is genuinely exercised — no real corpus here has one
    assert [{"source": e["source"], "target": e["target"], "weight": e["weight"]}
            for e in buckets[("uses", "collection")]] == [
        {"source": "r1", "target": "r2", "weight": 4}]


def test_asserted_edge_types_survive_to_the_payload(test_store):
    """A tracker run's trajectory must not arrive labelled as an import dependency.

    `repo_edges.type` is in the table's PRIMARY KEY and distinguishes the two asserted
    edge kinds — `repo_uses` (a manifest import) from `chain_next` (a run→run
    trajectory). The builder used to `SELECT from_repo, to_repo, weight` and hardcode
    `"uses"`, so the single thing that makes a run different from a repo was erased at
    the payload boundary. `state.js` filters edges by type, so the client silently drew
    a trajectory as a dependency.
    """
    _seed(test_store)
    c = test_store.conn
    for rid in ("r2", "r3"):
        c.execute("INSERT INTO collections (id, name, path, root_path, document_count, kind) "
                  "VALUES (?, ?, ?, ?, 1, 'tracker_run')", (rid, rid, rid, f"/tmp/{rid}"))
    c.execute("INSERT INTO collection_edges (source, target, type, weight) "
              "VALUES ('r1', 'r2', 'repo_uses', 4)")
    c.execute("INSERT INTO collection_edges (source, target, type, weight) "
              "VALUES ('r2', 'r3', 'chain_next', 1)")
    c.commit()

    asserted = {(e["source"], e["target"]): e["type"]
                for e in build_graph_v5(test_store)["edges"]
                if e["type"] != "cooccurrence"}
    assert asserted == {("r1", "r2"): "uses", ("r2", "r3"): "chain_next"}


def test_an_unknown_stored_edge_type_passes_through_rather_than_being_relabelled(test_store):
    """A future asserted edge kind should reach the client as itself.

    Coercing unknowns to `uses` is what caused the bug above. The client filters by
    type, so an unrecognized type is simply not drawn — strictly better than being
    drawn as the wrong relationship.
    """
    _seed(test_store)
    c = test_store.conn
    c.execute("INSERT INTO collections (id, name, path, root_path, document_count)"
              " VALUES ('r2', 'r2', 'r2', '/tmp/r2', 1)")
    c.execute("INSERT INTO collection_edges (source, target, type, weight)"
              " VALUES ('r1', 'r2', 'supersedes', 1)")
    c.commit()

    types = {e["type"] for e in build_graph_v5(test_store)["edges"] if e["type"] != "cooccurrence"}
    assert types == {"supersedes"}


def test_a_typeless_edge_cannot_be_written_in_the_first_place(test_store):
    """The builder still defaults a null `type` to "uses", but this schema makes that
    branch unreachable.

    Ported from the fork, where `collection_edges.type` is nullable and rows predating
    the column existed — so a *legacy typeless edge* was a real state to defend against.
    Here the column is `NOT NULL`, and `type` is part of the primary key, so the row is
    rejected at write time. Asserting the row still reads as "uses" would mean writing a
    row this schema forbids, i.e. testing a state that cannot occur.

    Pinning the constraint instead: if someone relaxes it to match the fork, this fails
    and the defaulting branch becomes load-bearing again.
    """
    import sqlite3
    _seed(test_store)
    c = test_store.conn
    c.execute("INSERT INTO collections (id, name, path, root_path, document_count)"
              " VALUES ('r2', 'r2', 'r2', '/tmp/r2', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO collection_edges (source, target, weight) VALUES ('r1', 'r2', 2)")


def test_cooccurrence_always_carries_a_scope(test_store):
    """`cooccurrence` alone is ambiguous: domain-level and repo-level co-occurrence are
    different relations that used to live in different keys. Scope is what keeps them
    apart, so a missing one would silently merge them."""
    _seed(test_store)
    for e in build_graph_v5(test_store)["edges"]:
        assert e["type"] in {"cooccurrence", "uses"}
        if e["type"] == "cooccurrence":
            assert e["scope"] in {"domain", "collection"}


# ── layout ──────────────────────────────────────────────────────────────────

def test_positions_partition_into_domains_and_repos(test_store):
    """One map holds every node type, and the client splits it by asking which ids are
    repos. That split has to be total, or a node ends up unplaceable."""
    _seed(test_store)
    v5 = build_graph_v5(test_store)
    positions = v5["layout"]["positions"]
    collection_ids = {n["id"] for n in v5["nodes"] if n["type"] == "collection"}
    domains = {k for k in positions if k not in collection_ids}
    repos = {k for k in positions if k in collection_ids}
    assert len(domains) + len(repos) == len(positions)
    assert repos == collection_ids, "every repo must be positioned"


def test_taxonomy_may_describe_more_domains_than_layout_places(test_store):
    """A pre-existing asymmetry the client must keep tolerating: taxonomy covers every
    domain with content while layout only positions the ones it laid out (156 vs 145 on
    the office graph). The client enumerates from positions for exactly this reason."""
    _seed(test_store)
    v5 = build_graph_v5(test_store)
    collection_ids = {n["id"] for n in v5["nodes"] if n["type"] == "collection"}
    placed = {k for k in v5["layout"]["positions"] if k not in collection_ids}
    described = {t["path"] for t in v5["taxonomy"]}
    assert placed <= described, "a positioned domain is missing from the taxonomy"


# ── the fields behind the visuals ───────────────────────────────────────────

def test_taxonomy_carries_spec_version_that_drives_maturity(test_store):
    """Nebula maturity is computed client-side from taxonomy[].spec_version, so the
    NUMBER has to survive, not just its truthiness. Without a seeded spec every domain
    is unformed and this path goes unexercised — which is exactly how a browser check on
    the code corpus once reported 145 domains all at maturity 0 and proved nothing."""
    _seed(test_store)
    test_store.conn.execute(
        "UPDATE domains SET spec_version = 3 WHERE path = 'alpha/beta/deep'")
    test_store.conn.commit()

    tax = {t["path"]: t["spec_version"] for t in build_graph_v5(test_store)["taxonomy"]}
    assert tax["alpha/beta/deep"] == 3
    assert tax["alpha"] is None


def test_meta_activity_reports_running_simmers_only(test_store):
    """Drives the `simmering` nebula state. active_simmers has never been non-empty in
    any real corpus here, so live data proves nothing — seed a RUNNING simmer plus
    negative controls."""
    _seed(test_store)
    c = test_store.conn
    c.execute("INSERT INTO jobs (id, type, target, status) "
              "VALUES ('j1', 'simmer_domain', 'alpha/beta/deep', 'running')")
    c.execute("INSERT INTO jobs (id, type, target, status) "
              "VALUES ('j2', 'simmer_domain', 'alpha', 'completed')")
    c.execute("INSERT INTO jobs (id, type, target, status) "
              "VALUES ('j3', 'extract_batch', 'alpha', 'running')")
    c.commit()

    activity = build_graph_v5(test_store)["meta"]["activity"]
    assert activity["simmering_domains"] == ["alpha/beta/deep"]


def test_images_need_no_special_casing(test_store):
    """Verified against a mixed corpus: an image differs from text only by the
    content_type discriminator, which v5 carries as a document's `subtype`."""
    _seed(test_store)
    docs = [n for n in build_graph_v5(test_store)["nodes"] if n["type"] == "document"]
    assert {"image", "text"} <= {n["subtype"] for n in docs}
    assert all(n["label"] and "memberships" in n for n in docs)
