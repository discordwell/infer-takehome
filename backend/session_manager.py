from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .models import Carrier, Document, SessionState, StatusEvent


@dataclass
class Session:
    id: str
    carrier: Carrier
    username: str
    state: SessionState = SessionState.IDLE
    mfa_event: asyncio.Event = field(default_factory=asyncio.Event)
    mfa_code: str | None = None
    docs: list[Document] = field(default_factory=list)
    doc_bytes: dict[str, bytes] = field(default_factory=dict)
    error: str | None = None
    detail: str | None = None
    timings_ms: dict[str, int] | None = None
    created_at: float = field(default_factory=time.time)
    subscribers: list[asyncio.Queue[StatusEvent]] = field(default_factory=list)
    task: asyncio.Task | None = None


class SessionNotFoundError(Exception):
    pass


class SessionManager:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds

    def create(self, carrier: Carrier, username: str) -> Session:
        self._prune_stale()
        session_id = uuid.uuid4().hex
        session = Session(id=session_id, carrier=carrier, username=username)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    def attach_task(self, session_id: str, task: asyncio.Task) -> None:
        self.get(session_id).task = task

    def subscribe(self, session_id: str) -> asyncio.Queue[StatusEvent]:
        session = self.get(session_id)
        queue: asyncio.Queue[StatusEvent] = asyncio.Queue()
        session.subscribers.append(queue)
        queue.put_nowait(self._snapshot(session))
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[StatusEvent]) -> None:
        session = self._sessions.get(session_id)
        if session and queue in session.subscribers:
            session.subscribers.remove(queue)

    def transition(
        self,
        session_id: str,
        state: SessionState,
        *,
        detail: str | None = None,
    ) -> None:
        session = self.get(session_id)
        session.state = state
        session.detail = detail
        self._publish(session)

    def set_docs(
        self,
        session_id: str,
        docs: list[Document],
        doc_bytes: dict[str, bytes],
        *,
        timings_ms: dict[str, int] | None = None,
    ) -> None:
        session = self.get(session_id)
        session.docs = docs
        session.doc_bytes = doc_bytes
        session.timings_ms = timings_ms
        session.state = SessionState.DONE
        self._publish(session, event="docs_ready")

    def set_error(self, session_id: str, error: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.state = SessionState.ERROR
        session.error = error
        self._publish(session, event="error")

    async def request_mfa(self, session_id: str, timeout: float = 90.0) -> str:
        """Called by the carrier flow when MFA is required.

        Surfaces MFA_REQUIRED state to subscribers, then blocks until the user
        submits a code via submit_mfa (or timeout/cancellation).
        """
        session = self.get(session_id)
        self.transition(session_id, SessionState.MFA_REQUIRED)
        try:
            await asyncio.wait_for(session.mfa_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError("MFA code not submitted within timeout") from e
        if session.mfa_code is None:
            raise RuntimeError("MFA event set but no code recorded")
        code = session.mfa_code
        session.mfa_event.clear()
        session.mfa_code = None
        return code

    def submit_mfa(self, session_id: str, code: str) -> None:
        """Accept an MFA code from the client.

        Transitions state to AUTHENTICATING atomically so a rapid second
        POST sees the wrong state and gets rejected with 409 — prevents the
        orchestrator from consuming the second code instead of the first.
        """
        session = self.get(session_id)
        if session.state != SessionState.MFA_REQUIRED:
            raise ValueError(
                f"Cannot submit MFA in state {session.state.value}"
            )
        session.mfa_code = code
        session.state = SessionState.AUTHENTICATING
        self._publish(session)
        session.mfa_event.set()

    def get_doc_bytes(self, session_id: str, doc_id: str) -> bytes | None:
        session = self.get(session_id)
        return session.doc_bytes.get(doc_id)

    def make_mfa_callable(
        self, session_id: str
    ) -> Callable[[], Awaitable[str]]:
        async def _request() -> str:
            return await self.request_mfa(session_id)

        return _request

    def _publish(self, session: Session, event: str = "state_change") -> None:
        snapshot = self._snapshot(session, event=event)
        for q in session.subscribers:
            q.put_nowait(snapshot)

    def _snapshot(self, session: Session, event: str = "state_change") -> StatusEvent:
        return StatusEvent(
            event=event,  # type: ignore[arg-type]
            state=session.state,
            detail=session.detail,
            docs=session.docs or None,
            error=session.error,
            server_ts_ms=int(time.time() * 1000),
            timings_ms=session.timings_ms,
        )

    def _prune_stale(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [sid for sid, s in self._sessions.items() if s.created_at < cutoff]
        for sid in stale:
            session = self._sessions.pop(sid)
            if session.task and not session.task.done():
                session.task.cancel()


manager = SessionManager()
