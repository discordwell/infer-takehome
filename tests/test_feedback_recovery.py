"""Tests for the feedback-recovery trigger.

The feedback path: user gets docs → clicks "wrong" → /api/feedback ok=false →
feedback_recovery.trigger → pdf_analyzer.analyze + auto_repair.capture_and_kick.
Here we mock both subprocesses to test the orchestration without spawning claude.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend import auto_repair, feedback_recovery, pdf_analyzer
from backend.models import Carrier, Document, SessionState
from backend.session_manager import manager


@pytest.fixture
def session_with_docs():
    sess = manager.create(Carrier.USAA, "alice@example.com", uid="uid-A")
    sess.docs = [
        Document(id="d1", name="Brochure.pdf", size_bytes=100),
    ]
    sess.doc_bytes = {"d1": b"%PDF-1.4 fake"}
    sess.state = SessionState.DONE
    return sess


async def test_trigger_kicks_claude_with_user_rejected(monkeypatch, session_with_docs):
    monkeypatch.setattr(
        pdf_analyzer, "analyze",
        AsyncMock(return_value=[
            {"name": "Brochure.pdf", "label": "marketing brochure",
             "description": "promotional content", "category": "cover_or_brochure"},
        ]),
    )
    kick = AsyncMock(return_value=True)
    monkeypatch.setattr(auto_repair, "capture_and_kick", kick)
    monkeypatch.setattr(auto_repair, "is_enabled", lambda: True)

    result = await feedback_recovery.trigger(session_with_docs.id)

    assert result == {"ok": True, "kicked": True}
    kick.assert_awaited_once()
    call = kick.await_args
    # session_id positional, then carrier, username, exception, step.
    assert call.args[0] == session_with_docs.id
    assert call.args[1] == "usaa"
    assert call.args[2] == "alice@example.com"
    assert call.kwargs["kick_reason"] == "user_rejected"
    extra = call.kwargs["extra_context"]
    assert extra["rejected_doc_count"] == 1
    assert extra["rejected_doc_names"] == ["Brochure.pdf"]
    assert extra["prior_analysis"][0]["category"] == "cover_or_brochure"
    # Side effects on the session.
    assert session_with_docs.feedback_recovery_active is True
    assert session_with_docs.repair_kicked is True
    assert session_with_docs.pdf_analysis is not None


async def test_trigger_when_repair_disabled_still_flags_session(
    monkeypatch, session_with_docs
):
    monkeypatch.setattr(auto_repair, "is_enabled", lambda: False)
    kick = AsyncMock()
    monkeypatch.setattr(auto_repair, "capture_and_kick", kick)

    result = await feedback_recovery.trigger(session_with_docs.id)
    assert result["ok"] is True
    assert "repair-disabled" in result["reason"]
    assert session_with_docs.feedback_recovery_active is True
    assert session_with_docs.repair_kicked is True
    kick.assert_not_awaited()


async def test_trigger_unknown_session():
    result = await feedback_recovery.trigger("not-a-real-session")
    assert result == {"ok": False, "reason": "unknown-session"}


async def test_trigger_no_docs_rejected(monkeypatch):
    sess = manager.create(Carrier.USAA, "alice@example.com", uid="uid-no-docs")
    sess.state = SessionState.ERROR
    result = await feedback_recovery.trigger(sess.id)
    assert result == {"ok": False, "reason": "no-docs-to-reject"}


async def test_trigger_already_active_is_idempotent(monkeypatch, session_with_docs):
    monkeypatch.setattr(auto_repair, "is_enabled", lambda: True)
    monkeypatch.setattr(pdf_analyzer, "analyze", AsyncMock(return_value=[]))
    monkeypatch.setattr(auto_repair, "capture_and_kick", AsyncMock(return_value=True))

    first = await feedback_recovery.trigger(session_with_docs.id)
    assert first == {"ok": True, "kicked": True}

    second = await feedback_recovery.trigger(session_with_docs.id)
    assert second == {"ok": True, "reason": "already-active"}
