"""Tests for the Resend-backed email notifier."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import email_notifier
from backend.config import settings
from backend.models import Carrier, Document, SessionState
from backend.session_manager import manager


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(
        settings,
        "resend_from_email",
        "Infer <noreply@mail.discordwell.com>",
    )
    monkeypatch.setattr(settings, "email_max_attachment_bytes", 1_000_000)
    monkeypatch.setattr(settings, "notify_wall_seconds", 2)


@pytest.fixture
def session_with_docs():
    sess = manager.create(Carrier.USAA, "alice@example.com", uid="uid-1")
    docs = [
        Document(id="d1", name="Declarations.pdf", size_bytes=8),
        Document(id="d2", name="ID Card.pdf", size_bytes=8),
    ]
    body = b"%PDF-1.4 small"
    sess.docs = docs
    sess.doc_bytes = {"d1": body, "d2": body}
    sess.state = SessionState.DONE
    return sess


def test_is_valid_email():
    assert email_notifier.is_valid_email("a@b.co")
    assert not email_notifier.is_valid_email("not-an-email")
    assert not email_notifier.is_valid_email("")


def test_build_plan_success_attaches_pdfs(session_with_docs):
    session_with_docs.notify_email = "u@example.com"
    plan = email_notifier._build_plan(session_with_docs, "success", None)
    assert "USAA documents are ready" in plan.subject
    assert plan.attachments is not None
    assert len(plan.attachments) == 2
    # base64-encoded
    assert all(isinstance(a["content"], str) for a in plan.attachments)


def test_build_plan_skips_attachments_when_over_cap(session_with_docs, monkeypatch):
    monkeypatch.setattr(settings, "email_max_attachment_bytes", 1)
    session_with_docs.notify_email = "u@example.com"
    plan = email_notifier._build_plan(session_with_docs, "success", None)
    assert plan.attachments is None
    assert "too large" in plan.body_text


def test_build_plan_failure_path(session_with_docs):
    session_with_docs.docs = []
    session_with_docs.doc_bytes = {}
    session_with_docs.error = "no docs found"
    plan = email_notifier._build_plan(session_with_docs, "failure", "no docs found")
    assert "couldn't" in plan.subject.lower()
    assert "no docs found" in plan.body_text
    assert plan.attachments is None


async def test_watch_sends_on_repair_done(monkeypatch, session_with_docs):
    session_with_docs.notify_email = "u@example.com"
    sender = AsyncMock()
    monkeypatch.setattr(email_notifier, "_send_email", sender)

    # Set the event before scheduling so wait_for returns immediately.
    session_with_docs.repair_done_event.set()
    await email_notifier._watch(session_with_docs.id)

    sender.assert_awaited_once()
    args = sender.await_args.args
    assert args[1] == "success"


async def test_watch_times_out_and_sends_failure(monkeypatch, session_with_docs):
    session_with_docs.notify_email = "u@example.com"
    sender = AsyncMock()
    monkeypatch.setattr(email_notifier, "_send_email", sender)
    monkeypatch.setattr(settings, "notify_wall_seconds", 0.05)
    # Don't set the event — let it time out.

    await email_notifier._watch(session_with_docs.id)
    sender.assert_awaited_once()
    assert sender.await_args.args[1] == "timeout"


async def test_send_email_hits_resend_with_payload(monkeypatch, session_with_docs):
    session_with_docs.notify_email = "u@example.com"

    captured = {}

    class FakeResp:
        status_code = 202
        text = ""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(email_notifier.httpx, "AsyncClient", FakeClient)

    await email_notifier._send_email(session_with_docs, "success", None)
    assert captured["url"] == email_notifier.RESEND_URL
    assert captured["payload"]["to"] == ["u@example.com"]
    assert captured["payload"]["from"] == "Infer <noreply@mail.discordwell.com>"
    assert "attachments" in captured["payload"]
    assert "Bearer re_test_key" in captured["headers"]["Authorization"]


async def test_send_email_no_api_key_is_noop(monkeypatch, session_with_docs):
    monkeypatch.setattr(settings, "resend_api_key", None)
    session_with_docs.notify_email = "u@example.com"

    # Should not raise; just logs and returns.
    await email_notifier._send_email(session_with_docs, "success", None)


def test_redact_email():
    assert email_notifier._redact("alice@example.com") == "al…@example.com"
    assert email_notifier._redact("a@b.co") == "a@b.co"
    assert email_notifier._redact("not-an-email") == "<invalid>"
