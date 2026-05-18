from __future__ import annotations

import logging

from .config import settings
from . import storage
from .carriers.base import CarrierFlow
from .carriers.registry import get_flow
from .models import Carrier, SessionState
from .playwright_runner import http_from_context, runner
from .session_manager import SessionManager

log = logging.getLogger(__name__)


async def execute_login(
    manager: SessionManager,
    session_id: str,
    carrier: Carrier,
    username: str,
    password: str,
) -> None:
    """Drive the full login → docs flow for one session.

    Run as an asyncio background task. Publishes state via `manager`.
    On exception, sets the session to ERROR; never raises.
    """
    try:
        flow = get_flow(carrier)
        stored_state = storage.load(carrier.value, username)
        context_options = flow.context_options()

        async with runner.new_context(
            storage_state=stored_state, **context_options
        ) as ctx:
            if stored_state is not None:
                if await _try_quick_path(
                    flow,
                    ctx,
                    manager,
                    session_id,
                    carrier,
                    username,
                    context_options,
                ):
                    return

            await _full_login(
                flow,
                ctx,
                manager,
                session_id,
                carrier,
                username,
                password,
                context_options,
            )
    except Exception as e:  # noqa: BLE001 — top of background task
        log.exception("login flow failed for session %s", session_id)
        manager.set_error(session_id, str(e) or e.__class__.__name__)


async def _try_quick_path(
    flow: CarrierFlow,
    ctx,
    manager: SessionManager,
    session_id: str,
    carrier: Carrier,
    username: str,
    context_options: dict,
) -> bool:
    """Try to fetch docs using stored cookies. Returns True on success."""
    manager.transition(
        session_id, SessionState.FETCHING_DOCS, detail="Resuming session"
    )
    _reset_timing(flow)
    _mark_timing(flow, "quick_path_start")
    page = await ctx.new_page()
    http = await http_from_context(
        ctx, user_agent=context_options.get("user_agent")
    )
    try:
        if carrier == Carrier.USAA:
            try:
                docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
                # refresh stored state in case carrier rotated cookies
                storage.save(carrier.value, username, await ctx.storage_state())
                _mark_timing(flow, "docs_ready_publish")
                manager.set_docs(session_id, docs, doc_bytes)
                _log_timing(flow, session_id)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("direct quick-path document fetch failed: %s", e)

        authed = await flow.is_authenticated(page)
        if not authed:
            return False
        await http.aclose()
        http = await http_from_context(
            ctx, user_agent=context_options.get("user_agent")
        )
        docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
        # refresh stored state in case carrier rotated cookies
        storage.save(carrier.value, username, await ctx.storage_state())
        _mark_timing(flow, "docs_ready_publish")
        manager.set_docs(session_id, docs, doc_bytes)
        _log_timing(flow, session_id)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("quick-path failed, falling back to login: %s", e)
        return False
    finally:
        await http.aclose()
        if not page.is_closed():
            await page.close()


async def _full_login(
    flow: CarrierFlow,
    ctx,
    manager: SessionManager,
    session_id: str,
    carrier: Carrier,
    username: str,
    password: str,
    context_options: dict,
) -> None:
    manager.transition(session_id, SessionState.LOGGING_IN, detail="Logging in")
    page = await ctx.new_page()
    await flow.login(page, username, password)

    if await flow.mfa_required(page):
        code = await manager.request_mfa(
            session_id, timeout=settings.mfa_timeout_seconds
        )
        _reset_timing(flow)
        _mark_timing(flow, "mfa_code_received")
        await flow.submit_mfa(page, code)
        _mark_timing(flow, "mfa_submit_returned")
    else:
        _reset_timing(flow)
        _mark_timing(flow, "no_mfa_fetch_start")

    manager.transition(
        session_id, SessionState.FETCHING_DOCS, detail="Fetching documents"
    )
    _mark_timing(flow, "fetching_docs_state")
    http = await http_from_context(ctx, user_agent=context_options.get("user_agent"))
    try:
        docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
    finally:
        await http.aclose()

    storage.save(carrier.value, username, await ctx.storage_state())
    _mark_timing(flow, "docs_ready_publish")
    manager.set_docs(session_id, docs, doc_bytes)
    _log_timing(flow, session_id)


def _reset_timing(flow: CarrierFlow) -> None:
    reset = getattr(flow, "reset_timings", None)
    if callable(reset):
        reset()


def _mark_timing(flow: CarrierFlow, label: str) -> None:
    mark = getattr(flow, "mark_timing", None)
    if callable(mark):
        mark(label)


def _log_timing(flow: CarrierFlow, session_id: str) -> None:
    report = getattr(flow, "timing_report", None)
    if callable(report):
        summary = report()
        if summary:
            log.info("timing summary for session %s: %s", session_id, summary)
