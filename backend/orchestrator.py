from __future__ import annotations

import logging
import time

from . import auto_repair, storage
from .carriers.base import CarrierFlow
from .carriers.registry import get_flow
from .config import settings
from .models import Carrier, SessionState
from .playwright_runner import http_from_context, runner
from .session_manager import SessionManager

log = logging.getLogger(__name__)


class NoDocumentsError(RuntimeError):
    """A carrier flow completed but returned zero documents.

    The whole point of a run is to surface policy documents, so an empty
    result is a failure, never a success — yet without this guard the
    orchestrator would publish ``DONE`` with an empty list and the UI would
    cheerfully report "0 documents retrieved." Treating empty as an error:

    - On the quick path (stored cookies), it forces a fresh login instead of
      "succeeding" with nothing when the saved session is merely stale.
    - On the full-login path, it surfaces ``ERROR`` so the user sees a clear
      message and auto-repair can engage.

    This mirrors the auto-repair verifier, which already rejects a fix whose
    ``fetch_documents`` returns 0 documents.
    """


async def execute_login(
    manager: SessionManager,
    session_id: str,
    carrier: Carrier,
    username: str,
    password: str,
) -> None:
    """Drive the full login -> docs flow for one session."""
    try:
        flow = get_flow(carrier)
        stored_state = storage.load(carrier.value, username)
        if not _should_use_stored_state(carrier, username, stored_state):
            if stored_state is not None:
                _discard_stale_carrier_state(flow, username)
            stored_state = None
        context_options = _context_options_for_username(flow, username)

        if stored_state is not None:
            async with runner.new_context(
                storage_state=stored_state, **context_options
            ) as ctx:
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

        await _run_full_login(
            flow,
            manager,
            session_id,
            carrier,
            username,
            password,
            context_options,
        )
    except Exception as e:  # noqa: BLE001 - top of background task
        log.exception("login flow failed for session %s", session_id)
        manager.set_error(session_id, str(e) or e.__class__.__name__)
        try:
            kicked = await auto_repair.capture_and_kick(
                session_id, carrier.value, username, e
            )
            if kicked:
                try:
                    session = manager.get(session_id)
                    session.repair_kicked = True
                except Exception:  # noqa: BLE001
                    pass
        except Exception as repair_err:  # noqa: BLE001
            log.warning(
                "auto-repair kick failed for session %s: %s",
                session_id,
                repair_err,
            )


async def _run_full_login(
    flow: CarrierFlow,
    manager: SessionManager,
    session_id: str,
    carrier: Carrier,
    username: str,
    password: str,
    context_options: dict,
) -> None:
    login_context = getattr(flow, "login_context", None)
    if callable(login_context):
        manager.transition(session_id, SessionState.LOGGING_IN, detail="Logging in")
        async with login_context(
            runner, username, password, context_options
        ) as login_result:
            ctx, page = login_result
            await _finish_after_login(
                flow,
                ctx,
                page,
                manager,
                session_id,
                carrier,
                username,
                context_options,
            )
        return

    async with runner.new_context(storage_state=None, **context_options) as ctx:
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


def _should_use_stored_state(
    carrier: Carrier,
    username: str,
    stored_state: dict | None,
) -> bool:
    if stored_state is None:
        return False

    max_age = (
        settings.usaa_quick_path_max_age_seconds
        if carrier == Carrier.USAA
        else settings.auth_state_max_age_seconds
    )
    if max_age <= 0:
        return True

    saved_at = storage.saved_at(carrier.value, username)
    if saved_at is None:
        log.info("skipping %s stored state: no saved_at timestamp", carrier.value)
        return False

    age_seconds = time.time() - saved_at
    if age_seconds > max_age:
        log.info(
            "skipping %s stored state: %.1fs old exceeds %.1fs freshness window",
            carrier.value,
            age_seconds,
            max_age,
        )
        return False
    return True


def _discard_stale_carrier_state(flow: CarrierFlow, username: str) -> None:
    discard = getattr(flow, "discard_stale_state", None)
    if callable(discard):
        discard(username)


def _context_options_for_username(flow: CarrierFlow, username: str) -> dict:
    scoped_options = getattr(flow, "context_options_for_username", None)
    if callable(scoped_options):
        return scoped_options(username)
    return flow.context_options()


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
    http = await http_from_context(ctx, user_agent=context_options.get("user_agent"))
    try:
        if carrier == Carrier.USAA:
            try:
                docs, doc_bytes = await _fetch_documents_with_progress(
                    flow, page, http, ctx, manager, session_id
                )
                storage.save(carrier.value, username, await ctx.storage_state())
                _mark_timing(flow, "docs_ready_publish")
                manager.set_docs(
                    session_id,
                    docs,
                    doc_bytes,
                    timings_ms=_timing_snapshot(flow),
                )
                _log_timing(flow, session_id)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("direct quick-path document fetch failed: %s", e)

        authed = await flow.is_authenticated(page)
        if not authed:
            return False
        await http.aclose()
        http = await http_from_context(ctx, user_agent=context_options.get("user_agent"))
        docs, doc_bytes = await _fetch_documents_with_progress(
            flow, page, http, ctx, manager, session_id
        )
        storage.save(carrier.value, username, await ctx.storage_state())
        _mark_timing(flow, "docs_ready_publish")
        manager.set_docs(
            session_id,
            docs,
            doc_bytes,
            timings_ms=_timing_snapshot(flow),
        )
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
    await _finish_after_login(
        flow,
        ctx,
        page,
        manager,
        session_id,
        carrier,
        username,
        context_options,
    )


async def _finish_after_login(
    flow: CarrierFlow,
    ctx,
    page,
    manager: SessionManager,
    session_id: str,
    carrier: Carrier,
    username: str,
    context_options: dict,
) -> None:
    if await flow.mfa_required(page):
        await _save_partial_auth_state(ctx, page, carrier, username, session_id)
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

    await _save_auth_state(ctx, carrier, username)
    manager.transition(
        session_id, SessionState.FETCHING_DOCS, detail="Fetching documents"
    )
    _mark_timing(flow, "fetching_docs_state")
    http = await http_from_context(ctx, user_agent=context_options.get("user_agent"))
    try:
        docs, doc_bytes = await _fetch_documents_with_progress(
            flow, page, http, ctx, manager, session_id
        )
    finally:
        await http.aclose()

    storage.save(carrier.value, username, await ctx.storage_state())
    _mark_timing(flow, "docs_ready_publish")
    manager.set_docs(
        session_id,
        docs,
        doc_bytes,
        timings_ms=_timing_snapshot(flow),
    )
    _log_timing(flow, session_id)


async def _save_auth_state(ctx, carrier: Carrier, username: str) -> None:
    try:
        state = await ctx.storage_state()
        storage.save(carrier.value, username, state)
        log.info(
            "saved %s browser auth state for user hash %s",
            carrier.value,
            storage.user_hash(username),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("failed to save %s browser auth state: %s", carrier.value, e)


async def _save_partial_auth_state(
    ctx,
    page,
    carrier: Carrier,
    username: str,
    session_id: str,
) -> None:
    try:
        path = storage.save_partial_auth(
            carrier=carrier.value,
            username=username,
            session_id=session_id,
            storage_state=await ctx.storage_state(),
            url=getattr(page, "url", None),
        )
        log.info(
            "saved non-reusable %s partial auth state for session %s -> %s",
            carrier.value,
            session_id,
            path,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("failed to save %s partial auth state: %s", carrier.value, e)


async def _fetch_documents_with_progress(
    flow: CarrierFlow,
    page,
    http,
    ctx,
    manager: SessionManager,
    session_id: str,
):
    """Fetch documents, wiring up the optional progress callback.

    Raises ``NoDocumentsError`` if the flow returns an empty result — the
    single chokepoint where every orchestrator path funnels its fetch, so the
    empty-as-failure guarantee holds for all carriers (and any future one).
    """
    setter = getattr(flow, "set_documents_progress_callback", None)
    if not callable(setter):
        return _require_documents(await flow.fetch_documents(page, http, ctx))

    async def publish_progress(
        docs,
        doc_bytes,
    ) -> None:
        _mark_timing(flow, "docs_progress_publish")
        manager.publish_docs_progress(
            session_id,
            docs,
            doc_bytes,
            timings_ms=_timing_snapshot(flow),
        )

    setter(publish_progress)
    try:
        return _require_documents(await flow.fetch_documents(page, http, ctx))
    finally:
        setter(None)


def _require_documents(result):
    """Pass ``(docs, doc_bytes)`` through unchanged, or raise if ``docs`` is empty."""
    docs, _doc_bytes = result
    if not docs:
        raise NoDocumentsError("carrier returned no documents")
    return result


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


def _timing_snapshot(flow: CarrierFlow) -> dict[str, int] | None:
    snapshot = getattr(flow, "timing_snapshot", None)
    if callable(snapshot):
        timings = snapshot()
        if timings:
            return timings
    return None
