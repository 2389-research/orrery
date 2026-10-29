# ABOUTME: Source-scan guard that summarization/generation sites use summarization_model.
# ABOUTME: Extraction sites must stay on extraction_model (guards an over-broad sweep).
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[1] / "src" / "jobs"
SRC = JOBS.parent

SUMMARIZATION_FILES = [
    JOBS / "enrich_stretches.py",
    JOBS / "ingest_meridian.py",
    JOBS / "ingest_ccvault.py",
    JOBS / "ingest_repo.py",
    JOBS / "sync_repo.py",
    JOBS / "ingest_tracker_runs.py",
    JOBS / "generate_commentary.py",
    SRC / "main.py",
]


@pytest.mark.parametrize("path", SUMMARIZATION_FILES, ids=lambda p: p.name)
def test_summarization_site_not_on_extraction_model(path):
    text = path.read_text()
    assert "settings.extraction_model" not in text
    assert "settings.summarization_model" in text


def test_extraction_site_stays_on_extraction_model():
    assert "settings.extraction_model" in (JOBS / "upsert_document.py").read_text()
