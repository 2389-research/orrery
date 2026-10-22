"""Pure (stdlib-only) helpers for slice-aware MCP tools.

Deliberately does NOT import `mcp` / FastMCP / `mcp_server`, so it is
unit-testable in environments without the MCP SDK.

Conventions:
- slice_query(...) returns a dict of only the non-empty params, in a stable
  order (since, until, as_of, window, domain, collection). Pass it straight to
  `call_api(path, params=...)`; httpx encodes it.
- to_qs(d) renders such a dict as a string fragment with a leading "&"
  (empty string when empty), safe to append to a path that already has a "?".
- normalize_list(resp, key) returns (items, scope). Error dicts ({"detail":..})
  yield ([], None); callers that need to surface the error must check
  `"detail" in resp` first.
"""
from urllib.parse import urlencode

_ORDER = ("since", "until", "as_of", "window", "domain", "collection")


def slice_query(*, since: str = "", until: str = "", as_of: str = "",
                window: str = "", domain: str = "", collection: str = "") -> dict:
    vals = {"since": since, "until": until, "as_of": as_of,
            "window": window, "domain": domain, "collection": collection}
    return {k: vals[k] for k in _ORDER if vals[k]}


def to_qs(d: dict) -> str:
    """Render params as '&k=v&...' (URL-encoded); '' when empty."""
    return "&" + urlencode(d) if d else ""


def normalize_list(resp, key: str) -> tuple[list, dict | None]:
    """Accept a bare list or {key: [...], "scope": {...}}; return (items, scope)."""
    if isinstance(resp, list):
        return resp, None
    if isinstance(resp, dict):
        items = resp.get(key)
        return (items if isinstance(items, list) else []), resp.get("scope")
    return [], None
