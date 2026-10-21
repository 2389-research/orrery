"""Abstract repository interfaces.

Each interface defines the operations needed by the application layer.
SQLite provides the concrete implementation.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# Sentinel distinguishing "silo not supplied" (back-compat, unscoped) from
# `silo=None` (a REAL value: the null-silo pool). Never use None as the default.
_UNSET = object()


# ── Data classes ──────────────────────────────────────────

@dataclass
class Document:
    id: str
    title: str
    content: str | None = None
    content_hash: str | None = None
    source_path: str | None = None
    status: str = "pending"
    created_at: str | None = None
    domains: list[str] = field(default_factory=list)
    entity_count: int = 0
    content_type: str = "text"
    thumbnail_path: str | None = None
    # documents.silo_id (task 11a exposure) — the resolved silo this document was
    # ingested into (source_id > collection_id > None; see pipeline/silo.py). `kind`
    # is NOT a field here: it's a property of the SILO, resolved live via the
    # `silo_kind` view at read time, never copied onto the document.
    silo_id: str | None = None


@dataclass
class Chunk:
    id: str
    document_id: str
    chunk_index: int
    text: str
    offset: int = 0
    length: int = 0
    embedding: bytes | None = None


@dataclass
class Domain:
    id: str
    path: str
    parent_path: str | None = None
    document_count: int = 0
    spec_version: int | None = None
    created_at: str | None = None
    layout_x: float | None = None
    layout_y: float | None = None


@dataclass
class Entity:
    id: str
    canonical_name: str
    type: str
    source_count: int = 0
    embedding: bytes | None = None
    created_at: str | None = None
    # DISTINCT documents.silo_id over the entity's sources. Only populated by
    # get_all_for_normalization() (batch normalization's silo-scoping, #50);
    # None elsewhere means "not computed", not "no silo info" — do not treat
    # a None here as the empty-set wildcard used by the silo-overlap check.
    silo_ids: frozenset | None = None


@dataclass
class EntitySource:
    entity_id: str
    document_id: str
    chunk_id: str | None = None
    extraction_pass: str | None = None
    spec_version: int | None = None
    job_id: str | None = None


@dataclass
class Relationship:
    id: str
    from_entity: str
    to_entity: str
    type: str
    weight: float
    source_chunk: str | None = None


@dataclass
class Job:
    id: str
    type: str
    target: str
    status: str = "queued"
    config: dict | None = None
    result: dict | None = None
    progress: dict | None = None       # live mid-run counters (docs_done/docs_total/entities_so_far)
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class Spec:
    id: str
    domain_path: str | None
    version: int
    spec_content: str
    golden_set: str | None = None
    score: float | None = None
    created_at: str | None = None


@dataclass
class SimmerIteration:
    id: str
    job_id: str
    phase: str
    iteration: int
    scores: dict | None = None
    composite: float | None = None
    key_change: str | None = None
    asi: str | None = None
    judge_mode: str | None = None
    regressed: bool = False
    candidate_preview: str | None = None
    criteria: list[dict] = field(default_factory=list)


@dataclass
class NormalizationReview:
    id: str
    entity_a_id: str
    entity_a_name: str
    entity_b_id: str
    entity_b_name: str
    similarity: float
    status: str = "pending"
    resolution: str | None = None


@dataclass
class DomainAssignment:
    document_id: str
    domain_path: str
    is_primary: bool
    confidence: float


@dataclass
class CoEntity:
    id: str
    canonical_name: str
    type: str
    weight: float
    shared_doc_ids: list[str] = field(default_factory=list)


# ── Repository interfaces ────────────────────────────────

class DocumentRepository(ABC):
    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def create(self, id: str, title: str, content: str, content_hash: str,
               source_path: str | None = None, content_type: str = "text",
               silo_id: str | None = None) -> str: ...

    @abstractmethod
    def get(self, doc_id: str) -> Document | None: ...

    @abstractmethod
    def list(self, limit: int = 50, offset: int = 0, since: str | None = None,
             until: str | None = None, order: str = "desc") -> list[Document]: ...

    @abstractmethod
    def get_by_hash(self, content_hash: str) -> Document | None: ...

    @abstractmethod
    def title_exists(self, title: str) -> bool: ...

    @abstractmethod
    def update_status(self, doc_id: str, status: str) -> None: ...

    @abstractmethod
    def get_for_domain(self, domain_path: str, status_filter: list[str] | None = None) -> list[Document]: ...

    @abstractmethod
    def get_recent(self, limit: int = 50, in_slice: bool = False) -> list[Document]: ...

    @abstractmethod
    def get_sample(self, limit: int = 10, status_filter: list[str] | None = None) -> list[Document]: ...

    @abstractmethod
    def delete(self, doc_id: str) -> dict: ...


class ChunkRepository(ABC):
    @abstractmethod
    def create_batch(self, chunks: list[Chunk]) -> None: ...

    @abstractmethod
    def get_for_document(self, doc_id: str) -> list[Chunk]: ...

    @abstractmethod
    def get_all_with_embeddings(self) -> list[Chunk]: ...

    @abstractmethod
    def update_embedding(self, chunk_id: str, embedding: bytes) -> None: ...


class DomainRepository(ABC):
    @abstractmethod
    def create(self, id: str, path: str, parent_path: str | None = None) -> None: ...

    @abstractmethod
    def get(self, path: str) -> Domain | None: ...

    @abstractmethod
    def get_by_id(self, domain_id: str) -> Domain | None: ...

    @abstractmethod
    def list(self, min_doc_count: int = 0) -> list[Domain]: ...

    @abstractmethod
    def get_all_paths(self) -> list[str]: ...

    @abstractmethod
    def increment_doc_count(self, path: str) -> None: ...

    @abstractmethod
    def update_spec_version(self, path: str, version: int) -> None: ...

    @abstractmethod
    def get_merge_target(self, label: str) -> str | None: ...

    @abstractmethod
    def assign_document(self, doc_id: str, domain_path: str,
                        is_primary: bool, confidence: float) -> None: ...

    @abstractmethod
    def get_domains_for_document(self, doc_id: str) -> list[DomainAssignment]: ...

    @abstractmethod
    def get_entity_domain_weights(self, entity_id: str) -> dict[str, float]: ...


class EntityRepository(ABC):
    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def create(self, id: str, name: str, type: str) -> str: ...

    @abstractmethod
    def get(self, entity_id: str, include_invalid: bool = False,
            in_slice: bool = False) -> Entity | None: ...

    @abstractmethod
    def get_by_name(self, name: str, type: str, include_invalid: bool = False,
                     silo: object = _UNSET) -> Entity | None: ...

    @abstractmethod
    def list(self, limit: int = 50, offset: int = 0,
             type_filter: str | None = None,
             domain_filter: str | None = None,
             job_id: str | None = None, in_slice: bool = False) -> list[Entity]: ...

    @abstractmethod
    def delete(self, entity_id: str) -> None: ...

    @abstractmethod
    def update_embedding(self, entity_id: str, embedding: bytes) -> None: ...

    @abstractmethod
    def get_all_for_normalization(self) -> list[Entity]:
        """Every entity (invalidated ones included — batch normalization must be able
        to re-attach to them, same as get()/get_by_name() with include_invalid=True),
        each with its silo_ids populated (see Entity.silo_ids)."""
        ...

    @abstractmethod
    def get_for_document(self, doc_id: str) -> list[Entity]: ...

    @abstractmethod
    def get_for_domain(self, domain_path: str, limit: int = 12) -> list[Entity]: ...


class EntitySourceRepository(ABC):
    @abstractmethod
    def create(self, entity_id: str, document_id: str, chunk_id: str | None = None,
               extraction_pass: str | None = None, spec_version: int | None = None,
               job_id: str | None = None) -> None: ...

    @abstractmethod
    def get_for_entity(self, entity_id: str, in_slice: bool = False) -> list[EntitySource]: ...

    @abstractmethod
    def get_source_count(self, entity_id: str) -> int: ...

    @abstractmethod
    def update_entity_id(self, from_id: str, to_id: str) -> None: ...

    @abstractmethod
    def get_shared_documents(self, entity_id: str, doc_ids: list[str]) -> dict[str, list[str]]: ...

    @abstractmethod
    def get_documents_for_entity(self, entity_id: str) -> list[dict]: ...


class RelationshipRepository(ABC):
    @abstractmethod
    def upsert_cooccurrence(self, id: str, from_entity: str, to_entity: str,
                             weight: float, source_chunk: str | None = None) -> None: ...

    @abstractmethod
    def get_cooccurrences(self, entity_id: str, limit: int = 10) -> list[CoEntity]: ...

    @abstractmethod
    def get_star_graph(self, entity_id: str, co_limit: int = 30) -> dict: ...

    @abstractmethod
    def get_trade_routes(self, in_slice: bool = False) -> list[dict]:
        """domain<->domain co-occurrence routes. PRECONDITION for in_slice=True:
        `ensure_slice_doc_temp_table()` must have been run on the SAME connection
        (TEMP TABLE `_slice_docs`); otherwise RuntimeError."""
        ...

    @abstractmethod
    def update_entity_references(self, from_id: str, to_id: str) -> None: ...


class CollectionRepository(ABC):
    @abstractmethod
    def get_collection_routes(self, in_slice: bool = False) -> list[dict]:
        """collection<->collection co-occurrence routes. PRECONDITION for
        in_slice=True: `ensure_slice_doc_temp_table()` must have been run on the SAME
        connection (TEMP TABLE `_slice_docs`); otherwise RuntimeError."""
        ...


class JobRepository(ABC):
    @abstractmethod
    def count_active(self) -> int: ...

    @abstractmethod
    def create(self, id: str, type: str, target: str, config: dict | None = None) -> None: ...

    @abstractmethod
    def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def list(self, status_filter: str | None = None) -> list[Job]: ...

    @abstractmethod
    def get_existing(self, type: str, target: str, statuses: list[str]) -> Job | None: ...

    @abstractmethod
    def pick_next(self) -> Job | None: ...

    @abstractmethod
    def mark_running(self, job_id: str) -> None: ...

    @abstractmethod
    def mark_completed(self, job_id: str, result: dict | None = None) -> None: ...

    @abstractmethod
    def mark_failed(self, job_id: str, error: str) -> None: ...


class SpecRepository(ABC):
    @abstractmethod
    def create(self, id: str, domain_path: str | None, version: int,
               content: str, golden_set: str | None = None, score: float | None = None) -> None: ...

    @abstractmethod
    def get_general(self) -> Spec | None: ...

    @abstractmethod
    def get_for_domain(self, domain_path: str) -> Spec | None: ...

    @abstractmethod
    def get_latest_version(self, domain_path: str) -> int: ...


class NormalizationRepository(ABC):
    @abstractmethod
    def get_review_by_id(self, review_id: str) -> NormalizationReview | None: ...

    @abstractmethod
    def get_existing_review(self, entity_a_id: str, entity_b_id: str) -> NormalizationReview | None: ...

    @abstractmethod
    def create_review(self, id: str, entity_a_id: str, entity_a_name: str,
                       entity_b_id: str, entity_b_name: str, similarity: float) -> None: ...

    @abstractmethod
    def get_review_queue(self) -> list[NormalizationReview]: ...

    @abstractmethod
    def resolve_review(self, review_id: str, action: str) -> None: ...

    @abstractmethod
    def create_merge_log(self, id: str, from_id: str, from_name: str,
                          to_id: str, to_name: str, method: str, similarity: float) -> None: ...

    @abstractmethod
    def get_merge_summary(self) -> dict: ...

    @abstractmethod
    def get_merge_map_entry(self, name: str, silo: object = _UNSET) -> str | None: ...

    @abstractmethod
    def create_merge_map_entry(self, from_name: str, to_entity_id: str) -> None: ...

    @abstractmethod
    def get_merge_history(self, entity_id: str) -> list[str]: ...


class LayoutRepository(ABC):
    @abstractmethod
    def get_stored_positions(self) -> dict[str, dict[str, float]]: ...

    @abstractmethod
    def store_position(self, domain_path: str, x: float, y: float,
                        embedding: bytes | None = None) -> None: ...

    @abstractmethod
    def delete_position(self, domain_path: str) -> None: ...

    @abstractmethod
    def store_model(self, model_blob: bytes, domain_count: int) -> None: ...

    @abstractmethod
    def get_model(self) -> dict | None: ...


class SimmerIterationRepository(ABC):
    @abstractmethod
    def create_iteration(self, id: str, job_id: str, phase: str, iteration: int,
                          scores: dict, composite: float, key_change: str | None = None,
                          asi: str | None = None, judge_mode: str | None = None,
                          regressed: bool = False, candidate_preview: str | None = None) -> None: ...

    @abstractmethod
    def create_criterion_detail(self, id: str, iteration_id: str, criterion: str,
                                 score: int, seed_score: int | None = None,
                                 evidence: str | None = None, improve: str | None = None) -> None: ...

    @abstractmethod
    def get_for_job(self, job_id: str) -> dict: ...


# ── Composite DataStore ──────────────────────────────────

class DataStore(ABC):
    """Top-level store providing access to all repositories.

    Usage:
        store = get_data_store()  # returns SQLiteDataStore or FirestoreDataStore
        doc = store.documents.get(doc_id)
        store.entities.create(...)
    """

    @property
    @abstractmethod
    def documents(self) -> DocumentRepository: ...

    @property
    @abstractmethod
    def chunks(self) -> ChunkRepository: ...

    @property
    @abstractmethod
    def domains(self) -> DomainRepository: ...

    @property
    @abstractmethod
    def entities(self) -> EntityRepository: ...

    @property
    @abstractmethod
    def entity_sources(self) -> EntitySourceRepository: ...

    @property
    @abstractmethod
    def relationships(self) -> RelationshipRepository: ...

    @property
    @abstractmethod
    def jobs(self) -> JobRepository: ...

    @property
    @abstractmethod
    def specs(self) -> SpecRepository: ...

    @property
    @abstractmethod
    def normalization(self) -> NormalizationRepository: ...

    @property
    @abstractmethod
    def layout(self) -> LayoutRepository: ...

    @property
    @abstractmethod
    def simmer_iterations(self) -> SimmerIterationRepository: ...
