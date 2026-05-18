import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response

from backend.config import settings
from backend.models import Carrier, LoginRequest, MfaRequest, SessionState, StatusEvent
from backend.worker_proxy import WorkerProxy, _terminal_sse_payload


@pytest.mark.asyncio
async def test_worker_proxy_forwards_login_mfa_and_docs(monkeypatch):
    worker_app = FastAPI()
    calls: list[tuple[str, str]] = []

    @worker_app.post("/api/login")
    async def login(payload: dict):
        calls.append(("login", payload["carrier"]))
        return {"session_id": "remote-session"}

    @worker_app.post("/api/mfa/remote-session")
    async def submit_mfa(payload: dict):
        calls.append(("mfa", payload["code"]))
        return {"status": "accepted"}

    @worker_app.get("/api/docs/remote-session/dec")
    async def get_doc():
        return Response(
            b"%PDF-1.7\nbody",
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="dec.pdf"'},
        )

    transport = httpx.ASGITransport(app=worker_app)
    proxy = WorkerProxy()
    monkeypatch.setattr(settings, "usaa_worker_base_url", "http://worker")
    monkeypatch.setattr(
        proxy,
        "_client",
        lambda timeout: httpx.AsyncClient(
            transport=transport,
            base_url="http://worker",
            timeout=timeout,
        ),
    )

    login_response = await proxy.login(
        LoginRequest(carrier=Carrier.USAA, username="u", password="p")
    )
    assert login_response.session_id != "remote-session"
    assert proxy.has_session(login_response.session_id)

    mfa_response = await proxy.submit_mfa(
        login_response.session_id, MfaRequest(code="123456")
    )
    assert mfa_response == {"status": "accepted"}

    doc_response = await proxy.get_doc(login_response.session_id, "dec")
    assert doc_response.body.startswith(b"%PDF")
    assert doc_response.headers["content-disposition"] == 'inline; filename="dec.pdf"'
    assert calls == [("login", "usaa"), ("mfa", "123456")]


def test_terminal_sse_payload_detection():
    done = StatusEvent(event="docs_ready", state=SessionState.DONE)
    error = StatusEvent(event="error", state=SessionState.ERROR, error="bad")
    mfa = StatusEvent(event="state_change", state=SessionState.MFA_REQUIRED)

    assert _terminal_sse_payload(done.model_dump_json())
    assert _terminal_sse_payload(error.model_dump_json())
    assert not _terminal_sse_payload(mfa.model_dump_json())
    assert not _terminal_sse_payload("ping")
