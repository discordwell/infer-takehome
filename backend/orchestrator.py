from __future__ import annotations

import logging

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

        async with runner.new_context(storage_state=stored_state) as ctx:
            if stored_state is not None:
                if await _try_quick_path(flow, ctx, manager, session_id, carrier, username):
                    return

            await _full_login(
                flow, ctx, manager, session_id, carrier, username, password
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
) -> bool:
    """Try to fetch docs using stored cookies. Returns True on success."""
    manager.transition(
        session_id, SessionState.FETCHING_DOCS, detail="Resuming session"
    )
    page = await ctx.new_page()
    try:
        authed = await flow.is_authenticated(page)
        if not authed:
            await page.close()
            return False
        http = await http_from_context(ctx)
        try:
            docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
        finally:
            await http.aclose()
        # refresh stored state in case carrier rotated cookies
        storage.save(carrier.value, username, await ctx.storage_state())
        manager.set_docs(session_id, docs, doc_bytes)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("quick-path failed, falling back to login: %s", e)
        return False
    finally:
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
) -> None:
    manager.transition(session_id, SessionState.LOGGING_IN, detail="Logging in")
    page = await ctx.new_page()
    await flow.login(page, username, password)

    if await flow.mfa_required(page):
        code = await manager.request_mfa(session_id)
        await flow.submit_mfa(page, code)

    manager.transition(
        session_id, SessionState.FETCHING_DOCS, detail="Fetching documents"
    )
    http = await http_from_context(ctx)
    try:
        docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
    finally:
        await http.aclose()

    storage.save(carrier.value, username, await ctx.storage_state())
    manager.set_docs(session_id, docs, doc_bytes)
