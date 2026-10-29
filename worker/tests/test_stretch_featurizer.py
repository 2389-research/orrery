import json

import pytest

from src.jobs.stretch_featurizer import build_prompt, enrich_batch, normalize_facet


class _Resp:
    def __init__(self, text):
        self.text = text


class FakeRelay:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self.responses.pop(0))


def _stretch(sid, text="we should use the ticket board for everything"):
    return {"stretch_id": sid, "doc_id": "d", "mic_id": "m1", "start": "10:05:30",
            "end": "10:08:00", "text": text, "content_hash": "h"}


def _facet(sid, quote="ticket board"):
    return {"leaf_id": sid, "summary": "They discussed the ticket board.",
            "topics": [{"tag": "ticket board", "statement": "Use it for everything."}],
            "ideas": [{"kind": "proposal", "text": "Use the board.", "quote": quote, "tag": "ticket board"}],
            "nouns": ["ticket board"]}


def test_normalize_verbatim_quote_ok_and_topics_renamed():
    f = normalize_facet(_facet("a", "We should USE the  ticket board"), "we should use the ticket board for everything")
    assert f["ideas"][0]["quote_ok"] is True
    assert "topics" not in f and f["concepts"] == [{"tag": "ticket board", "statement": "Use it for everything."}]
    assert set(f["ideas"][0]) == {"kind", "text", "quote", "quote_ok"}


def test_normalize_paraphrased_quote_not_ok():
    f = normalize_facet(_facet("a", "use the kanban board"), "we should use the ticket board for everything")
    assert f["ideas"][0]["quote_ok"] is False


def test_normalize_none_summary_empties_lists():
    f = normalize_facet({**_facet("a"), "summary": "none"}, "x")
    assert f["summary"] == "none"
    assert f["concepts"] == [] and f["ideas"] == [] and f["nouns"] == []


def test_normalize_coerces_bad_shapes():
    f = normalize_facet({"summary": "s", "topics": "oops", "ideas": [{"kind": "bogus", "text": "t"}, "x"],
                         "nouns": ["a", 3]}, "t")
    assert f["concepts"] == [] and f["ideas"] == [] and f["nouns"] == ["a"]


async def test_enrich_batch_retry_fills_missing():
    s = [_stretch("a"), _stretch("b")]
    relay = FakeRelay([json.dumps({"facets": [_facet("a")]}),
                       "```json\n" + json.dumps({"facets": [_facet("b")]}) + "\n```"])
    out = await enrich_batch(relay, "gemma", s)
    assert set(out) == {"a", "b"} and len(relay.calls) == 2
    assert "b" in relay.calls[1]["messages"][0]["content"]
    assert "[a]" not in relay.calls[1]["messages"][0]["content"].split("STRETCHES:")[-1]


async def test_enrich_batch_missing_twice_omitted_no_raise():
    relay = FakeRelay([json.dumps({"facets": [_facet("a")]}), "not json at all"])
    out = await enrich_batch(relay, "gemma", [_stretch("a"), _stretch("b")])
    assert set(out) == {"a"}


def test_build_prompt_contains_ids_and_system_text():
    p = build_prompt([_stretch("abc123"), _stretch("def456")])
    assert "abc123" in p and "def456" in p
    assert "write a careful facet for EACH stretch" in p
    assert "[abc123] (m1 10:05)" in p


@pytest.mark.asyncio
async def test_non_string_leaf_id_is_skipped_not_raised():
    from src.jobs.stretch_featurizer import _call
    st = [{"stretch_id": "s1", "text": "hello there", "start": "a", "end": "b"}]
    bad = json.dumps([{"leaf_id": [], "summary": "x"}, {"leaf_id": {"a": 1}, "summary": "y"}])

    class R:
        async def complete(self, **kw):
            return type("T", (), {"text": bad})()

    assert await _call(R(), "m", st) == {}
