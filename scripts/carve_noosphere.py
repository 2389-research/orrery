#!/usr/bin/env python3
# ABOUTME: Carve a subject-scoped noosphere out of an existing one by COPYING the
# ABOUTME: document set and everything derived from it — no re-ingestion, no LLM.
"""Carve a smaller noosphere out of a bigger one.

Why copy instead of re-ingest: the extraction already happened. Re-ingesting the same
source material spends LLM time to produce *different* entities (the pipeline is not
deterministic), throwing away results that have already been reviewed. Copying the
rows preserves the graph exactly as it is.

Why carve at all: because a noosphere is the natural unit of scope. `GET /export/html`
exports a noosphere whole, and a `graph_snapshot` only renders the top N nodes by
degree — so exporting a 97k-entity noosphere silently yields a top-N dump of the whole
corpus wearing that noosphere's name. A dedicated, subject-scoped noosphere exports as
*itself*, with no slicing, no caps and no rescaling.

What travels, and why:

  documents / chunks              the corpus
  document_domains / domains      classification
  document_collections / collections  the repo layer
  entity_sources / entities       extraction
  entity_embeddings               dedup + search; recomputable but expensive
  relationships                   co-occurrence, kept only where BOTH endpoints do
  collection_edges                asserted `uses` / `chain_next` edges, kept only
                                  where BOTH endpoints do
  domain_layout                   UMAP positions, so the carved galaxy keeps the
                                  geometry it had inside the parent graph
  specs                           so later extraction in this noosphere matches
  merge_map                       normalization decisions for the kept entities
  watched_sources                 the silo rows the documents point at, copied
                                  DISABLED — needed for provenance_kind to resolve
                                  through the `silo_kind` view, but a carve is a
                                  snapshot, not a live feed, and a re-scan here would
                                  ingest the parent's whole source into the child

Stored document_count on domains/collections is RECOUNTED against the carried document
set rather than copied, or every body would render at the parent's magnitude.

What deliberately does not:

  jobs              history of work done elsewhere
  layout_model      a pickled UMAP reducer — unpickles only under compatible library
                    versions, and re-fitting is cheap (same reasoning as
                    scripts/export_noosphere.py)
  graph_snapshot    derived; left empty and dirty so the importer builds its own
                    rather than serving the parent's

The document selection is imported from embed/build_embed.py rather than restated, so
carving a noosphere and building an embed cannot disagree about what the subject is.

Usage:
    python3 scripts/carve_noosphere.py \\
        --from ~/orrery-data/clone2389/workspaces/default/orrery.db \\
        --to   ~/orrery-data/_lightmode_data/workspaces/<id>/orrery.db \\
        [--repo-depth root|group|all] [--dry-run]
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_selection():
    """Import select_subject_docs from the embed CLI by path.

    By path rather than as a package: embed/ is a script directory, not an installed
    module, and this keeps ONE definition of the subject predicates.
    """
    spec = importlib.util.spec_from_file_location("build_embed", REPO / "embed" / "build_embed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _copy(src, dst, table, rows, label=None):
    """Copy full rows, matching the DESTINATION's column set.

    Column-aware rather than `SELECT *`: the two schemas can differ by a migration,
    and a positional insert would then quietly write values into the wrong columns.
    """
    if not rows:
        print(f"  {label or table:24} 0")
        return 0
    dst_cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
    src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    cols = [c for c in dst_cols if c in src_cols]
    missing = [c for c in dst_cols if c not in src_cols]
    if missing:
        print(f"    (note: {table} columns absent in source, left default: {', '.join(missing)})")
    ph = ",".join("?" * len(cols))
    idx = {c: i for i, c in enumerate(src_cols)}
    dst.executemany(
        f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})",
        [tuple(r[idx[c]] for c in cols) for r in rows],
    )
    print(f"  {label or table:24} {len(rows)}")
    return len(rows)


def _fetch_in(src, table, column, values, cols="*"):
    out = []
    for b in _chunks(values):
        ph = ",".join("?" * len(b))
        out += src.execute(f"SELECT {cols} FROM {table} WHERE {column} IN ({ph})", list(b)).fetchall()
    return out


def _connect(path: Path) -> sqlite3.Connection:
    """Open a workspace DB the way the services do.

    A script cannot import `orchestrator.src.db.get_connection`, but the PRAGMAs it
    sets are not optional here: the destination may be open by the orchestrator and
    worker right now, and with no busy_timeout the carve aborts mid-copy on
    `database is locked`.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def carve(src_path: Path, dst_path: Path, repo_depth: str, dry_run: bool) -> None:
    be = _load_selection()
    src = _connect(src_path)
    doc_ids, slugs, repo_ids, parts = be.select_subject_docs(src, repo_depth=repo_depth)
    print(f"subject: {parts['website']} website + {parts['repo']} repo({repo_depth}) + "
          f"{parts['vault']} vault = {len(doc_ids)} docs over {len(slugs)} products")

    # entity closure: the entities extracted FROM those documents
    ent_ids = sorted({r[0] for r in _fetch_in(src, "entity_sources", "document_id", doc_ids, "entity_id")})
    dom_paths = sorted({r[0] for r in _fetch_in(src, "document_domains", "document_id", doc_ids, "domain_path")})
    coll_ids = sorted({r[0] for r in _fetch_in(src, "document_collections", "document_id", doc_ids, "collection_id")})
    print(f"closure: {len(ent_ids)} entities, {len(dom_paths)} domains, {len(coll_ids)} collections")

    if dry_run:
        print("dry run — nothing written")
        return

    # Refuse an empty selection rather than "carving" nothing: the writes below
    # (clearing the snapshot, disabling sources) would still run and would damage a
    # destination that already holds a graph. The subject predicates are specific to
    # one corpus, so pointing --from at another DB legitimately selects nothing.
    if not doc_ids:
        sys.exit("selected 0 documents — nothing to carve.\n"
                 "WEBSITE_SILOS / WEBSITE_PREDICATE in embed/build_embed.py describe a "
                 "specific corpus; point --from at the DB that holds it, or edit them.")

    if not dst_path.exists():
        sys.exit(f"destination has no database: {dst_path}\n"
                 "Create the noosphere first (POST /workspaces) so its schema and "
                 "registry entry exist, then carve into it.")
    dst = _connect(dst_path)
    existing = dst.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing:
        print(f"  (destination already holds {existing} documents; INSERT OR IGNORE will merge)")

    print("copying:")
    _copy(src, dst, "documents", _fetch_in(src, "documents", "id", doc_ids))
    _copy(src, dst, "chunks", _fetch_in(src, "chunks", "document_id", doc_ids))
    _copy(src, dst, "document_domains", _fetch_in(src, "document_domains", "document_id", doc_ids))
    _copy(src, dst, "domains", _fetch_in(src, "domains", "path", dom_paths))
    _copy(src, dst, "document_collections", _fetch_in(src, "document_collections", "document_id", doc_ids))
    _copy(src, dst, "collections", _fetch_in(src, "collections", "id", coll_ids))
    _copy(src, dst, "entities", _fetch_in(src, "entities", "id", ent_ids))
    # entity_sources has NO primary key (see db.py), so INSERT OR IGNORE has nothing to
    # ignore against and a re-run would insert every row a second time — doubling every
    # entity's source_count and degree, and with them domain radii and route weights.
    # Clearing this carve's own document ids first makes the script idempotent.
    for b in _chunks(doc_ids):
        dst.execute(f"DELETE FROM entity_sources WHERE document_id IN ({','.join('?' * len(b))})", list(b))
    _copy(src, dst, "entity_sources", _fetch_in(src, "entity_sources", "document_id", doc_ids))
    _copy(src, dst, "entity_embeddings", _fetch_in(src, "entity_embeddings", "entity_id", ent_ids))
    _copy(src, dst, "domain_layout", _fetch_in(src, "domain_layout", "domain_path", dom_paths),
          label="domain_layout (UMAP)")
    _copy(src, dst, "specs", src.execute("SELECT * FROM specs").fetchall())

    # Asserted collection→collection edges (`uses`, `chain_next`). These are what
    # distinguish a tracker trajectory from a bag of repos, and graph_v5 emits them as
    # scope:"collection" edges the viz draws — so dropping them would quietly flatten
    # the carved galaxy's repo layer. Both endpoints must be present, same as
    # relationships.
    cset = set(coll_ids)
    ce_cols = [r[1] for r in src.execute("PRAGMA table_info(collection_edges)")]
    ce_target = ce_cols.index("target")
    coll_edges = [r for r in _fetch_in(src, "collection_edges", "source", coll_ids)
                  if r[ce_target] in cset] if coll_ids else []
    _copy(src, dst, "collection_edges", coll_edges, label="collection_edges (both ends)")

    # Relationships only where BOTH endpoints came along — a dangling edge would
    # point at an entity this noosphere does not contain.
    keep = set(ent_ids)
    rel_cols = [r[1] for r in src.execute("PRAGMA table_info(relationships)")]
    rel_target = rel_cols.index("to_entity")
    rels = [r for r in _fetch_in(src, "relationships", "from_entity", ent_ids)
            if r[rel_target] in keep] if ent_ids else []
    _copy(src, dst, "relationships", rels, label="relationships (both ends)")

    # Normalization decisions for kept entities, so later ingestion dedups the same way.
    # merge_map is (from_name -> to_entity_id): the alias decisions that made these
    # entities canonical. Without them, later ingestion into the carved noosphere
    # re-derives aliases from scratch and re-splits what the parent had already merged.
    _copy(src, dst, "merge_map", _fetch_in(src, "merge_map", "to_entity_id", ent_ids))

    # The silo rows the carried documents point at. `documents.silo_id` travels
    # verbatim, and provenance_kind is read through the `silo_kind` view
    # (watched_sources UNION collections) — so without these rows every vault document
    # resolves to kind NULL instead of human_vault, the kind filters return nothing for
    # it, and an agent_report silo loses its co-occurrence emission gating (#50/#79).
    # Copied DISABLED: a carve is a snapshot, not a feed, and a re-scan here would pull
    # the parent's whole source into the child.
    silo_ids = sorted({r[0] for r in _fetch_in(src, "documents", "id", doc_ids, "silo_id") if r[0]})
    sources = _fetch_in(src, "watched_sources", "id", silo_ids) if silo_ids else []
    n_src = _copy(src, dst, "watched_sources", sources, label="watched_sources (disabled)")
    if n_src:
        for b in _chunks([r[0] for r in sources]):
            dst.execute(
                f"UPDATE watched_sources SET enabled = 0 WHERE id IN ({','.join('?' * len(b))})",
                list(b))
    # Scoped to the rows just copied on purpose: a blanket UPDATE would disable syncs
    # the destination already owned.

    # Both tables carry a STORED document_count that graph_v5 reads directly (taxonomy
    # counts, collection degree) and the viz sizes bodies from. Copied verbatim it is
    # the PARENT's count — measured here at 3557 for a domain holding 2 carved docs —
    # so every domain renders near-max radius and the map goes uniform. This is the same
    # bug embed/build_embed.py rescales for; recount against what actually travelled.
    dst.execute("""UPDATE domains SET document_count = (
                       SELECT COUNT(*) FROM document_domains dd WHERE dd.domain_path = domains.path)""")
    dst.execute("""UPDATE collections SET document_count = (
                       SELECT COUNT(*) FROM document_collections dc WHERE dc.collection_id = collections.id)""")
    print("  rescaled document_count on domains + collections to the carved doc set")

    # Derived: make the child build its own rather than serve the parent's.
    dst.execute("DELETE FROM graph_snapshot")
    dst.execute("INSERT OR REPLACE INTO graph_snapshot (id, payload, dirty) VALUES ('current', NULL, 1)")
    dst.commit()

    n = lambda t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"result: {n('documents')} docs, {n('entities')} entities, {n('relationships')} relationships, "
          f"{n('domains')} domains, {n('collections')} collections")
    print("graph_snapshot cleared + marked dirty — it rebuilds on the next /graph")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", required=True, type=Path)
    ap.add_argument("--to", dest="dst", required=True, type=Path)
    ap.add_argument("--repo-depth", choices=("root", "group", "all"), default="root")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    carve(a.src.expanduser(), a.dst.expanduser(), a.repo_depth, a.dry_run)


if __name__ == "__main__":
    main()
