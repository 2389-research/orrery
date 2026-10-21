"""Tests for the mcp-free slice helpers (imports only src.mcp_slice)."""
from urllib.parse import urlencode, parse_qs

from src.mcp_slice import slice_query, normalize_list, to_qs


def test_slice_query_only_set_params_stable_order():
    q = slice_query(window="7d", since="2026-01-01", domain="")
    assert q == {"since": "2026-01-01", "window": "7d"}
    assert list(q) == ["since", "window"]


def test_slice_query_all_blank():
    assert slice_query() == {}
    assert to_qs({}) == ""


def test_to_qs_encodes_special_chars_round_trip():
    q = slice_query(domain="software/web dev", collection="a&b")
    qs = to_qs(q)
    assert qs.startswith("&")
    assert " " not in qs and "%20" in qs or "+" in qs
    assert parse_qs(qs[1:]) == {"domain": ["software/web dev"], "collection": ["a&b"]}


def test_normalize_list_bare():
    assert normalize_list([1, 2], "entities") == ([1, 2], None)


def test_normalize_list_wrapped():
    scope = {"a": 1}
    assert normalize_list({"entities": [1], "scope": scope}, "entities") == ([1], scope)
    assert normalize_list({"documents": [2], "scope": scope}, "documents") == ([2], scope)


def test_normalize_list_error_and_missing():
    assert normalize_list({"detail": "x"}, "entities") == ([], None)
    assert normalize_list({"scope": {"a": 1}}, "entities") == ([], {"a": 1})
