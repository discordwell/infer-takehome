"""Email-on-completion notifier.

When the user clicks "email me when done" during an active repair, /api/notify
records the email on the session and spawns a watcher coroutine. The watcher
awaits the session's `repair_done_event` with a 5h cap, then sends an email
via Resend:

- success path: subject "Your <carrier> docs are ready", with the verified
  PDFs attached (capped by EMAIL_MAX_ATTACHMENT_BYTES — over the cap we
  send a links-only email instead).
- failure / timeout path: subject "We couldn't fetch your <carrier> docs",
  with a brief reason.

This module is intentionally self-contained: just httpx to talk to Resend,
no third-party SDK.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .config import settings
from .session_manager import Session, SessionNotFoundError, manager

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_inflight_tasks: set[asyncio.Task] = set()


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value or ""))


def schedule_watch(session_id: str) -> bool:
    """Spawn a background watcher for this session. No-op if already running.

    Idempotent — called from POST /api/notify. Returns True if a new
    watcher was scheduled.
    """
    try:
        session = manager.get(session_id)
    except SessionNotFoundError:
        return False
    if any(
        getattr(t, "_email_session_id", None) == session_id
        for t in _inflight_tasks
        if not t.done()
    ):
        return False

    task = asyncio.create_task(_watch(session_id))
    task._email_session_id = session_id  # type: ignore[attr-defined]
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)
    log.info(
        "email_notifier: watching session=%s email=%s wall=%ds",
        session_id,
        _redact(session.notify_email or ""),
        settings.notify_wall_seconds,
    )
    return True


async def _watch(session_id: str) -> None:
    try:
        session = manager.get(session_id)
    except SessionNotFoundError:
        return
    try:
        await asyncio.wait_for(
            session.repair_done_event.wait(),
            timeout=settings.notify_wall_seconds,
        )
        verdict = "success" if session.docs else "failure"
        reason = session.error or None
    except asyncio.TimeoutError:
        verdict = "timeout"
        reason = (
            f"Auto-repair did not finish within "
            f"{settings.notify_wall_seconds // 3600}h."
        )
    except asyncio.CancelledError:
        return

    try:
        await _send_email(session, verdict, reason)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "email_notifier: send failed session=%s: %s", session_id, e
        )


@dataclass
class _Plan:
    subject: str
    body_html: str
    body_text: str
    attachments: list[dict] | None


def _build_plan(session: Session, verdict: str, reason: str | None) -> _Plan:
    carrier = session.carrier.value.upper()
    if verdict == "success" and session.docs:
        doc_names = ", ".join(d.name for d in session.docs)
        intro = (
            f"Good news — your {carrier} documents are ready. "
            f"{len(session.docs)} file{'s' if len(session.docs) != 1 else ''} "
            f"attached."
        )
        intro_html = (
            f"<p>Good news — your <strong>{carrier}</strong> documents are "
            f"ready. {len(session.docs)} file"
            f"{'s' if len(session.docs) != 1 else ''} attached.</p>"
        )
        if session.uid:
            link_base = (
                f"https://infer.discordwell.com/api/docs/{session.id}"
            )
            doc_links_html = "".join(
                f'<li><a href="{link_base}/{d.id}">{d.name}</a></li>'
                for d in session.docs
            )
            intro_html += (
                f"<p>Also available at these links (live ~24h):</p>"
                f"<ul>{doc_links_html}</ul>"
            )
        # Attach PDFs unless we'd blow past the size cap.
        attachments = _build_attachments(session)
        if attachments is None:
            intro = (
                f"Your {carrier} documents are ready ({len(session.docs)} "
                f"files), but they're too large to attach. Use the links "
                f"in this email (live ~24h). Files: {doc_names}"
            )
            intro_html = (
                f"<p>Your <strong>{carrier}</strong> documents are ready "
                f"({len(session.docs)} files), but the bundle exceeded our "
                f"email attachment cap. Click the links below — they stay "
                f"live for about 24 hours.</p>"
            )
            if session.uid:
                link_base = (
                    f"https://infer.discordwell.com/api/docs/{session.id}"
                )
                doc_links_html = "".join(
                    f'<li><a href="{link_base}/{d.id}">{d.name}</a></li>'
                    for d in session.docs
                )
                intro_html += f"<ul>{doc_links_html}</ul>"
        subject = f"Your {carrier} documents are ready"
        return _Plan(
            subject=subject,
            body_text=intro,
            body_html=intro_html,
            attachments=attachments,
        )

    # Failure / timeout / no docs delivered
    subject = f"We couldn't fetch your {carrier} documents"
    line = reason or "Auto-repair finished without delivering documents."
    body_text = (
        f"Unfortunately we weren't able to fetch your {carrier} documents.\n"
        f"Reason: {line}\n\n"
        f"You can try again at https://infer.discordwell.com/."
    )
    body_html = (
        f"<p>Unfortunately we weren't able to fetch your "
        f"<strong>{carrier}</strong> documents.</p>"
        f"<p>Reason: {line}</p>"
        f'<p>You can <a href="https://infer.discordwell.com/">try again</a>.</p>'
    )
    return _Plan(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=None,
    )


def _build_attachments(session: Session) -> list[dict] | None:
    total = sum(
        len(b) for b in session.doc_bytes.values()
    )
    if total > settings.email_max_attachment_bytes:
        log.info(
            "email_notifier: skipping attachments session=%s total=%d cap=%d",
            session.id, total, settings.email_max_attachment_bytes,
        )
        return None
    attachments = []
    for doc in session.docs:
        body = session.doc_bytes.get(doc.id)
        if not body:
            continue
        attachments.append(
            {
                "filename": doc.name,
                "content": base64.b64encode(body).decode("ascii"),
            }
        )
    return attachments or None


async def _send_email(session: Session, verdict: str, reason: str | None) -> None:
    if not session.notify_email:
        return
    if not settings.resend_api_key:
        log.warning(
            "email_notifier: RESEND_API_KEY not configured; "
            "would have sent verdict=%s to %s",
            verdict, _redact(session.notify_email),
        )
        return
    plan = _build_plan(session, verdict, reason)
    payload = {
        "from": settings.resend_from_email,
        "to": [session.notify_email],
        "subject": plan.subject,
        "html": plan.body_html,
        "text": plan.body_text,
    }
    if plan.attachments:
        payload["attachments"] = plan.attachments
    started = time.time()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            RESEND_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"resend {resp.status_code}: {resp.text[:300]}"
        )
    log.info(
        "email_notifier: sent session=%s verdict=%s to=%s elapsed=%dms attachments=%d",
        session.id,
        verdict,
        _redact(session.notify_email),
        int((time.time() - started) * 1000),
        len(plan.attachments) if plan.attachments else 0,
    )


def _redact(email: str) -> str:
    if "@" not in email:
        return "<invalid>"
    local, _, domain = email.partition("@")
    head = (local[:2] + "…") if len(local) > 2 else local
    return f"{head}@{domain}"


async def shutdown() -> None:
    """Cancel any in-flight watchers. Called from FastAPI lifespan teardown."""
    if not _inflight_tasks:
        return
    log.info(
        "email_notifier shutdown: cancelling %d watcher(s)",
        len(_inflight_tasks),
    )
    for task in list(_inflight_tasks):
        task.cancel()
    await asyncio.gather(*_inflight_tasks, return_exceptions=True)
