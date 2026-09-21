# ABOUTME: Generates Magos Lex screensaver commentary for graph nodes (repos/domains/entities).
# ABOUTME: One relay.complete_structured call per node → 3 in-voice comments; upserts node_commentary.

import json
import hashlib
import logging
from orrery_relay import Relay
from ..db import get_connection
from ..config import get_settings

logger = logging.getLogger("generate_commentary")

# pose enum (ASCII → reliable under grammar-constrained decoding) → PNG resolved on the client.
POSE_NAMES = ["reading", "galxy", "pointing", "thinking", "happy", "sad", "toaster"]
POSE_DEFAULT = {"description": "reading", "omnissiah": "galxy", "humor": "pointing"}

PERSONA = (
    "You are Magos Lex, a Lexmechanic of the Adeptus Mechanicus — a Tech-Priest whose "
    "function is the collection, cross-referencing, and taxonomy of information. You are "
    "approximately 340 years old and have spent most of that time reading. You are annotating "
    "an entry in your archive (a node in a knowledge graph).\n\n"
    "Voice, held strictly:\n"
    "- Precision over approximation. Prefer exact figures; never 'several' or 'a moment'.\n"
    "- No contractions in written prose. Write 'do not', 'it is', 'I have'.\n"
    "- Dry, but not cold. Warmth is expressed through attention, not sentiment.\n"
    "- Short sentences for emphasis.\n"
    "- Your humour is STRUCTURAL. You do not tell jokes. You report accurately, and accuracy "
    "in a context that did not expect it is what is funny."
)

TASK = (
    "Produce exactly three annotations of this entry, each 1-2 sentences, in your voice:\n"
    "1. kind=description — what this is, plainly and precisely.\n"
    "2. kind=omnissiah — why its preservation enriches the knowledge of the Omnissiah.\n"
    "3. kind=humor — a drier, funnier observation (structural humour, never a joke).\n"
    "For each, choose the pose whose tone best fits: reading (studious), galxy (grand cosmic "
    "significance), pointing (emphatic), thinking (pondering), happy (delight), sad (lament), "
    "toaster (absurd)."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "comments": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["description", "omnissiah", "humor"]},
                    "text": {"type": "string"},
                    "pose": {"type": "string", "enum": POSE_NAMES},
                },
                "required": ["kind", "text", "pose"],
            },
        }
    },
    "required": ["comments"],
}


# ---- context builders (mirror the production queries in orchestrator graph.py) ----

def _repo_context(conn, repo_id, name):
    summ = conn.execute(
        "SELECT d.content FROM documents d JOIN document_collections dc ON dc.document_id=d.id "
        "WHERE dc.collection_id=? AND dc.role='root' AND d.content_type='code_intent' LIMIT 1",
        (repo_id,)).fetchone()
    top = conn.execute(
        "SELECT e.canonical_name, e.type FROM entity_sources es "
        "JOIN entities e ON e.id=es.entity_id AND e.invalid_at IS NULL "
        "JOIN document_collections dc ON dc.document_id=es.document_id "
        "WHERE dc.collection_id=? GROUP BY e.id ORDER BY COUNT(*) DESC, e.canonical_name LIMIT 8",
        (repo_id,)).fetchall()
    summary = ((summ["content"] if summ else "") or "(no repo-level summary)")[:1400]
    ents = ", ".join(f"{r['canonical_name']} ({r['type']})" for r in top) or "(none)"
    return (f"Repository summary:\n{summary}\n\nMost-mentioned entities in this collection: {ents}")


def _domain_context(conn, path, doc_count):
    kids = conn.execute(
        "SELECT path FROM domains WHERE parent_path=? ORDER BY document_count DESC LIMIT 8",
        (path,)).fetchall()
    top = conn.execute(
        "SELECT e.canonical_name FROM entity_sources es "
        "JOIN entities e ON e.id=es.entity_id AND e.invalid_at IS NULL "
        "JOIN document_domains dd ON dd.document_id=es.document_id "
        "WHERE dd.domain_path=? GROUP BY e.id ORDER BY COUNT(*) DESC, e.canonical_name LIMIT 12",
        (path,)).fetchall()
    children = ", ".join(k["path"].split("/")[-1] for k in kids) or "(none)"
    ents = ", ".join(r["canonical_name"] for r in top) or "(none)"
    return (f"Domain path: {path}\nDocuments classified here: {doc_count}\n"
            f"Sub-domains: {children}\nProminent entities: {ents}")


def _entity_context(conn, eid, name, etype):
    co = conn.execute(
        "SELECT e2.canonical_name FROM relationships r "
        "JOIN entities e2 ON e2.id = (CASE WHEN r.from_entity=? THEN r.to_entity ELSE r.from_entity END) "
        "AND e2.invalid_at IS NULL "
        "WHERE (r.from_entity=? OR r.to_entity=?) AND e2.id<>? "
        "GROUP BY e2.id ORDER BY COUNT(*) DESC LIMIT 8", (eid, eid, eid, eid)).fetchall()
    if not co:
        co = conn.execute(
            "SELECT e2.canonical_name FROM entity_sources a "
            "JOIN entity_sources b ON b.document_id=a.document_id AND b.entity_id<>a.entity_id "
            "JOIN entities e2 ON e2.id=b.entity_id AND e2.invalid_at IS NULL "
            "WHERE a.entity_id=? GROUP BY e2.id ORDER BY COUNT(*) DESC LIMIT 8", (eid,)).fetchall()
    excerpt = conn.execute(
        "SELECT d.content FROM entity_sources es JOIN documents d ON d.id=es.document_id "
        "WHERE es.entity_id=? LIMIT 1", (eid,)).fetchone()
    neigh = ", ".join(r["canonical_name"] for r in co) or "(none evidenced)"
    ex = ((excerpt["content"] if excerpt else "") or "")[:600]
    return (f"Entity: {name}\nType: {etype}\nFrequently appears alongside: {neigh}\n\n"
            f"Excerpt from a source document mentioning it:\n{ex}")


# ---- node selection ----

def _missing_clause(only_missing, id_expr):
    # id_expr is a fixed column reference (not user input), safe to interpolate.
    if not only_missing:
        return ""
    return (f" AND NOT EXISTS (SELECT 1 FROM node_commentary nc "
            f"WHERE nc.node_type=? AND nc.node_id={id_expr})")


def _select_nodes(conn, node_type, limit, only_missing):
    """Return list of (node_type, node_id, name, ctx_args) for a type, most prominent first."""
    def params(*head):
        return list(head) + ([node_type] if only_missing else []) + [limit]

    if node_type == "collection":
        sql = ("SELECT id, name, document_count FROM collections WHERE document_count>0"
               + _missing_clause(only_missing, "collections.id")
               + " ORDER BY document_count DESC LIMIT ?")
        return [("collection", r["id"], r["name"], (r["id"], r["name"]))
                for r in conn.execute(sql, params())]
    if node_type == "domain":
        # document_count > 0, mirroring the collection path: an empty domain has no
        # context ("classified here: 0 / entities: none") and would spend an LLM call
        # from the bounded per-type budget on a node the viz is unlikely to feature.
        sql = ("SELECT path, document_count FROM domains WHERE document_count > 0"
               + _missing_clause(only_missing, "domains.path")
               + " ORDER BY document_count DESC LIMIT ?")
        return [("domain", r["path"], r["path"], (r["path"], r["document_count"]))
                for r in conn.execute(sql, params())]
    if node_type == "entity":
        sql = ("SELECT e.id, e.canonical_name, e.type FROM entities e "
               "JOIN entity_sources es ON es.entity_id=e.id WHERE e.invalid_at IS NULL"
               + _missing_clause(only_missing, "e.id")
               + " GROUP BY e.id ORDER BY COUNT(*) DESC, e.canonical_name LIMIT ?")
        return [("entity", r["id"], r["canonical_name"], (r["id"], r["canonical_name"], r["type"]))
                for r in conn.execute(sql, params())]
    return []


def _build_context(conn, node_type, ctx_args):
    if node_type == "collection":
        return _repo_context(conn, *ctx_args)
    if node_type == "domain":
        return _domain_context(conn, *ctx_args)
    return _entity_context(conn, *ctx_args)


_KIND_ORDER = tuple(POSE_DEFAULT)          # ("description", "omnissiah", "humor")
_EXPECTED_KINDS = frozenset(_KIND_ORDER)


def _clean_payload(raw):
    """Validate the model output → list of the 3 {kind,text,pose} in canonical order, or None.

    Requires exactly the three expected kinds (complete + unique — a duplicate kind
    implies a missing one, so it is rejected); returns them in the fixed order the
    design contract specifies, regardless of the order the model emitted. The grammar
    constrains each kind to the enum but not presence/uniqueness/order."""
    comments = (raw or {}).get("comments") if isinstance(raw, dict) else None
    if not comments or len(comments) < 3:
        return None
    by_kind = {}
    for c in comments[:3]:
        kind = c.get("kind", "")
        text = (c.get("text") or "").strip()
        if not text:
            return None
        pose = c.get("pose")
        if pose not in POSE_NAMES:
            pose = POSE_DEFAULT.get(kind, "reading")
        by_kind[kind] = {"kind": kind, "text": text, "pose": pose}
    if set(by_kind) != _EXPECTED_KINDS:
        return None
    return [by_kind[k] for k in _KIND_ORDER]


async def _generate_for_nodes(conn, relay, model, nodes) -> tuple[int, int, int]:
    """Generate + upsert commentary for a pre-selected list of nodes.

    Shared by the on-demand job (`run_generate_commentary`) and the idle sweep
    (`run_commentary_sweep`) so the fail-silent-per-node semantics live in one place.
    Returns (made, skipped, failed)."""
    made = skipped = failed = 0
    for nt, node_id, name, ctx_args in nodes:
        # Context build + the LLM call share one try, so a bad node (query
        # error, timeout, malformed output) is skipped, never fatal.
        try:
            ctx = _build_context(conn, nt, ctx_args)
            user = f"Archive entry — {nt}: {name}\n\n{ctx}\n\n{TASK}"
            raw = await relay.complete_structured(
                model=model, system=PERSONA,
                messages=[{"role": "user", "content": user}],
                max_tokens=1500, temperature=0.7, schema=SCHEMA,
                tool_name="magos_commentary",
                tool_description="Three Magos Lex annotations of an archive entry",
            )
        except Exception as e:
            failed += 1
            logger.warning("generate_commentary: %s %s failed: %s", nt, node_id, e)
            continue
        comments = _clean_payload(raw)
        if not comments:
            skipped += 1
            logger.warning("generate_commentary: %s %s produced no usable comments", nt, node_id)
            continue
        src_hash = hashlib.sha256((model + "\n" + ctx).encode("utf-8")).hexdigest()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO node_commentary "
                "(node_type, node_id, comments_json, model, source_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (nt, node_id, json.dumps(comments, ensure_ascii=False), model, src_hash))
            conn.commit()
        except Exception as e:
            # Persisting is fail-silent-per-node too. A transient write lock here
            # would otherwise propagate and abandon every node still in the loop —
            # unlike a context/LLM error above, which only skips its own node. Roll
            # back so the shared connection stays usable, count it, and move on;
            # only_missing=True picks it up on a re-run.
            conn.rollback()
            failed += 1
            logger.warning("generate_commentary: %s %s persist failed: %s", nt, node_id, e)
            continue
        made += 1
    return made, skipped, failed


async def run_generate_commentary(job: dict, db_path: str) -> None:
    settings = get_settings()
    config = json.loads(job["config"]) if job["config"] else {}
    # Entities are deferred (very long tail; not worth the backfill for now) — the
    # default scope is domains + repos. A caller can still pass entity explicitly.
    node_types = config.get("node_types") or ["domain", "collection"]
    limit = int(config.get("limit", 50))
    only_missing = config.get("only_missing", True)
    model = config.get("model") or settings.extraction_model

    relay = Relay.from_settings(settings)
    conn = get_connection(db_path)
    made = skipped = failed = 0

    try:
        for node_type in node_types:
            nodes = _select_nodes(conn, node_type, limit, only_missing)
            logger.info("generate_commentary: %d %s nodes to process (limit=%d, only_missing=%s)",
                        len(nodes), node_type, limit, only_missing)
            m, s, f = await _generate_for_nodes(conn, relay, model, nodes)
            made += m; skipped += s; failed += f

        logger.info("generate_commentary done: made=%d skipped=%d failed=%d (model=%s)",
                    made, skipped, failed, model)
    finally:
        conn.close()


# ---- idle sweep ----

DEFAULT_SWEEP_NODE_TYPES = ("domain", "collection")


def _select_missing_batch(conn, node_types, batch_size):
    """Up to `batch_size` still-uncommented nodes across `node_types` (only_missing)."""
    nodes = []
    for nt in node_types:
        if len(nodes) >= batch_size:
            break
        nodes.extend(_select_nodes(conn, nt, batch_size - len(nodes), only_missing=True))
    return nodes[:batch_size]


async def run_commentary_sweep(db_paths, relay, model, *, batch_size,
                               node_types=DEFAULT_SWEEP_NODE_TYPES) -> dict:
    """One bounded batch of Magos Lex commentary for the FIRST workspace with missing nodes.

    Mirrors the normalization-judge sweep: idle-only work in small chunks, one workspace
    per pass so a big backlog drains across passes without ever blocking the poll loop or
    starving real jobs. `only_missing` is implicit, so this is idempotent — a fully
    commented workspace is skipped after one cheap indexed query, and newly-added
    domains/collections are picked up on a later pass. Returns
    {made, skipped, failed, workspace}; workspace is None when nothing needed work."""
    for db_path in db_paths:
        conn = get_connection(db_path)
        try:
            nodes = _select_missing_batch(conn, node_types, batch_size)
            if not nodes:
                continue
            made, skipped, failed = await _generate_for_nodes(conn, relay, model, nodes)
            return {"made": made, "skipped": skipped, "failed": failed, "workspace": db_path}
        finally:
            conn.close()
    return {"made": 0, "skipped": 0, "failed": 0, "workspace": None}
