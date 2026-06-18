from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import auto_repair, email_notifier, feedback_recovery, identity, result_store
from .config import settings
from .logging_config import configure_logging
from .models import Carrier, LoginRequest, MfaRequest, SessionState, StatusEvent
from .orchestrator import execute_login
from .playwright_runner import runner
from .session_manager import SessionNotFoundError, manager
from .slot_manager import ClaimResult, slot_manager
from .worker_proxy import worker_proxy

configure_logging()
log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting Playwright runner")
    await runner.start()
    repair_task = asyncio.create_task(auto_repair.cadence_loop())
    try:
        yield
    finally:
        log.info("shutting down auto-repair and Playwright runner")
        repair_task.cancel()
        try:
            await repair_task
        except asyncio.CancelledError:
            pass
        await auto_repair.shutdown()
        await email_notifier.shutdown()
        await runner.shutdown()


app = FastAPI(title="Infer Take-Home: Carrier Document Puller", lifespan=lifespan)


@app.post("/api/login")
async def login(req: LoginRequest, request: Request) -> Response:
    uid, is_new_uid = _resolve_uid(request)

    if worker_proxy.enabled_for(req.carrier):
        proxied = await worker_proxy.login(req)
        # Best-effort: track the worker-proxied session under this uid's slot
        # so the SSE heartbeat can release it on disconnect.
        slot_manager.claim(uid, req.carrier, proxied.session_id)
        return _json(
            {"session_id": proxied.session_id}, request, uid, is_new_uid
        )

    existing_slot = slot_manager.get(uid)
    if existing_slot and existing_slot.carrier == req.carrier:
        try:
            existing = manager.get(existing_slot.session_id)
        except SessionNotFoundError:
            existing = None
        if existing and existing.state not in (
            SessionState.DONE,
            SessionState.ERROR,
        ):
            slot_manager.tick(uid)
            return _json(
                {"session_id": existing.id}, request, uid, is_new_uid
            )

    # Claim the slot BEFORE minting the Session, so a CARRIER_BUSY rejection
    # doesn't leave a phantom session lying around.
    outcome = slot_manager.claim(uid, req.carrier, "")
    if outcome.result is ClaimResult.CARRIER_BUSY:
        return _json(
            {
                "detail": "carrier-busy",
                "carrier": req.carrier.value,
                "boring_url": "/api/cache",
            },
            request,
            uid,
            is_new_uid,
            status_code=423,
        )

    session = manager.create(req.carrier, req.username, uid=uid)
    # Update the slot to point at the real session id now that we have one.
    slot_manager.claim(uid, req.carrier, session.id)

    task = asyncio.create_task(
        execute_login(
            manager,
            session.id,
            req.carrier,
            req.username,
            req.password,
        )
    )
    manager.attach_task(session.id, task)
    return _json({"session_id": session.id}, request, uid, is_new_uid)


def _resolve_uid(request: Request) -> tuple[str, bool]:
    """Return (uid, is_new). New uids need their cookie stamped on the response."""
    existing = identity.get_uid(request)
    if existing:
        return existing, False
    return identity.mint_uid(), True


def _json(
    content: dict,
    request: Request,
    uid: str,
    is_new_uid: bool,
    *,
    status_code: int = 200,
) -> JSONResponse:
    resp = JSONResponse(content=content, status_code=status_code)
    if is_new_uid:
        identity.set_uid_cookie(resp, uid, request)
    return resp


def _should_close_stream(evt: StatusEvent, *, repair_active: bool) -> bool:
    """Whether the SSE stream should close after delivering ``evt``.

    A ``repair_done`` event is always terminal. Otherwise the stream closes
    once the session reaches a terminal state (DONE or ERROR) — UNLESS a repair
    is in flight for this session (``repair_active``), in which case we hold the
    stream open so the live repair log and any same-run re-delivered docs still
    reach the client.

    This covers both paths that set ``repair_kicked``: an orchestrator failure
    (state ERROR) and user-rejected feedback recovery, which leaves the session
    in DONE while Claude looks for better docs. Without honoring the flag on
    DONE, the reopened feedback-recovery stream would close on the first DONE
    snapshot and never deliver the live log or the replacement documents.
    """
    if evt.event == "repair_done":
        return True
    if evt.state in (SessionState.DONE, SessionState.ERROR):
        return not repair_active
    return False


@app.get("/api/status/{session_id}")
async def status_stream(session_id: str, request: Request) -> EventSourceResponse:
    if worker_proxy.has_session(session_id):
        return worker_proxy.status_stream(session_id, request)

    try:
        queue = manager.subscribe(session_id)
    except SessionNotFoundError:
        persisted = manager.get_persisted_status(session_id)
        if persisted is None:
            raise HTTPException(status_code=404, detail="unknown session")

        async def persisted_event_gen():
            yield {"event": "docs_ready", "data": persisted.model_dump_json()}

        return EventSourceResponse(persisted_event_gen())

    uid = identity.get_uid(request)

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    if uid:
                        slot_manager.tick(uid)
                    yield {"event": "heartbeat", "data": "ping"}
                    continue
                if uid:
                    slot_manager.tick(uid)
                yield {"event": evt.event, "data": evt.model_dump_json()}
                # A repair kicked for this session (orchestrator ERROR, or a
                # user-rejected feedback recovery that stays in DONE) holds the
                # stream open past the terminal state so the live repair log and
                # any same-run re-delivered docs still reach the client. Without
                # an active repair the stream closes on DONE/ERROR rather than
                # blocking forever on repair_done events that won't arrive.
                repair_active = False
                if evt.state in (SessionState.DONE, SessionState.ERROR):
                    try:
                        repair_active = manager.get(session_id).repair_kicked
                    except SessionNotFoundError:
                        repair_active = False
                if _should_close_stream(evt, repair_active=repair_active):
                    break
        finally:
            manager.unsubscribe(session_id, queue)
            # Always release: tab closed, flow finished, or repair done.
            # Reload re-claims via cookie+/api/login if the same uid still
            # wants the same carrier.
            if uid:
                slot_manager.release(uid)

    return EventSourceResponse(event_gen())


@app.post("/api/feedback/{session_id}")
async def feedback(session_id: str, payload: dict) -> dict:
    """User feedback on returned docs.

    payload: {"ok": bool}. ok=true: log + release slot. ok=false: kick the
    feedback-recovery Claude flow (analyzer + auto_repair with user_rejected
    context).
    """
    ok = bool(payload.get("ok"))
    try:
        session = manager.get(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="unknown session")

    if ok:
        slot_manager.release_session(session_id)
        log.info(
            "feedback: positive session=%s docs=%d", session_id, len(session.docs)
        )
        return {"ok": True, "action": "released"}

    result = await feedback_recovery.trigger(session_id)
    return {"ok": False, **result}


@app.post("/api/notify/{session_id}")
async def notify_endpoint(session_id: str, payload: dict) -> dict:
    """Register an email to be notified when the active repair concludes."""
    email = (payload.get("email") or "").strip()
    if not email_notifier.is_valid_email(email):
        raise HTTPException(status_code=400, detail="invalid email")
    try:
        session = manager.get(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="unknown session")
    if not session.repair_kicked:
        raise HTTPException(
            status_code=409,
            detail="no active repair for this session — nothing to notify about",
        )
    import time as _time

    session.notify_email = email
    session.notify_started_at = _time.time()
    started_new = email_notifier.schedule_watch(session_id)
    return {"ok": True, "watching": True, "started_new": started_new}


@app.post("/api/mfa/{session_id}", status_code=status.HTTP_202_ACCEPTED)
async def submit_mfa(session_id: str, req: MfaRequest) -> dict[str, str]:
    if worker_proxy.has_session(session_id):
        return await worker_proxy.submit_mfa(session_id, req)

    try:
        manager.submit_mfa(session_id, req.code)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="unknown session")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "accepted"}


@app.get("/api/dev/credentials")
async def dev_credentials(request: Request) -> dict:
    if not settings.dev_prefill_creds:
        raise HTTPException(status_code=404, detail="dev credential prefill disabled")
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1", "testclient", None}:
        raise HTTPException(status_code=403, detail="local requests only")

    creds = {}
    if settings.usaa_username and settings.usaa_password:
        creds[Carrier.USAA.value] = {
            "username": settings.usaa_username,
            "password": settings.usaa_password,
        }
    if settings.geico_username and settings.geico_password:
        creds[Carrier.GEICO.value] = {
            "username": settings.geico_username,
            "password": settings.geico_password,
        }
    if settings.progressive_username and settings.progressive_password:
        creds[Carrier.PROGRESSIVE.value] = {
            "username": settings.progressive_username,
            "password": settings.progressive_password,
        }
    if settings.allstate_username and settings.allstate_password:
        creds[Carrier.ALLSTATE.value] = {
            "username": settings.allstate_username,
            "password": settings.allstate_password,
        }
    if settings.state_farm_username and settings.state_farm_password:
        creds[Carrier.STATE_FARM.value] = {
            "username": settings.state_farm_username,
            "password": settings.state_farm_password,
        }
    if settings.mercury_username and settings.mercury_password:
        creds[Carrier.MERCURY.value] = {
            "username": settings.mercury_username,
            "password": settings.mercury_password,
        }
    return {"credentials": creds}


@app.get("/api/cache")
async def cache_endpoint(request: Request) -> Response:
    """Per-uid cached results. Used as the boring path when a carrier is busy.

    Privacy: each browser (uid cookie) only ever sees its own past runs."""
    uid, is_new_uid = _resolve_uid(request)
    if not settings.persist_completed_results:
        return _json({"uid_known": True, "results": []}, request, uid, is_new_uid)
    entries = result_store.latest_for_uid(uid)
    enriched = []
    for entry in entries:
        status_event = result_store.load_status(entry["session_id"])
        if status_event is None or status_event.docs is None:
            continue
        enriched.append(
            {
                "carrier": entry["carrier"],
                "session_id": entry["session_id"],
                "saved_at": entry["saved_at"],
                "docs": [doc.model_dump(mode="json") for doc in status_event.docs],
                "timings_ms": status_event.timings_ms,
            }
        )
    return _json(
        {"uid_known": True, "results": enriched}, request, uid, is_new_uid
    )


@app.get("/api/docs/{session_id}/{doc_id}")
async def get_doc(session_id: str, doc_id: str) -> Response:
    if worker_proxy.has_session(session_id):
        return await worker_proxy.get_doc(session_id, doc_id)

    try:
        body = manager.get_doc_bytes(session_id, doc_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="unknown session")
    if body is None:
        raise HTTPException(status_code=404, detail="unknown doc")
    try:
        session = manager.get(session_id)
        doc_meta = next((d for d in session.docs if d.id == doc_id), None)
    except SessionNotFoundError:
        doc_meta = manager.get_persisted_doc(session_id, doc_id)
    headers = {
        "Content-Disposition": (
            f'inline; filename="{doc_meta.name if doc_meta else doc_id + ".pdf"}"'
        ),
        "Cache-Control": "no-store",
    }
    return Response(
        content=body,
        media_type=(doc_meta.content_type if doc_meta else "application/pdf"),
        headers=headers,
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
