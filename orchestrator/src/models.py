from pydantic import BaseModel
from datetime import datetime

class DocumentSummary(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime
    domains: list[str] = []
    entity_count: int = 0

class DocumentDetail(BaseModel):
    id: str
    title: str
    source_path: str | None
    content: str
    metadata: dict | None
    status: str
    created_at: datetime
    domains: list[dict] = []
    entities: list[dict] = []

class DomainInfo(BaseModel):
    id: str
    path: str
    parent_path: str | None
    document_count: int
    spec_version: int | None
    children: list["DomainInfo"] = []

class EntitySummary(BaseModel):
    id: str
    canonical_name: str
    type: str
    source_count: int = 0
    domains: list[str] = []

class EntityDetail(BaseModel):
    id: str
    canonical_name: str
    type: str
    created_at: datetime
    sources: list[dict] = []
    merge_history: list[str] = []

class JobInfo(BaseModel):
    id: str
    type: str
    target: str
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

class Stats(BaseModel):
    document_count: int
    entity_count: int
    domain_count: int
    active_jobs: int
    image_count: int = 0

class IngestResult(BaseModel):
    document_id: str
    title: str
    domains: list[str]
    entity_count: int
    jobs_queued: list[str]
    content_type: str = "text"

class DirectoryIngestRequest(BaseModel):
    path: str


class TextIngestRequest(BaseModel):
    """Ingest a document from raw text (no file upload) — the JSON entry point used by
    programmatic callers and the MCP `ingest_text` tool. The text IS the source, so no
    raw artifact is stored (source_path is None)."""
    title: str
    content: str


class RepoIngestRequest(BaseModel):
    """A git checkout to summarize into a collection of code_intent documents.

    `path` is a server-side directory, not an upload: the repo is read where it sits
    and only the SUMMARIES are stored (the graph is a map over the code, not a copy
    of it). `name` becomes the collection's name and its unique `path` key.
    """
    path: str
    name: str
    provenance_kind: str | None = None  # override; falls back to the flow default for
                                         # a git_repo collection if absent/invalid


class PdfIngestRequest(BaseModel):
    """A server-side PDF to ingest as an ordered chain of page-documents."""
    path: str
    name: str
    vision_mode: str = "always"          # always | fallback | off
    provenance_kind: str | None = None


class TrackerRunsIngestRequest(BaseModel):
    """A corpus of tracker code-gen runs. One run becomes one collection.

    `path` is either a directory of pre-made per-run summary JSONs (recognised by an
    `index.json`, needing no model calls at all) or a raw corpus the worker summarizes
    itself. `chain` states the trajectory order explicitly; without it the worker falls
    back to the ground-truth rung order. `runs_dir` is where the raw artifacts are
    staged so `documents.source_path` resolves in-container.
    """
    path: str
    chain: list[str] | None = None
    runs_dir: str | None = None


class CcvaultIngestRequest(BaseModel):
    """A staged ccvault session archive to ingest into the ACTIVE (target) noosphere.

    `path` is a server-side location holding ccvault's output — a `ccvault.db` file or a
    directory containing one (staged the way repos stage under /data/repos). Ingestion is
    incremental and idempotent per workspace via the ccvault_* ledgers, so re-pointing at
    the same archive only picks up new sessions / unseen query_ids.

    IMPORTANT: target a CLONE of the source noosphere (see docs/ccvault-ingestion.md) so
    the [entity:…] ids in sessions resolve — never a formal/full noosphere. `label` names
    the single persistent ccvault collection in this workspace (get-or-create).
    """
    path: str
    label: str = "ccvault"
    provenance_kind: str | None = None  # override; defaults to agent_report for a ccvault silo
