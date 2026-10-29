# ABOUTME: Live smoke test of the openai backend against LunaRoute (gw.lunaroute.com).
# ABOUTME: Skipped unless LUMAROUTE_API_KEY / LUNAROUTE_API_KEY is set; makes a few cheap real calls.
import os

import pytest

from orrery_relay import Relay

KEY = os.environ.get("LUMAROUTE_API_KEY") or os.environ.get("LUNAROUTE_API_KEY")

pytestmark = pytest.mark.skipif(not KEY, reason="LUMAROUTE_API_KEY / LUNAROUTE_API_KEY not set")

MODEL = "deepseek-4.1-flash"
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


@pytest.fixture
def relay():
    return Relay(
        backend="openai",
        gateway_url="https://gw.lunaroute.com/v1",
        gateway_api_key=KEY,
        reasoning_effort="none",
    )


async def test_complete(relay):
    resp = await relay.complete(
        model=MODEL,
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=64,
    )
    assert resp.text.strip()
    assert "PONG" in resp.text.upper()
    assert resp.output_tokens > 0


async def test_complete_structured(relay):
    result = await relay.complete_structured(
        model=MODEL,
        messages=[{"role": "user", "content": "What is the capital of France? Give it as the answer."}],
        max_tokens=256,
        schema=ANSWER_SCHEMA,
        tool_name="record_answer",
    )
    assert isinstance(result, dict)
    assert isinstance(result.get("answer"), str) and result["answer"].strip()


def test_complete_sync(relay):
    resp = relay.complete_sync(
        model=MODEL,
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=64,
    )
    assert resp.text.strip()


async def test_reasoning_off_batch_prompt_not_truncated(relay):
    """Regression guard: with reasoning_effort=none a batch-style prompt must not burn
    the token budget on hidden thinking (which truncated structured output)."""
    items = "\n".join(
        f"{i}. {name}"
        for i, name in enumerate(
            ["Redis", "PostgreSQL", "Kafka", "Docker", "Kubernetes", "FastAPI",
             "SQLite", "Ollama", "Next.js", "Terraform", "Grafana", "RabbitMQ"],
            1,
        )
    )
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "category": {"type": "string"},
                    },
                    "required": ["index", "category"],
                },
            }
        },
        "required": ["items"],
    }
    prompt = (
        "Assign each numbered software tool a short category (one or two words, e.g. "
        "'database', 'queue'). Return one entry per item.\n\n" + items
    )
    max_tokens = 2048
    result = await relay.complete_structured(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        schema=schema,
        tool_name="categorize",
    )
    assert isinstance(result, dict)
    assert len(result["items"]) == 12

    # Reasoning actually off: a plain reply is ~200 tokens; thinking would eat the budget.
    resp = await relay.complete(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    assert resp.text.strip()
    assert resp.output_tokens < 1500
