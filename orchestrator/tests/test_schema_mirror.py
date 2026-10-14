"""The files the orchestrator and the worker each keep their own copy of must not drift.

Two pairs live here: `db.py` (the shared schema) and `classifier.py` (the shared
classification prompt + schema). Both are duplicated on purpose — neither process
imports the other's package — and in both cases a one-sided edit is invisible at
runtime and wrong.

For db.py: the two processes open the SAME database files. A table or index present in one and
missing from the other is not a style problem: whichever process opens a workspace
first decides its shape, and the other then reads a schema it does not expect.

That is a live hazard rather than a hypothetical — every table added in this change had
to be written into both files by hand, and nothing but this test makes a one-sided edit
visible.

Scoped deliberately to the shared graph surface rather than whole-file equality: the two
files legitimately differ elsewhere (busy_timeout, the orchestrator's per-process init
guard). If a divergence here is intentional, add it to `_ALLOWED_DIVERGENCE` with the
reason rather than deleting the assertion.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ORCH = _ROOT / "orchestrator" / "src" / "db.py"
_WORKER = _ROOT / "worker" / "src" / "db.py"

# Tables both processes write. Their DDL must match.
_MIRRORED_TABLES = ["graph_snapshot", "domain_edges", "collections",
                    "document_collections", "collection_edges",
                    # The worker WRITES judge verdicts here and the orchestrator serves
                    # them to the review UI, so a column present in one file and not the
                    # other means the judge writes somewhere the API cannot read.
                    "normalization_review_queue",
                    # The worker's generate_commentary job WRITES here and the
                    # orchestrator's GET /commentary reads it — same hazard.
                    "node_commentary",
                    # Incremental source sync: the worker writes invalid_at/modified_at/
                    # source_id on documents (via migration ALTERs in both files) and both
                    # services read watched_sources — both are now cross-service surface.
                    "documents", "watched_sources",
                    # The worker writes jobs.progress (extraction counters) and the
                    # orchestrator serves it to the extraction UI — same cross-service
                    # hazard: a one-sided column edit would be invisible until runtime.
                    "jobs",
                    # Silo-aware batch normalization (#50): the worker's faiss stage now
                    # WRITES cross-silo merge proposals here directly (it cannot import
                    # the orchestrator's graph_repair module), and the orchestrator both
                    # writes proposals via propose_correction() and serves them to the
                    # CorrectionsPanel — a one-sided column edit breaks either writer or
                    # the reader silently.
                    "graph_issues",
                    # ccvault ingestion ledgers: the worker's ingest_ccvault job WRITES
                    # the dedup watermarks and the orchestrator's /ingest/ccvault route
                    # reads them (get-or-create + no-op re-ingest) — cross-service surface.
                    "ccvault_sessions_seen", "ccvault_processed",
                    # Session-trace replay (feat/session-trace-replay): orchestrator-only
                    # surface today, but mirrored into both files so the shared DB has one
                    # shape regardless of which process opens a workspace first — same
                    # first-opener hazard as every other table here.
                    "traces", "trace_events", "trace_segments"]

# Indexes on that surface. Table DDL alone is not enough: an index dropped from one
# file costs nothing structurally and everything in latency, so it is exactly the kind
# of drift that goes unnoticed.
_MIRRORED_INDEXES = ["idx_document_collections_collection",
                     "idx_entity_sources_entity", "idx_entity_sources_document",
                     "idx_document_domains_path",
                     "idx_norm_review_pending",
                     # The worker writes co-occurrence edges and the orchestrator reads
                     # them for the graph; both need the same pair indexes or one side
                     # full-scans a table the other keeps indexed.
                     "idx_relationships_pair", "idx_relationships_to",
                     # Sync identity lookups join documents on source_path.
                     "idx_documents_source_path",
                     # Per-source silos (spec: silos + provenance, task 1) — normalization
                     # scoping will filter/join on this from both services.
                     "idx_documents_silo"]

_ALLOWED_DIVERGENCE: dict[str, str] = {
    # name -> why it is allowed to differ. Empty: nothing diverges today.
}


def _table_ddl(source: str, table: str) -> str | None:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", source, re.S)
    if not m:
        return None
    # Normalise whitespace and strip comments — layout may differ, meaning may not.
    body = re.sub(r"--[^\n]*", "", m.group(1))
    return re.sub(r"\s+", " ", body).strip()


def _join_adjacent_literals(source: str) -> str:
    """Splice Python implicit string concatenation back together.

    An index can be written inline in the SCHEMA text or built from adjacent string
    literals in a migration call, and the two files do not always agree on which.
    That is formatting, not drift, so normalise it away before comparing — otherwise
    this test fails on line wrapping and gets muted.
    """
    return re.sub(r'"\s*\n\s*"', "", source)


def _index_ddl(source: str, name: str) -> str | None:
    source = _join_adjacent_literals(source)
    m = re.search(rf"CREATE INDEX IF NOT EXISTS {name}\s+(ON[^\"';]*)", source, re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


@pytest.mark.parametrize("table", _MIRRORED_TABLES)
def test_table_ddl_is_mirrored(table):
    orch, worker = _table_ddl(_ORCH.read_text(), table), _table_ddl(_WORKER.read_text(), table)
    # Presence is checked BEFORE the allow-list. The escape hatch exists to permit a
    # deliberate DIFFERENCE, never to excuse a table missing from one file — that is
    # the failure it was written to catch.
    assert orch is not None, f"`{table}` missing from orchestrator/src/db.py"
    assert worker is not None, f"`{table}` missing from worker/src/db.py"
    if table in _ALLOWED_DIVERGENCE:
        pytest.skip(_ALLOWED_DIVERGENCE[table])
    assert orch == worker, (
        f"the `{table}` DDL differs between the two db.py files. Both processes open "
        f"the same databases, so whichever opens a workspace first decides its shape.")


@pytest.mark.parametrize("name", _MIRRORED_INDEXES)
def test_index_is_mirrored(name):
    orch, worker = _index_ddl(_ORCH.read_text(), name), _index_ddl(_WORKER.read_text(), name)
    assert orch is not None, f"`{name}` missing from orchestrator/src/db.py"
    assert worker is not None, f"`{name}` missing from worker/src/db.py"
    # Same allow-list as the tables — it was documented as covering both, but only the
    # table test consulted it, so an intentional index difference had no way to be
    # declared and the escape hatch was a dead letter.
    if name in _ALLOWED_DIVERGENCE:
        pytest.skip(_ALLOWED_DIVERGENCE[name])
    assert orch == worker, f"the `{name}` index differs between the two db.py files"


# ── classifier.py ───────────────────────────────────────────────────────────────
# The worker classifies during repo/run ingest and the orchestrator during ordinary
# document upload, into the SAME taxonomy. If the two prompts drift, the same content
# gets different domain paths depending on which door it came in through — and the
# graph's whole premise is that equivalent content from different sources merges into
# one node. Kept byte-identical below the ABOUTME header so drift needs no parsing to
# detect: the copy is mechanical, so the check should be too.
_ORCH_CLASSIFIER = _ROOT / "orchestrator" / "src" / "pipeline" / "classifier.py"
_WORKER_CLASSIFIER = _ROOT / "worker" / "src" / "classifier.py"

_HEADER = re.compile(r"\A(?:#[^\n]*\n)+\n")  # leading ABOUTME comment block + blank line


def _below_header(path: Path) -> str:
    source = path.read_text()
    m = _HEADER.match(source)
    assert m, f"{path.name} does not start with an ABOUTME comment block"
    return source[m.end():]


def test_classifier_is_mirrored():
    orch, worker = _below_header(_ORCH_CLASSIFIER), _below_header(_WORKER_CLASSIFIER)
    assert orch == worker, (
        "orchestrator/src/pipeline/classifier.py and worker/src/classifier.py have "
        "diverged below their ABOUTME headers. Both classify into the same taxonomy, "
        "so a prompt or schema change must be made in both or the same document gets "
        "a different domain depending on which process handled it.")


def test_the_classifier_mirror_check_can_actually_fail():
    """The comparison must not be vacuous — both sides must have real content."""
    body = _below_header(_ORCH_CLASSIFIER)
    assert "CLASSIFICATION_PROMPT_STATIC" in body
    assert "CLASSIFICATION_SCHEMA" in body
    assert len(body) > 5000, "header stripping ate the body"


def test_the_mirror_check_can_actually_fail():
    """Guard against the comparison silently matching nothing.

    Every assertion above compares two extractions. If the extractor returned None for
    both, they would be "equal" and the test would pass while checking nothing.
    """
    assert _table_ddl(_ORCH.read_text(), "no_such_table") is None
    assert _index_ddl(_ORCH.read_text(), "idx_no_such_index") is None
    assert _table_ddl(_ORCH.read_text(), "collections") is not None
    assert _index_ddl(_ORCH.read_text(), "idx_document_collections_collection") is not None


# ── silo.py ──────────────────────────────────────────────────────────────────────
# Both processes resolve a document's silo the same way (source_id > collection_id >
# None) and share the SQL fragment that scopes normalization to a silo. Kept
# byte-identical below the ABOUTME header for the same reason as classifier.py.
_ORCH_SILO = _ROOT / "orchestrator" / "src" / "pipeline" / "silo.py"
_WORKER_SILO = _ROOT / "worker" / "src" / "silo.py"


def test_silo_is_mirrored():
    orch, worker = _below_header(_ORCH_SILO), _below_header(_WORKER_SILO)
    assert orch == worker, (
        "orchestrator/src/pipeline/silo.py and worker/src/silo.py have diverged "
        "below their ABOUTME headers. Both resolve silo_id and scope normalization "
        "the same way, so a one-sided edit means a document's silo depends on which "
        "process ingested it.")


# ── image_prep.py / image_embedding.py ────────────────────────────────────────────
# The upcoming ingest_pdf worker job ingests each PDF page like an image and needs the
# orchestrator's image helpers, but the worker cannot import the orchestrator's package.
# Both modules are self-contained (stdlib + numpy + lazy torch/PIL/transformers), so they
# are mirrored byte-identical below the ABOUTME header for the same reason as classifier.py
# and silo.py: a one-sided edit means a PDF page gets prepared/embedded differently
# depending on which process handled it, and the shared vision-language space breaks.
_ORCH_IMAGE_PREP = _ROOT / "orchestrator" / "src" / "pipeline" / "image_prep.py"
_WORKER_IMAGE_PREP = _ROOT / "worker" / "src" / "image_prep.py"
_ORCH_IMAGE_EMBEDDING = _ROOT / "orchestrator" / "src" / "pipeline" / "image_embedding.py"
_WORKER_IMAGE_EMBEDDING = _ROOT / "worker" / "src" / "image_embedding.py"


def test_image_prep_is_mirrored():
    assert _below_header(_ORCH_IMAGE_PREP) == _below_header(_WORKER_IMAGE_PREP), \
        "image_prep.py drifted between orchestrator and worker"


def test_image_embedding_is_mirrored():
    assert _below_header(_ORCH_IMAGE_EMBEDDING) == _below_header(_WORKER_IMAGE_EMBEDDING), \
        "image_embedding.py drifted between orchestrator and worker"


def _fn_source(path, name):
    """Extract a top-level function's source text (def line through the last indented
    line before the next top-level statement or EOF)."""
    text = path.read_text()
    m = re.search(rf"\ndef {name}\(.*?(?=\n\S|\Z)", text, re.S)
    return m.group(0) if m else None


def test_recompute_cooccurrence_is_mirrored():
    """recompute_cooccurrence is the one shared helper Spec 1 adds: the worker writes
    co_occurs via it and the orchestrator reads them, so a one-sided edit is the same
    hazard the schema mirror guards against. Compare the function source byte-for-byte."""
    orch = _fn_source(_ORCH, "recompute_cooccurrence")
    worker = _fn_source(_WORKER, "recompute_cooccurrence")
    assert orch is not None, "recompute_cooccurrence missing from orchestrator/src/db.py"
    assert worker is not None, "recompute_cooccurrence missing from worker/src/db.py"
    assert orch == worker


def test_backfill_silo_ids_is_mirrored():
    """backfill_silo_ids (spec: per-source silos + provenance, task 1) is the pure
    derivation both services rely on to have already run — same cross-service hazard
    as recompute_cooccurrence. Compare the function source byte-for-byte."""
    orch = _fn_source(_ORCH, "backfill_silo_ids")
    worker = _fn_source(_WORKER, "backfill_silo_ids")
    assert orch is not None, "backfill_silo_ids missing from orchestrator/src/db.py"
    assert worker is not None, "backfill_silo_ids missing from worker/src/db.py"
    assert orch == worker


def _view_ddl(source: str, name: str) -> str | None:
    # `\s+` (not a literal space) between AS and the SELECT — the view is authored
    # multi-line in db.py, so a literal space would never match the newline.
    m = re.search(rf"CREATE VIEW IF NOT EXISTS {name} AS\s+(.*?);", source, re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


def test_silo_kind_view_is_mirrored():
    """silo_kind (spec: per-source silos + provenance, task 9) unifies watched_sources
    and collections so a document's provenance kind resolves via one join on
    documents.silo_id. It's a VIEW, not a TABLE, so it isn't in _MIRRORED_TABLES (whose
    `_table_ddl` regex only matches `CREATE TABLE ...` and would find nothing here) —
    same cross-service hazard as the mirrored tables above, checked with a view-aware
    regex instead."""
    orch = _view_ddl(_ORCH.read_text(), "silo_kind")
    worker = _view_ddl(_WORKER.read_text(), "silo_kind")
    assert orch is not None, "silo_kind view missing from orchestrator/src/db.py"
    assert worker is not None, "silo_kind view missing from worker/src/db.py"
    assert orch == worker, (
        "the `silo_kind` view differs between the two db.py files. Both processes open "
        "the same databases, so whichever opens a workspace first decides its shape.")


def test_backfill_provenance_kind_is_mirrored():
    """backfill_provenance_kind (spec: per-source silos + provenance, task 9) is the
    pure derivation both services rely on to have already run — same cross-service
    hazard as backfill_silo_ids. Compare the function source byte-for-byte."""
    orch = _fn_source(_ORCH, "backfill_provenance_kind")
    worker = _fn_source(_WORKER, "backfill_provenance_kind")
    assert orch is not None, "backfill_provenance_kind missing from orchestrator/src/db.py"
    assert worker is not None, "backfill_provenance_kind missing from worker/src/db.py"
    assert orch == worker
