"""Three-tier entity normalization cascade.

Tier 1: String rules (lowercase, strip, plural collapse) — runs inline during extraction
Tier 2: Embedding similarity (all-MiniLM-L6-v2, cosine) — runs after batch extraction
Tier 3: LLM review for ambiguous clusters — manual trigger, reviews queued pairs

Validated from warhammer pipeline: 14,033 → 12,159 entities (13.4% reduction).
"""

import uuid
import sqlite3
import numpy as np
from collections import defaultdict

from .silo import silo_match, kind_match

# Lazy-load the model (heavy import)
_model = None

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed_entities(names: list[str]) -> np.ndarray:
    """Embed a list of entity names. Returns (N, 384) array."""
    model = _get_model()
    return model.encode(names, normalize_embeddings=True)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two normalized vectors."""
    return float(np.dot(a, b))


# --- Tier 1: String rules ---

PLURAL_SUFFIXES = [
    ("ies", "y"),   # companies → company
    ("ses", "s"),   # processes → process
    ("es", "e"),    # techniques → technique
    ("s", ""),      # agents → agent
]

def normalize_string(name: str) -> str:
    """Lowercase, strip, basic cleanup."""
    return name.lower().strip().replace("  ", " ")


def collapse_plural(name: str, existing_names: set[str]) -> str | None:
    """If the singular form exists in the corpus, return it. Otherwise None."""
    for suffix, replacement in PLURAL_SUFFIXES:
        if name.endswith(suffix):
            singular = name[:-len(suffix)] + replacement
            if singular in existing_names and singular != name:
                return singular
    return None


# --- Tier 2: Embedding similarity ---

AUTO_MERGE_THRESHOLD = 0.85    # Above this: auto-merge
REVIEW_THRESHOLD = 0.70        # Between 0.70-0.85: queue for LLM review
# Below 0.70: definitely different


def _entity_silo_set(conn: sqlite3.Connection, entity_id: str) -> set:
    """All distinct silo ids an entity is sourced in (may include None, may be empty).

    A post-merge entity can carry sources from more than one silo — that's a real,
    already-merged multi-silo entity, not a bug — so this is a set, not a scalar.
    """
    rows = conn.execute(
        "SELECT DISTINCT d.silo_id FROM entity_sources es JOIN documents d ON d.id = es.document_id "
        "WHERE es.entity_id = ?",
        (entity_id,)
    ).fetchall()
    return {r[0] for r in rows}


def _silos_overlap(a: set, b: set) -> bool:
    """Two silo-sets overlap if they share any silo id. None is treated as an ordinary
    value here, so "both contain NULL" falls straight out of set intersection — no
    special-casing needed. Disjoint (including null-vs-non-null) means no overlap.

    An EMPTY set (no entity_sources row joins to a real document at all — distinct
    from a document that exists but has silo_id IS NULL) means the entity carries no
    silo information whatsoever. Treat that as a wildcard rather than "disjoint from
    everything": pre-silo callers/tests never wire up entity_sources -> documents,
    and there's nothing to gate on when one or both sides have no source at all.
    """
    if not a or not b:
        return True
    return bool(a & b)


# Sentinel kind for sources in documents with silo_id IS NULL (the loose-upload pool):
# silo-less data only overlaps other silo-less data, never a real kind.
NOSILO_KIND = "__nosilo__"


def _entity_kind_set(conn: sqlite3.Connection, entity_id: str) -> set:
    """All distinct provenance kinds an entity is sourced under (entity_sources ->
    documents -> silo_kind). Empty when no source resolves to a silo with a kind row."""
    rows = conn.execute(
        "SELECT DISTINCT CASE WHEN d.silo_id IS NULL THEN ? ELSE sk.kind END "
        "FROM entity_sources es "
        "JOIN documents d ON d.id = es.document_id "
        "LEFT JOIN silo_kind sk ON sk.silo_id = d.silo_id "
        "WHERE es.entity_id = ?",
        (NOSILO_KIND, entity_id)
    ).fetchall()
    return {r[0] for r in rows}


def _kinds_overlap(a: set, b: set) -> bool:
    """Same null-wildcard semantics as _silos_overlap: an EMPTY kind-set carries no
    kind information and overlaps with anything; otherwise they overlap only on a
    shared REAL kind. A None kind (real silo, provenance_kind unset) is unknown, and
    unknown != unknown, so it never matches: unset-kind silos fall back to silo isolation."""
    if not a or not b:
        return True
    return bool((a & b) - {None})


def _propose_cross_silo_merge(
    conn: sqlite3.Connection,
    a_id: str, a_name: str,
    b_id: str, b_name: str,
    similarity: float, method: str,
) -> None:
    """Direct SQL insert into graph_issues — the worker cannot import the
    orchestrator's graph_repair module (see task notes), so this mirrors its
    propose_correction() column-for-column rather than reusing it. A pending 'merge'
    proposal for a human to approve/reject via the existing corrections flow.
    Keyed on entity ids (not names); deduped against any prior proposal for the
    same pair so repeated runs don't spam the queue.
    """
    existing = conn.execute(
        "SELECT id FROM graph_issues WHERE action = 'merge' AND "
        "((target_entity_id = ? AND target_b_entity_id = ?) OR "
        " (target_entity_id = ? AND target_b_entity_id = ?))",
        (a_id, b_id, b_id, a_id)
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO graph_issues "
        "(id, action, target_entity_id, target_entity_name, target_b_entity_id, target_b_name, "
        "rationale, proposer, status) "
        "VALUES (?, 'merge', ?, ?, ?, ?, ?, ?, 'pending')",
        (str(uuid.uuid4()), a_id, a_name, b_id, b_name,
         f"cross-silo candidate: {method} similarity {similarity:.3f} — not auto-merged "
         f"because the two entities have no silo in common and are not an exact-name "
         f"match within a shared kind",
         "worker.normalizer")
    )


def _load_stored_embeddings(conn: sqlite3.Connection) -> dict[str, np.ndarray]:
    """Load persisted per-entity vectors from entity_embeddings.

    This table is the durable embedding store: an entity is embedded once, when
    it first appears, and reused forever. A row's presence also marks the entity
    as already-processed, so a later normalization run only does work for
    entities missing here (the incremental gate)."""
    out: dict[str, np.ndarray] = {}
    for eid, blob in conn.execute("SELECT entity_id, embedding FROM entity_embeddings"):
        if blob is not None:
            out[eid] = np.frombuffer(blob, dtype=np.float32)
    return out


def run_batch_normalization(conn: sqlite3.Connection) -> dict:
    """Incremental entity normalization.

    Only entities WITHOUT a stored embedding are treated as new; each run embeds
    just those, appends them to entity_embeddings, and searches them against the
    full set (new-vs-all) via a single vectorized inner-product per type. Old
    entities are never re-embedded and old-vs-old pairs are never re-generated,
    so an already-adjudicated pair is never revisited and a re-run with nothing
    new costs ~zero. On a DB whose embedding store is empty (fresh graph) every
    entity is "new", so the pass degrades to a full from-scratch normalization —
    identical decisions to the previous all-pairs implementation.

    Returns summary of what was done.
    """
    results = {
        "plural_merges": 0,
        "embedding_merges": 0,
        "queued_for_review": 0,
        "cross_silo_proposed": 0,
        "total_entities_before": 0,
        "total_entities_after": 0,
    }

    entities = conn.execute(
        "SELECT id, canonical_name, type FROM entities WHERE invalid_at IS NULL ORDER BY canonical_name"
    ).fetchall()
    results["total_entities_before"] = len(entities)
    if len(entities) < 2:
        results["total_entities_after"] = len(entities)
        return results

    stored = _load_stored_embeddings(conn)
    new_ids = {e[0] for e in entities if e[0] not in stored}

    # --- Tier 1: Plural collapse (new entities only) ---
    # A new plural whose singular already exists collapses into it. Restricting
    # to new entities keeps this incremental; old plurals were handled when they
    # were new.
    all_names = {e[1] for e in entities}
    for eid, name, etype in entities:
        if eid not in new_ids:
            continue
        singular = collapse_plural(name, all_names)
        if singular:
            # An unscoped lookup returns an ARBITRARY row when the singular
            # exists as more than one distinct entity across silos (#50) —
            # silos let the same name exist as multiple rows, and there's no
            # ORDER BY / unique constraint on canonical_name. Filtering that
            # one candidate against the plural's silo-set (the old approach)
            # is order-dependent and can silently skip a valid same-silo
            # collapse. Instead, loop over eid's OWN silo-set and do a
            # silo-scoped lookup per silo — the first silo-overlapping
            # candidate wins, since a candidate sourced in silo `s` (where `s`
            # is one of eid's own silos) is guaranteed to overlap eid by
            # construction.
            plural_silos = _entity_silo_set(conn, eid)
            target = None
            if plural_silos:
                for s in plural_silos:
                    row = conn.execute(
                        "SELECT e.id FROM entities e WHERE e.canonical_name = ? AND e.type = ? "
                        f"AND e.invalid_at IS NULL AND {silo_match('e.id')}",
                        (singular, etype, s)
                    ).fetchone()
                    if row and row[0] != eid:
                        target = row
                        break
            else:
                # Wildcard: eid carries no silo information at all (pre-silo
                # callers / sourceless test fixtures) — same carve-out as
                # _silos_overlap's empty-set case.
                target = conn.execute(
                    "SELECT id FROM entities WHERE canonical_name = ? AND type = ? AND invalid_at IS NULL",
                    (singular, etype)
                ).fetchone()
            if not target:
                # Different silo, but the EXACT tier (plural<->singular) widens from
                # silo to kind: a same-name singular sourced under any kind the
                # plural is also sourced under is the same thing. Empty kind-set =
                # wildcard (mirrors _kinds_overlap), so fall back to an unscoped lookup.
                plural_kinds = _entity_kind_set(conn, eid)
                if plural_kinds:
                    for k in sorted(plural_kinds, key=str):
                        if k is None:
                            continue  # real silo with unset kind: never merge on NULL (cf. _kinds_overlap)
                        # sentinel bucket = null-silo sources, which kind_match's inner
                        # join on silo_kind can't see; match them via silo_match(None).
                        frag, arg = (silo_match('e.id'), None) if k == NOSILO_KIND \
                            else (kind_match('e.id'), k)
                        row = conn.execute(
                            "SELECT e.id FROM entities e WHERE e.canonical_name = ? AND e.type = ? "
                            f"AND e.invalid_at IS NULL AND e.id != ? AND {frag}",
                            (singular, etype, eid, arg)
                        ).fetchone()
                        if row:
                            target = row
                            break
                else:
                    target = conn.execute(
                        "SELECT id FROM entities WHERE canonical_name = ? AND type = ? "
                        "AND invalid_at IS NULL AND id != ?",
                        (singular, etype, eid)
                    ).fetchone()
            if not target:
                # A singular exists but only under a different kind: not ours to
                # merge — propose to the human-gated queue and leave both distinct.
                other = conn.execute(
                    "SELECT id FROM entities WHERE canonical_name = ? AND type = ? "
                    "AND invalid_at IS NULL AND id != ?",
                    (singular, etype, eid)
                ).fetchone()
                if other:
                    _propose_cross_silo_merge(conn, eid, name, other[0], singular,
                                              similarity=1.0, method="plural")
                    results["cross_silo_proposed"] += 1
            if target and target[0] != eid:
                _merge_entities(conn, from_id=eid, from_name=name,
                               to_id=target[0], to_name=singular, method="plural", similarity=1.0)
                results["plural_merges"] += 1
                new_ids.discard(eid)

    # Re-fetch (plural merges deleted some) and recompute the new set.
    entities = conn.execute(
        "SELECT id, canonical_name, type FROM entities WHERE invalid_at IS NULL ORDER BY canonical_name"
    ).fetchall()
    if len(entities) < 2:
        results["total_entities_after"] = len(entities)
        return results
    stored = _load_stored_embeddings(conn)
    new_entities = [e for e in entities if e[0] not in stored]

    # --- Layer 1: embed ONLY new entities. Hold the vectors in memory for Layer
    # 2 but DON'T persist them yet. A stored embedding is what marks an entity
    # "already processed", so committing it before adjudication would — on a crash
    # in between — leave the entity embedded-but-never-compared and silently skip
    # its merges forever. We persist survivors together with the Layer 2 writes in
    # one final commit, so a crash just rolls back and the entity stays new. ---
    if not new_entities:
        # Nothing new — no pairs to (re)adjudicate.
        results["total_entities_after"] = len(entities)
        return results
    vecs = embed_entities([e[1] for e in new_entities]).astype(np.float32)
    for e, v in zip(new_entities, vecs, strict=True):  # strict: length mismatch must error, not truncate
        stored[e[0]] = v

    new_id_set = {e[0] for e in new_entities}

    # --- Layer 2: candidate search, per type, new-vs-all via faiss. ---
    # Merges are within-type, so group by type and build one faiss IndexFlatIP
    # per type over ALL its vectors (old + new). Vectors are normalized, so inner
    # product = cosine. range_search returns every neighbor above the review
    # threshold (no arbitrary top-k cutoff). We only query the NEW entities, so
    # new-vs-old and new-vs-new are covered while old-vs-old is never generated.
    # Swap IndexFlatIP → IndexIVFFlat/HNSW here if we ever need sub-quadratic.
    # Import faiss lazily (not at module top) so it loads only when there's work
    # AND after torch/sentence-transformers is already initialized — mirrors
    # orchestrator/pipeline/search.py and avoids the OpenMP double-load abort you
    # get when faiss and torch both initialize eagerly.
    import faiss

    by_type: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for eid, name, etype in entities:
        if eid in stored:
            by_type[etype].append((eid, name))

    merged_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for members in by_type.values():
        if len(members) < 2:
            continue
        ids_t = [m[0] for m in members]
        names_t = [m[1] for m in members]
        q_idx = [i for i, eid in enumerate(ids_t) if eid in new_id_set]
        if not q_idx:
            continue
        A = np.ascontiguousarray(np.vstack([stored[eid] for eid in ids_t]), dtype=np.float32)  # (t, d)
        index = faiss.IndexFlatIP(A.shape[1])
        index.add(A)
        Q = np.ascontiguousarray(A[q_idx], dtype=np.float32)  # (m, d), the new rows
        # lims[r]:lims[r+1] delimits query r's neighbors in (sims, nbrs).
        lims, sims_all, nbrs_all = index.range_search(Q, REVIEW_THRESHOLD)

        for r, qi in enumerate(q_idx):
            qid = ids_t[qi]
            if qid in merged_ids:
                continue
            seg = slice(int(lims[r]), int(lims[r + 1]))
            # Strongest candidate first so the best merge wins (range_search is unordered).
            cand = sorted(zip(sims_all[seg], nbrs_all[seg]), key=lambda x: -x[0])
            for sim, j in cand:
                j = int(j)
                if j == qi:
                    continue
                sim = float(sim)
                tid = ids_t[j]
                if tid in merged_ids:
                    continue
                pair = (qid, tid) if qid < tid else (tid, qid)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                if sim >= AUTO_MERGE_THRESHOLD:
                    # A high-similarity pair only auto-merges if the two entities
                    # share a silo. Cross-silo (disjoint silo-sets, including
                    # null-vs-non-null) is a REAL near-duplicate but not ours to
                    # merge unilaterally — propose it to the human-gated
                    # graph_issues queue instead and leave both entities distinct.
                    # EXACT tier exception: an identical canonical_name (same type is
                    # guaranteed by by_type) auto-merges across silos when the two
                    # entities share a provenance kind. Fuzzy never widens past silo.
                    if not _silos_overlap(_entity_silo_set(conn, qid), _entity_silo_set(conn, tid)) \
                            and not (names_t[qi] == names_t[j]
                                     and _kinds_overlap(_entity_kind_set(conn, qid),
                                                        _entity_kind_set(conn, tid))):
                        _propose_cross_silo_merge(
                            conn, qid, names_t[qi], tid, names_t[j],
                            similarity=sim, method="embedding",
                        )
                        results["cross_silo_proposed"] += 1
                        continue
                    # Keep the one with more sources (= more common).
                    count_q = conn.execute(
                        "SELECT COUNT(*) FROM entity_sources WHERE entity_id = ?", (qid,)
                    ).fetchone()[0]
                    count_t = conn.execute(
                        "SELECT COUNT(*) FROM entity_sources WHERE entity_id = ?", (tid,)
                    ).fetchone()[0]
                    if count_q >= count_t:
                        _merge_entities(conn, from_id=tid, from_name=names_t[j],
                                       to_id=qid, to_name=names_t[qi],
                                       method="embedding", similarity=sim)
                        merged_ids.add(tid)
                        results["embedding_merges"] += 1
                    else:
                        _merge_entities(conn, from_id=qid, from_name=names_t[qi],
                                       to_id=tid, to_name=names_t[j],
                                       method="embedding", similarity=sim)
                        merged_ids.add(qid)
                        results["embedding_merges"] += 1
                        break  # qid is gone — stop scanning its row
                else:
                    # Review range: queue unless this pair was EVER queued before
                    # (any status) — a resolved pair is not re-surfaced.
                    existing = conn.execute(
                        "SELECT id FROM normalization_review_queue WHERE "
                        "(entity_a_id = ? AND entity_b_id = ?) OR (entity_a_id = ? AND entity_b_id = ?)",
                        (qid, tid, tid, qid)
                    ).fetchone()
                    if not existing:
                        conn.execute(
                            "INSERT INTO normalization_review_queue (id, entity_a_id, entity_a_name, entity_b_id, entity_b_name, similarity) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (str(uuid.uuid4()), qid, names_t[qi], tid, names_t[j], sim)
                        )
                        results["queued_for_review"] += 1

    # Persist embeddings for the new entities that SURVIVED adjudication — the
    # merged-away ones are gone and must not be marked processed. Committed
    # together with the Layer 2 writes so the whole run is one atomic unit.
    for e in new_entities:
        if e[0] in merged_ids:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (e[0], stored[e[0]].tobytes())
        )

    conn.commit()
    results["total_entities_after"] = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE invalid_at IS NULL"
    ).fetchone()[0]
    return results


def _merge_entities(
    conn: sqlite3.Connection,
    from_id: str, from_name: str,
    to_id: str, to_name: str,
    method: str, similarity: float,
) -> None:
    """Merge from_entity into to_entity. Updates all references."""
    # Move entity_sources to target
    conn.execute(
        "UPDATE entity_sources SET entity_id = ? WHERE entity_id = ?",
        (to_id, from_id)
    )
    # Move relationship references
    conn.execute(
        "UPDATE relationships SET from_entity = ? WHERE from_entity = ?",
        (to_id, from_id)
    )
    conn.execute(
        "UPDATE relationships SET to_entity = ? WHERE to_entity = ?",
        (to_id, from_id)
    )
    # Add to merge map
    conn.execute(
        "INSERT OR REPLACE INTO merge_map (from_name, to_entity_id) VALUES (?, ?)",
        (from_name, to_id)
    )
    # Log the merge
    conn.execute(
        "INSERT INTO normalization_log (id, from_entity_id, from_name, to_entity_id, to_name, method, similarity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), from_id, from_name, to_id, to_name, method, similarity)
    )
    # Delete the merged entity
    conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (from_id,))
    conn.execute("DELETE FROM entities WHERE id = ?", (from_id,))
    conn.commit()


def get_normalization_summary(conn: sqlite3.Connection) -> dict:
    """Get summary of all normalization activity."""
    merges = conn.execute(
        "SELECT method, COUNT(*) FROM normalization_log GROUP BY method"
    ).fetchall()

    recent = conn.execute(
        "SELECT from_name, to_name, method, similarity, created_at "
        "FROM normalization_log ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    pending_reviews = conn.execute(
        "SELECT COUNT(*) FROM normalization_review_queue WHERE status = 'pending'"
    ).fetchone()[0]

    return {
        "merges_by_method": {r[0]: r[1] for r in merges},
        "total_merges": sum(r[1] for r in merges),
        "pending_reviews": pending_reviews,
        "recent_merges": [
            {"from": r[0], "to": r[1], "method": r[2], "similarity": round(r[3], 3), "date": r[4]}
            for r in recent
        ],
    }


def get_review_queue(conn: sqlite3.Connection) -> list[dict]:
    """Get pending normalization reviews."""
    rows = conn.execute(
        "SELECT id, entity_a_name, entity_b_name, similarity "
        "FROM normalization_review_queue WHERE status = 'pending' "
        "ORDER BY similarity DESC"
    ).fetchall()
    return [
        {"id": r[0], "entity_a": r[1], "entity_b": r[2], "similarity": round(r[3], 3)}
        for r in rows
    ]


def resolve_review(conn: sqlite3.Connection, review_id: str, action: str) -> None:
    """Resolve a review queue item. action = 'merge' or 'keep_separate'."""
    review = conn.execute(
        "SELECT entity_a_id, entity_a_name, entity_b_id, entity_b_name, similarity "
        "FROM normalization_review_queue WHERE id = ?",
        (review_id,)
    ).fetchone()
    if not review:
        return

    if action == "merge":
        # Merge b into a (a is kept as canonical)
        count_a = conn.execute(
            "SELECT COUNT(*) FROM entity_sources WHERE entity_id = ?", (review[0],)
        ).fetchone()[0]
        count_b = conn.execute(
            "SELECT COUNT(*) FROM entity_sources WHERE entity_id = ?", (review[2],)
        ).fetchone()[0]

        if count_a >= count_b:
            _merge_entities(conn, from_id=review[2], from_name=review[3],
                           to_id=review[0], to_name=review[1],
                           method="llm_review", similarity=review[4])
        else:
            _merge_entities(conn, from_id=review[0], from_name=review[1],
                           to_id=review[2], to_name=review[3],
                           method="llm_review", similarity=review[4])

    conn.execute(
        "UPDATE normalization_review_queue SET status = 'resolved', resolution = ? WHERE id = ?",
        (action, review_id)
    )
    conn.commit()
