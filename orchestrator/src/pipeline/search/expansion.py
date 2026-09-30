# ABOUTME: Stage 0: Query expansion via the configured small/extraction tier.
# ABOUTME: Uses tool use for guaranteed valid JSON output.

from orrery_relay import Relay

EXPANSION_SCHEMA = {
    "type": "object",
    "properties": {
        "sub_queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Expanded search sub-queries",
        },
    },
    "required": ["sub_queries"],
}


async def expand_query(
    relay: Relay,
    query: str,
    max_sub_queries: int = 5,
    model: str | None = None,
) -> list[str]:
    """Expand a query into multiple sub-queries.

    Runs on the configured small/fast tier (`EXTRACTION_MODEL`) rather than a hardcoded
    model, so query expansion follows whatever provider/model the deployment is set to
    (and loads no local LLM when that tier points at a remote provider). Pass `model` to
    override; otherwise it resolves from settings.
    """
    if model is None:
        from ...config import get_settings
        model = get_settings().extraction_model
    result = await relay.complete_structured(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Given this search query, generate {max_sub_queries} sub-queries that would help find all relevant information. Include:
- The original query (cleaned up)
- Synonym variations
- Related concepts
- More specific versions of vague terms

Query: {query}""",
        }],
        schema=EXPANSION_SCHEMA,
        tool_name="expand_query",
        tool_description="Generate search sub-queries to expand the original query",
    )
    return result.get("sub_queries", [query])[:max_sub_queries]
