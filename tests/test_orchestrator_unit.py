from backend import orchestrator
from backend.config import settings
from backend.models import Carrier


def test_should_use_stored_state_requires_state():
    assert not orchestrator._should_use_stored_state(Carrier.USAA, "u", None)


def test_should_use_stored_state_keeps_non_usaa_reuse():
    assert orchestrator._should_use_stored_state(
        Carrier.GEICO, "u", {"cookies": [], "origins": []}
    )


def test_should_use_stored_state_rejects_stale_usaa(monkeypatch):
    monkeypatch.setattr(settings, "usaa_quick_path_max_age_seconds", 300)
    monkeypatch.setattr(orchestrator.storage, "saved_at", lambda carrier, username: 1000.0)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1301.0)

    assert not orchestrator._should_use_stored_state(
        Carrier.USAA, "u", {"cookies": [], "origins": []}
    )


def test_should_use_stored_state_accepts_fresh_usaa(monkeypatch):
    monkeypatch.setattr(settings, "usaa_quick_path_max_age_seconds", 300)
    monkeypatch.setattr(orchestrator.storage, "saved_at", lambda carrier, username: 1000.0)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1299.0)

    assert orchestrator._should_use_stored_state(
        Carrier.USAA, "u", {"cookies": [], "origins": []}
    )


def test_should_use_stored_state_rejects_usaa_without_timestamp(monkeypatch):
    monkeypatch.setattr(orchestrator.storage, "saved_at", lambda carrier, username: None)

    assert not orchestrator._should_use_stored_state(
        Carrier.USAA, "u", {"cookies": [], "origins": []}
    )


def test_discard_stale_carrier_state_uses_optional_hook():
    calls = []

    class Flow:
        def discard_stale_state(self, username):
            calls.append(username)

    orchestrator._discard_stale_carrier_state(Flow(), "u")

    assert calls == ["u"]
