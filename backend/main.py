from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .config import settings
from .models import Carrier, LoginRequest, LoginResponse, MfaRequest, SessionState
from .orchestrator import execute_login
from .playwright_runner import runner
from .session_manager import SessionNotFoundError, manager
from .worker_proxy import worker_proxy

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting Playwright runner")
    await runner.start()
    yield
    log.info("shutting down Playwright runner")
    await runner.shutdown()


app = FastAPI(title="Infer Take-Home: Carrier Document Puller", lifespan=lifespan)


@app.post("/api/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    if worker_proxy.enabled_for(req.carrier):
        return await worker_proxy.login(req)

    session = manager.create(req.carrier, req.username)
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
    return LoginResponse(session_id=session.id)


@app.get("/api/status/{session_id}")
async def status_stream(session_id: str, request: Request) -> EventSourceResponse:
    if worker_proxy.has_session(session_id):
        return worker_proxy.status_stream(session_id, request)

    try:
        queue = manager.subscribe(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="unknown session")

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "ping"}
                    continue
                yield {"event": evt.event, "data": evt.model_dump_json()}
                if evt.state in (SessionState.DONE, SessionState.ERROR):
                    break
        finally:
            manager.unsubscribe(session_id, queue)

    return EventSourceResponse(event_gen())


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
    return {"credentials": creds}


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
    session = manager.get(session_id)
    doc_meta = next((d for d in session.docs if d.id == doc_id), None)
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
