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
    timings = {"doc_pdf_bytes": 1234, "docs_ready_publish": 2345}
    mgr.set_docs(session.id, docs, {"d1": b"%PDF-1.4"}, timings_ms=timings)
    evt = queue.get_nowait()
    assert evt.event == "docs_ready"
    assert evt.state == SessionState.DONE
    assert evt.docs == docs
    assert evt.timings_ms == timings
    assert isinstance(evt.server_ts_ms, int)
    assert mgr.get_doc_bytes(session.id, "d1") == b"%PDF-1.4"


def test_set_docs_persists_completed_result_for_later_manager():
    mgr = SessionManager()
    session = mgr.create(Carrier.PROGRESSIVE, "user")
    docs = [Document(id="d1", name="dec.pdf", size_bytes=8)]
    mgr.set_docs(session.id, docs, {"d1": b"%PDF-1.4"})

    restored = SessionManager()

    status = restored.get_persisted_status(session.id)
    assert status is not None
    assert status.state == SessionState.DONE
    assert status.docs == docs
    assert restored.get_persisted_doc(session.id, "d1") == docs[0]
    assert restored.get_doc_bytes(session.id, "d1") == b"%PDF-1.4"


def test_publish_docs_progress_keeps_fetching_and_publishes():
    mgr = SessionManager()
    session = mgr.create(Carrier.USAA, "user")
    queue = mgr.subscribe(session.id)
    queue.get_nowait()
    mgr.transition(session.id, SessionState.FETCHING_DOCS)
    queue.get_nowait()

    docs = [Document(id="d1", name="first.pdf", size_bytes=1024)]
    timings = {"doc_pdf_bytes": 1234, "docs_progress_publish": 1300}
    mgr.publish_docs_progress(
        session.id, docs, {"d1": b"%PDF first"}, timings_ms=timings
    )

    evt = queue.get_nowait()
    assert evt.event == "docs_ready"
    assert evt.state == SessionState.FETCHING_DOCS
    assert evt.docs == docs
    assert evt.timings_ms == timings
    assert mgr.get_doc_bytes(session.id, "d1") == b"%PDF first"


def test_publish_docs_progress_ignores_regressions():
    mgr = SessionManager()
    session = mgr.create(Carrier.USAA, "user")
    queue = mgr.subscribe(session.id)
    queue.get_nowait()
    mgr.transition(session.id, SessionState.FETCHING_DOCS)
    queue.get_nowait()

    docs = [
        Document(id="d1", name="first.pdf", size_bytes=1024),
        Document(id="d2", name="second.pdf", size_bytes=2048),
    ]
    mgr.publish_docs_progress(
        session.id,
        docs,
        {"d1": b"%PDF first", "d2": b"%PDF second"},
    )
    queue.get_nowait()

    mgr.publish_docs_progress(
        session.id,
        [docs[0]],
        {"d1": b"%PDF first"},
    )

    assert queue.empty()
    assert [d.id for d in mgr.get(session.id).docs] == ["d1", "d2"]


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


def test_subscribe_replays_terminal_verdict_first_even_with_repair_log():
    """A client that reconnects after repair_done must replay the terminal
    verdict FIRST — ahead of the repair_log replay and snapshot — so its stream
    closes immediately instead of hanging on a repair_kicked session whose
    repair_done won't fire a second time. Ordering is load-bearing: the
    repair_log replays carry the terminal state, so the terminal must precede
    them to guarantee closure regardless of repair_kicked."""
    mgr = SessionManager()
    session = mgr.create(Carrier.USAA, "user")
    session.repair_kicked = True
    session.state = SessionState.DONE
    # A repair ran (some live log) and then concluded while nobody was connected.
    mgr.publish_repair_log(session.id, {"turn": 1, "kind": "text", "text": "probing"})
    mgr.publish_repair_done(session.id, verdict="DONE", first_line="STATUS: DONE")
    assert session.repair_terminal == {"verdict": "DONE", "first_line": "STATUS: DONE"}

    # The late subscriber receives the terminal verdict before anything else.
    queue = mgr.subscribe(session.id)
    evt = queue.get_nowait()
    assert evt.event == "repair_done"
    assert evt.repair_chunk == {"verdict": "DONE", "first_line": "STATUS: DONE"}
    assert evt.repair_active is False
    # The replayed log + snapshot still follow (the stream closes on the first
    # event, but the queue is fully populated).
    remaining = [queue.get_nowait().event for _ in range(queue.qsize())]
    assert "repair_log" in remaining
    assert "state_change" in remaining


def test_subscribe_after_successful_repair_redelivers_docs():
    """Reconnect after a successful repair: the terminal closes the stream, but
    the snapshot in the same batch still carries the newly delivered docs, so a
    client that missed the live docs_ready can still recover them."""
    mgr = SessionManager()
    session = mgr.create(Carrier.USAA, "user")
    session.repair_kicked = True
    new_docs = [Document(id="better", name="better.pdf", size_bytes=4)]
    mgr.set_docs(session.id, new_docs, {"better": b"%PDF"})  # docs_ready, state DONE
    mgr.publish_repair_done(session.id, verdict="DONE", first_line="STATUS: DONE")

    queue = mgr.subscribe(session.id)
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert events[0].event == "repair_done"  # terminal first → closes the stream
    snapshot = next(e for e in events if e.event == "state_change")
    assert [d.id for d in (snapshot.docs or [])] == ["better"]


def test_subscribe_without_terminal_emits_only_snapshot():
    """Sessions with no concluded repair never replay a repair_done on subscribe."""
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    queue = mgr.subscribe(session.id)
    evt = queue.get_nowait()
    assert evt.event == "state_change"
    assert queue.empty()


def test_unsubscribe_stops_publishing():
    mgr = SessionManager()
    session = mgr.create(Carrier.GEICO, "user")
    queue = mgr.subscribe(session.id)
    queue.get_nowait()
    mgr.unsubscribe(session.id, queue)
    mgr.transition(session.id, SessionState.LOGGING_IN)
    assert queue.empty()
