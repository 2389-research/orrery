# ABOUTME: Wiring test — enrich_stretches sends its facet (summarization) call through the resolver.
# ABOUTME: Remote workspace -> summarization relay + model; local/unconfigured -> the local relay.

import json
import uuid

import pytest

from src.db import get_connection
from src.jobs import enrich_stretches
from src.jobs.enrich_stretches import run_enrich_stretches

WORDS = " ".join(f"word{i}" for i in range(20))
LOCAL, SUMM = object(), object()


class LocalRelay:
    @classmethod
    def from_settings(cls, settings, **overrides):
        return LOCAL


@pytest.fixture
def spies(monkeypatch, test_db):
    seen = {"enrich": [], "resolve": []}

    async def fake_enrich(relay, model, stretches):
        seen["enrich"].append((relay, model))
        return {s["stretch_id"]: {"summary": "x", "concepts": [], "ideas": [], "nouns": []}
                for s in stretches}

    monkeypatch.setattr(enrich_stretches, "Relay", LocalRelay)
    monkeypatch.setattr(enrich_stretches, "enrich_batch", fake_enrich)
    monkeypatch.setattr(enrich_stretches, "build_summarization_relay", lambda s: SUMM)
    conn = get_connection(test_db)
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, source_path, status, content_type, silo_id, metadata) "
        "VALUES (?, 't', ?, 'h', 'horton:chunk/MIC/2026-10-15T10:00:00Z', 'pending', 'transcript', 'c', ?)",
        (str(uuid.uuid4()), f"[10:00:00] {WORDS}\n[10:00:10] {WORDS}",
         json.dumps({"day": "2026-10-15", "window_start": "2026-10-15T17:00:00Z"})))
    conn.commit()
    conn.close()
    return seen


def _patch_resolve(monkeypatch, seen, target):
    def fake_resolve(settings, db_path, *, local_relay, summ_relay):
        seen["resolve"].append((db_path, local_relay, summ_relay))
        return (summ_relay, "remote-model") if target == "remote" else (local_relay, "local-model")
    monkeypatch.setattr(enrich_stretches, "resolve_summarization", fake_resolve)


async def _run(test_db):
    conn = get_connection(test_db)
    jid = str(uuid.uuid4())
    conn.execute("INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'enrich_stretches', 'x', 'running', '{}')", (jid,))
    conn.commit()
    conn.close()
    await run_enrich_stretches({"id": jid, "config": "{}"}, test_db)


@pytest.mark.asyncio
async def test_remote_workspace_facets_use_summarization_relay(spies, monkeypatch, test_db):
    _patch_resolve(monkeypatch, spies, "remote")
    await _run(test_db)
    assert spies["resolve"] == [(test_db, LOCAL, SUMM)]  # local relay stays the fallback input
    assert spies["enrich"] and all(c == (SUMM, "remote-model") for c in spies["enrich"])


@pytest.mark.asyncio
async def test_local_workspace_facets_use_local_relay(spies, monkeypatch, test_db):
    _patch_resolve(monkeypatch, spies, "local")
    await _run(test_db)
    assert spies["enrich"] and all(c == (LOCAL, "local-model") for c in spies["enrich"])
