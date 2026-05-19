from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import result_store
from .config import settings
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
    uid: str | None = None
    repair_log: list[dict] = field(default_factory=list)
    repair_kicked: bool = False
    repair_done_event: asyncio.Event = field(default_factory=asyncio.Event)
    feedback_recovery_active: bool = False
    notify_email: str | None = None
    notify_started_at: float | None = None
    pdf_analysis: list[dict] | None = None


class SessionNotFoundError(Exception):
    pass


class SessionManager:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds

    def create(
        self, carrier: Carrier, username: str, uid: str | None = None
    ) -> Session:
        self._prune_stale()
        session_id = uuid.uuid4().hex
        session = Session(
            id=session_id, carrier=carrier, username=username, uid=uid
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    def discard(self, session_id: str) -> None:
        """Remove a session that was created but never started.

        Used by /api/login when a slot claim fails after session creation."""
        session = self._sessions.pop(session_id, None)
        if session and session.task and not session.task.done():
            session.task.cancel()

    def attach_task(self, session_id: str, task: asyncio.Task) -> None:
        self.get(session_id).task = task

    def subscribe(self, session_id: str) -> asyncio.Queue[StatusEvent]:
        session = self.get(session_id)
        queue: asyncio.Queue[StatusEvent] = asyncio.Queue()
        session.subscribers.append(queue)
        # Replay any accumulated repair_log so a late SSE reconnect catches up.
        for chunk in session.repair_log:
            queue.put_nowait(
                StatusEvent(
                    event="repair_log",
                    state=session.state,
                    detail=session.detail,
                    server_ts_ms=int(time.time() * 1000),
                    repair_chunk=chunk,
                    repair_active=True,
                )
            )
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
        if settings.persist_completed_results:
            result_store.save_done(
                session_id=session_id,
                carrier=session.carrier,
                username=session.username,
                docs=docs,
                doc_bytes=doc_bytes,
                timings_ms=timings_ms,
                uid=session.uid,
            )
        self._publish(session, event="docs_ready")

    def publish_docs_progress(
        self,
        session_id: str,
        docs: list[Document],
        doc_bytes: dict[str, bytes],
        *,
        timings_ms: dict[str, int] | None = None,
    ) -> None:
        session = self.get(session_id)
        if session.state in (SessionState.DONE, SessionState.ERROR):
            return
        if len(docs) < len(session.docs):
            return
        session.docs = list(docs)
        session.doc_bytes = dict(doc_bytes)
        session.timings_ms = timings_ms
        self._publish(session, event="docs_ready")

    def set_error(self, session_id: str, error: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.state = SessionState.ERROR
        session.error = error
        self._publish(session, event="error")

    def publish_repair_log(
        self,
        session_id: str,
        chunk: dict,
        *,
        active: bool = True,
    ) -> None:
        """Push an incremental Claude-output chunk to SSE subscribers.

        Callers (auto_repair) provide a dict like {turn, kind, text}. We retain
        the last entries on the session so a late SSE subscriber can replay.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.repair_log.append(chunk)
        # No back-pressure cap by design — user wanted full firehose.
        snapshot = StatusEvent(
            event="repair_log",
            state=session.state,
            detail=session.detail,
            server_ts_ms=int(time.time() * 1000),
            repair_chunk=chunk,
            repair_active=active,
        )
        for q in session.subscribers:
            q.put_nowait(snapshot)

    def publish_repair_done(
        self,
        session_id: str,
        verdict: str,
        *,
        first_line: str,
    ) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        chunk = {"verdict": verdict, "first_line": first_line}
        snapshot = StatusEvent(
            event="repair_done",
            state=session.state,
            detail=session.detail,
            server_ts_ms=int(time.time() * 1000),
            repair_chunk=chunk,
            repair_active=False,
        )
        for q in session.subscribers:
            q.put_nowait(snapshot)
        # Email notifier (if any) is awaiting this event.
        session.repair_done_event.set()

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
        session = self._sessions.get(session_id)
        if session is not None:
            return session.doc_bytes.get(doc_id)
        if not settings.persist_completed_results:
            return None
        result_store.prune(self._ttl)
        return result_store.load_doc_bytes(session_id, doc_id)

    def get_persisted_status(self, session_id: str) -> StatusEvent | None:
        if not settings.persist_completed_results:
            return None
        result_store.prune(self._ttl)
        return result_store.load_status(session_id)

    def get_persisted_doc(self, session_id: str, doc_id: str) -> Document | None:
        if not settings.persist_completed_results:
            return None
        result_store.prune(self._ttl)
        return result_store.load_doc(session_id, doc_id)

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
        if settings.persist_completed_results:
            result_store.prune(self._ttl)


manager = SessionManager(ttl_seconds=settings.session_ttl_seconds)
