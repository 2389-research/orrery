# ABOUTME: Shared TimeScope parser — resolves since/until/as_of into a SQLite-comparable
# ABOUTME: UTC window over documents.created_at. One helper, used by every time-scoped read.

"""Parse `since` / `until` / `as_of` into a resolved window.

A value may be:
- ISO-8601: `2026-10-19T13:30:00Z` or `2026-10-19T13:30:00` (the `Z`/offset is honored,
  then normalized to UTC);
- a bare date: `2026-10-19` (→ midnight UTC that day);
- a relative duration against server "now": `-<n><unit>` where unit is `m` (minutes),
  `h` (hours) or `d` (days) — e.g. `-30m`, `-24h`, `-7d` (for agents). Note `m` is minutes,
  not months; only this exact lowercase, leading-`-` form is accepted (`-1w`/`+7d`/`-1D`
  raise TimeScopeError → HTTP 400).

The output is the SQLite storage format `YYYY-MM-DD HH:MM:SS` in UTC, so it compares
directly against `documents.created_at` (which SQLite stores in exactly that shape).
`as_of` is sugar for `until=as_of` and cannot be combined with `until`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_SQLITE_FMT = "%Y-%m-%d %H:%M:%S"
_REL_RE = re.compile(r"^-(\d+)([mhd])$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days"}


class TimeScopeError(ValueError):
    """A time parameter was malformed or an illegal combination was requested."""


def _now() -> datetime:
    """Server 'now', in UTC. A seam so tests can pin it."""
    return datetime.now(timezone.utc)


def _parse_one(value: str) -> str:
    """Resolve a single time token to the SQLite UTC string, or raise TimeScopeError."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    # Relative duration, e.g. -7d.
    rel = _REL_RE.match(value)
    if rel:
        amount, unit = int(rel.group(1)), rel.group(2)
        dt = _now() - timedelta(**{_UNIT[unit]: amount})
        return dt.astimezone(timezone.utc).strftime(_SQLITE_FMT)

    # ISO-8601 (with or without time / Z / offset) or a bare date.
    iso = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise TimeScopeError(f"unrecognized time value: {value!r}")
    # A bare date or a naive datetime is interpreted as UTC; an aware one is converted.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_SQLITE_FMT)


def resolve_scope(since: str | None = None, until: str | None = None,
                  as_of: str | None = None) -> dict:
    """Return `{"since": str|None, "until": str|None}` in SQLite UTC format.

    `as_of` is an alias for `until`; passing both raises. All three default to None,
    which means "no bound" — an all-time read, exactly today's behavior.
    """
    if as_of is not None and until is not None:
        raise TimeScopeError("pass either `until` or `as_of`, not both")
    if as_of is not None:
        until = as_of
    return {"since": _parse_one(since), "until": _parse_one(until)}
