# ABOUTME: Domain classification for repo/run ingest — classifies on the grounded
# ABOUTME: repo-level summary. MIRROR of orchestrator/src/pipeline/classifier.py:
# ABOUTME: everything below this header must stay byte-identical (enforced by
# ABOUTME: orchestrator/tests/test_schema_mirror.py), like db.py.

from orrery_relay import Relay

# The prompt is split so the big, identical prefix (instructions + reference
# vocabulary + tool schema) can be prompt-cached: classify_document sends it as a
# cache_control="ephemeral" block, so Bedrock caches ~3k tokens and every later
# call reads it at ~0.1x. The breakpoint sits BEFORE the existing-taxonomy block
# because that grows as the graph fills in (not cacheable). Ollama ignores
# cache_control (no caching, still correct).
CLASSIFICATION_PROMPT_STATIC = """You are a domain classifier for a COMPANY knowledge graph that holds code, documents, notes, reports — everything the organization knows. Given an excerpt (a code repo/module/file summary, OR a note/report/doc) and the existing taxonomy, assign it to domain paths.

Domain paths are hierarchical, lowercase, "/"-separated, using hyphenated singular nouns:
  region/category/topic   (e.g. software/backend/rest-api, business/finance/budgeting, product/product-management/roadmap)

Two facets:
  PRIMARY  = the main subject — what this content IS about. It can be in ANY region (software, business, product, operations, people, research, ...), not just software.
  SECONDARY (0-3) = extra context: a second subject area, the industry it serves (under `industry/`), or a notable technology/topic.

Classify by SUBJECT, not by employer. Even at a software company, plenty of notes are not about software: a hiring rubric or interview scorecard is `people/hiring`; a contract/legal/policy doc is `business/legal-compliance`; budgets/expenses/runway are `business/finance`; office, vendor, or logistics notes are `operations/...`; a roadmap or spec is `product/...`. Pick the region that best fits the subject.

Naming rules (follow exactly — consistent naming is what lets equivalent content from different sources merge into one node):
  - all lowercase; words joined by single hyphens; singular nouns (rest-api, not REST_APIs or rest_apis)
  - REUSE a path from the reference vocabulary or the existing taxonomy whenever it fits — never invent a near-duplicate (use `llm-orchestration`, not `llm_agents` or `agent-coordination`)
  - be specific: prefer `business/finance/budgeting` over just `business/finance`

REFERENCE VOCABULARY — a strong baseline. Reuse these; you MAY add a new topic (or, when nothing fits, a new category or region) following the same naming rules:
{reference_vocab}

industry/<sector> — use as SECONDARY when the content clearly serves a sector: fintech, healthtech, edtech, ecommerce, gaming, media-streaming, adtech-martech, govtech-civic, logistics-supply-chain, proptech, hr-tech, legal-tech, biotech, energy-climate, telecom, security-defense

Assign 1 primary domain, 0-3 secondary domains, and a confidence 0.0-1.0.

If (and ONLY if) the excerpt is a CODE REPOSITORY summary, ALSO identify SUBDOMAINS (4-8): finer leaf paths capturing the DISTINCT internal components/topics within the repo (a parser, an async-runtime, ffi-bindings, a cli) — individual files are placed by matching them to these, so make them genuinely distinct facets, not synonyms of the primary. For a single note/document, leave subdomains empty.

Worked examples:
- A React component library with themeable design tokens -> primary software/frontend/design-systems; secondary [software/frontend/accessibility]; subdomains [software/frontend/state-management, software/frontend/accessibility, software/developer-tools/build-systems, software/testing-qa/unit-testing]
- A Django service that processes card payments and runs fraud checks -> primary software/backend/rest-api; secondary [software/security/threat-detection, industry/fintech]; subdomains [software/backend/authentication, software/security/threat-detection, software/databases/relational, software/backend/background-jobs]
- A quarterly board report on runway and hiring plans -> primary business/finance/financial-planning; secondary [people/hiring/recruiting, business/strategy/okrs-goals]; subdomains []
- A meeting note deciding Q3 product roadmap priorities -> primary product/product-management/roadmap; secondary [product/product-management/prioritization]; subdomains []
- A doc describing the internal on-call rotation and incident process -> primary operations/internal-operations/processes-workflows; secondary [software/devops-infra/site-reliability]; subdomains []

Now classify the document in the next message, reusing an existing-taxonomy path ONLY when it is the SAME topic."""

# Title lives in the DYNAMIC half, with the excerpt: it varies per document, so
# putting it in the cached static block would invalidate the cache every call.
CLASSIFICATION_PROMPT_DYNAMIC = """Existing taxonomy (already in this graph — reuse a path from here ONLY when it is the SAME topic; it is NOT a preference, and a graph full of software paths must not pull unrelated content toward software):
{taxonomy}

Title: {title}

Document:
{excerpt}"""

# Back-compat: the full single-string prompt (used by tests / ad-hoc renders).
CLASSIFICATION_PROMPT = CLASSIFICATION_PROMPT_STATIC + "\n\n" + CLASSIFICATION_PROMPT_DYNAMIC

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_domain": {
            "type": "string",
            "description": "Primary domain path (region/parent/subdomain)",
        },
        # The list caps are enforced in code (see _clamp_lists), NOT with `maxItems`
        # here. Measured against gemma4:26b, `maxItems` in a schema Ollama compiles to
        # a decoding grammar is unreliable in both directions: on a single-property
        # schema it capped the array by force — truncating a string mid-token into
        # malformed JSON in one run, and in others cramming ten values into one
        # element ("_RUBY_SWIFT_KOTLIN_RUST_GO_...") to fit the limit — while on THIS
        # schema it was ignored outright and returned 12 subdomains against a cap of 8.
        # A constraint that sometimes corrupts the payload and sometimes does nothing
        # is worse than no constraint, so the counts stay advisory here (stated in the
        # prompt, which is where a model can comply gracefully) and binding after the
        # parse, where it is deterministic on every backend.
        "secondary_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "0-3 secondary domain paths",
        },
        "subdomains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "4-8 finer leaf paths for this repo's distinct internal components/topics (used to place individual files)",
        },
        "confidence": {
            "type": "number",
            "description": "Classification confidence 0.0-1.0",
        },
    },
    "required": ["primary_domain", "secondary_domains", "confidence"],
}

# Ollama context window for classification. This is a CAP, not a floor — a correction
# to what this comment previously claimed. Measured on the current Ollama: with num_ctx
# UNSET the context is sized to the prompt (a 15k-token prompt was counted in full), and
# truncation happens only when num_ctx is explicitly set BELOW the prompt — at 4096 a
# 5.1k-token prompt was counted as 2051, silently discarding the instructions and the
# reference vocabulary from the head and leaving the model answering from the excerpt
# alone. So the value here has to stay comfortably ABOVE the largest prompt this
# function can build, or it becomes the bug it was meant to prevent.
#
# It is set at all for portability: older Ollama releases default to 2048/4096 rather
# than auto-sizing, and this runs against whatever Ollama the user has. Headroom check:
# ~2.4k static instructions + vocabulary, plus an existing-taxonomy block that grows
# with the graph (~2.3k at 145 domains). If a deployment's taxonomy ever approaches
# ~12k tokens, raise this — do not remove the num_ctx and hope the default auto-sizes.
_OLLAMA_OPTIONS = {"num_ctx": 16384}

# Binding caps for the list facets, applied after the parse. The prompt asks for these
# counts and a healthy call respects them; this is the backstop for when it does not.
# It is not hypothetical: every secondary domain becomes a `document_domains` row, so an
# unbounded list writes unbounded graph edges. A left-truncated local call — where the
# sentence stating "0-3" is the part Ollama dropped — returned 74 of them.
_MAX_SECONDARY_DOMAINS = 3
_MAX_SUBDOMAINS = 8


def _clamp_lists(result: dict) -> dict:
    """Trim the list facets to their documented maxima, tolerating a malformed payload.

    Defensive about types rather than trusting the schema: a local model can return a
    bare string or a null where an array was declared, and this sits directly upstream
    of code that iterates the value. Clamps in place and returns the same dict, so a
    caller reading `result` still sees the trimmed lists.
    """
    # A JSON array or scalar parses successfully but is not a classification, and every
    # caller indexes this by key — return the empty dict the callers already handle.
    if not isinstance(result, dict):
        return {}
    for key, cap in (("secondary_domains", _MAX_SECONDARY_DOMAINS),
                     ("subdomains", _MAX_SUBDOMAINS)):
        value = result.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            result[key] = []
            continue
        # Keep only genuine strings; order is preserved, so trimming keeps the model's
        # own ranking rather than an arbitrary subset.
        result[key] = [v for v in value if isinstance(v, str)][:cap]
    return result


async def classify_document(
    relay: Relay,
    title: str,
    excerpt: str,
    existing_taxonomy: list[str],
    model: str,
) -> dict:
    from .taxonomy import reference_vocab_text
    taxonomy_str = "\n".join(f"  - {d}" for d in existing_taxonomy) if existing_taxonomy else "  (empty — propose new domains)"

    static = CLASSIFICATION_PROMPT_STATIC.format(reference_vocab=reference_vocab_text())
    dynamic = CLASSIFICATION_PROMPT_DYNAMIC.format(
        taxonomy=taxonomy_str, title=title, excerpt=excerpt)
    result = await relay.complete_structured(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content": [
            # Cache breakpoint: the static instructions + reference vocab (+ tool
            # schema, which precedes it) are cached and re-read at ~0.1x per call.
            {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic},
        ]}],
        schema=CLASSIFICATION_SCHEMA,
        tool_name="classify_document",
        tool_description="Classify a document into domain paths for the knowledge graph",
        ollama_options=_OLLAMA_OPTIONS,
    )
    return _clamp_lists(result)


IMAGE_CLASSIFICATION_PROMPT = """You are a classifier for a knowledge graph system. Look at this image and classify it into domain paths.

Existing taxonomy:
{taxonomy}

Rules:
- Use existing domains when they fit
- Create new domain paths if the image covers a topic not in the taxonomy
- Domain paths are hierarchical: region/parent/subdomain
- Consider: subject matter, setting, activity, objects visible, any text in the image
- An image can have 1 primary and 0-3 secondary domains
"""


async def classify_image(
    relay: Relay,
    image_base64: str,
    media_type: str,
    existing_taxonomy: list[str],
    model: str,
    caption: str | None = None,
) -> dict:
    taxonomy_str = "\n".join(f"  - {d}" for d in existing_taxonomy) if existing_taxonomy else "  (empty — propose new domains)"
    text_prompt = IMAGE_CLASSIFICATION_PROMPT.format(taxonomy=taxonomy_str)
    if caption:
        text_prompt += f"\n\nUser-provided caption: {caption}"

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_base64}},
        {"type": "text", "text": text_prompt},
    ]

    # Same schema as the text path, so the same caps apply: an image's secondary
    # domains become `document_domains` rows exactly like a document's, and nothing
    # downstream distinguishes the two. Deliberately NOT given `ollama_options` —
    # this prompt omits the reference vocabulary (~1.4k tokens smaller). The relay
    # keeps `num_predict` off the vision path (it breaks gemma4 vision); it does apply
    # the process-wide `num_ctx` pin (verified on gemma4:26b vision, 2026-10-30).
    result = await relay.complete_structured(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content": content}],
        schema=CLASSIFICATION_SCHEMA,
        tool_name="classify_document",
        tool_description="Classify an image into domain paths for the knowledge graph",
    )
    return _clamp_lists(result)
