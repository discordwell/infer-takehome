from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse

from .config import settings
from .models import Carrier, LoginRequest, LoginResponse, MfaRequest, SessionState, StatusEvent


@dataclass
class WorkerSession:
    remote_session_id: str
    created_at: float


class WorkerProxy:
    def __init__(self) -> None:
        self._sessions: dict[str, WorkerSession] = {}

    def enabled_for(self, carrier: Carrier) -> bool:
        return bool(_worker_base_url()) and carrier in _worker_proxy_carriers()

    def has_session(self, session_id: str) -> bool:
        self._prune_stale()
        return session_id in self._sessions

    async def login(self, req: LoginRequest) -> LoginResponse:
        self._prune_stale()
        local_session_id = uuid.uuid4().hex
        async with self._client(timeout=60.0) as client:
            resp = await client.post("/api/login", json=req.model_dump(mode="json"))
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        remote_session_id = resp.json()["session_id"]
        self._sessions[local_session_id] = WorkerSession(
            remote_session_id=remote_session_id,
            created_at=time.time(),
        )
        return LoginResponse(session_id=local_session_id)

    def status_stream(self, session_id: str, request: Request) -> EventSourceResponse:
        worker_session = self._get(session_id)

        async def event_gen() -> AsyncIterator[dict[str, str]]:
            try:
                async with self._client(timeout=None) as client:
                    async with client.stream(
                        "GET", f"/api/status/{worker_session.remote_session_id}"
                    ) as resp:
                        if resp.status_code >= 400:
                            yield _error_event(
                                f"worker status failed with {resp.status_code}"
                            )
                            return
                        async for event, data in _iter_sse(resp):
                            if await request.is_disconnected():
                                break
                            yield {"event": event, "data": data}
                            if _terminal_sse_payload(data):
                                break
            except httpx.HTTPError as e:
                yield _error_event(f"worker status stream failed: {e}")

        return EventSourceResponse(event_gen())

    async def submit_mfa(self, session_id: str, req: MfaRequest) -> dict[str, str]:
        worker_session = self._get(session_id)
        async with self._client(timeout=30.0) as client:
            resp = await client.post(
                f"/api/mfa/{worker_session.remote_session_id}",
                json=req.model_dump(mode="json"),
            )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="unknown session")
        if resp.status_code == 409:
            raise HTTPException(status_code=409, detail=resp.text)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return {"status": "accepted"}

    async def get_doc(self, session_id: str, doc_id: str) -> Response:
        worker_session = self._get(session_id)
        async with self._client(timeout=60.0) as client:
            resp = await client.get(
                f"/api/docs/{worker_session.remote_session_id}/{doc_id}"
            )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="unknown doc")
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        headers = {"Cache-Control": "no-store"}
        if disposition := resp.headers.get("content-disposition"):
            headers["Content-Disposition"] = disposition
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "application/pdf"),
            headers=headers,
        )

    def _get(self, session_id: str) -> WorkerSession:
        self._prune_stale()
        session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    def _client(self, timeout) -> httpx.AsyncClient:
        base_url = _worker_base_url()
        assert base_url is not None
        return httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            follow_redirects=False,
        )

    def _prune_stale(self) -> None:
        cutoff = time.time() - settings.session_ttl_seconds
        stale = [sid for sid, s in self._sessions.items() if s.created_at < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)


async def _iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    event = "message"
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())


def _terminal_sse_payload(data: str) -> bool:
    try:
        state = StatusEvent.model_validate_json(data).state
    except Exception:
        return False
    return state in (SessionState.DONE, SessionState.ERROR)


def _error_event(message: str) -> dict[str, str]:
    payload = StatusEvent(
        event="error",
        state=SessionState.ERROR,
        error=message,
    )
    return {"event": "error", "data": payload.model_dump_json()}


def _worker_base_url() -> str | None:
    return settings.worker_base_url or settings.usaa_worker_base_url


def _worker_proxy_carriers() -> set[Carrier]:
    raw = settings.worker_proxy_carriers.strip().lower()
    if raw in {"*", "all"}:
        return set(Carrier)

    carriers: set[Carrier] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            carriers.add(Carrier(value))
        except ValueError:
            continue
    return carriers


worker_proxy = WorkerProxy()
