# ABOUTME: Repo adapter for incremental sync — summarize a repo into leaf/group/root docs.
# ABOUTME: Reuses the spine: upsert_document per artifact, apply_deletions for gone files.
"""The repo adapter (spec 2026-08-14 incremental-source-sync §15 / Spec 2).

A repo source enumerates the working tree via codesum into three doc roles — `file`
(leaf, per-file intent, emits co-occurrence), `module` (group), `repo` (root rollup);
only leaves emit. Each artifact is upserted through the shared primitive with repo-shaped
params, so identity, change detection, projection, and soft-delete all come from the spine.

This is the FULL-TREE version: it re-summarizes the whole repo each scan. Per-file
content_hash still makes re-extraction of unchanged summaries a no-op (skip), but codesum
itself runs over everything. The git-diff short-circuit (only re-featurize changed files)
and threshold-gated module/root regeneration are layered on top of this in follow-ups.
"""
import asyncio
import json
import os
import subprocess
import uuid

from orrery_codesum import summarize_repo_incremental, make_summarize_fn

from ..classifier import classify_document
from ..silo import flow_default_kind, resolve_kind
from .upsert_document import upsert_document, _recount_collection
from .scan_source import apply_deletions
from .ingest_repo import _git_coordinates

_UNCLASSIFIED = "unclassified/needs-review"
_ROLE = {"file": "leaf", "module": "group", "repo": "root"}
_REPO_PATH = "."   # codesum's relpath sentinel for the repo/root artifact


def _git_changed_files(root_path: str, base_sha: str) -> tuple[set | None, set]:
    """(changed, deleted) file relpaths between base_sha and HEAD via git. `changed` is
    None when the diff can't be computed (→ caller does a full re-summarize)."""
    try:
        # --relative makes git emit paths relative to root_path, matching codesum's
        # relpaths, even when root_path is a subdirectory of the git repo.
        # `-c safe.directory=<root_path>` or the call fails in Docker: the checkout
        # arrives on a mount owned by the host user while this process runs as another
        # uid, and git refuses a repo it sees as someone else's ("dubious ownership").
        # Scoped to this directory, not the global `*` (repo-local config is executable
        # surface). Same reasoning as ingest_repo._git_coordinates.
        out = subprocess.run(
            ["git", "-c", f"safe.directory={root_path}", "-C", root_path,
             "diff", "--name-status", "--relative", f"{base_sha}..HEAD"],
            capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None, set()
    if out.returncode != 0:
        return None, set()
    changed, deleted = set(), set()
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:   # rename: old gone, new added
            deleted.add(parts[1]); changed.add(parts[2])
        elif status.startswith("D"):
            deleted.add(parts[1])
        else:                                             # A / M / C / T ...
            changed.add(parts[1])
    return changed, deleted


def _build_cache(conn, source_id: str, root_path: str) -> dict:
    """{relpath -> stored intent} for every doc this source currently owns — the prior
    summaries the incremental traversal reuses for unchanged nodes."""
    cache = {}
    for r in conn.execute(
            "SELECT source_path, content FROM documents WHERE source_id = ? AND invalid_at IS NULL",
            (source_id,)).fetchall():
        rel = os.path.relpath(r["source_path"], root_path)
        cache[rel if rel != "." else _REPO_PATH] = r["content"]
    return cache


def _git_head_sha(root_path: str) -> str | None:
    """Current HEAD sha, or None if `root_path` is not a resolvable git checkout.
    Used to short-circuit a scan when the working tree hasn't moved since last sync."""
    try:
        # `-c safe.directory=<root_path>` — see the note in _git_changed_files: without
        # it every git call fails on a mounted checkout owned by a different uid.
        out = subprocess.run(["git", "-c", f"safe.directory={root_path}", "-C", root_path,
                              "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — git missing / not a repo -> no short-circuit
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _resolve_collection(conn, source_id: str, root_path: str, source_config: dict) -> tuple[str, str]:
    """Return (collection_id, collection_path). Keyed on the WATCHED SOURCE, not the
    basename — two repos can share a basename (e.g. two `utils` dirs) and must not
    collide into one collection. The collection id is recorded in the source's
    config_json so later scans reuse it."""
    stored_id = source_config.get("_collection_id")
    if stored_id:
        row = conn.execute("SELECT id, path FROM collections WHERE id = ?", (stored_id,)).fetchone()
        if row:
            return row["id"], row["path"]

    base = source_config.get("collection") or os.path.basename(os.path.normpath(root_path))
    # `collections.path` is UNIQUE; suffix with the source id if the basename is taken.
    coll_path = base
    if conn.execute("SELECT 1 FROM collections WHERE path = ?", (coll_path,)).fetchone():
        coll_path = f"{base}-{source_id[:8]}"
    collection_id = str(uuid.uuid4())
    # Documents from this source carry `source_id`, so their silo resolves through
    # watched_sources.provenance_kind (source_id wins over collection_id — see
    # silo.resolve_silo_id). Stamped here too for consistency and for any reader that
    # looks at the collection directly.
    stamped_kind = resolve_kind(flow_default_kind("git_repo"), source_config.get("provenance_kind"))
    conn.execute(
        "INSERT INTO collections (id, name, path, root_path, kind, provenance_kind) "
        "VALUES (?, ?, ?, ?, 'git_repo', ?)",
        (collection_id, base, coll_path, root_path, stamped_kind))
    source_config["_collection_id"] = collection_id
    conn.execute("UPDATE watched_sources SET config_json = ? WHERE id = ?",
                 (json.dumps(source_config), source_id))
    conn.commit()
    return collection_id, coll_path


async def _repo_domain(conn, relay, settings, ws, source_config, source_id,
                       coll_path, artifacts) -> str:
    """Classify the repo ONCE on its grounded root summary, then reuse. Stored in
    config_json under a reserved key so later scans don't reclassify (and churn domains)."""
    if source_config.get("_domain"):
        return source_config["_domain"]
    root_summary = next((a["intent"] for a in artifacts if a["level"] == "repo"), "")
    taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains").fetchall()]
    try:
        classification = await classify_document(
            relay=relay, title=coll_path,
            excerpt=f"Repository: {coll_path}\n\n{root_summary}",
            existing_taxonomy=taxonomy, model=settings.classification_model)
        domain_path = classification.get("primary_domain") or _UNCLASSIFIED
    except Exception as e:  # noqa: BLE001 — keep the artifacts even if classification fails
        print(f"[sync_repo] classify failed ({type(e).__name__}: {e}); filing under {_UNCLASSIFIED}", flush=True)
        domain_path = _UNCLASSIFIED
    source_config["_domain"] = domain_path
    conn.execute("UPDATE watched_sources SET config_json = ? WHERE id = ?",
                 (json.dumps(source_config), source_id))
    conn.commit()
    return domain_path


async def sync_repo(conn, relay, settings, ws, source_config, source_id) -> dict:
    root_path = ws["uri"]
    collection_id, coll_path = _resolve_collection(conn, source_id, root_path, source_config)

    # Source-level short-circuit (spec §4/§15): if HEAD hasn't moved since the last
    # successful sync, nothing changed — skip codesum entirely (zero model calls). The
    # commit_sha doubles as the git provenance ref, so storing it here keeps that fresh.
    head_sha = _git_head_sha(root_path)
    stored = conn.execute(
        "SELECT commit_sha, remote_url FROM collections WHERE id = ?", (collection_id,)).fetchone()
    stored_sha = stored["commit_sha"] if stored else None
    stored_remote = stored["remote_url"] if stored else None
    if head_sha and stored_sha and head_sha == stored_sha:
        # Nothing to re-summarize (zero model calls), but backfill git provenance for
        # collections synced BEFORE remote_url was captured: the short-circuit is the only
        # path a static-HEAD repo ever takes, so a missing remote_url would otherwise never
        # be filled (defeating the fetchable-ref lookup in sqlite_store). One-shot, cheap,
        # and model-call-free — once populated this branch is skipped.
        if stored_remote is None:
            remote_url, commit_sha = _git_coordinates(root_path)
            if commit_sha and remote_url:
                conn.execute("UPDATE collections SET commit_sha = ?, remote_url = ? WHERE id = ?",
                             (commit_sha, remote_url, collection_id))
        # Heal document_count on unchanged-HEAD scans too: repos synced before this recount
        # existed read 0 despite having docs, and their HEAD may never move again.
        _recount_collection(conn, collection_id)
        conn.commit()
        return {"actions": {"created": 0, "updated": 0, "skipped": 0, "conflict": 0},
                "deleted": 0, "unchanged": True}

    # Partial re-ingestion (spec §15): reuse cached summaries and re-featurize only the
    # files git says changed since the last synced sha. changed=None (no prior sha, or a
    # diff we can't compute) falls back to a full pass. Deleted files force their parent
    # module/root to re-summarize (their summary still mentions the gone file).
    cache = _build_cache(conn, source_id, root_path)
    changed = None
    force_modules: set[str] = set()
    if stored_sha and head_sha:
        changed, deleted = _git_changed_files(root_path, stored_sha)
        for d in deleted:
            parent = os.path.dirname(d)
            force_modules.add(parent if parent else _REPO_PATH)
    module_change_ratio = float(source_config.get("module_change_ratio", 0.0))

    # codesum traversal is synchronous and issues one blocking model call per re-summarized
    # node — run it off the event loop so the poll loop keeps ticking.
    summarize_fn = make_summarize_fn(relay, settings.extraction_model)
    artifacts = await asyncio.to_thread(
        summarize_repo_incremental, root_path, summarize_fn, coll_path,
        cache=cache, changed=changed, force_modules=force_modules,
        module_change_ratio=module_change_ratio)

    domain_path = await _repo_domain(conn, relay, settings, ws, source_config, source_id,
                                     coll_path, artifacts)

    seen_paths: set[str] = set()
    actions = {"created": 0, "updated": 0, "skipped": 0, "conflict": 0}
    for a in artifacts:
        role = _ROLE.get(a["level"], "leaf")
        source_path = os.path.join(root_path, a["path"])
        seen_paths.add(source_path)
        res = await upsert_document(
            conn, relay, settings, source_path=source_path, title=a["path"],
            content=a["intent"], source_id=source_id, collection_id=collection_id,
            role=role, parent_path=a.get("parent_path"), content_type="code_intent",
            domain_path=domain_path, classify=False, pre_chunked=True,
            emits_cooccurrence=(role == "leaf"))
        actions[res["action"]] = actions.get(res["action"], 0) + 1

    deleted = apply_deletions(conn, source_id, seen_paths)

    # Record provenance so the graph can hand an agent a resolvable ref back to the exact
    # code these summaries describe — the whole point of the code-as-index model. Reuses
    # the one-shot ingest path's helper (clean-tree + toplevel==root + scoped
    # safe.directory), which returns BOTH remote_url and commit_sha, all-or-nothing.
    remote_url, commit_sha = _git_coordinates(root_path)
    if commit_sha:
        conn.execute("UPDATE collections SET commit_sha = ?, remote_url = ? WHERE id = ?",
                     (commit_sha, remote_url, collection_id))
    elif head_sha:
        # No full provenance (dirty tree / no origin / nested checkout), but HEAD is still
        # valid for the next scan's short-circuit comparison.
        conn.execute("UPDATE collections SET commit_sha = ? WHERE id = ?", (head_sha, collection_id))

    _recount_collection(conn, collection_id)
    conn.commit()
    return {"actions": actions, "deleted": deleted}
