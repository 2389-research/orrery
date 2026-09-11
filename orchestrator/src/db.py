import sqlite3
import os
import threading
import time
import uuid
from pathlib import Path

from .pipeline.silo import FLOW_DEFAULT_KIND

# Paths already migrated by init_db in this process. The schema + migrations
# only need to run once per DB file per process — repeating them takes a write
# lock and contends with concurrent requests / the worker.
_initialized: set[str] = set()
_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT,
    source_path TEXT,
    content TEXT,
    content_hash TEXT,
    metadata TEXT,
    content_type TEXT DEFAULT 'text',
    thumbnail_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending',
    modified_at TIMESTAMP,
    invalid_at TIMESTAMP,
    source_id TEXT,
    silo_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT REFERENCES documents(id),
    chunk_index INTEGER,
    offset INTEGER,
    length INTEGER,
    text TEXT,
    embedding BLOB,
    image_embedding BLOB
);

CREATE TABLE IF NOT EXISTS domains (
    id TEXT PRIMARY KEY,
    path TEXT UNIQUE,
    parent_path TEXT,
    document_count INTEGER DEFAULT 0,
    spec_version INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS domain_merge_map (
    from_label TEXT PRIMARY KEY,
    to_path TEXT REFERENCES domains(path)
);

CREATE TABLE IF NOT EXISTS document_domains (
    document_id TEXT REFERENCES documents(id),
    domain_path TEXT REFERENCES domains(path),
    is_primary BOOLEAN,
    confidence REAL,
    PRIMARY KEY (document_id, domain_path)
);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT,
    type TEXT,
    embedding BLOB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_sources (
    entity_id TEXT REFERENCES entities(id),
    document_id TEXT REFERENCES documents(id),
    chunk_id TEXT REFERENCES chunks(id),
    extraction_pass TEXT,
    spec_version INTEGER,
    job_id TEXT REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS merge_map (
    from_name TEXT PRIMARY KEY,
    to_entity_id TEXT REFERENCES entities(id)
);

CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    from_entity TEXT REFERENCES entities(id),
    to_entity TEXT REFERENCES entities(id),
    type TEXT,
    weight REAL,
    source_chunk TEXT REFERENCES chunks(id)
);
-- Every co-occurrence read filters by from/to entity, and extraction now accumulates
-- into an existing pair row instead of inserting a fresh one — both were full table
-- scans without these. The pair index serves the (from,to,type) equality lookup and
-- the `from_entity IN (...)` arm of the neighbourhood read; the `to` index serves its
-- `OR to_entity IN (...)` arm.
CREATE INDEX IF NOT EXISTS idx_relationships_pair ON relationships(from_entity, to_entity, type);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships(to_entity);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT,
    target TEXT,
    status TEXT DEFAULT 'queued',
    config TEXT,
    result TEXT,
    progress TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_embeddings (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id),
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS normalization_log (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT,
    from_name TEXT,
    to_entity_id TEXT,
    to_name TEXT,
    method TEXT,
    similarity REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS normalization_review_queue (
    id TEXT PRIMARY KEY,
    entity_a_id TEXT,
    entity_a_name TEXT,
    entity_b_id TEXT,
    entity_b_name TEXT,
    similarity REAL,
    status TEXT DEFAULT 'pending',
    resolution TEXT,
    judge_verdict TEXT,        -- advisory: merge | keep | unsure (normalization judge)
    judge_confidence REAL,     -- advisory
    judge_rationale TEXT,      -- advisory
    judge_attempts INTEGER DEFAULT 0,  -- failed judge sweeps; skip a pair after a cap
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_norm_review_pending ON normalization_review_queue(status);

CREATE TABLE IF NOT EXISTS graph_issues (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,                 -- invalidate | merge | retype | rename
    target_entity_id TEXT NOT NULL REFERENCES entities(id),
    target_entity_name TEXT NOT NULL,
    target_b_entity_id TEXT REFERENCES entities(id),  -- merge: the other node
    target_b_name TEXT,
    proposed_type TEXT,                   -- retype
    proposed_name TEXT,                   -- rename
    rationale TEXT,
    proposer TEXT,                        -- proposing agent id
    status TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | rejected
    judge_verdict TEXT,                   -- advisory, written by the judge stage
    judge_confidence REAL,                -- advisory
    judge_rationale TEXT,                 -- advisory
    reviewer TEXT,                        -- human, written on resolve
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_graph_issues_status ON graph_issues(status);
CREATE INDEX IF NOT EXISTS idx_graph_issues_target ON graph_issues(target_entity_id);

CREATE TABLE IF NOT EXISTS simmer_iterations (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    phase TEXT,
    iteration INTEGER,
    scores TEXT,
    composite REAL,
    key_change TEXT,
    asi TEXT,
    judge_mode TEXT,
    regressed BOOLEAN,
    candidate_preview TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_simmer_iterations_job ON simmer_iterations(job_id);

CREATE TABLE IF NOT EXISTS simmer_criterion_details (
    id TEXT PRIMARY KEY,
    iteration_id TEXT REFERENCES simmer_iterations(id),
    criterion TEXT,
    score INTEGER,
    seed_score INTEGER,
    evidence TEXT,
    improve TEXT
);


CREATE TABLE IF NOT EXISTS domain_layout (
    domain_path TEXT PRIMARY KEY,
    x REAL,
    y REAL,
    embedding BLOB,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS layout_model (
    id TEXT PRIMARY KEY DEFAULT 'umap',
    model_blob BLOB,
    domain_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS specs (
    id TEXT PRIMARY KEY,
    domain_path TEXT,
    version INTEGER,
    spec_content TEXT,
    golden_set TEXT,
    score REAL,
    media_type TEXT DEFAULT 'text',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Pre-generated Magos Lex screensaver commentary, one row per node: three in-voice
-- lines (description / omnissiah / humor), each carrying a mascot pose. Written offline
-- by the generate_commentary worker job and read by GET /commentary + the attract-mode
-- overlay. Additive and fail-silent: a node with no row simply shows no commentary.
CREATE TABLE IF NOT EXISTS node_commentary (
    node_type TEXT,
    node_id TEXT,
    comments_json TEXT,
    model TEXT,
    source_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (node_type, node_id)
);

-- ── Graph read-model foundations ────────────────────────────────────────────
-- Nothing reads these yet; they land ahead of the read layer so the schema and
-- the code that uses it move in separate, revertible steps.

-- Materialized `/graph` payload. The graph only changes when a job writes to it,
-- so the payload is computed once and served cached; writers flip `dirty` and a
-- background task rebuilds. One logical row, id='current'.
CREATE TABLE IF NOT EXISTS graph_snapshot (
    id TEXT PRIMARY KEY,
    payload TEXT,
    built_at TIMESTAMP,
    entity_count INTEGER,
    edge_count INTEGER,
    dirty INTEGER DEFAULT 1
);

-- Materialized domain↔domain co-occurrence edges (domains sharing entities).
-- Computing one domain's neighbours live costs seconds on a large domain because
-- every entity-mention fans out to every document sharing that entity; the
-- snapshot build already computes the whole edge set, so this stores what it
-- found where a scoped read can seek it.
CREATE TABLE IF NOT EXISTS domain_edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    weight REAL,
    PRIMARY KEY (source, target)
);
CREATE INDEX IF NOT EXISTS idx_domain_edges_source ON domain_edges(source);
CREATE INDEX IF NOT EXISTS idx_domain_edges_target ON domain_edges(target);

-- A COLLECTION is a labelled, hierarchical grouping of documents — a git repo, an
-- agent run, anything whose documents arrived together. It sits BESIDE `domains`,
-- not above or below: both are containers of documents, and they stay distinct
-- because their provenance differs. A domain is inferred (LLM-classified,
-- spec-cascaded); a collection is given (it is where the documents came from).
--
-- `kind` does not branch the schema. It selects which *asserted* edges an ingest
-- path writes (see collection_edges.type); the DERIVED edge — co-occurrence over
-- shared entities — comes free to every kind.
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT,
    path TEXT UNIQUE,
    root_path TEXT,
    parent_path TEXT,
    document_count INTEGER DEFAULT 0,
    remote_url TEXT,
    commit_sha TEXT,
    kind TEXT DEFAULT 'git_repo',
    pos_x REAL,
    pos_y REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    provenance_kind TEXT          -- neutral_summary|human_vault|agent_report|human_reviewed (task 9); NOT the collection TYPE (that's `kind` above)
);

-- Membership + where the document sits in its collection's tree.
--
-- `role` and `emits_cooccurrence` are deliberately separate. Structural position
-- and extraction behaviour are independent: a root or group summary mentions
-- everything beneath it, so its co-occurrence is noise, but that is a default
-- rather than a law. Deriving one from the other forces every new collection kind
-- to impersonate a code repo to opt in.
CREATE TABLE IF NOT EXISTS document_collections (
    document_id TEXT NOT NULL REFERENCES documents(id),
    collection_id TEXT NOT NULL REFERENCES collections(id),
    parent_path TEXT,
    role TEXT,                              -- 'root' | 'group' | 'leaf'
    emits_cooccurrence INTEGER DEFAULT 1,   -- explicit, never inferred from role
    PRIMARY KEY (document_id, collection_id)
);
CREATE INDEX IF NOT EXISTS idx_document_collections_collection
    ON document_collections(collection_id);

-- Asserted collection→collection edges. `type` distinguishes them and is part of
-- the key: 'uses' (a declared dependency) and 'chain_next' (a trajectory).
-- `source`/`target` rather than from_/to_ so this matches `domain_edges` — the two
-- container kinds should not describe the same idea with different column names.
CREATE TABLE IF NOT EXISTS collection_edges (
    source TEXT NOT NULL REFERENCES collections(id),
    target TEXT NOT NULL REFERENCES collections(id),
    type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (source, target, type)
);

-- A WATCHED SOURCE is a vault dir or a repo the worker re-scans on a cadence to keep
-- the graph in sync (spec 2026-08-14 incremental-source-sync). Lives per-workspace DB,
-- so (source_id, source_path) identity is unambiguous within a file. The worker sweep
-- reads its own DB's rows and enqueues scan_source jobs into the same DB.
CREATE TABLE IF NOT EXISTS watched_sources (
    id TEXT PRIMARY KEY,
    type TEXT,                    -- 'vault' | 'repo'
    uri TEXT,                     -- vault dir (as the worker sees it) | git url/path
    noosphere TEXT,               -- workspace this source feeds (provenance/logging only)
    cadence_hours REAL DEFAULT 24,
    config_json TEXT,             -- adapter-specific (ext filter, branch, thresholds…)
    enabled INTEGER DEFAULT 1,
    last_scanned_at TIMESTAMP,
    last_status TEXT,             -- 'ok' | 'error' | 'running'
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    provenance_kind TEXT          -- neutral_summary|human_vault|agent_report|human_reviewed (task 9)
);
-- ccvault ingestion ledgers (docs/ccvault-ingestion.md). Per-workspace dedup so re-ingesting
-- the same archive is a no-op. ccvault_sessions_seen = Flow A watermark (session summarized here?);
-- ccvault_processed = Flow B watermark (one row per query_id, so a segment citing several is
-- deduped per call). document_id points at the agent_report doc the work produced.
CREATE TABLE IF NOT EXISTS ccvault_sessions_seen (
    session_id  TEXT PRIMARY KEY,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ccvault_processed (
    query_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    document_id TEXT,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Unifies the two silo tables so a consumer resolves a document's provenance kind with
# one join: documents.silo_id -> silo_kind.silo_id. Relies on the invariant that both
# watched_sources.id and collections.id are str(uuid.uuid4()) — disjoint id spaces — so
# a given silo_id matches AT MOST ONE row of this view (spec: per-source silos +
# provenance, task 9).
#
# NOT part of SCHEMA above: SCHEMA is executed via executescript() before the guarded
# provenance_kind ALTERs run (see init_db), so on a pre-existing DB the column would not
# exist yet at that point. Created explicitly in init_db AFTER those ALTERs instead.
SILO_KIND_VIEW = """
CREATE VIEW IF NOT EXISTS silo_kind AS
    SELECT id AS silo_id, provenance_kind AS kind FROM watched_sources
    UNION ALL
    SELECT id AS silo_id, provenance_kind AS kind FROM collections;
"""

# repos/* -> collections/*. Applied BEFORE the schema script (see migrate_to_collections).
_LEGACY_TABLES = [
    ("repos", "collections"),
    ("document_repos", "document_collections"),
    ("repo_edges", "collection_edges"),
]
_LEGACY_COLUMNS = [
    ("document_collections", "repo_id", "collection_id"),
    ("collection_edges", "from_repo", "source"),
    ("collection_edges", "to_repo", "target"),
]


def migrate_to_collections(conn) -> None:
    """Rename the repo-era tables and columns in place.

    MUST run BEFORE `executescript(SCHEMA)`. `CREATE TABLE IF NOT EXISTS collections`
    would otherwise create an EMPTY table beside the populated `repos`, the rename
    would then fail with "table collections already exists", and every existing corpus
    would be stranded behind an empty one — silently, since reads would just return
    nothing.

    `ALTER TABLE ... RENAME TO` also rewrites REFERENCES clauses in other tables
    (SQLite >= 3.25 with legacy_alter_table off, the default), so the foreign keys
    follow automatically. Idempotent: each step is skipped once it has been applied.

    ATOMIC. SQLite makes DDL transactional, so the whole sequence runs inside a
    savepoint: a failure part-way through would otherwise leave one table renamed and
    the next not — exactly the half-migrated state the preflight below refuses to
    start from, reached by a different route and with no way to retry out of it.

    The write lock is taken UP FRONT with BEGIN IMMEDIATE, not lazily. The orchestrator
    and the worker both open every workspace, so they genuinely race on a legacy
    database's first open. A deferred transaction would let both read the legacy schema,
    and the second one to attempt a write would get SQLITE_BUSY_SNAPSHOT when upgrading
    its now-stale read snapshot — a failure `busy_timeout` cannot rescue, because
    waiting does not make an outdated snapshot current. BEGIN IMMEDIATE makes the
    contention happen at the lock instead, where busy_timeout does apply, so the loser
    waits and then observes the migrated schema rather than failing.
    """
    # Nothing to do is the OVERWHELMINGLY common case, and it must not cost a write
    # lock. Both services call init_db on every workspace database on every poll pass
    # (worker: every 5s across all of them), so taking BEGIN IMMEDIATE unconditionally
    # meant one exclusive lock per workspace per pass, forever, on databases that were
    # migrated long ago. That is contention manufactured out of nothing, it scales with
    # the number of workspaces, and it collides with genuinely long writes — an
    # in-flight extract_batch made the poll loop log "database is locked" and skip that
    # workspace for the cycle. The check below is pure reads, so it costs nothing and
    # takes no lock.
    if not _collections_migration_needed(conn):
        return

    # If a transaction is already open (a caller mid-write), the savepoint alone is
    # correct — and BEGIN would raise. Only take the lock when we are the outermost.
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT collections_migration")
    try:
        _migrate_to_collections(conn)
    except Exception:
        conn.execute("ROLLBACK TO collections_migration")
        conn.execute("RELEASE collections_migration")
        if started:
            conn.rollback()
        raise
    conn.execute("RELEASE collections_migration")
    if started:
        # Commit here rather than leaving the write open: init_db keeps running
        # (executescript, ALTERs) and holding the lock across all of it would widen
        # the window the other process has to wait through.
        conn.commit()


def _assert_no_column_conflicts(conn, tables: set[str]) -> None:
    """Refuse a table carrying BOTH the legacy and the replacement column.

    The same hazard the table-level preflight refuses, one level down: values live
    under each name and `ALTER TABLE ... RENAME COLUMN` cannot merge them, so there is
    no safe automatic resolution.

    Left alone it fails in the worst way available — silently and forever. The rename
    in `_migrate_to_collections` is guarded on `new_col not in cols`, so it skips;
    nothing else touches the legacy column; and the detector keeps answering `True`
    because the legacy column still exists. Every `init_db` on every poll pass then
    takes the write lock, does nothing, and releases it — which is precisely the churn
    this precheck was added to remove, reintroduced by a database in a state no code
    path here creates.

    Raised from the read-only pass ON PURPOSE, before `BEGIN IMMEDIATE`: a database
    that cannot be migrated should not cost a write lock to find that out, once or
    repeatedly.
    """
    for table, old_col, new_col in _LEGACY_COLUMNS:
        if table not in tables:
            continue
        # `table` comes from the module-level _LEGACY_COLUMNS constant, never from
        # caller input, and PRAGMA takes no bound parameters.
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if old_col in cols and new_col in cols:
            raise RuntimeError(
                f"cannot migrate: {table} has both {old_col!r} and {new_col!r}. "
                f"Values under each would have to be merged by hand — pick the "
                f"authoritative column, copy anything worth keeping into it, then "
                f"drop {old_col!r}.")


def _collections_migration_needed(conn) -> bool:
    """Is there any repo-era shape left? Read-only, and deliberately so.

    Mirrors every condition `_migrate_to_collections` acts on, so a `False` here means
    that function would be a no-op. It must stay conservative in one direction: saying
    `False` when work remains would skip the migration silently, so anything uncertain
    answers `True` and lets the real body decide under the lock.

    The both-TABLE-names conflict still reaches the body — `old in tables` is what
    triggers it — so a half-migrated database is still refused loudly rather than
    quietly skipped here. The both-COLUMN-names conflict is refused right here, for
    the reason in `_assert_no_column_conflicts`.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # BEFORE the legacy-table return, not after. A database can hold a legacy TABLE
    # and a conflicting column pair at once, and returning True first skips this
    # check entirely: the body then takes the write lock, renames what it can, and
    # silently steps over the conflicting column because that rename is guarded on
    # `new_col not in cols`. The refusal only surfaces a pass later, once the table
    # rename is done — after a lock was taken and partial work committed. The check
    # is pure reads, so there is no reason for it to be conditional.
    _assert_no_column_conflicts(conn, tables)
    if any(old in tables for old, _ in _LEGACY_TABLES):
        return True
    for table, old_col, _ in _LEGACY_COLUMNS:
        if table in tables and old_col in {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            return True
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_document_repos_repo'").fetchone():
        return True
    if "collection_edges" in tables and conn.execute(
            "SELECT 1 FROM collection_edges WHERE type='repo_uses' LIMIT 1").fetchone():
        return True
    return False


def _migrate_to_collections(conn) -> None:
    """The migration body. Always call `migrate_to_collections`, which makes it atomic."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # PREFLIGHT every pair before touching anything. Raising mid-loop would leave the
    # database half-renamed — one table moved, the next not — which is a worse state
    # than the one being refused, and init_db would fail identically on every retry.
    conflicts = [(old, new) for old, new in _LEGACY_TABLES
                 if old in tables and new in tables]
    if conflicts:
        # Rows live under each name and the schema script is about to make the new
        # name authoritative, silently orphaning everything still under the old one.
        # No safe automatic merge exists (ids can collide), so stop loudly.
        pairs = ", ".join(f"{o!r}+{n!r}" for o, n in conflicts)
        raise RuntimeError(
            f"cannot migrate: both names exist for {pairs}. Rows under the old name "
            f"would become unreachable. Merge them by hand, then drop the old table.")
    for old, new in _LEGACY_TABLES:
        if old in tables and new not in tables:
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
            tables.discard(old)
            tables.add(new)
    for table, old_col, new_col in _LEGACY_COLUMNS:
        if table not in tables:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if old_col in cols and new_col not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
    # The index survives the table rename but keeps its old NAME; the schema script
    # recreates it as idx_document_collections_collection.
    conn.execute("DROP INDEX IF EXISTS idx_document_repos_repo")
    # The stored discriminator followed the tables. `repo_uses` was the repo-era name
    # for what the contract has always exposed as `uses`; writers now emit `uses`
    # directly. Idempotent — a second pass matches nothing.
    # Guarded: this runs BEFORE the schema script, so on a fresh database the table
    # does not exist yet — and there is nothing to migrate there anyway.
    if "collection_edges" in tables:
        # A pair can carry BOTH spellings after a mixed-version deploy (one process
        # writing `repo_uses`, another `uses`). A bare UPDATE then violates the
        # composite key (source, target, type) and init_db raises — the database stops
        # opening at all. So fold the duplicates first, keeping the larger weight,
        # then rewrite what is left.
        conn.execute("""UPDATE collection_edges SET weight = MAX(weight, (
                            SELECT r.weight FROM collection_edges r
                            WHERE r.source = collection_edges.source
                              AND r.target = collection_edges.target
                              AND r.type = 'repo_uses'))
                        WHERE type = 'uses' AND EXISTS (
                            SELECT 1 FROM collection_edges r
                            WHERE r.source = collection_edges.source
                              AND r.target = collection_edges.target
                              AND r.type = 'repo_uses')""")
        conn.execute("""DELETE FROM collection_edges WHERE type = 'repo_uses' AND EXISTS (
                            SELECT 1 FROM collection_edges r
                            WHERE r.source = collection_edges.source
                              AND r.target = collection_edges.target
                              AND r.type = 'uses')""")
        conn.execute("UPDATE collection_edges SET type = 'uses' WHERE type = 'repo_uses'")




def _migrate_collection_columns(conn) -> None:
    """Bring a RENAMED legacy collection table up to the current column set.

    `migrate_to_collections` moves `repos` -> `collections`, but a renamed table keeps
    its old COLUMNS, and `CREATE TABLE IF NOT EXISTS` adds none to a table that already
    exists. So a legacy corpus arrives with the right table names and the wrong shape,
    and the reads that select `role` or filter `emits_cooccurrence` fail on it — or
    worse, a column added with a DEFAULT reads as though it had been chosen.

    Must run AFTER the schema script (the tables have to exist on a fresh database).
    Idempotent: every step is guarded on the column being absent.
    """
    coll_cols = {r[1] for r in conn.execute("PRAGMA table_info(collections)").fetchall()}
    for col, decl in [("pos_x", "REAL"), ("pos_y", "REAL"),
                      ("remote_url", "TEXT"), ("commit_sha", "TEXT")]:
        if col not in coll_cols:
            conn.execute(f"ALTER TABLE collections ADD COLUMN {col} {decl}")
    # `kind` discriminates a git repo from a tracker run. Legacy corpora predate it and
    # `remote_url IS NULL` is not a proxy (a locally-ingested repo has it NULL too).
    if "kind" not in coll_cols:
        conn.execute("ALTER TABLE collections ADD COLUMN kind TEXT DEFAULT 'git_repo'")
        conn.execute("UPDATE collections SET kind = 'git_repo' WHERE kind IS NULL")
    # Recover the kind of collections ingested before that column existed.
    # `chain_next` is written by exactly one code path — tracker-run ingest — so
    # participating in such an edge is a fact about provenance, not a guess. Defaulting
    # these to 'git_repo' would mislabel an entire trajectory corpus on first open.
    # Limitation: a corpus of ONE run has no chain edge and stays 'git_repo'. Partial
    # recovery beats none, and re-ingesting sets `kind` directly.
    conn.execute("""UPDATE collections SET kind = 'tracker_run'
                    WHERE kind = 'git_repo' AND id IN (
                        SELECT source FROM collection_edges WHERE type = 'chain_next'
                        UNION
                        SELECT target FROM collection_edges WHERE type = 'chain_next')""")

    # `role` + `emits_cooccurrence` replaced the overloaded `level`, which did three
    # unrelated jobs: structural position, a co-occurrence switch (`level == 'file'`),
    # and the lookup key for a collection's own summary doc.
    dc_cols = {r[1] for r in conn.execute("PRAGMA table_info(document_collections)").fetchall()}
    added: list[str] = []
    if "role" not in dc_cols:
        conn.execute("ALTER TABLE document_collections ADD COLUMN role TEXT")
        added.append("role")
    if "emits_cooccurrence" not in dc_cols:
        conn.execute("ALTER TABLE document_collections ADD COLUMN emits_cooccurrence INTEGER DEFAULT 1")
        added.append("emits_cooccurrence")
    # Only a MIGRATED table has `level`; this schema never creates it, so the backfill
    # is guarded on the column's presence rather than assuming it (the fork's version
    # could assume it — its schema still declared the column — and copying that
    # verbatim would raise "no such column: level" on every fresh database).
    # Derive ONLY the columns this call actually added. `level` stays populated on
    # legacy rows forever, so a backfill gated on `level IS NOT NULL` alone would re-run
    # on every open and RESET these two columns from the stale legacy value — silently
    # reverting anything written since. That is not hypothetical: `emits_cooccurrence`
    # exists precisely so it can be set independently of structural role, and a legacy
    # row would have been pinned to the level-derived value permanently, undoing an
    # operator fix or a re-ingest on the next process start.
    #
    # Running only for freshly-added columns also makes this a true one-shot: after the
    # first open the columns exist, `added` is empty, and no UPDATE runs at all.
    _CASE = {
        "role": """CASE level
                       WHEN 'repo' THEN 'root'
                       WHEN 'module' THEN 'group'
                       WHEN 'file' THEN 'leaf'
                   END""",
        # `level == 'file'` is what the old co-occurrence guard tested.
        "emits_cooccurrence": "CASE WHEN level = 'file' THEN 1 ELSE 0 END",
    }
    # Only a MIGRATED table has `level`; this schema never creates it, so this is guarded
    # on the column's presence rather than assuming it (the fork's version could assume
    # it — its schema still declared the column — and copying that verbatim raises
    # "no such column: level" on every fresh database).
    if added and "level" in dc_cols:
        sets = ", ".join(f"{col} = {_CASE[col]}" for col in added)
        conn.execute(f"UPDATE document_collections SET {sets} WHERE level IS NOT NULL")


def init_db(db_path: str) -> None:
    # Fast path: skip migrations if we've already initialized this DB in this process.
    if db_path in _initialized:
        return
    with _init_lock:
        if db_path in _initialized:
            return
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # One factory owns WAL + busy_timeout; init_db must not re-specify them.
        conn = get_connection(db_path)
        try:
            migrate_to_collections(conn)   # MUST run BEFORE the schema script — see above
            conn.executescript(SCHEMA)
            # Migrate: add columns for image support if missing
            cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
            if "content_type" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN content_type TEXT DEFAULT 'text'")
            if "thumbnail_path" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN thumbnail_path TEXT")
            # Incremental source sync: stamped on update-in-place; soft-delete marker;
            # FK (nullable) to the watched_sources row that owns this doc.
            if "modified_at" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN modified_at TIMESTAMP")
            if "invalid_at" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN invalid_at TIMESTAMP")
            if "source_id" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN source_id TEXT")
            # Per-source silo: a document's normalization scope (source_id, else the
            # collection it belongs to, else NULL) — see backfill_silo_ids below.
            if "silo_id" not in cols:
                try:
                    conn.execute("ALTER TABLE documents ADD COLUMN silo_id TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Extraction progress: live mid-run counters on the job (issue #51).
            job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "progress" not in job_cols:
                try:
                    conn.execute("ALTER TABLE jobs ADD COLUMN progress TEXT")
                except sqlite3.OperationalError as e:
                    # Only the orchestrator/worker init_db race (column already added) is expected;
                    # don't swallow a real error like 'database is locked'.
                    if "duplicate column" not in str(e).lower():
                        raise
            # Migrate specs table
            spec_cols = {r[1] for r in conn.execute("PRAGMA table_info(specs)").fetchall()}
            if "media_type" not in spec_cols:
                conn.execute("ALTER TABLE specs ADD COLUMN media_type TEXT DEFAULT 'text'")
            # Migrate chunks table — SigLIP image embedding column
            chunk_cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
            if "image_embedding" not in chunk_cols:
                conn.execute("ALTER TABLE chunks ADD COLUMN image_embedding BLOB")
            # Backfill: tag legacy image rows by file extension
            conn.execute("""
                UPDATE documents SET content_type = 'image'
                WHERE (content_type IS NULL OR content_type = 'text')
                AND (source_path LIKE '%.jpg' OR source_path LIKE '%.jpeg'
                     OR source_path LIKE '%.png' OR source_path LIKE '%.webp'
                     OR source_path LIKE '%.gif')
            """)
            # Migrate: graph self-healing soft-delete + generalized correction log
            ent_cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
            if "invalid_at" not in ent_cols:
                conn.execute("ALTER TABLE entities ADD COLUMN invalid_at TIMESTAMP")
            if "invalid_reason" not in ent_cols:
                conn.execute("ALTER TABLE entities ADD COLUMN invalid_reason TEXT")
            if "updated_at" not in ent_cols:
                conn.execute("ALTER TABLE entities ADD COLUMN updated_at TIMESTAMP")
            rel_cols = {r[1] for r in conn.execute("PRAGMA table_info(relationships)").fetchall()}
            if "invalid_at" not in rel_cols:
                conn.execute("ALTER TABLE relationships ADD COLUMN invalid_at TIMESTAMP")
            if "invalid_reason" not in rel_cols:
                conn.execute("ALTER TABLE relationships ADD COLUMN invalid_reason TEXT")
            log_cols = {r[1] for r in conn.execute("PRAGMA table_info(normalization_log)").fetchall()}
            for col, decl in [
                ("action", "TEXT"), ("before_value", "TEXT"), ("after_value", "TEXT"),
                ("actor", "TEXT"), ("reason", "TEXT"), ("model_verdict", "TEXT"),
                ("model_confidence", "REAL"), ("reviewer", "TEXT"),
            ]:
                if col not in log_cols:
                    conn.execute(f"ALTER TABLE normalization_log ADD COLUMN {col} {decl}")
            # Backfill: existing merge-log rows predate `action`
            conn.execute("UPDATE normalization_log SET action = 'merge' WHERE action IS NULL")

            # Advisory verdict columns for the normalization judge. Declared in SCHEMA above AND
            # added here, because `CREATE TABLE IF NOT EXISTS` adds nothing to a table that
            # already exists — so on every pre-existing workspace the judge would hit
            # "no such column: judge_verdict". Exactly the failure the collections rename caused,
            # reached by a different route.
            nrq_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(normalization_review_queue)").fetchall()}
            for col in ("judge_verdict", "judge_confidence", "judge_rationale"):
                if col not in nrq_cols:
                    decl = "REAL" if col == "judge_confidence" else "TEXT"
                    conn.execute(
                        f"ALTER TABLE normalization_review_queue ADD COLUMN {col} {decl}")
            if "judge_attempts" not in nrq_cols:
                conn.execute("ALTER TABLE normalization_review_queue "
                             "ADD COLUMN judge_attempts INTEGER DEFAULT 0")
            # The queue is read by status on every sweep; without this it scans.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_norm_review_pending "
                         "ON normalization_review_queue(status)")

            _migrate_collection_columns(conn)

            # Provenance kind lives on the SILO rows (watched_sources / collections), not
            # on documents — see silo_kind view below. These guarded ALTERs MUST run
            # before that view is created and before backfill_provenance_kind runs: both
            # reference the column directly, and on a pre-existing DB (where the CREATE
            # TABLE above was a no-op) it would not exist yet otherwise.
            ws_cols = {r[1] for r in conn.execute("PRAGMA table_info(watched_sources)").fetchall()}
            if "provenance_kind" not in ws_cols:
                try:
                    conn.execute("ALTER TABLE watched_sources ADD COLUMN provenance_kind TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            coll_cols_pk = {r[1] for r in conn.execute("PRAGMA table_info(collections)").fetchall()}
            if "provenance_kind" not in coll_cols_pk:
                try:
                    conn.execute("ALTER TABLE collections ADD COLUMN provenance_kind TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.executescript(SILO_KIND_VIEW)
            conn.commit()
            # Backfill provenance_kind on any pre-existing silo rows left NULL by the
            # ALTERs above. Idempotent — only ever fills NULLs — so safe to run on every
            # boot.
            backfill_provenance_kind(conn)

            # entity_sources has no primary key, so without this the graph build's
            # per-entity source_count and the trade-route self-joins scan the whole
            # table — tens of seconds on a mid-size graph.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_sources_entity "
                         "ON entity_sources(entity_id)")
            # The reverse lookup — "which entities are in this doc" — is just as hot:
            # the star graph's per-doc n_entities/shared-docs reads filter on
            # document_id, which had no index, forcing a full scan of the largest
            # table on every star-graph load. Index it too (verified via EXPLAIN).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_sources_document "
                         "ON entity_sources(document_id)")
            # Any domain-scoped read (a domain's docs, its entities, its neighbours)
            # filters on domain_path, but the only index is the composite PK
            # (document_id, domain_path), which cannot be seeked by path — so the
            # planner falls back to scanning.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_document_domains_path "
                         "ON document_domains(domain_path)")
            # Sync identity lookups join documents on source_path.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_source_path "
                         "ON documents(source_path)")
            # Per-source silos: normalization scoping filters/joins on silo_id.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_silo "
                         "ON documents(silo_id)")
            # Seed the single snapshot row (dirty, empty) so a writer can flip the bit
            # with a plain UPDATE before the first build has ever run.
            conn.execute("INSERT OR IGNORE INTO graph_snapshot (id, dirty) VALUES ('current', 1)")
            conn.commit()
            # Backfill silo_id on any pre-existing rows left NULL by the ALTER above.
            # Idempotent — only ever fills NULLs — so safe to run on every boot.
            backfill_silo_ids(conn)
        finally:
            conn.close()
        _initialized.add(db_path)


def reset_initialized(db_path: str | None = None) -> None:
    """Test helper: clear the init guard for a path (or all paths)."""
    with _init_lock:
        if db_path is None:
            _initialized.clear()
        else:
            _initialized.discard(db_path)


def _enable_wal(conn: sqlite3.Connection, attempts: int = 6) -> None:
    """Put the connection's database into WAL mode, tolerating a concurrent switcher.

    Changing journal mode needs an exclusive moment, and `busy_timeout` does NOT
    reliably cover it — setting the timeout first (which this still does, and must) was
    necessary but not sufficient. Two processes opening the same not-yet-WAL file, or one
    opening while another holds a write transaction on it, can still get SQLITE_BUSY
    here. Both services open every workspace on startup, so that is the normal case for
    a freshly imported database, not an exotic one.

    Retries briefly, and treats "already WAL" as success: if another process won the
    race, the work is done and there is nothing left to do. Only a persistent failure
    propagates, because operating in rollback-journal mode would silently drop the
    concurrency guarantee the rest of this codebase assumes.
    """
    for attempt in range(attempts):
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if row and str(row[0]).lower() == "wal":
                return
        except sqlite3.OperationalError as e:
            # Only contention is retryable; a genuine problem (an unwritable file, say)
            # must surface now rather than after six pointless sleeps.
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
        # Someone else may have completed the switch while we were blocked.
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            if row and str(row[0]).lower() == "wal":
                return
        except sqlite3.OperationalError:
            pass
        time.sleep(0.05 * (attempt + 1))
    # Out of attempts. `PRAGMA journal_mode=WAL` RETURNS the resulting mode and does not
    # necessarily raise when it could not switch, so a final bare execute would hand back
    # a connection that is quietly still in rollback-journal mode — losing the concurrent
    # reader/writer guarantee the rest of this codebase assumes, with no error anywhere.
    # The returned value is the only reliable signal, so it is what gets checked.
    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    mode = str(row[0]).lower() if row else "unknown"
    if mode != "wal":
        raise sqlite3.OperationalError(
            f"could not enable WAL after {attempts} attempts (journal_mode is {mode!r}); "
            f"another process may hold the database, or it may not be writable")


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout FIRST — necessary but not sufficient, see _enable_wal. A pragma cannot
    # be covered by a timeout that has not been set yet, and the journal-mode switch is
    # itself a contended write on a database not already in WAL.
    conn.execute("PRAGMA busy_timeout=30000")
    _enable_wal(conn)
    return conn


def mark_graph_dirty(conn) -> None:
    """Flag the cached /graph snapshot for rebuild.

    Call after any write that changes the graph. Deliberately a plain UPDATE on a
    row seeded by init_db, so a writer never has to care whether a snapshot exists
    yet. Cheap enough to call unconditionally.
    """
    conn.execute("UPDATE graph_snapshot SET dirty = 1 WHERE id = 'current'")


def recompute_cooccurrence(conn, affected_entity_ids):
    """Rebuild the co_occurs edges touching any of `affected_entity_ids` as a PURE
    PROJECTION of entity_sources (spec 2026-08-14 incremental-source-sync 9). This is
    the SOLE writer of co_occurs rows. Two entities co-occur when they share a chunk;
    weight = number of shared chunks. A document whose document_collections membership
    sets emits_cooccurrence = 0 (a repo/tracker rollup or module summary that mentions
    everything beneath it) contributes NO edges — a doc outside any collection has no
    row and emits by default, matching the write gate in extract_batch. Human-invalidated
    edges (invalid_at NOT NULL) are preserved and never revived. Caller commits.
    """
    if not affected_entity_ids:
        return
    ids = list(dict.fromkeys(affected_entity_ids))
    ph = ",".join("?" * len(ids))
    # 1. Drop the VALID projected rows we're about to rebuild (keep invalidated ones).
    conn.execute(
        f"DELETE FROM relationships WHERE type='co_occurs' AND invalid_at IS NULL "
        f"AND (from_entity IN ({ph}) OR to_entity IN ({ph}))",
        ids + ids,
    )
    # 2. Re-derive from entity_sources over ACTIVE entities and EMITTING documents only.
    #    The emits_cooccurrence gate is honoured on BOTH endpoints: a summary document
    #    mentions everything under it, so ignoring the flag reinstates exactly the
    #    hub-node noise the column was added to remove (see get_collection_routes).
    #    A second, independent gate suppresses by the document's SILO KIND (task 10):
    #    an agent_report silo's docs are opinion, not observation, and must not reshape
    #    shared edge weights. Since the join is on s1.chunk_id = s2.chunk_id (same chunk
    #    => same document), d1.id == d2.id, so checking d1's kind alone suffices; a
    #    correlated subquery on silo_kind avoids a join against a nonexistent alias and
    #    any row-multiplication risk. COALESCE(..., '') means a null/unknown silo emits
    #    by default. (One kind is excluded today; this generalizes to a kind->emits map
    #    if more kinds need gating later.)
    rows = conn.execute(
        f"""
        SELECT s1.entity_id AS a, s2.entity_id AS b, COUNT(DISTINCT s1.chunk_id) AS w
        FROM entity_sources s1
        JOIN entity_sources s2
          ON s1.chunk_id = s2.chunk_id AND s1.entity_id < s2.entity_id
        JOIN entities e1 ON e1.id = s1.entity_id AND e1.invalid_at IS NULL
        JOIN entities e2 ON e2.id = s2.entity_id AND e2.invalid_at IS NULL
        JOIN documents d1 ON d1.id = s1.document_id AND d1.invalid_at IS NULL
        JOIN documents d2 ON d2.id = s2.document_id AND d2.invalid_at IS NULL
        WHERE s1.chunk_id IS NOT NULL
          AND COALESCE((SELECT MIN(emits_cooccurrence) FROM document_collections
                        WHERE document_id = s1.document_id), 1) = 1
          AND COALESCE((SELECT MIN(emits_cooccurrence) FROM document_collections
                        WHERE document_id = s2.document_id), 1) = 1
          AND COALESCE((SELECT kind FROM silo_kind WHERE silo_id = d1.silo_id), '') != 'agent_report'
          AND (s1.entity_id IN ({ph}) OR s2.entity_id IN ({ph}))
        GROUP BY a, b
        """,
        ids + ids,
    ).fetchall()
    for r in rows:
        a, b, w = r["a"], r["b"], r["w"]
        # Skip if a human-invalidated edge exists for this pair (either endpoint order).
        if conn.execute(
            "SELECT 1 FROM relationships WHERE type='co_occurs' AND invalid_at IS NOT NULL "
            "AND ((from_entity=? AND to_entity=?) OR (from_entity=? AND to_entity=?)) LIMIT 1",
            (a, b, b, a),
        ).fetchone():
            continue
        conn.execute(
            "INSERT INTO relationships (id, from_entity, to_entity, type, weight, source_chunk) "
            "VALUES (?, ?, ?, 'co_occurs', ?, NULL)",
            (str(uuid.uuid4()), a, b, w),
        )


def backfill_silo_ids(conn) -> int:
    """Derive documents.silo_id for any row left NULL: source_id wins, else the
    collection the document belongs to (if any), else NULL. Pure and idempotent —
    only ever fills NULLs — so it is safe to call on every init_db (spec: per-source
    silos + provenance, task 1)."""
    conn.execute("UPDATE documents SET silo_id = source_id "
                 "WHERE silo_id IS NULL AND source_id IS NOT NULL")
    conn.execute("""UPDATE documents SET silo_id = (
                        SELECT dc.collection_id FROM document_collections dc
                        WHERE dc.document_id = documents.id LIMIT 1)
                     WHERE silo_id IS NULL AND source_id IS NULL
                       AND EXISTS (SELECT 1 FROM document_collections dc2 WHERE dc2.document_id = documents.id)""")
    conn.commit()
    return conn.total_changes


def backfill_provenance_kind(conn) -> int:
    """Fill NULL provenance_kind on existing silo rows (watched_sources, collections)
    from the flow-default map keyed by source type. Built from FLOW_DEFAULT_KIND itself
    (one CASE per table) rather than hand-duplicated SQL, so the two stay in sync.
    Pure and idempotent — only ever fills NULLs — so it is safe to call on every
    init_db (spec: per-source silos + provenance, task 9)."""
    case = " ".join(f"WHEN '{t}' THEN '{k}'" for t, k in FLOW_DEFAULT_KIND.items())
    conn.execute(f"""UPDATE watched_sources SET provenance_kind = (CASE type {case} END)
                     WHERE provenance_kind IS NULL""")
    conn.execute(f"""UPDATE collections SET provenance_kind = (CASE kind {case} END)
                     WHERE provenance_kind IS NULL""")
    conn.commit()
    return conn.total_changes
