"""Tests for the per-uid / per-carrier slot manager.

The slot is reset between tests by the autouse `reset_slot_manager` fixture
in conftest.py.
"""

from __future__ import annotations

import time

import pytest

from backend.models import Carrier
from backend.slot_manager import (
    ClaimResult,
    Slot,
    SlotManager,
)


@pytest.fixture
def sm() -> SlotManager:
    return SlotManager(idle_ttl_seconds=60.0)


def test_first_claim_succeeds(sm):
    outcome = sm.claim("uid-a", Carrier.USAA, "session-1")
    assert outcome.result is ClaimResult.OK
    assert isinstance(outcome.slot, Slot)
    assert outcome.slot.session_id == "session-1"


def test_same_uid_same_carrier_returns_existing_slot(sm):
    first = sm.claim("uid-a", Carrier.USAA, "session-1").slot
    second = sm.claim("uid-a", Carrier.USAA, "session-2")
    assert second.result is ClaimResult.OK
    # Same slot object, session_id updated to the new one.
    assert second.slot is first
    assert second.slot.session_id == "session-2"


def test_different_uid_same_carrier_returns_carrier_busy(sm):
    sm.claim("uid-a", Carrier.USAA, "session-1")
    outcome = sm.claim("uid-b", Carrier.USAA, "session-2")
    assert outcome.result is ClaimResult.CARRIER_BUSY
    assert outcome.busy_owner_uid == "uid-a"
    assert outcome.slot is None


def test_different_uid_different_carrier_succeeds(sm):
    sm.claim("uid-a", Carrier.USAA, "session-1")
    outcome = sm.claim("uid-b", Carrier.GEICO, "session-2")
    assert outcome.result is ClaimResult.OK
    assert sm.carrier_owner(Carrier.USAA) == "uid-a"
    assert sm.carrier_owner(Carrier.GEICO) == "uid-b"


def test_same_uid_switching_carriers_releases_old(sm):
    sm.claim("uid-a", Carrier.USAA, "session-1")
    sm.claim("uid-a", Carrier.GEICO, "session-2")
    assert sm.carrier_owner(Carrier.USAA) is None
    assert sm.carrier_owner(Carrier.GEICO) == "uid-a"
    assert sm.get("uid-a").carrier is Carrier.GEICO


def test_release_frees_carrier(sm):
    sm.claim("uid-a", Carrier.USAA, "session-1")
    sm.release("uid-a")
    assert sm.get("uid-a") is None
    assert sm.carrier_owner(Carrier.USAA) is None

    # Now another uid can take it.
    outcome = sm.claim("uid-b", Carrier.USAA, "session-2")
    assert outcome.result is ClaimResult.OK


def test_release_session_finds_by_session_id(sm):
    sm.claim("uid-a", Carrier.USAA, "session-xyz")
    sm.release_session("session-xyz")
    assert sm.get("uid-a") is None
    assert sm.carrier_owner(Carrier.USAA) is None


def test_release_session_unknown_id_is_noop(sm):
    sm.claim("uid-a", Carrier.USAA, "session-xyz")
    sm.release_session("not-a-real-id")
    assert sm.get("uid-a") is not None


def test_tick_extends_heartbeat(sm):
    sm.claim("uid-a", Carrier.USAA, "session-1")
    before = sm.get("uid-a").last_heartbeat
    time.sleep(0.02)
    assert sm.tick("uid-a") is True
    assert sm.get("uid-a").last_heartbeat > before


def test_tick_unknown_uid_returns_false(sm):
    assert sm.tick("nobody") is False


def test_prune_stale_releases_idle_slot():
    sm = SlotManager(idle_ttl_seconds=0.05)
    sm.claim("uid-a", Carrier.USAA, "session-1")
    time.sleep(0.1)
    released = sm.prune_stale()
    assert released == ["uid-a"]
    assert sm.get("uid-a") is None
    assert sm.carrier_owner(Carrier.USAA) is None


def test_stale_slot_does_not_block_new_claim():
    sm = SlotManager(idle_ttl_seconds=0.05)
    sm.claim("uid-a", Carrier.USAA, "session-1")
    time.sleep(0.1)
    # uid-b's claim triggers prune internally and should succeed.
    outcome = sm.claim("uid-b", Carrier.USAA, "session-2")
    assert outcome.result is ClaimResult.OK
    assert sm.carrier_owner(Carrier.USAA) == "uid-b"


def test_snapshot_redacts_uid(sm):
    sm.claim("super-secret-uid-value", Carrier.USAA, "session-1")
    snap = sm.snapshot()
    assert len(snap) == 1
    # 6-char prefix + ellipsis — full uid never leaks.
    assert snap[0]["uid_hint"].startswith("super-")
    assert "super-secret-uid-value" not in snap[0]["uid_hint"]
    assert snap[0]["carrier"] == "usaa"
