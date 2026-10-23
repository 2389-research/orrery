# ABOUTME: Tests for the meridian pull-API reader — fake fetch, no network.
# ABOUTME: Fixtures are shaped after meridian/fixtures/api (envelopes, agent_id, raw dicts).

from src.jobs.meridian_reader import (
    get_agents, get_records, get_session, iter_agent_segments, list_changed_sessions,
)


def _rec(uuid, agent="main", inherited=False, text="hi", ts="2026-10-01T00:00:00Z", typ="user"):
    return {"uuid": uuid, "id": f"meridian:record/{uuid}", "agent_id": agent,
            "inherited": inherited, "sort_ts": ts, "seq": 1, "type": typ,
            "raw": {"type": typ, "uuid": uuid, "message": {"content": text}}}


def fake_fetch(routes):
    calls = []

    def fetch(path, params=None):
        calls.append((path, dict(params or {})))
        key = (path, (params or {}).get("page_token") or (params or {}).get("updated_after") or "")
        return routes[key]
    fetch.calls = calls
    return fetch


RECORDS = [
    _rec("m1", "main", text="main one", ts="2026-10-01T00:00:01Z"),
    _rec("m2", "main", typ="assistant", text="main two", ts="2026-10-01T00:00:02Z"),
    _rec("s1", "a1", text="sub one", ts="2026-10-01T00:00:03Z"),
]
AGENTS = [
    {"agent_id": "main", "agent_type": None, "parent_tool_use_id": None, "spawn_depth": 0},
    {"agent_id": "a1", "agent_type": "general-purpose", "parent_tool_use_id": "tu9", "spawn_depth": 1},
]


def test_dedupes_records_by_uuid_and_drops_inherited():
    f = fake_fetch({
        ("/v1/sessions/c1%2Fs1/records", ""): {
            "items": [_rec("u1"), _rec("x", inherited=True)], "has_more": True, "next_page_token": "t2"},
        ("/v1/sessions/c1%2Fs1/records", "t2"): {
            "items": [_rec("u1"), _rec("u2")], "has_more": False, "next_page_token": ""},
    })
    recs = get_records(f, "c1/s1")
    assert [r["uuid"] for r in recs] == ["u1", "u2"]


def test_segments_carry_stable_content_hash_and_agent_link():
    segs = iter_agent_segments(RECORDS, AGENTS)
    sub = [s for s in segs if s["agent_id"] == "a1"]
    assert [s for s in segs if s["agent_id"] == "main"]
    assert sub and sub[0]["parent_tool_use_id"] == "tu9" and sub[0]["spawn_depth"] == 1
    assert all(len(s["content_hash"]) == 64 for s in segs)
    assert "sub one" in sub[0]["text"] and "sub one" not in segs[0]["text"]
    assert iter_agent_segments(RECORDS, AGENTS)[0]["content_hash"] == segs[0]["content_hash"]


def test_segments_split_long_stream_with_ordered_seq():
    recs = [_rec(f"u{i}", text="x" * 400, ts=f"2026-10-01T00:00:{i:02d}Z") for i in range(10)]
    segs = iter_agent_segments(recs, AGENTS[:1], target_chars=1000)
    assert len(segs) > 1 and [s["seq"] for s in segs] == list(range(len(segs)))


def test_list_changed_sessions_pages_until_has_more_false():
    f = fake_fetch({
        ("/v1/sessions", ""): {"items": [{"session_key": "a/1"}, {"session_key": "a/2"}],
                               "next_cursor": "c1", "has_more": True},
        ("/v1/sessions", "c1"): {"items": [{"session_key": "a/3"}],
                                 "next_cursor": "c2", "has_more": False},
    })
    items, cur, more = list_changed_sessions(f, None)
    assert len(items) == 3 and more is False and cur == "c2"


def test_session_key_is_percent_encoded_in_paths():
    f = fake_fetch({("/v1/sessions/c1%2Fs1", ""): {"session_key": "c1/s1"},
                    ("/v1/sessions/c1%2Fs1/agents", ""): {"items": AGENTS, "has_more": False}})
    assert get_session(f, "c1/s1")["session_key"] == "c1/s1"
    assert get_agents(f, "c1/s1") == AGENTS


def test_spawn_depth_derived_from_parent_chain_when_absent():
    agents = [{"agent_id": "main", "parent_agent_id": None},
              {"agent_id": "a1", "parent_agent_id": "main", "parent_tool_use_id": "tu9"},
              {"agent_id": "a2", "parent_agent_id": "a1", "parent_tool_use_id": "tu10"}]
    recs = [_rec("x", "a2", text="deep")]
    assert iter_agent_segments(recs, agents)[0]["spawn_depth"] == 2
