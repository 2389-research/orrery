"""The shared TimeScope parser (#116, trimmed).

One helper resolves `since`/`until`/`as_of` from ISO-8601, a bare date, or a relative
duration (`-7d`) into the SQLite storage format (`YYYY-MM-DD HH:MM:SS`, UTC) so the
window compares correctly against `documents.created_at`.
"""

from datetime import datetime, timezone

import pytest

from src.pipeline.time_scope import resolve_scope, TimeScopeError


def test_bare_date_is_midnight_utc():
    assert resolve_scope(since="2026-10-19")["since"] == "2026-10-19 00:00:00"


def test_iso_with_t_and_z():
    assert resolve_scope(until="2026-10-20T13:30:00Z")["until"] == "2026-10-20 13:30:00"


def test_relative_hours(monkeypatch):
    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("src.pipeline.time_scope._now", lambda: now)
    assert resolve_scope(since="-24h")["since"] == "2026-10-19 12:00:00"


def test_relative_days_and_minutes(monkeypatch):
    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("src.pipeline.time_scope._now", lambda: now)
    assert resolve_scope(since="-7d")["since"] == "2026-10-13 12:00:00"
    assert resolve_scope(since="-30m")["since"] == "2026-10-20 11:30:00"


def test_as_of_resolves_to_until():
    out = resolve_scope(as_of="2026-10-19")
    assert out["until"] == "2026-10-19 00:00:00"
    assert out["since"] is None


def test_until_and_as_of_conflict():
    with pytest.raises(TimeScopeError):
        resolve_scope(until="2026-10-19", as_of="2026-10-20")


def test_empty_is_all_none():
    assert resolve_scope() == {"since": None, "until": None}


def test_bad_value_raises():
    with pytest.raises(TimeScopeError):
        resolve_scope(since="not-a-date")
