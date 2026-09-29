# ABOUTME: Query expansion runs on the configured small/extraction tier, not a hardcoded model.
import pytest

from src.pipeline.search import expansion


class FakeRelay:
    def __init__(self):
        self.model = None

    async def complete_structured(self, **kwargs):
        self.model = kwargs.get("model")
        return {"sub_queries": ["a", "b"]}


@pytest.mark.asyncio
async def test_expand_query_uses_configured_extraction_tier(monkeypatch):
    # No longer hardcoded to Haiku — follows EXTRACTION_MODEL so a remote-model deploy
    # loads no local LLM for query expansion.
    monkeypatch.setenv("EXTRACTION_MODEL", "small-tier-model")
    r = FakeRelay()
    out = await expansion.expand_query(r, "hello world", max_sub_queries=5)
    assert r.model == "small-tier-model"
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_expand_query_explicit_model_overrides(monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODEL", "small-tier-model")
    r = FakeRelay()
    await expansion.expand_query(r, "hello world", model="explicit-x")
    assert r.model == "explicit-x"
