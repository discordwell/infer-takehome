"""Unit tests for the SSE stream-termination rule in backend.main.

`_should_close_stream` decides whether the `/api/status/{id}` SSE generator
closes after delivering an event. The subtle case is a repair held open over a
terminal state: an orchestrator failure leaves the session in ERROR, while a
user-rejected feedback recovery leaves it in DONE. Both set `repair_kicked`,
and both must keep the stream open until the final `repair_done` so the live
repair log and any re-delivered documents reach the client.
"""

import pytest

from backend.main import _should_close_stream
from backend.models import SessionState, StatusEvent

NON_TERMINAL_STATES = [
    SessionState.IDLE,
    SessionState.LOGGING_IN,
    SessionState.MFA_REQUIRED,
    SessionState.AUTHENTICATING,
    SessionState.FETCHING_DOCS,
]


def _evt(event: str = "state_change", state: SessionState = SessionState.DONE) -> StatusEvent:
    return StatusEvent(event=event, state=state)


def test_done_closes_without_active_repair():
    assert _should_close_stream(_evt(state=SessionState.DONE), repair_active=False) is True


def test_done_stays_open_during_repair():
    # Regression: feedback recovery leaves the session in DONE while Claude
    # looks for better docs. The reopened stream must stay open to deliver the
    # live repair log + replacement docs, not close on the first DONE snapshot.
    assert _should_close_stream(_evt(state=SessionState.DONE), repair_active=True) is False


def test_error_closes_without_active_repair():
    assert _should_close_stream(_evt(state=SessionState.ERROR), repair_active=False) is True


def test_error_stays_open_during_repair():
    assert _should_close_stream(_evt(state=SessionState.ERROR), repair_active=True) is False


def test_repair_done_is_always_terminal():
    # repair_done ends the stream regardless of state or repair_active.
    for state in (SessionState.DONE, SessionState.ERROR, SessionState.FETCHING_DOCS):
        assert (
            _should_close_stream(_evt(event="repair_done", state=state), repair_active=True)
            is True
        )
        assert (
            _should_close_stream(_evt(event="repair_done", state=state), repair_active=False)
            is True
        )


@pytest.mark.parametrize("state", NON_TERMINAL_STATES)
def test_non_terminal_states_keep_stream_open(state):
    assert _should_close_stream(_evt(event="state_change", state=state), repair_active=False) is False
    assert _should_close_stream(_evt(event="repair_log", state=state), repair_active=True) is False
