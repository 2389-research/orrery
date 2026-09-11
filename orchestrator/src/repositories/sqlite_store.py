from __future__ import annotations
"""SQLite implementation of the DataStore repositories.

Wraps all existing SQL queries behind the repository interfaces.
This is a drop-in replacement — the same queries, same behavior,
just organized behind a clean interface.
"""

import json
import sqlite3
from .interfaces import (
    DataStore,
    DocumentRepository, ChunkRepository, DomainRepository,
    EntityRepository, EntitySourceRepository, RelationshipRepository,
    JobRepository, SpecRepository, NormalizationRepository,
    LayoutRepository, SimmerIterationRepository,
    Document, Chunk, Domain, Entity, EntitySource, Relationship,
    Job, Spec, SimmerIteration, NormalizationReview, DomainAssignment, CoEntity,
    _UNSET,
)
from ..db import get_connection, init_db
from ..pipeline.silo import silo_match, flow_default_kind, resolve_kind


def _safe_json(value):
    """Parse JSON string, returning None if empty or not valid JSON."""
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": value}


class SQLiteDocumentRepository(DocumentRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def count(self):
        return self._conn.execute("SELECT COUNT(*) FROM documents WHERE invalid_at IS NULL").fetchone()[0]

    def create(self, id, title, content, content_hash, source_path=None, content_type="text", silo_id=None):
        self._conn.execute(
            "INSERT INTO documents (id, title, content, content_hash, source_path, status, content_type, silo_id) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (id, title, content, content_hash, source_path, content_type, silo_id),
        )
        self._conn.commit()
        return id

    def get(self, doc_id):
        row = self._conn.execute("SELECT * FROM documents WHERE id = ? AND invalid_at IS NULL", (doc_id,)).fetchone()
        if not row:
            return None
        return Document(
            id=row["id"], title=row["title"], content=row["content"],
            content_hash=row["content_hash"], source_path=row["source_path"],
            status=row["status"], created_at=row["created_at"],
            content_type=row["content_type"] or "text",
            thumbnail_path=row["thumbnail_path"] if "thumbnail_path" in row.keys() else None,
            silo_id=row["silo_id"] if "silo_id" in row.keys() else None,
        )

    def get_titles(self, ids):
        """Map doc_id -> {title, content_type, silo_id, kind} for the given ids. Lets a
        caller label a set of documents (e.g. an entity's source docs) without joining
        against the paginated `list()` — which only returns the first page, so
        callers were falling back to showing raw doc-id hashes. Batched to stay
        under SQLite's bound-parameter limit (an entity can cite thousands).

        `kind` is resolved LIVE via the `silo_kind` view join on every call — never
        cached alongside the title — so a source re-classified after ingest shows up
        on the very next read (task 11a)."""
        out = {}
        ids = list(dict.fromkeys(ids))  # de-dupe, preserve order
        for i in range(0, len(ids), 900):
            batch = ids[i:i + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                f"SELECT d.id, d.title, d.content_type, d.silo_id, sk.kind FROM documents d "
                f"LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id "
                f"WHERE d.id IN ({placeholders}) AND d.invalid_at IS NULL",
                batch,
            ).fetchall()
            for r in rows:
                out[r["id"]] = {"title": r["title"], "content_type": r["content_type"] or "text",
                                 "silo_id": r["silo_id"], "kind": r["kind"]}
        return out

    def list(self, limit=50, offset=0):
        rows = self._conn.execute(
            "SELECT d.*, GROUP_CONCAT(dd.domain_path) as domains FROM documents d "
            "LEFT JOIN document_domains dd ON d.id = dd.document_id "
            "WHERE d.invalid_at IS NULL "
            "GROUP BY d.id ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        result = []
        for r in rows:
            doc = Document(id=r["id"], title=r["title"], status=r["status"], created_at=r["created_at"],
                          content_type=r["content_type"] or "text", source_path=r["source_path"])
            doc.domains = r["domains"].split(",") if r["domains"] else []
            result.append(doc)
        return result

    def get_by_hash(self, content_hash):
        # Exclude soft-deleted docs: this is the interactive upload's dedup gate, and a
        # doc a source sync soft-deleted must not suppress a fresh re-upload of the same
        # content (it would otherwise return an invalid id that reads then hide).
        row = self._conn.execute(
            "SELECT id FROM documents WHERE content_hash = ? AND invalid_at IS NULL",
            (content_hash,)).fetchone()
        return Document(id=row["id"], title="") if row else None

    def title_exists(self, title):
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE title = ? AND invalid_at IS NULL LIMIT 1",
            (title,)).fetchone()
        return row is not None

    def update_status(self, doc_id, status):
        self._conn.execute("UPDATE documents SET status = ? WHERE id = ?", (status, doc_id))
        self._conn.commit()

    def get_for_domain(self, domain_path, status_filter=None):
        query = """SELECT d.* FROM documents d
            JOIN document_domains dd ON d.id = dd.document_id
            WHERE dd.domain_path = ? AND d.invalid_at IS NULL"""
        params = [domain_path]
        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            query += f" AND d.status IN ({placeholders})"
            params.extend(status_filter)
        rows = self._conn.execute(query, params).fetchall()
        return [Document(id=r["id"], title=r["title"], content=r["content"],
                         status=r["status"], created_at=r["created_at"]) for r in rows]

    def get_recent(self, limit=50):
        rows = self._conn.execute(
            "SELECT d.id, d.title, d.content_type, d.silo_id, GROUP_CONCAT(dd.domain_path) as domains "
            "FROM documents d LEFT JOIN document_domains dd ON d.id = dd.document_id "
            "WHERE d.invalid_at IS NULL "
            "GROUP BY d.id ORDER BY d.created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            doc = Document(id=r["id"], title=r["title"], content_type=r["content_type"] or "text",
                           silo_id=r["silo_id"])
            doc.domains = r["domains"].split(",") if r["domains"] else []
            result.append(doc)
        return result

    def get_sample(self, limit=10, status_filter=None):
        query = "SELECT * FROM documents"
        params = []
        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            query += f" WHERE status IN ({placeholders})"
            params.extend(status_filter)
        query += " ORDER BY RANDOM() LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [Document(id=r["id"], title=r["title"], content=r["content"],
                         status=r["status"]) for r in rows]

    def delete(self, doc_id):
        """Delete a document and cascade to entity_sources, orphaned entities,
        relationships, merge_map entries, document_domains, and chunks."""
        conn = self._conn

        affected_entity_ids = [
            r["entity_id"] for r in conn.execute(
                "SELECT DISTINCT entity_id FROM entity_sources WHERE document_id = ?", (doc_id,)
            ).fetchall()
        ]

        conn.execute("DELETE FROM entity_sources WHERE document_id = ?", (doc_id,))

        entities_removed = []
        for entity_id in affected_entity_ids:
            remaining = conn.execute(
                "SELECT COUNT(*) as c FROM entity_sources WHERE entity_id = ?", (entity_id,)
            ).fetchone()["c"]
            if remaining == 0:
                conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
                conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
                conn.execute("DELETE FROM merge_map WHERE to_entity_id = ?", (entity_id,))
                conn.execute(
                    "DELETE FROM relationships WHERE from_entity = ? OR to_entity = ?",
                    (entity_id, entity_id),
                )
                entities_removed.append(entity_id)

        # Co-occurrence is a projection of entity_sources: the doc's rows are gone, so
        # rebuild the edges for the entities that SURVIVED (removed ones already had all
        # their edges dropped above). The old source_chunk-keyed DELETE is now a no-op —
        # projected rows carry source_chunk=NULL — so this recompute is what retracts the
        # deleted doc's contribution and de-inflates shared-pair weights.
        from ..db import recompute_cooccurrence
        survivors = [e for e in affected_entity_ids if e not in entities_removed]
        if survivors:
            recompute_cooccurrence(conn, survivors)

        affected_domains = [
            r["domain_path"] for r in conn.execute(
                "SELECT domain_path FROM document_domains WHERE document_id = ?", (doc_id,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM document_domains WHERE document_id = ?", (doc_id,))
        for domain_path in affected_domains:
            conn.execute(
                "UPDATE domains SET document_count = MAX(document_count - 1, 0) WHERE path = ?",
                (domain_path,),
            )

        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()

        return {"entities_removed": entities_removed}


class SQLiteChunkRepository(ChunkRepository):
    def __init__(self, conn):
        self._conn = conn

    def create_batch(self, chunks):
        for c in chunks:
            self._conn.execute(
                "INSERT INTO chunks (id, document_id, chunk_index, offset, length, text) VALUES (?, ?, ?, ?, ?, ?)",
                (c.id, c.document_id, c.chunk_index, c.offset, c.length, c.text),
            )
        self._conn.commit()

    def get_for_document(self, doc_id):
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY chunk_index", (doc_id,)
        ).fetchall()
        return [Chunk(id=r["id"], document_id=r["document_id"], chunk_index=r["chunk_index"],
                       text=r["text"], offset=r["offset"], length=r["length"],
                       embedding=r["embedding"]) for r in rows]

    def get_all_with_embeddings(self):
        rows = self._conn.execute("SELECT id, document_id, chunk_index, text, embedding FROM chunks").fetchall()
        return [Chunk(id=r["id"], document_id=r["document_id"], chunk_index=r["chunk_index"],
                       text=r["text"], embedding=r["embedding"]) for r in rows]

    def update_embedding(self, chunk_id, embedding):
        self._conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (embedding, chunk_id))
        self._conn.commit()


class SQLiteDomainRepository(DomainRepository):
    def __init__(self, conn):
        self._conn = conn

    def create(self, id, path, parent_path=None):
        self._conn.execute(
            "INSERT INTO domains (id, path, parent_path, document_count) VALUES (?, ?, ?, 0)",
            (id, path, parent_path),
        )
        self._conn.commit()

    def get(self, path):
        row = self._conn.execute("SELECT * FROM domains WHERE path = ?", (path,)).fetchone()
        if not row:
            return None
        return Domain(id=row["id"], path=row["path"], parent_path=row["parent_path"],
                      document_count=row["document_count"], spec_version=row["spec_version"],
                      created_at=row["created_at"])

    def get_by_id(self, domain_id):
        row = self._conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if not row:
            return None
        return Domain(id=row["id"], path=row["path"], parent_path=row["parent_path"],
                      document_count=row["document_count"], spec_version=row["spec_version"])

    def list(self, min_doc_count=0):
        rows = self._conn.execute(
            "SELECT * FROM domains WHERE document_count >= ? ORDER BY path", (min_doc_count,)
        ).fetchall()
        return [Domain(id=r["id"], path=r["path"], parent_path=r["parent_path"],
                        document_count=r["document_count"], spec_version=r["spec_version"],
                        created_at=r["created_at"]) for r in rows]

    def get_all_paths(self):
        rows = self._conn.execute("SELECT path FROM domains ORDER BY path").fetchall()
        return [r["path"] for r in rows]

    def increment_doc_count(self, path):
        self._conn.execute("UPDATE domains SET document_count = document_count + 1 WHERE path = ?", (path,))
        self._conn.commit()

    def update_spec_version(self, path, version):
        self._conn.execute("UPDATE domains SET spec_version = ? WHERE path = ?", (version, path))
        self._conn.commit()

    def get_merge_target(self, label):
        row = self._conn.execute("SELECT to_path FROM domain_merge_map WHERE from_label = ?",
                                  (label.lower().strip(),)).fetchone()
        return row["to_path"] if row else None

    def assign_document(self, doc_id, domain_path, is_primary, confidence):
        self._conn.execute(
            "INSERT OR REPLACE INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES (?, ?, ?, ?)",
            (doc_id, domain_path, is_primary, confidence),
        )
        self._conn.commit()

    def get_domains_for_document(self, doc_id):
        rows = self._conn.execute(
            "SELECT * FROM document_domains WHERE document_id = ?", (doc_id,)
        ).fetchall()
        return [DomainAssignment(document_id=r["document_id"], domain_path=r["domain_path"],
                                  is_primary=bool(r["is_primary"]), confidence=r["confidence"]) for r in rows]

    def get_entity_domain_weights(self, entity_id):
        rows = self._conn.execute("""
            SELECT dd.domain_path, COUNT(*) as weight
            FROM entity_sources es
            JOIN document_domains dd ON es.document_id = dd.document_id
            WHERE es.entity_id = ?
            GROUP BY dd.domain_path
        """, (entity_id,)).fetchall()
        total = sum(r["weight"] for r in rows)
        if total == 0:
            return {}
        return {r["domain_path"]: round(r["weight"] / total, 3) for r in rows}


class SQLiteEntityRepository(EntityRepository):
    def __init__(self, conn):
        self._conn = conn

    def count(self):
        return self._conn.execute("SELECT COUNT(*) FROM entities WHERE invalid_at IS NULL").fetchone()[0]

    def create(self, id, name, type):
        self._conn.execute(
            "INSERT INTO entities (id, canonical_name, type) VALUES (?, ?, ?)", (id, name, type),
        )
        self._conn.commit()
        return id

    def get(self, entity_id, include_invalid=False):
        clause = "" if include_invalid else " AND invalid_at IS NULL"
        row = self._conn.execute(
            f"SELECT * FROM entities WHERE id = ?{clause}", (entity_id,)
        ).fetchone()
        if not row:
            return None
        count = self._conn.execute(
            "SELECT COUNT(*) as c FROM entity_sources WHERE entity_id = ?", (entity_id,)
        ).fetchone()["c"]
        return Entity(id=row["id"], canonical_name=row["canonical_name"], type=row["type"],
                      source_count=count, embedding=row["embedding"], created_at=row["created_at"])

    def get_by_name(self, name, type, include_invalid=False, silo=_UNSET):
        clause = "" if include_invalid else " AND invalid_at IS NULL"
        params = [name, type]
        silo_clause = ""
        if silo is not _UNSET:
            silo_clause = f" AND {silo_match('e.id')}"
            params.append(silo)
        row = self._conn.execute(
            f"SELECT * FROM entities e WHERE canonical_name = ? AND type = ?{clause}{silo_clause}",
            params
        ).fetchone()
        if not row:
            return None
        return Entity(id=row["id"], canonical_name=row["canonical_name"], type=row["type"])

    def list(self, limit=50, offset=0, type_filter=None, domain_filter=None, job_id=None):
        query = """SELECT e.id, e.canonical_name, e.type,
                   (SELECT COUNT(*) FROM entity_sources es WHERE es.entity_id = e.id) as source_count
                   FROM entities e"""
        params = []
        joins = []
        conditions = ["e.invalid_at IS NULL"]

        if domain_filter:
            joins.append("JOIN entity_sources es2 ON e.id = es2.entity_id "
                         "JOIN document_domains dd ON es2.document_id = dd.document_id AND dd.domain_path LIKE ? || '%'")
            params.append(domain_filter)
        if job_id:
            joins.append("JOIN entity_sources es3 ON e.id = es3.entity_id AND es3.job_id = ?")
            params.append(job_id)
        if type_filter:
            conditions.append("e.type = ?")
            params.append(type_filter)

        if joins:
            query += " " + " ".join(joins)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " GROUP BY e.id ORDER BY source_count DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        return [Entity(id=r["id"], canonical_name=r["canonical_name"], type=r["type"],
                        source_count=r["source_count"]) for r in rows]

    def delete(self, entity_id):
        self._conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        self._conn.commit()

    def update_embedding(self, entity_id, embedding):
        self._conn.execute("UPDATE entities SET embedding = ? WHERE id = ?", (embedding, entity_id))
        self._conn.commit()

    def get_all_for_normalization(self):
        # No invalid_at filter — intentional (#50): batch normalization must see
        # invalidated entities too, same as get_by_name(include_invalid=True).
        rows = self._conn.execute(
            "SELECT id, canonical_name, type FROM entities ORDER BY canonical_name"
        ).fetchall()

        # One extra query for every entity's silo-set, grouped in Python rather than
        # via SQL GROUP_CONCAT (which silently drops NULL members — losing exactly
        # the null-silo membership the overlap check needs to see).
        silo_map: dict[str, set] = {}
        for r in self._conn.execute(
            "SELECT DISTINCT es.entity_id, d.silo_id FROM entity_sources es "
            "JOIN documents d ON d.id = es.document_id"
        ):
            silo_map.setdefault(r["entity_id"], set()).add(r["silo_id"])

        return [
            Entity(id=r["id"], canonical_name=r["canonical_name"], type=r["type"],
                   silo_ids=frozenset(silo_map.get(r["id"], set())))
            for r in rows
        ]

    def get_for_document(self, doc_id):
        rows = self._conn.execute("""
            SELECT DISTINCT e.id, e.canonical_name, e.type,
                   (SELECT COUNT(*) FROM entity_sources es2 WHERE es2.entity_id = e.id) as source_count
            FROM entities e
            JOIN entity_sources es ON e.id = es.entity_id
            WHERE es.document_id = ? AND e.invalid_at IS NULL
            ORDER BY source_count DESC
        """, (doc_id,)).fetchall()
        return [Entity(id=r["id"], canonical_name=r["canonical_name"], type=r["type"],
                        source_count=r["source_count"]) for r in rows]

    def get_for_domain(self, domain_path, limit=12):
        rows = self._conn.execute("""
            SELECT e.canonical_name FROM entities e
            JOIN entity_sources es ON e.id = es.entity_id
            JOIN document_domains dd ON es.document_id = dd.document_id
            WHERE dd.domain_path = ? AND e.invalid_at IS NULL
            GROUP BY e.id ORDER BY COUNT(*) DESC LIMIT ?
        """, (domain_path, limit)).fetchall()
        return [Entity(id="", canonical_name=r["canonical_name"], type="") for r in rows]


class SQLiteEntitySourceRepository(EntitySourceRepository):
    def __init__(self, conn):
        self._conn = conn

    def create(self, entity_id, document_id, chunk_id=None, extraction_pass=None,
               spec_version=None, job_id=None):
        self._conn.execute(
            "INSERT INTO entity_sources (entity_id, document_id, chunk_id, extraction_pass, spec_version, job_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, document_id, chunk_id, extraction_pass, spec_version, job_id),
        )
        self._conn.commit()

    def get_for_entity(self, entity_id):
        rows = self._conn.execute(
            "SELECT * FROM entity_sources WHERE entity_id = ?", (entity_id,)
        ).fetchall()
        return [EntitySource(entity_id=r["entity_id"], document_id=r["document_id"],
                              chunk_id=r["chunk_id"], extraction_pass=r["extraction_pass"],
                              spec_version=r["spec_version"], job_id=r["job_id"]) for r in rows]

    def get_source_count(self, entity_id):
        row = self._conn.execute(
            "SELECT COUNT(*) as c FROM entity_sources WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        return row["c"]

    def update_entity_id(self, from_id, to_id):
        self._conn.execute(
            "UPDATE entity_sources SET entity_id = ? WHERE entity_id = ?", (to_id, from_id)
        )
        self._conn.commit()

    def get_shared_documents(self, entity_id, doc_ids):
        if not doc_ids:
            return {}
        placeholders = ",".join("?" * len(doc_ids))
        rows = self._conn.execute(f"""
            SELECT es.entity_id, es.document_id
            FROM entity_sources es
            WHERE es.entity_id = ? AND es.document_id IN ({placeholders})
        """, [entity_id] + doc_ids).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["entity_id"], []).append(r["document_id"])
        return result

    def get_documents_for_entity(self, entity_id):
        rows = self._conn.execute("""
            SELECT DISTINCT d.id, d.title
            FROM entity_sources es
            JOIN documents d ON es.document_id = d.id
            WHERE es.entity_id = ? AND d.invalid_at IS NULL
            ORDER BY d.title
        """, (entity_id,)).fetchall()
        return [{"id": r["id"], "title": r["title"]} for r in rows]


class SQLiteRelationshipRepository(RelationshipRepository):
    def __init__(self, conn):
        self._conn = conn

    def upsert_cooccurrence(self, id, from_entity, to_entity, weight, source_chunk=None):
        self._conn.execute(
            "INSERT OR REPLACE INTO relationships (id, from_entity, to_entity, type, weight, source_chunk) "
            "VALUES (?, ?, ?, 'co_occurs', ?, ?)",
            (id, from_entity, to_entity, weight, source_chunk),
        )
        self._conn.commit()

    def get_cooccurrences(self, entity_id, limit=10):
        rows = self._conn.execute("""
            SELECT e.id, e.canonical_name, e.type, SUM(r.weight) as total_weight
            FROM relationships r
            JOIN entities e ON (
                CASE WHEN r.from_entity = ? THEN r.to_entity ELSE r.from_entity END
            ) = e.id
            WHERE (r.from_entity = ? OR r.to_entity = ?) AND r.type = 'co_occurs'
              AND r.invalid_at IS NULL AND e.invalid_at IS NULL
            GROUP BY e.id ORDER BY total_weight DESC LIMIT ?
        """, (entity_id, entity_id, entity_id, limit)).fetchall()
        return [CoEntity(id=r["id"], canonical_name=r["canonical_name"], type=r["type"],
                          weight=r["total_weight"]) for r in rows]

    def get_trade_routes(self):
        # es1 and es2 share the same entity_id, so one join to entities filters
        # both sides: an invalidated (soft-deleted) entity contributes no routes.
        rows = self._conn.execute("""
            SELECT dd1.domain_path, dd2.domain_path, COUNT(*) as weight
            FROM entity_sources es1
            JOIN entity_sources es2 ON es1.entity_id = es2.entity_id AND es1.document_id != es2.document_id
            JOIN entities e ON e.id = es1.entity_id AND e.invalid_at IS NULL
            JOIN documents d1 ON d1.id = es1.document_id AND d1.invalid_at IS NULL
            JOIN documents d2 ON d2.id = es2.document_id AND d2.invalid_at IS NULL
            JOIN document_domains dd1 ON es1.document_id = dd1.document_id
            JOIN document_domains dd2 ON es2.document_id = dd2.document_id
            WHERE dd1.domain_path < dd2.domain_path
            GROUP BY dd1.domain_path, dd2.domain_path
        """).fetchall()
        return [{"source": r[0], "target": r[1], "weight": r[2]} for r in rows]

    def get_star_graph(self, entity_id, co_limit=150):
        # Entity info — an invalidated (soft-deleted) entity has no star graph.
        entity = self._conn.execute(
            "SELECT id, canonical_name, type FROM entities WHERE id = ? AND invalid_at IS NULL",
            (entity_id,),
        ).fetchone()
        if not entity:
            return None

        # Documents for this entity. Every id-list query below is CHUNKED (400/batch):
        # a hub entity can have thousands of docs, and an unbounded `IN (…)` would blow
        # past SQLite's SQLITE_MAX_VARIABLE_NUMBER (999 on older builds) and 500 the whole
        # endpoint. _galaxy_pos already chunked for this reason; the doc-id paths now do too.
        doc_rows = self._conn.execute("""
            SELECT DISTINCT d.id, d.title, d.content_type
            FROM entity_sources es
            JOIN documents d ON es.document_id = d.id
            WHERE es.entity_id = ? AND d.invalid_at IS NULL
            ORDER BY d.title
        """, (entity_id,)).fetchall()
        doc_ids = [r["id"] for r in doc_rows]

        def _chunks(seq, n=400):
            for i in range(0, len(seq), n):
                yield seq[i:i + n]

        # Primary domain per doc — one grouped fetch, not a correlated subquery evaluated
        # once per doc row. (document_id, domain_path) is the PK and is_primary is an
        # unconstrained flag, so we map the FIRST primary seen per doc (setdefault), which
        # can never multiply doc rows the way a JOIN could. None when a doc has no primary.
        primary_domain = {}
        for chunk in _chunks(doc_ids):
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(f"""
                SELECT document_id, domain_path FROM document_domains
                WHERE document_id IN ({ph}) AND is_primary = 1
            """, list(chunk)):
                primary_domain.setdefault(r["document_id"], r["domain_path"])

        # n_entities per doc: distinct ACTIVE entities extracted from that doc.
        n_entities = {}
        for chunk in _chunks(doc_ids):
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(f"""
                SELECT es.document_id, COUNT(DISTINCT es.entity_id) AS n
                FROM entity_sources es
                JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL
                WHERE es.document_id IN ({ph})
                GROUP BY es.document_id
            """, list(chunk)):
                n_entities[r["document_id"]] = r["n"]

        documents = [{
            "id": r["id"], "title": r["title"],
            "content_type": r["content_type"] or "text",
            "domain_path": primary_domain.get(r["id"]),
            "n_entities": n_entities.get(r["id"], 1),   # >=1: the entity itself
        } for r in doc_rows]

        # Co-entities
        co_rows = self._conn.execute("""
            SELECT e.id, e.canonical_name, e.type, SUM(r.weight) as total_weight
            FROM relationships r
            JOIN entities e ON (
                CASE WHEN r.from_entity = ? THEN r.to_entity ELSE r.from_entity END
            ) = e.id
            WHERE (r.from_entity = ? OR r.to_entity = ?) AND r.type = 'co_occurs'
              AND r.invalid_at IS NULL AND e.invalid_at IS NULL
            GROUP BY e.id ORDER BY total_weight DESC LIMIT ?
        """, (entity_id, entity_id, entity_id, co_limit)).fetchall()

        co_entity_ids = list(dict.fromkeys(r["id"] for r in co_rows))

        # Shared docs — chunk on doc_ids so the placeholder count stays ~co_limit + 400.
        shared_docs = {}
        if co_entity_ids and doc_ids:
            ph_co = ",".join("?" * len(co_entity_ids))
            for chunk in _chunks(doc_ids):
                ph_doc = ",".join("?" * len(chunk))
                for r in self._conn.execute(f"""
                    SELECT es.entity_id, es.document_id FROM entity_sources es
                    WHERE es.entity_id IN ({ph_co}) AND es.document_id IN ({ph_doc})
                """, co_entity_ids + list(chunk)):
                    shared_docs.setdefault(r["entity_id"], []).append(r["document_id"])

        # Galaxy positions: where each co-entity (and the core) sits on the galaxy map —
        # the cube-weighted centroid of its domains' UMAP positions (domain_layout), the
        # same membership-centroid the galaxy uses (state.js _makeEntityNode). Lets the
        # star view preserve galaxy spatial nuance instead of arbitrary placement.
        dom_pos = {r["domain_path"]: (r["x"], r["y"]) for r in
                   self._conn.execute("SELECT domain_path, x, y FROM domain_layout WHERE x IS NOT NULL").fetchall()}

        def _galaxy_pos(ids):
            if not ids or not dom_pos:
                return {}
            acc = {}
            for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
                ph = ",".join("?" * len(chunk))
                for r in self._conn.execute(f"""
                    SELECT es.entity_id AS eid, dd.domain_path AS dp, COUNT(*) AS w
                    FROM entity_sources es
                    JOIN document_domains dd ON dd.document_id = es.document_id
                    WHERE es.entity_id IN ({ph}) GROUP BY es.entity_id, dd.domain_path
                """, list(chunk)):
                    p = dom_pos.get(r["dp"])
                    if not p:
                        continue
                    cw = r["w"] ** 3
                    a = acc.setdefault(r["eid"], [0.0, 0.0, 0.0])
                    a[0] += p[0] * cw; a[1] += p[1] * cw; a[2] += cw
            return {eid: {"gx": a[0] / a[2], "gy": a[1] / a[2]} for eid, a in acc.items() if a[2] > 0}

        # One pass for co-entities AND the core (the core was a second redundant query).
        co_pos = _galaxy_pos(co_entity_ids + [entity_id])
        core_pos = co_pos.get(entity_id)

        co_entities = [{
            "id": r["id"], "canonical_name": r["canonical_name"], "type": r["type"],
            "weight": r["total_weight"],
            "shared_doc_ids": list(set(shared_docs.get(r["id"], []))),
            **(co_pos.get(r["id"]) or {}),   # gx, gy: galaxy position (absent if no positioned domain)
        } for r in co_rows]

        from ..pipeline.graph_snapshot import domain_palette
        palette = domain_palette(self._conn)
        # Filter to the leaf domains actually present on this star.
        present = {dp for dp in primary_domain.values() if dp}
        palette = {p: palette[p] for p in present if p in palette}

        return {
            "entity": {"id": entity["id"], "canonical_name": entity["canonical_name"],
                        "type": entity["type"], "source_count": len(doc_ids),
                        **(core_pos or {})},   # gx, gy: the core's galaxy position
            "documents": documents,
            "co_entities": co_entities,
            "palette": palette,
        }

    def update_entity_references(self, from_id, to_id):
        self._conn.execute("UPDATE relationships SET from_entity = ? WHERE from_entity = ?", (to_id, from_id))
        self._conn.execute("UPDATE relationships SET to_entity = ? WHERE to_entity = ?", (to_id, from_id))
        self._conn.commit()


class SQLiteJobRepository(JobRepository):
    def __init__(self, conn):
        self._conn = conn

    def count_active(self):
        return self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
        ).fetchone()[0]

    def create(self, id, type, target, config=None):
        self._conn.execute(
            "INSERT INTO jobs (id, type, target, status, config) VALUES (?, ?, ?, 'queued', ?)",
            (id, type, target, json.dumps(config) if config else None),
        )
        self._conn.commit()

    def get(self, job_id):
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return Job(id=row["id"], type=row["type"], target=row["target"], status=row["status"],
                   config=json.loads(row["config"]) if row["config"] else None,
                   result=json.loads(row["result"]) if row["result"] else None,
                   progress=_safe_json(row["progress"]),
                   created_at=row["created_at"], started_at=row["started_at"],
                   completed_at=row["completed_at"])

    def list(self, status_filter=None):
        if status_filter:
            rows = self._conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC",
                                       (status_filter,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [Job(id=r["id"], type=r["type"], target=r["target"], status=r["status"],
                     config=json.loads(r["config"]) if r["config"] else None,
                     result=_safe_json(r["result"]),
                     progress=_safe_json(r["progress"]),
                     created_at=r["created_at"], started_at=r["started_at"],
                     completed_at=r["completed_at"]) for r in rows]

    def get_existing(self, type, target, statuses):
        placeholders = ",".join("?" * len(statuses))
        row = self._conn.execute(
            f"SELECT id FROM jobs WHERE type = ? AND target = ? AND status IN ({placeholders})",
            [type, target] + statuses,
        ).fetchone()
        return Job(id=row["id"], type=type, target=target) if row else None

    def pick_next(self):
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return Job(id=row["id"], type=row["type"], target=row["target"], status=row["status"],
                   config=json.loads(row["config"]) if row["config"] else None)

    def mark_running(self, job_id):
        self._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,)
        )
        self._conn.commit()

    def mark_completed(self, job_id, result=None):
        self._conn.execute(
            "UPDATE jobs SET status = 'completed', completed_at = CURRENT_TIMESTAMP, result = ? WHERE id = ?",
            (json.dumps(result) if result else None, job_id),
        )
        self._conn.commit()

    def mark_failed(self, job_id, error):
        self._conn.execute(
            "UPDATE jobs SET status = 'failed', completed_at = CURRENT_TIMESTAMP, result = ? WHERE id = ?",
            (json.dumps({"error": error}), job_id),
        )
        self._conn.commit()


class SQLiteSpecRepository(SpecRepository):
    def __init__(self, conn):
        self._conn = conn

    def create(self, id, domain_path, version, content, golden_set=None, score=None):
        self._conn.execute(
            "INSERT INTO specs (id, domain_path, version, spec_content, golden_set, score) VALUES (?, ?, ?, ?, ?, ?)",
            (id, domain_path, version, content, golden_set, score),
        )
        self._conn.commit()

    def get_general(self):
        row = self._conn.execute(
            "SELECT * FROM specs WHERE domain_path IS NULL ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return Spec(id=row["id"], domain_path=None, version=row["version"],
                    spec_content=row["spec_content"], golden_set=row["golden_set"],
                    score=row["score"])

    def get_for_domain(self, domain_path):
        row = self._conn.execute(
            "SELECT * FROM specs WHERE domain_path = ? ORDER BY version DESC LIMIT 1",
            (domain_path,),
        ).fetchone()
        if not row:
            return None
        return Spec(id=row["id"], domain_path=row["domain_path"], version=row["version"],
                    spec_content=row["spec_content"], golden_set=row["golden_set"],
                    score=row["score"])

    def get_latest_version(self, domain_path):
        row = self._conn.execute(
            "SELECT MAX(version) as v FROM specs WHERE domain_path = ?", (domain_path,)
        ).fetchone()
        return row["v"] or 0


class SQLiteNormalizationRepository(NormalizationRepository):
    def __init__(self, conn):
        self._conn = conn

    def get_review_by_id(self, review_id):
        row = self._conn.execute(
            "SELECT * FROM normalization_review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        if not row:
            return None
        return NormalizationReview(
            id=row["id"], entity_a_id=row["entity_a_id"], entity_a_name=row["entity_a_name"],
            entity_b_id=row["entity_b_id"], entity_b_name=row["entity_b_name"],
            similarity=row["similarity"], status=row["status"], resolution=row["resolution"],
        )

    def get_existing_review(self, entity_a_id, entity_b_id):
        row = self._conn.execute(
            "SELECT * FROM normalization_review_queue WHERE "
            "((entity_a_id = ? AND entity_b_id = ?) OR (entity_a_id = ? AND entity_b_id = ?))",
            (entity_a_id, entity_b_id, entity_b_id, entity_a_id),
        ).fetchone()
        if not row:
            return None
        return NormalizationReview(
            id=row["id"], entity_a_id=row["entity_a_id"], entity_a_name=row["entity_a_name"],
            entity_b_id=row["entity_b_id"], entity_b_name=row["entity_b_name"],
            similarity=row["similarity"], status=row["status"], resolution=row["resolution"],
        )

    def create_review(self, id, entity_a_id, entity_a_name, entity_b_id, entity_b_name, similarity):
        self._conn.execute(
            "INSERT INTO normalization_review_queue (id, entity_a_id, entity_a_name, entity_b_id, entity_b_name, similarity) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (id, entity_a_id, entity_a_name, entity_b_id, entity_b_name, similarity),
        )
        self._conn.commit()

    def get_review_queue(self):
        rows = self._conn.execute(
            "SELECT * FROM normalization_review_queue WHERE status = 'pending' ORDER BY similarity DESC"
        ).fetchall()
        return [NormalizationReview(
            id=r["id"], entity_a_id=r["entity_a_id"], entity_a_name=r["entity_a_name"],
            entity_b_id=r["entity_b_id"], entity_b_name=r["entity_b_name"],
            similarity=r["similarity"], status=r["status"],
        ) for r in rows]

    def resolve_review(self, review_id, action):
        self._conn.execute(
            "UPDATE normalization_review_queue SET status = 'resolved', resolution = ? WHERE id = ?",
            (action, review_id),
        )
        self._conn.commit()

    def create_merge_log(self, id, from_id, from_name, to_id, to_name, method, similarity):
        self._conn.execute(
            "INSERT INTO normalization_log (id, from_entity_id, from_name, to_entity_id, to_name, method, similarity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, from_id, from_name, to_id, to_name, method, similarity),
        )
        self._conn.commit()

    def get_merge_summary(self):
        by_method = self._conn.execute(
            "SELECT method, COUNT(*) as c FROM normalization_log GROUP BY method"
        ).fetchall()
        total = self._conn.execute("SELECT COUNT(*) as c FROM normalization_log").fetchone()["c"]
        pending = self._conn.execute(
            "SELECT COUNT(*) as c FROM normalization_review_queue WHERE status = 'pending'"
        ).fetchone()["c"]
        recent = self._conn.execute(
            "SELECT from_name, to_name, method, similarity, created_at FROM normalization_log "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        return {
            "merges_by_method": {r["method"]: r["c"] for r in by_method},
            "total_merges": total,
            "pending_reviews": pending,
            "recent_merges": [{"from": r["from_name"], "to": r["to_name"], "method": r["method"],
                                "similarity": r["similarity"], "date": r["created_at"]} for r in recent],
        }

    def get_merge_map_entry(self, name, silo=_UNSET):
        params = [name]
        silo_clause = ""
        if silo is not _UNSET:
            silo_clause = f" AND {silo_match('to_entity_id')}"
            params.append(silo)
        row = self._conn.execute(
            f"SELECT to_entity_id FROM merge_map WHERE from_name = ?{silo_clause}", params
        ).fetchone()
        return row["to_entity_id"] if row else None

    def create_merge_map_entry(self, from_name, to_entity_id):
        self._conn.execute("INSERT OR REPLACE INTO merge_map (from_name, to_entity_id) VALUES (?, ?)",
                            (from_name, to_entity_id))
        self._conn.commit()

    def get_merge_history(self, entity_id):
        rows = self._conn.execute("SELECT from_name FROM merge_map WHERE to_entity_id = ?", (entity_id,)).fetchall()
        return [r[0] for r in rows]


class SQLiteLayoutRepository(LayoutRepository):
    def __init__(self, conn):
        self._conn = conn

    def get_stored_positions(self):
        rows = self._conn.execute("SELECT domain_path, x, y FROM domain_layout").fetchall()
        return {r["domain_path"]: {"x": r["x"], "y": r["y"]} for r in rows}

    def store_position(self, domain_path, x, y, embedding=None):
        self._conn.execute(
            "INSERT OR REPLACE INTO domain_layout (domain_path, x, y, embedding) VALUES (?, ?, ?, ?)",
            (domain_path, x, y, embedding),
        )
        self._conn.commit()

    def delete_position(self, domain_path):
        self._conn.execute("DELETE FROM domain_layout WHERE domain_path = ?", (domain_path,))
        self._conn.commit()

    def store_model(self, model_blob, domain_count):
        self._conn.execute(
            "INSERT OR REPLACE INTO layout_model (id, model_blob, domain_count) VALUES (?, ?, ?)",
            ("umap", model_blob, domain_count),
        )
        self._conn.commit()

    def get_model(self):
        row = self._conn.execute("SELECT model_blob, domain_count FROM layout_model WHERE id = 'umap'").fetchone()
        if not row or not row["model_blob"]:
            return None
        return {"model_blob": row["model_blob"], "domain_count": row["domain_count"]}


class SQLiteSimmerIterationRepository(SimmerIterationRepository):
    def __init__(self, conn):
        self._conn = conn

    def create_iteration(self, id, job_id, phase, iteration, scores, composite,
                          key_change=None, asi=None, judge_mode=None, regressed=False,
                          candidate_preview=None):
        self._conn.execute(
            "INSERT INTO simmer_iterations (id, job_id, phase, iteration, scores, composite, "
            "key_change, asi, judge_mode, regressed, candidate_preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, job_id, phase, iteration, json.dumps(scores), composite,
             key_change, asi, judge_mode, regressed, candidate_preview),
        )
        self._conn.commit()

    def create_criterion_detail(self, id, iteration_id, criterion, score, seed_score=None,
                                 evidence=None, improve=None):
        self._conn.execute(
            "INSERT INTO simmer_criterion_details (id, iteration_id, criterion, score, seed_score, evidence, improve) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, iteration_id, criterion, score, seed_score, evidence, improve),
        )
        self._conn.commit()

    def get_for_job(self, job_id):
        job = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return None

        iters = self._conn.execute(
            "SELECT * FROM simmer_iterations WHERE job_id = ? ORDER BY phase, iteration", (job_id,)
        ).fetchall()

        phases = {}
        for it in iters:
            criteria = self._conn.execute(
                "SELECT * FROM simmer_criterion_details WHERE iteration_id = ?", (it["id"],)
            ).fetchall()
            phase = it["phase"]
            if phase not in phases:
                phases[phase] = []
            phases[phase].append({
                "id": it["id"], "iteration": it["iteration"],
                "scores": json.loads(it["scores"]) if it["scores"] else {},
                "composite": it["composite"], "key_change": it["key_change"],
                "asi": it["asi"], "judge_mode": it["judge_mode"],
                "regressed": bool(it["regressed"]),
                "candidate_preview": it["candidate_preview"],
                "criterion_details": [{"criterion": c["criterion"], "score": c["score"],
                               "seed_score": c["seed_score"], "evidence": c["evidence"],
                               "improve": c["improve"]} for c in criteria],
            })

        return {
            "job": {"id": job["id"], "type": job["type"], "target": job["target"],
                     "status": job["status"], "created_at": job["created_at"],
                     "started_at": job["started_at"], "completed_at": job["completed_at"]},
            "phases": phases,
        }


# ── Composite DataStore ──────────────────────────────────

class SQLiteCollectionRepository:
    """Reads and writes for the collection layer.

    A COLLECTION is a labelled, hierarchical grouping of documents — a git repo, an
    agent run, anything whose documents arrived together. It sits beside `domains`
    rather than above or below: both are containers of documents, and they stay
    distinct because their provenance differs (a domain is inferred, a collection is
    given).
    """

    def __init__(self, conn):
        self._conn = conn

    def create(self, id, name, path, root_path, parent_path=None, kind="git_repo",
               provenance_kind=None):
        """`provenance_kind` is an optional override; the flow default is derived from
        `kind` (spec: per-source silos + provenance, task 9) and used whenever the
        override is absent or not a recognized KINDS value."""
        stamped_kind = resolve_kind(flow_default_kind(kind), provenance_kind)
        self._conn.execute(
            "INSERT INTO collections (id, name, path, root_path, parent_path, kind, provenance_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, name, path, root_path, parent_path, kind, stamped_kind),
        )
        self._conn.commit()
        return id

    def get_by_path(self, path):
        """Look up a collection by its unique `path` key, or None.

        `collections.path` is UNIQUE, so callers that create a collection from a
        user-supplied name need to check first — otherwise a repeat ingest surfaces as
        an IntegrityError and a 500 rather than a conflict the caller can act on.
        """
        row = self._conn.execute(
            "SELECT id, name, path, root_path, kind FROM collections WHERE path = ?", (path,)
        ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "path": row["path"],
                "root_path": row["root_path"], "kind": row["kind"]}

    def delete(self, collection_id):
        """Hard-delete a collection row. For compensating an interrupted create ONLY.

        Not part of the corrections path, which soft-deletes with `invalid_at` so an
        edit stays reversible. This exists because `create` commits: if the work that
        was supposed to follow it fails, the committed row is an orphan, and since
        `path` is UNIQUE it would block every retry of the same name. Deliberately
        narrow — it removes the row and nothing else, because the only caller uses it
        before any document has been attached.
        """
        self._conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        self._conn.commit()

    def link_document(self, document_id, collection_id, *, parent_path=None,
                      role=None, emits_cooccurrence=None):
        """Attach a document to a collection at a position in its tree.

        `role` ('root' | 'group' | 'leaf') and `emits_cooccurrence` are deliberately
        separate. Structural position and extraction behaviour are independent: a root
        or group summary mentions everything beneath it, so its co-occurrence is noise
        — but that is a DEFAULT, not a law. Deriving one from the other is what forces
        every new collection kind to impersonate a code repo in order to opt in.
        """
        if emits_cooccurrence is None:
            emits_cooccurrence = role == "leaf"
        self._conn.execute(
            "INSERT OR IGNORE INTO document_collections "
            "(document_id, collection_id, parent_path, role, emits_cooccurrence) "
            "VALUES (?, ?, ?, ?, ?)",
            (document_id, collection_id, parent_path, role, int(bool(emits_cooccurrence))),
        )
        self._conn.commit()

    def get_source_ref(self, document_id):
        """Resolve a code document to a fetchable GitHub reference
        ({remote, commit, path, url}). The graph stores the map — the LLM summary
        plus these git coordinates — not the code; an agent uses the ref to pull
        the exact source version from GitHub. Returns None for non-repo documents
        or repos ingested before git provenance was captured.

        This is the shareable counterpart to `documents.source_path`, which is an
        absolute path inside the ingesting container and does not resolve anywhere
        else — the limitation scripts/NOOSPHERE.md warns recipients about.
        """
        import os
        from urllib.parse import quote
        # content_type filter keeps the contract: only code docs get a ref (a
        # non-code doc is never linked to a repo, but be explicit — null otherwise).
        row = self._conn.execute(
            """SELECT c.remote_url, c.commit_sha, c.root_path, d.source_path
               FROM document_collections dc
               JOIN collections c ON c.id = dc.collection_id
               JOIN documents d ON d.id = dc.document_id
               WHERE dc.document_id = ? AND d.content_type = 'code_intent' LIMIT 1""",
            (document_id,),
        ).fetchone()
        if not row or not row["remote_url"]:
            return None
        remote, commit = row["remote_url"], row["commit_sha"]
        root_path, source_path = row["root_path"], row["source_path"]
        rel = None
        if source_path and root_path:
            try:
                rel = os.path.relpath(source_path, root_path)
            except ValueError:
                rel = None
            # Drop repo-level artifacts and any stale/mislinked path that escapes
            # the repo root (relpath can yield "../…") — those don't resolve.
            #
            # Compared on a PATH-COMPONENT boundary, not as a string prefix:
            # `startswith("..")` also rejects legitimate names, because a directory
            # may simply be called `...` or `..config`. Only `..` itself, or a `../`
            # leading the path, actually escapes the root.
            if rel is not None and (
                rel in (".", "", os.pardir) or rel.startswith(os.pardir + os.sep)
            ):
                rel = None
        ref = {"remote": remote, "commit": commit, "path": rel}
        if commit and rel:
            # safe="/" keeps path separators; encodes '#', '?', spaces, etc.
            ref["url"] = f"https://{remote}/blob/{commit}/{quote(rel, safe='/')}"
        return ref

    def get_collection_routes(self):
        """collection <-> collection edges DERIVED from shared entities.

        One join to `entities` filters both sides, since dc1 and dc2 reach the same
        entity_id — an invalidated entity contributes no routes.

        `emits_cooccurrence` is honoured on BOTH sides. It is the explicit replacement
        for the old `level == 'file'` test, and a derived read that ignores it silently
        reinstates the behaviour the column was added to remove: a collection's rollup
        and per-group summary documents mention everything under them, so letting them
        contribute makes every collection look related to every other.
        """
        rows = self._conn.execute("""
            SELECT dc1.collection_id, dc2.collection_id, COUNT(*) as weight
            FROM entity_sources es1
            JOIN entity_sources es2 ON es1.entity_id = es2.entity_id
                                   AND es1.document_id != es2.document_id
            JOIN entities e ON e.id = es1.entity_id AND e.invalid_at IS NULL
            JOIN document_collections dc1 ON es1.document_id = dc1.document_id
                                         AND dc1.emits_cooccurrence = 1
            JOIN document_collections dc2 ON es2.document_id = dc2.document_id
                                         AND dc2.emits_cooccurrence = 1
            WHERE dc1.collection_id < dc2.collection_id
            GROUP BY dc1.collection_id, dc2.collection_id
        """).fetchall()
        return [{"source": r[0], "target": r[1], "weight": r[2]} for r in rows]

    def get_collection_weights(self):
        """entity id -> {collection_id: normalized share of its mentions}.

        Same `emits_cooccurrence` opt-out as the routes above: a summary document
        mentions everything beneath it, so counting it would pull every entity's mass
        toward whichever collection has the wordiest rollup.
        """
        rows = self._conn.execute("""
            SELECT es.entity_id, dc.collection_id, COUNT(*) as weight
            FROM entity_sources es
            JOIN document_collections dc ON es.document_id = dc.document_id
                                        AND dc.emits_cooccurrence = 1
            JOIN entities e ON e.id = es.entity_id AND e.invalid_at IS NULL
            GROUP BY es.entity_id, dc.collection_id
        """).fetchall()
        # Named access, not positional: the columns are (entity_id, collection_id,
        # weight), so an index slip silently sums ids instead of counts.
        totals: dict[str, int] = {}
        for r in rows:
            totals[r["entity_id"]] = totals.get(r["entity_id"], 0) + r["weight"]
        result: dict[str, dict[str, float]] = {}
        for r in rows:
            total = totals[r["entity_id"]]
            result.setdefault(r["entity_id"], {})[r["collection_id"]] = (
                round(r["weight"] / total, 3) if total else 0)
        return result


class SQLiteDataStore(DataStore):
    def __init__(self, db_path: str):
        init_db(db_path)
        self._conn = get_connection(db_path)
        self._documents = SQLiteDocumentRepository(self._conn)
        self._chunks = SQLiteChunkRepository(self._conn)
        self._domains = SQLiteDomainRepository(self._conn)
        self._entities = SQLiteEntityRepository(self._conn)
        self._entity_sources = SQLiteEntitySourceRepository(self._conn)
        self._relationships = SQLiteRelationshipRepository(self._conn)
        self._jobs = SQLiteJobRepository(self._conn)
        self._specs = SQLiteSpecRepository(self._conn)
        self._normalization = SQLiteNormalizationRepository(self._conn)
        self._layout = SQLiteLayoutRepository(self._conn)
        self._simmer_iterations = SQLiteSimmerIterationRepository(self._conn)
        self._collections = SQLiteCollectionRepository(self._conn)

    @property
    def collections(self): return self._collections
    @property
    def documents(self): return self._documents
    @property
    def chunks(self): return self._chunks
    @property
    def domains(self): return self._domains
    @property
    def entities(self): return self._entities
    @property
    def entity_sources(self): return self._entity_sources
    @property
    def relationships(self): return self._relationships
    @property
    def jobs(self): return self._jobs
    @property
    def specs(self): return self._specs
    @property
    def normalization(self): return self._normalization
    @property
    def layout(self): return self._layout
    @property
    def simmer_iterations(self): return self._simmer_iterations

    @property
    def conn(self):
        """Direct connection access for legacy code during migration."""
        return self._conn

    def close(self):
        # Don't close if this is the test store (shared across requests)
        from .factory import _test_store
        if _test_store is self:
            return
        self._conn.close()
