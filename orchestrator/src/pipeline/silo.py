# ABOUTME: Resolve documents.silo_id at ingest and the SQL fragment that scopes
# ABOUTME: normalization to a silo. MIRROR of worker/src/silo.py: everything below
# ABOUTME: this header must stay byte-identical (enforced by
# ABOUTME: orchestrator/tests/test_schema_mirror.py), like classifier.py.

def resolve_silo_id(source_id, collection_id):
    """Precedence: source_id > collection_id > None (spec §5)."""
    return source_id or collection_id or None


# SQL fragment: <entity_col> has at least one source in the silo bound to a POSITIONAL ? (NULL-safe).
# Positional `?` everywhere (sqlite raises if one statement mixes named + numbered params).
def silo_match(entity_col: str) -> str:
    return (f"EXISTS (SELECT 1 FROM entity_sources es JOIN documents d ON d.id = es.document_id "
            f"WHERE es.entity_id = {entity_col} AND d.silo_id IS ?)")


# Provenance kind: a property of the SOURCE (the silo), not the document — a user can
# re-classify a source later, and a per-doc copy would go stale. Resolved by consumers
# via the silo_kind view (documents.silo_id -> silo_kind.kind), never materialized
# per-document (spec: per-source silos + provenance, task 9).
KINDS = {"neutral_summary", "human_vault", "agent_report", "human_reviewed"}

# Flow default keyed by featurizer / source type (spec §4.1).
FLOW_DEFAULT_KIND = {
    "vault": "human_vault",
    "repo": "neutral_summary", "git_repo": "neutral_summary",
    "tracker": "neutral_summary", "tracker_run": "neutral_summary",
    "codesum": "neutral_summary", "tracksum": "neutral_summary",
    "pdf": "neutral_summary",
    "ccvault": "agent_report",
}


def flow_default_kind(source_type):
    return FLOW_DEFAULT_KIND.get(source_type)  # None if unknown flow


def resolve_kind(flow_default, override=None):
    """override (a first-class provenance_kind value) wins IF valid; else the flow default."""
    return override if override in KINDS else flow_default
