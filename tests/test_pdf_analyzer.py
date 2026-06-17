"""Tests for the lazy Claude PDF analyzer.

We don't actually spawn `claude` in unit tests — we mock `_run_claude` to
return canned JSON text and verify parsing + caching.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from backend import pdf_analyzer
from backend.models import Document


@pytest.fixture(autouse=True)
def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf_analyzer, "ANALYSIS_DIR", tmp_path / "analyses")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "storage" / "repair").mkdir(parents=True, exist_ok=True)


def _docs() -> tuple[list[Document], dict[str, bytes]]:
    docs = [
        Document(id="d1", name="Declarations Page.pdf", size_bytes=200),
        Document(id="d2", name="Brochure.pdf", size_bytes=180),
    ]
    body = b"%PDF-1.4\n" + b"x" * 100
    return docs, {"d1": body, "d2": body}


async def test_analyze_parses_claude_json_array(monkeypatch):
    canned = json.dumps([
        {"name": "Declarations Page.pdf", "label": "declarations page",
         "description": "USAA auto policy declarations", "category": "policy_doc"},
        {"name": "Brochure.pdf", "label": "marketing brochure",
         "description": "general info about coverage", "category": "cover_or_brochure"},
    ])
    monkeypatch.setattr(pdf_analyzer, "_run_claude", AsyncMock(return_value=canned))
    docs, doc_bytes = _docs()

    result = await pdf_analyzer.analyze("sid-abc", docs, doc_bytes)

    assert len(result) == 2
    assert result[0]["category"] == "policy_doc"
    assert result[1]["category"] == "cover_or_brochure"


async def test_analyze_handles_prose_around_json(monkeypatch):
    canned = (
        "Sure, here's the analysis:\n```json\n"
        + json.dumps([
            {"name": "x.pdf", "label": "login page",
             "description": "appears to be a login screenshot",
             "category": "login_or_error"}
        ])
        + "\n```\nLet me know if you'd like more detail."
    )
    monkeypatch.setattr(pdf_analyzer, "_run_claude", AsyncMock(return_value=canned))
    docs, doc_bytes = _docs()

    result = await pdf_analyzer.analyze("sid-prose", docs, doc_bytes)
    assert len(result) == 1
    assert result[0]["category"] == "login_or_error"


async def test_analyze_invalid_category_normalized_to_other(monkeypatch):
    canned = json.dumps([
        {"name": "x.pdf", "label": "weird", "description": "?", "category": "made_up"}
    ])
    monkeypatch.setattr(pdf_analyzer, "_run_claude", AsyncMock(return_value=canned))
    docs, doc_bytes = _docs()

    result = await pdf_analyzer.analyze("sid-norm", docs, doc_bytes)
    assert result[0]["category"] == "other"


async def test_analyze_caches_result(monkeypatch):
    canned = json.dumps([
        {"name": "x.pdf", "label": "?", "description": "?", "category": "other"}
    ])
    runner = AsyncMock(return_value=canned)
    monkeypatch.setattr(pdf_analyzer, "_run_claude", runner)
    docs, doc_bytes = _docs()

    await pdf_analyzer.analyze("sid-cache", docs, doc_bytes)
    await pdf_analyzer.analyze("sid-cache", docs, doc_bytes)
    # Second call should hit the cache, no second spawn.
    assert runner.await_count == 1


async def test_analyze_unparseable_returns_empty(monkeypatch):
    monkeypatch.setattr(
        pdf_analyzer, "_run_claude",
        AsyncMock(return_value="i could not find any pdfs to analyze"),
    )
    docs, doc_bytes = _docs()

    assert await pdf_analyzer.analyze("sid-noparse", docs, doc_bytes) == []


async def test_analyze_no_docs_returns_empty(monkeypatch):
    monkeypatch.setattr(pdf_analyzer, "_run_claude", AsyncMock(return_value="[]"))
    assert await pdf_analyzer.analyze("sid-empty", [], {}) == []


def test_extract_json_array_handles_nested_brackets():
    text = 'noise [1,[2,3], {"a":"b]c"}] tail'
    assert pdf_analyzer._extract_json_array(text) == [1, [2, 3], {"a": "b]c"}]


def test_extract_json_array_returns_none_on_missing():
    assert pdf_analyzer._extract_json_array("nothing here") is None
