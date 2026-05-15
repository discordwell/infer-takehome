import asyncio

import pytest

from backend.models import Carrier, Document, SessionState
from backend.session_manager import SessionManager, SessionNotFoundError


def test_create_session_starts_idle():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user@example.com")
    assert session.state == SessionState.IDLE
    assert session.carrier == Carrier.GEICO
    assert session.username == "user@example.com"
    assert session.id


def test_get_missing_session_raises():
    mgr = SessionManager()
    with pytest.raises(SessionNotFoundError):
        mgr.get("does-not-exist")


def test_transition_publishes_event_to_subscribers():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    queue = mgr.subscribe(session.id)
    initial = queue.get_nowait()
    assert initial.state == SessionState.IDLE

    mgr.transition(session.id, SessionState.LOGGING_IN, detail="hi")
    evt = queue.get_nowait()
    assert evt.state == SessionState.LOGGING_IN
    assert evt.detail == "hi"


async def test_request_mfa_blocks_until_submit():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")

    async def submitter():
        await asyncio.sleep(0.05)
        mgr.submit_mfa(session.id, "123456")

    task = asyncio.create_task(submitter())
    code = await mgr.request_mfa(session.id, timeout=2.0)
    await task
    assert code == "123456"
    assert mgr.get(session.id).state == SessionState.AUTHENTICATING


async def test_request_mfa_times_out():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    with pytest.raises(TimeoutError):
        await mgr.request_mfa(session.id, timeout=0.1)


def test_submit_mfa_wrong_state_raises():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    with pytest.raises(ValueError):
        mgr.submit_mfa(session.id, "123")


async def test_rapid_double_submit_rejects_second():
    """The second submit_mfa within a quick window must be rejected, not silently
    overwrite the first code (which would cause the wrong code to be sent)."""
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")

    waiter = asyncio.create_task(mgr.request_mfa(session.id, timeout=2.0))
    # let request_mfa publish MFA_REQUIRED
    await asyncio.sleep(0.01)

    mgr.submit_mfa(session.id, "first")
    with pytest.raises(ValueError):
        mgr.submit_mfa(session.id, "second")

    code = await waiter
    assert code == "first"


def test_set_docs_transitions_to_done_and_publishes():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    queue = mgr.subscribe(session.id)
    queue.get_nowait()  # drain initial

    docs = [Document(id="d1", name="dec.pdf", size_bytes=1024)]
    mgr.set_docs(session.id, docs, {"d1": b"%PDF-1.4"})
    evt = queue.get_nowait()
    assert evt.event == "docs_ready"
    assert evt.state == SessionState.DONE
    assert evt.docs == docs
    assert mgr.get_doc_bytes(session.id, "d1") == b"%PDF-1.4"


def test_set_error_transitions_to_error_and_publishes():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    queue = mgr.subscribe(session.id)
    queue.get_nowait()

    mgr.set_error(session.id, "bad password")
    evt = queue.get_nowait()
    assert evt.event == "error"
    assert evt.state == SessionState.ERROR
    assert evt.error == "bad password"


def test_unsubscribe_stops_publishing():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    queue = mgr.subscribe(session.id)
    queue.get_nowait()
    mgr.unsubscribe(session.id, queue)
    mgr.transition(session.id, SessionState.LOGGING_IN)
    assert queue.empty()
