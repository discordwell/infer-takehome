"""User-driven recovery: when the user clicks "wrong documents," kick the
existing auto-repair Claude with a feedback-prompt variant.

Flow:
1. Run pdf_analyzer on the docs the user just rejected (gives Claude context
   on what was wrong).
2. Mark session.feedback_recovery_active = True so the UI can adjust.
3. Call auto_repair.capture_and_kick with kick_reason='user_rejected' and
   the analysis stuffed in extra_context.
4. Claude reads context.json, sees the prior_analysis, focuses on finding
   better docs (not patching the adapter unless it spots a real bug).
5. New PDFs delivered via the existing storage/repair/<sid>/delivered/
   bridge (auto_repair._check_done → repair_deliver).
"""

from __future__ import annotations

import logging

from . import auto_repair, pdf_analyzer
from .session_manager import SessionNotFoundError, manager

log = logging.getLogger(__name__)


class UserRejectedDocsError(Exception):
    """Synthetic exception used as the kick trigger for feedback recovery.

    Treated as a regular exception by capture_and_kick — the message gets
    recorded in context.json as the "exception" field so claude sees it.
    """


async def trigger(session_id: str) -> dict:
    """Start the feedback-recovery loop for a session that just got bad docs.

    Returns a small status dict for the HTTP handler.
    """
    try:
        session = manager.get(session_id)
    except SessionNotFoundError:
        return {"ok": False, "reason": "unknown-session"}

    if not session.docs:
        return {"ok": False, "reason": "no-docs-to-reject"}

    if session.feedback_recovery_active:
        return {"ok": True, "reason": "already-active"}

    if not auto_repair.is_enabled():
        log.info(
            "feedback_recovery: REPAIR_ENABLED is false — surfacing a terminal "
            "verdict to the user instead of spawning claude"
        )
        session.feedback_recovery_active = True
        session.repair_kicked = True
        # No repair will run, so close the loop with a terminal verdict rather
        # than leaving the reopened SSE waiting on a repair_done that won't come.
        manager.publish_repair_done(
            session_id,
            verdict="NEED_HUMAN",
            first_line="Automatic repair is disabled in this environment.",
        )
        return {"ok": True, "reason": "repair-disabled-but-flagged"}

    # 1. Analyze the rejected docs so claude knows what was wrong.
    try:
        analysis = await pdf_analyzer.analyze(
            session_id, session.docs, session.doc_bytes
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "feedback_recovery: pdf_analyzer failed session=%s: %s",
            session_id, e,
        )
        analysis = []
    session.pdf_analysis = analysis

    # 2. Flag the session for the UI + SSE keep-alive logic.
    session.feedback_recovery_active = True
    session.repair_kicked = True

    # 3. Kick claude with the feedback-recovery context.
    extra = {
        "prior_analysis": analysis,
        "rejected_doc_names": [d.name for d in session.docs],
        "rejected_doc_count": len(session.docs),
    }
    summary = (
        f"User rejected {len(session.docs)} doc(s) from carrier "
        f"{session.carrier.value}: "
        + ", ".join(
            f"{d.name} ({a.get('category', '?')})"
            for d, a in zip(session.docs, analysis or [{}] * len(session.docs))
        )
    )
    err = UserRejectedDocsError(summary)
    kicked = await auto_repair.capture_and_kick(
        session_id,
        session.carrier.value,
        session.username,
        err,
        step="user_feedback",
        kick_reason="user_rejected",
        extra_context=extra,
    )
    if not kicked:
        log.info(
            "feedback_recovery: capture_and_kick returned False "
            "(folded into existing repair or disabled) session=%s",
            session_id,
        )
        # A fresh repair did not start for THIS session — it folded into an
        # already-active carrier repair whose repair_done only reaches the
        # owning session. Surface a terminal verdict so this session's reopened
        # SSE doesn't hang waiting for an event that will never arrive here.
        manager.publish_repair_done(
            session_id,
            verdict="NEED_HUMAN",
            first_line="Folded into an active repair for this carrier.",
        )
    return {"ok": True, "kicked": kicked}
