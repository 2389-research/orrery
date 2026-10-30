# ABOUTME: CRUD for watched_sources — the registry of vaults/repos the worker re-syncs.
# ABOUTME: Thin rows: "which sources, at what cadence". The worker sweep does the scanning.
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import get_auth_store, require_writable, AuthStore
from ..pipeline.silo import flow_default_kind, resolve_kind

router = APIRouter()


class WatchedSourceCreate(BaseModel):
    """Register a source the worker re-syncs.

    `config_json` is a per-type featurizer config. For a **vault** (Obsidian-style note
    directory) the worker's vault featurizer reads:
      - `ext`: list of note extensions to ingest (default `[".md"]`); leading dot optional.
      - `ignore`: extra directory *names* to prune, on top of the built-in defaults
        (any dotfolder — so `.obsidian`/`.trash` are always skipped — plus `.git`,
        `node_modules`, `__pycache__`, and binary/attachment suffixes). Text-only MVP:
        attachments (PDF/image) are intentionally skipped.
      - `folder_domains` (bool, default off): when true, each note's parent folder path
        becomes its domain (lowercase `/`-separated), skipping LLM classification. Left
        opt-in so existing imports keep today's classify behavior.
    Parsed YAML frontmatter is stripped from the body and carried on the document's
    `metadata` (provenance); wikilinks/embeds/comments are cleaned before extraction.

    Known limitations (see plan 2026-08-17-obsidian-vault-import-41):
      - Renaming a note changes its `source_path` -> treated as delete + create (no move
        detection).
      - `source_path` is the mounted absolute path -> remounting the vault elsewhere
        re-ingests everything. Relativization is a spine decision, tracked separately.
      - Attachments (PDF/image) are not ingested (text-only).
    """
    type: str                      # 'vault' | 'repo'
    uri: str
    noosphere: str | None = None
    cadence_hours: float = 24
    config_json: dict | None = None
    provenance_kind: str | None = None  # override; falls back to the flow default for `type`


class WatchedSourcePatch(BaseModel):
    enabled: bool | None = None
    cadence_hours: float | None = None
    config: dict | None = None  # shallow-merged into config_json; a null value removes the key


def _to_dict(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    if d.get("config_json"):
        try:
            d["config_json"] = json.loads(d["config_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def _enqueue_scan(store, source_id: str) -> dict:
    """Enqueue a scan_source job for a source (idempotent: skip if one is already
    queued/running). This is the ONE invocation primitive — the ingestion flow
    (vault / repo / any future type) is resolved by the source's `type` in the worker,
    so the trigger stays type-agnostic and new flows plug in behind it unchanged."""
    existing = store.jobs.get_existing("scan_source", source_id, ["queued", "running"])
    if existing:
        return {"source_id": source_id, "job_id": existing.id, "status": "already_pending"}
    job_id = str(uuid.uuid4())
    store.jobs.create(job_id, "scan_source", source_id, {"source_id": source_id})
    return {"source_id": source_id, "job_id": job_id, "status": "queued"}


@router.post("/watched-sources")
def create_watched_source(body: WatchedSourceCreate, auth: AuthStore = Depends(require_writable)):
    store = auth.store
    source_id = str(uuid.uuid4())
    kind = resolve_kind(flow_default_kind(body.type), body.provenance_kind)
    store.conn.execute(
        "INSERT INTO watched_sources (id, type, uri, noosphere, cadence_hours, config_json, "
        "provenance_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, body.type, body.uri, body.noosphere, body.cadence_hours,
         json.dumps(body.config_json) if body.config_json is not None else None, kind),
    )
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM watched_sources WHERE id = ?", (source_id,)).fetchone()
    store.close()
    return _to_dict(row)


@router.get("/watched-sources")
def list_watched_sources(auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    rows = store.conn.execute(
        "SELECT * FROM watched_sources ORDER BY created_at DESC").fetchall()
    store.close()
    return [_to_dict(r) for r in rows]


@router.post("/watched-sources/scan-due")
def scan_due_sources(force: bool = False, auth: AuthStore = Depends(require_writable)):
    """Enqueue a scan for every enabled source whose cadence is due (force=true → all
    enabled, ignoring cadence). This is the same selection the worker's timer sweep uses,
    exposed so a cron job (or a single endpoint call) can drive ingestion on ANY external
    schedule — the invocation surface is independent of how often the worker's own timer
    ticks. Idempotent per source."""
    store = auth.store
    if force:
        rows = store.conn.execute("SELECT id FROM watched_sources WHERE enabled = 1").fetchall()
    else:
        rows = store.conn.execute(
            "SELECT id FROM watched_sources WHERE enabled = 1 AND "
            "(last_scanned_at IS NULL OR "
            " (julianday('now') - julianday(last_scanned_at)) * 24 >= cadence_hours)").fetchall()
    triggered = [_enqueue_scan(store, r["id"]) for r in rows]
    store.close()
    return {"triggered": triggered}


@router.post("/watched-sources/{source_id}/scan")
def scan_watched_source(source_id: str, auth: AuthStore = Depends(require_writable)):
    """Enqueue a scan for one source now, regardless of cadence. Type-agnostic: the
    worker resolves the ingestion flow from the source's `type`."""
    store = auth.store
    if not store.conn.execute("SELECT 1 FROM watched_sources WHERE id = ?", (source_id,)).fetchone():
        store.close()
        raise HTTPException(status_code=404, detail="watched source not found")
    result = _enqueue_scan(store, source_id)
    store.close()
    return result


@router.patch("/watched-sources/{source_id}")
def patch_watched_source(source_id: str, body: WatchedSourcePatch,
                         auth: AuthStore = Depends(require_writable)):
    store = auth.store
    if not store.conn.execute(
            "SELECT 1 FROM watched_sources WHERE id = ?", (source_id,)).fetchone():
        store.close()
        raise HTTPException(status_code=404, detail="watched source not found")

    sets, params = [], []
    if body.enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if body.enabled else 0)
    if body.cadence_hours is not None:
        sets.append("cadence_hours = ?")
        params.append(body.cadence_hours)
    if body.config is not None:
        existing = store.conn.execute(
            "SELECT config_json FROM watched_sources WHERE id = ?", (source_id,)).fetchone()[0]
        try:
            merged = json.loads(existing) if existing else {}
        except (json.JSONDecodeError, TypeError):
            merged = None
        if not isinstance(merged, dict):
            store.close()
            raise HTTPException(
                status_code=422,
                detail="existing config_json is not a JSON object; refusing to overwrite it")
        for k, v in body.config.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        sets.append("config_json = ?")
        params.append(json.dumps(merged))
    if sets:
        params.append(source_id)
        store.conn.execute(
            f"UPDATE watched_sources SET {', '.join(sets)} WHERE id = ?", params)
        store.conn.commit()

    row = store.conn.execute("SELECT * FROM watched_sources WHERE id = ?", (source_id,)).fetchone()
    store.close()
    return _to_dict(row)
