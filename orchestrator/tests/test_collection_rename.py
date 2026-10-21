"""repos/* -> collections/* — the in-place rename of the collection layer.

NOTE: this is the ONE file that must keep spelling the old names. Its fixture builds a
repo-era database on purpose, so a global rename sweep across the test suite will
silently gut it — rewriting the fixture into the new schema and inverting the
assertions into tautologies. If you are renaming things, skip this file.

The migration has real blast radius: it renames live tables holding every existing
corpus. The ordering constraint is the whole story — `migrate_to_collections` MUST run
before `executescript(SCHEMA)`, or `CREATE TABLE IF NOT EXISTS collections` creates an
empty table beside the populated `repos`, the rename fails, and every corpus is
stranded behind an empty one. Silently: reads would just return nothing.
"""

import sqlite3

import pytest

import src.db as db

_OLD = {"repos", "document_repos", "repo_edges"}
_NEW = {"collections", "document_collections", "collection_edges"}


@pytest.fixture
def repo_era_db(tmp_path):
    """A DB in the pre-rename shape, populated, ready to migrate.

    Hand-written rather than derived from the current schema: this is the one migration
    whose *source* shape no longer exists in the codebase, so it has to be spelled out.
    Mirrors the real repo-era schema including the FK references, which the rename has
    to carry across.
    """
    p = str(tmp_path / "repo_era.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, content TEXT,
                                content_hash TEXT, source_path TEXT,
                                content_type TEXT DEFAULT 'text', status TEXT,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE repos (
            id TEXT PRIMARY KEY, name TEXT, path TEXT UNIQUE, root_path TEXT,
            parent_path TEXT, document_count INTEGER DEFAULT 0,
            remote_url TEXT, commit_sha TEXT, kind TEXT DEFAULT 'git_repo',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE document_repos (
            document_id TEXT REFERENCES documents(id),
            repo_id TEXT REFERENCES repos(id),
            level TEXT, parent_path TEXT, role TEXT,
            emits_cooccurrence INTEGER DEFAULT 1,
            PRIMARY KEY (document_id, repo_id));
        CREATE INDEX idx_document_repos_repo ON document_repos(repo_id);
        CREATE TABLE repo_edges (
            from_repo TEXT REFERENCES repos(id), to_repo TEXT REFERENCES repos(id),
            type TEXT, weight REAL DEFAULT 1.0,
            PRIMARY KEY (from_repo, to_repo, type));

        INSERT INTO repos (id, name, path, root_path, kind) VALUES
            ('c1', 'demo', 'demo', '/demo', 'git_repo'),
            ('c2', 'run1', 'run1', '/run1', 'tracker_run');
        INSERT INTO documents (id, title, content_hash) VALUES
            ('d1', 'pkg', 'h1'), ('d2', 'a.py', 'h2');
        INSERT INTO document_repos (document_id, repo_id, role, parent_path,
                                    emits_cooccurrence) VALUES
            ('d1', 'c1', 'group', '.', 0),
            ('d2', 'c1', 'leaf', 'pkg', 1);
        INSERT INTO repo_edges (from_repo, to_repo, type, weight) VALUES
            ('c1', 'c2', 'repo_uses', 2.0);
    """)
    c.commit()
    c.close()
    return p


def _migrate(path):
    db._initialized.clear()          # init_db memoizes per process
    db.init_db(path)
    return db.get_connection(path)


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_tables_are_renamed_and_the_old_names_are_gone(repo_era_db):
    names = _tables(_migrate(repo_era_db))
    assert _NEW <= names
    assert not (_OLD & names), \
        "an old table surviving means the schema script created an empty sibling"


def test_every_row_survives_the_rename(repo_era_db):
    """The failure this guards against is silent: a stranded corpus reads as empty."""
    conn = _migrate(repo_era_db)
    assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM document_collections").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM collection_edges").fetchone()[0] == 1
    # ...and the values, not just the counts.
    rows = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM collections")}
    assert rows == {"c1": "git_repo", "c2": "tracker_run"}


def test_columns_are_renamed(repo_era_db):
    conn = _migrate(repo_era_db)
    dc = {r[1] for r in conn.execute("PRAGMA table_info(document_collections)")}
    assert "collection_id" in dc and "repo_id" not in dc
    ce = {r[1] for r in conn.execute("PRAGMA table_info(collection_edges)")}
    assert {"source", "target"} <= ce and not ({"from_repo", "to_repo"} & ce)
    # The data moved with the column, not just the header.
    edge = conn.execute("SELECT source, target, type, weight FROM collection_edges").fetchone()
    # `repo_uses` is rewritten to the contract value `uses` by the same migration —
    # the discriminator was repo-era spelling for what the payload always called `uses`.
    assert tuple(edge) == ("c1", "c2", "uses", 2.0)
    assert conn.execute("SELECT collection_id FROM document_collections "
                        "WHERE document_id = 'd2'").fetchone()[0] == "c1"


def test_foreign_keys_follow_the_rename(repo_era_db):
    """SQLite rewrites REFERENCES clauses on RENAME TO (>=3.25, legacy_alter_table off).
    If that ever regressed the FK would dangle at a table that no longer exists, and
    only bite later under PRAGMA foreign_keys."""
    conn = _migrate(repo_era_db)
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='document_collections'").fetchone()[0]
    # SQLite QUOTES the identifier it rewrites — REFERENCES "collections"(id) — so
    # normalise before matching rather than assuming the bare form.
    normalised = ddl.replace('"collections"', "collections")
    assert "REFERENCES collections(id)" in normalised
    assert "repos" not in ddl


def test_the_index_is_reindexed_under_the_new_name(repo_era_db):
    """The index survives RENAME TO but keeps its old name; the schema script recreates
    it. Without the explicit DROP a DB would carry both."""
    conn = _migrate(repo_era_db)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='document_collections'")}
    assert "idx_document_collections_collection" in idx
    assert "idx_document_repos_repo" not in idx


def test_migration_is_idempotent(repo_era_db):
    """It runs on every open. A second pass must be a no-op, not a second rename."""
    def counts():
        c = db.get_connection(repo_era_db)
        return [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(_NEW)]

    first_tables = _tables(_migrate(repo_era_db))
    first_counts = counts()
    assert _tables(_migrate(repo_era_db)) == first_tables
    assert counts() == first_counts


def test_a_fresh_db_needs_no_rename(tmp_path):
    p = str(tmp_path / "fresh.db")
    db._initialized.clear()
    db.init_db(p)
    names = _tables(db.get_connection(p))
    assert _NEW <= names
    assert not (_OLD & names)


def test_the_kind_backfill_still_runs_after_a_rename(repo_era_db):
    """The two migrations compose: the rename happens first, then the kind recovery
    reads `collection_edges` under its new name. A chain_next edge written in the
    repo-era schema must still be recognised."""
    c = sqlite3.connect(repo_era_db)
    c.execute("INSERT INTO repos (id, name, path, root_path) VALUES ('c3','run2','run2','/run2')")
    c.execute("INSERT INTO repo_edges (from_repo, to_repo, type, weight) "
              "VALUES ('c2', 'c3', 'chain_next', 1.0)")
    c.commit()
    c.close()

    conn = _migrate(repo_era_db)
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM collections")}
    assert kinds["c3"] == "tracker_run", "kind recovery must read the RENAMED edge table"
    assert kinds["c1"] == "git_repo"


def test_the_repo_era_edge_discriminator_is_rewritten(repo_era_db):
    """`repo_uses` was the repo-era spelling of what the contract exposes as `uses`.

    Leaving it in place would keep a repository-specific value inside a table that is
    no longer about repositories, and the payload would go on translating it forever.
    Idempotent: a second pass matches nothing.
    """
    conn = _migrate(repo_era_db)
    types = {r[0] for r in conn.execute("SELECT DISTINCT type FROM collection_edges")}
    assert types == {"uses"}
    assert _migrate(repo_era_db).execute(
        "SELECT COUNT(*) FROM collection_edges WHERE type = 'repo_uses'").fetchone()[0] == 0


def test_a_half_renamed_database_is_refused_rather_than_half_read(tmp_path):
    """Both `repos` and `collections` present means rows live under each name.

    The schema script is about to make `collections` authoritative, which would orphan
    everything still in `repos` — silently, since reads would simply not find them.
    There is no safe automatic merge (ids can collide), so stop loudly.
    """
    p = str(tmp_path / "half.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE repos (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
        INSERT INTO repos VALUES ('stranded', 'would-go-dark');
    """)
    c.commit()
    c.close()

    db._initialized.clear()
    with pytest.raises(RuntimeError, match="both names exist"):
        db.init_db(p)


def test_duplicate_edge_spellings_are_folded_not_crashed(tmp_path):
    """A pair carrying BOTH `repo_uses` and `uses` must not break the migration.

    Reachable after a mixed-version deploy — one process still writing `repo_uses`
    while another writes `uses`. A bare `UPDATE ... SET type='uses'` then violates the
    composite key (source, target, type), `init_db` raises IntegrityError, and the
    database stops opening AT ALL. Verified to reproduce before the fold was added.
    """
    p = str(tmp_path / "dupes.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, content TEXT,
                                content_hash TEXT, source_path TEXT,
                                content_type TEXT DEFAULT 'text', status TEXT,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE repos (id TEXT PRIMARY KEY, name TEXT, path TEXT UNIQUE,
                            root_path TEXT, parent_path TEXT,
                            document_count INTEGER DEFAULT 0, remote_url TEXT,
                            commit_sha TEXT, kind TEXT DEFAULT 'git_repo',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE document_repos (document_id TEXT, repo_id TEXT, level TEXT,
                                     parent_path TEXT, role TEXT,
                                     emits_cooccurrence INTEGER DEFAULT 1,
                                     PRIMARY KEY (document_id, repo_id));
        CREATE TABLE repo_edges (from_repo TEXT, to_repo TEXT, type TEXT,
                                 weight REAL DEFAULT 1.0,
                                 PRIMARY KEY (from_repo, to_repo, type));
        INSERT INTO repos (id, name, path, root_path) VALUES
            ('a', 'a', 'a', '/a'), ('b', 'b', 'b', '/b');
        INSERT INTO repo_edges VALUES ('a', 'b', 'repo_uses', 2.0),
                                      ('a', 'b', 'uses', 5.0);
    """)
    c.commit()
    c.close()

    conn = _migrate(p)
    rows = conn.execute("SELECT source, target, type, weight FROM collection_edges").fetchall()
    assert [tuple(r) for r in rows] == [("a", "b", "uses", 5.0)], \
        "the pair folds to one row, keeping the larger weight"


def test_a_conflict_on_a_later_table_leaves_nothing_renamed(tmp_path):
    """The conflict check preflights every pair before the first rename.

    Raising mid-loop would move `repos` and then refuse on `repo_edges`, leaving a
    half-renamed database — a worse state than the one being refused, and one that
    fails identically on every retry.
    """
    p = str(tmp_path / "late_conflict.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE repos (id TEXT PRIMARY KEY);
        CREATE TABLE repo_edges (source TEXT);
        CREATE TABLE collection_edges (source TEXT);
    """)
    c.commit()
    c.close()

    db._initialized.clear()
    with pytest.raises(RuntimeError, match="both names exist"):
        db.init_db(p)

    after = _tables(sqlite3.connect(p))
    assert "repos" in after, "the earlier table must NOT have been renamed"
    assert "collections" not in after


def test_a_failure_part_way_through_rolls_the_whole_migration_back(repo_era_db):
    """The migration is atomic, so a failure leaves the database exactly as it was.

    Without the savepoint an error after the first `ALTER TABLE` leaves `repos` renamed
    and `repo_edges` not — the same half-migrated shape the preflight refuses to start
    from, reached by a different route and with no way to retry out of it: the next
    open would see both `collections` and `repo_edges` and raise forever.
    """
    class FailOnce:
        """Delegates to the real connection, failing exactly once on the Nth execute.

        Only once: the rollback runs through this same object, so failing every
        subsequent call would break the cleanup being tested.
        """
        def __init__(self, conn, n):
            self._conn, self._n, self._calls = conn, n, 0

        def execute(self, sql, *args):
            self._calls += 1
            if self._calls == self._n:
                raise sqlite3.OperationalError("simulated mid-migration failure")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            # Proxy everything else (in_transaction, rollback, commit, ...). Overriding
            # only `execute` made the double break as soon as the code under test used
            # any other part of the connection API — a property of the double, not of
            # the behaviour being tested.
            return getattr(self._conn, name)

    conn = sqlite3.connect(repo_era_db)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("repos", "document_repos", "repo_edges")}

    # Fail on the 5th execute: BEGIN IMMEDIATE, SAVEPOINT, the table scan, the FIRST
    # rename, then boom. The count matters — the point is to fail with one rename
    # already applied, so the rollback has something to undo. At 4 the failure lands on
    # the first rename instead of after it, nothing is half-applied, and the assertions
    # below pass while testing nothing.
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        db.migrate_to_collections(FailOnce(conn, 5))

    after = _tables(conn)
    assert _OLD <= after, "every legacy table must still be there"
    assert not (_NEW & after), "no half-applied rename may survive"
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("repos", "document_repos", "repo_edges")} == before
    conn.close()

    # ...and the database is still migratable afterwards, not wedged.
    migrated = _migrate(repo_era_db)
    assert _NEW <= _tables(migrated)
    assert migrated.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == before["repos"]
