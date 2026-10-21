# Graph Slices PR 3 — Search respects the active slice + MCP parity

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Make `/search` respect the active slice (virtual noosphere + full ad-hoc facets), and bring the MCP tools to parity — pass slice/time params through, fix the shape bug under views, add `list_documents` + `create_view`, and surface view metadata — so an agent working inside a selected view gets sliced results across the whole surface.

**Architecture:** `/search` consults `auth.slice` (today it only post-filters on explicit `since/until/as_of`) and gains `window/domain/collection`, post-filtering via the slice engine (`_slice_docs`/`in_slice` entity filter). MCP: `call_api` grows `params`; an importable helper builds the slice query-string and normalizes the bare-list-vs-`{..,scope}` response shapes (unit-testable without `mcp`); read tools thread the params; new `list_documents`/`create_view` tools; `list_noospheres` shows `kind/parent/slice`.

**Tech Stack:** FastAPI, SQLite, FastMCP (not importable in this env — see Testing), pytest.

Builds on PR 1 (merged) + PR 2 (`feat/graph-slices-pr2`, this branch is stacked on it). Design: `docs/superpowers/specs/2026-10-21-graph-slices-virtual-noospheres-design.md` §4. Map refs below are `M = orchestrator/src/mcp_server.py`.

## Testing reality (read first)
- The 3 `tests/test_mcp_*.py` files `import src.mcp_server` → FastMCP → **collection error in this env** (`mcp` not installed). So MCP tool tests can't run locally; they run in CI.
- Mitigations: (a) put the pure slice-query + shape-normalize logic in an **importable, mcp-free module** and unit-test it locally; (b) write MCP tool tests in the existing monkeypatch-`call_api` style (run in CI); (c) add `pytest.importorskip("mcp")` to the 3 mcp test files so the suite stops erroring and they SKIP locally / RUN in CI. Verify everything runnable; rely on the extracted-helper tests + the final review for the rest.

## Scope
**In:** `/search` slice-aware (auth.slice + `since/until/as_of/window/domain/collection`); MCP `call_api` params; read tools (`search_knowledge_graph`, `get_entity`, `list_entities`, `get_neighborhood`, `get_shared_context`, `find_paths`, `get_subgraph`, `explore_domain`) thread slice params + echo scope; **fix `list_entities` (and defensively `get_document`) for the `{..,scope}` shape**; `list_documents` MCP tool; `create_view` MCP tool; `list_noospheres` shows `kind/parent/slice`; `importorskip` cleanup.
**Deferred (note in PR):** surfacing `first_seen`/`last_seen` via MCP (needs new HTTP on `/entities*`); multi-noosphere search (#116 §8, absent); write-tool pre-guard on virtual ids (the HTTP read-only 400 already catches it — MCP just surfaces the error).

---

## Task 1: `/search` respects the active slice (HTTP, fully testable)
**Files:** `orchestrator/src/routes/search.py` (`search_query` ~L16); test `orchestrator/tests/test_search_slice.py`.

- [ ] **Step 1 — failing tests.** Seed docs/entities/chunks across two dates with an entity only last week. Under a this-week slice (both ad-hoc `?window=1w` AND a virtual-noosphere `X-Workspace-Id`), `/search` returns only in-slice entity/chunk hits (the last-week-only entity absent); echoes `scope`; unsliced `/search` unchanged. `?domain=X`/`?collection=` narrow too. Bad window → 400.
- [ ] **Step 2 — FAIL.**
- [ ] **Step 3 — implement.** Add `window/domain/collection` params (it has `since/until/as_of`). Resolve the effective slice = `auth.slice` else `parse_slice(params)` (the `_active_slice` helper from `routes/entities.py` — import it). When sliced, `ensure_slice_doc_temp_table(store.conn, slc)` and post-filter the search results to in-slice docs/entities (reuse the existing #118 post-filter path, extended from time-only to the full `_slice_docs` set; drop entities with no in-slice source, chunks whose doc isn't in-slice). Echo `scope`. Unsliced path unchanged.
- [ ] **Step 4 — PASS**, plus `pytest tests/ -k search` (minus mcp files) green.
- [ ] **Step 5 — commit** `feat(search): /search respects the active slice (virtual noosphere + facets)`.

## Task 2: importable MCP slice helper + `call_api` params
**Files:** create `orchestrator/src/mcp_slice.py` (NO `mcp` import); modify `mcp_server.py` `call_api` (M:105); test `orchestrator/tests/test_mcp_slice_helper.py`.

- [ ] **Step 1 — failing tests** (local, no `mcp`): `slice_query(since=..,window=..,...)` → a query-string fragment with only non-empty params, URL-encoded, stable order; `normalize_list(resp, key)` → returns the list from either a bare list OR `{key:[...], "scope":...}`, and exposes the scope. Empty/None → "".
- [ ] **Step 2 — FAIL.**
- [ ] **Step 3 — implement** `mcp_slice.py` with `slice_query(**facets)` and `normalize_list(resp, key) -> (items, scope)`. Add `params: dict | None = None` to `call_api` and pass to httpx (covers POST `/graph/subgraph` query params). Keep back-compat (path-built queries still work).
- [ ] **Step 4 — PASS** (`pytest tests/test_mcp_slice_helper.py`).
- [ ] **Step 5 — commit** `feat(mcp): importable slice-query + response-shape helper; call_api params`.

## Task 3: thread slice params through read MCP tools + fix shape bug
**Files:** `mcp_server.py` (the read tools); test `orchestrator/tests/test_mcp_slice_tools.py` (CI-run; `importorskip("mcp")`).

- [ ] **Step 1 — tests** (monkeypatch `call_api` style): each read tool, given slice params, sends them on the request (assert recorded path/params); `list_entities` handles BOTH the bare list and `{entities,scope}` (regression for the current break under a view); tool output states the resolved slice when present. `importorskip("mcp")` at top.
- [ ] **Step 2 — FAIL** (in CI; locally SKIP).
- [ ] **Step 3 — implement.** Add slice params (`since/until/as_of/window/domain/collection`, `str=""`) to `search_knowledge_graph` (pass only `since/until/as_of` — the set `/search` accepts... after Task 1 it accepts all 6, so pass all 6), `get_entity`, `list_entities`, `get_neighborhood`, `get_shared_context`, `find_paths`, `get_subgraph`, `explore_domain`; build the query via `slice_query`. Use `normalize_list` in `list_entities` (and `get_document`/any bare-list reader) so the `{..,scope}` shape doesn't break them. Echo the scope line in output when present. Descriptions mention the relative format (`window=1w`, `since=-7d`).
- [ ] **Step 4 — PASS in CI;** locally confirm the file imports-parse (`python -c "import ast; ast.parse(open('src/mcp_server.py').read())"`) and the helper tests pass.
- [ ] **Step 5 — commit** `feat(mcp): slice params on read tools; fix list_entities scoped-shape handling`.

## Task 4: new MCP tools + view metadata
**Files:** `mcp_server.py`; extend `test_mcp_slice_tools.py`.

- [ ] **Step 1 — tests** (CI): `list_documents(since,until,as_of,order,limit)` → `GET /documents` with params, handles `{documents,scope}`; `create_view(name, parent, slice_window=.., domain=.., ...)` → `POST /views` with `{name,parent,slice:{...}}`; `list_noospheres` output includes `kind`/`parent`/`slice` for virtual entries.
- [ ] **Step 2 — FAIL** (CI).
- [ ] **Step 3 — implement** the two tools + enrich `list_noospheres` formatting (show `[view of <parent>: <slice>]` for `kind=="virtual"`). `create_view` builds the slice dict from its args. Add catalog lines in the tool-list docstring.
- [ ] **Step 4 — PASS in CI;** local parse check.
- [ ] **Step 5 — commit** `feat(mcp): list_documents + create_view tools; show view metadata in list_noospheres`.

## Task 5: suite hygiene
**Files:** `tests/test_mcp_capture_ids.py`, `test_mcp_entity_lookup.py`, `test_mcp_write_tools.py` (+ the new mcp test files).

- [ ] Add `pytest.importorskip("mcp")` (module level) to each mcp test file so `pytest tests/` no longer errors at collection when `mcp` is absent (they SKIP locally, RUN in CI). Confirm `cd orchestrator && pytest tests/ -q` runs with NO `--ignore` flags and no collection errors.
- [ ] **Commit** `test(mcp): importorskip so the suite is clean without the mcp package`.

---

## Wrap-up
- [ ] `cd orchestrator && pytest tests/ -q` (should now need no `--ignore`) + `cd worker && pytest -q`. Only the pre-existing non-mcp failures remain (image columns / image-simmer / PDF-415).
- [ ] Final independent pre-merge review over `main..feat/graph-slices-pr3` (note: MCP tool code reviewed by reading, since it can't execute locally).
- [ ] Open PR (base `main`; **stacked on PR 2 #121 — merge that first**). Note deferred: first_seen/last_seen via MCP, multi-noosphere search; PR 4 = fold one-off slices onto the resolver.
