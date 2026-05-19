"""Per-uid slot manager with per-carrier exclusion.

One slot per uid. Heartbeats from the SSE keep the slot alive; 60s of silence
(closed tab, dropped connection) auto-releases it so the demo doesn't lock up.

A carrier can only be driven by one uid at a time — when a second uid tries
the same carrier mid-flow, `claim` returns CARRIER_BUSY and the caller serves
the boring (cached) path instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from .models import Carrier

SLOT_IDLE_TTL_SECONDS = 60.0


class ClaimResult(str, Enum):
    OK = "OK"
    CARRIER_BUSY = "CARRIER_BUSY"


@dataclass
class Slot:
    uid: str
    carrier: Carrier
    session_id: str
    claimed_at: float
    last_heartbeat: float


@dataclass
class ClaimOutcome:
    result: ClaimResult
    slot: Slot | None = None
    busy_owner_uid: str | None = None


class SlotManager:
    """Single-event-loop-safe coordinator for slots and per-carrier ownership.

    All methods are intentionally synchronous. There are no `await` points
    inside any method, so two coroutines can never interleave mid-mutation —
    the GIL plus cooperative scheduling makes the dict reads/writes atomic
    relative to each other. **Adding an `await` inside any method here would
    introduce a TOCTOU race on carrier ownership.** If async work is ever
    needed, wrap the whole method body in an `asyncio.Lock`.
    """

    def __init__(self, idle_ttl_seconds: float = SLOT_IDLE_TTL_SECONDS) -> None:
        self._slots: dict[str, Slot] = {}
        self._carrier_owners: dict[Carrier, str] = {}
        self._ttl = idle_ttl_seconds

    def claim(
        self, uid: str, carrier: Carrier, session_id: str
    ) -> ClaimOutcome:
        self.prune_stale()
        existing = self._slots.get(uid)
        if existing is not None and existing.carrier == carrier:
            existing.session_id = session_id
            existing.last_heartbeat = time.time()
            return ClaimOutcome(ClaimResult.OK, existing)

        owner = self._carrier_owners.get(carrier)
        if owner is not None and owner != uid:
            return ClaimOutcome(ClaimResult.CARRIER_BUSY, busy_owner_uid=owner)

        if existing is not None:
            # Same uid switching carriers — release the old one.
            self._release_internal(uid)

        now = time.time()
        slot = Slot(
            uid=uid,
            carrier=carrier,
            session_id=session_id,
            claimed_at=now,
            last_heartbeat=now,
        )
        self._slots[uid] = slot
        self._carrier_owners[carrier] = uid
        return ClaimOutcome(ClaimResult.OK, slot)

    def get(self, uid: str) -> Slot | None:
        return self._slots.get(uid)

    def carrier_owner(self, carrier: Carrier) -> str | None:
        return self._carrier_owners.get(carrier)

    def tick(self, uid: str) -> bool:
        """Extend the heartbeat for `uid`'s slot. Returns False if no slot."""
        slot = self._slots.get(uid)
        if slot is None:
            return False
        slot.last_heartbeat = time.time()
        return True

    def release(self, uid: str) -> None:
        self._release_internal(uid)

    def release_session(self, session_id: str) -> None:
        """Release whichever slot currently holds this session_id (if any)."""
        for uid, slot in list(self._slots.items()):
            if slot.session_id == session_id:
                self._release_internal(uid)
                return

    def prune_stale(self) -> list[str]:
        cutoff = time.time() - self._ttl
        stale_uids = [
            uid for uid, slot in self._slots.items() if slot.last_heartbeat < cutoff
        ]
        for uid in stale_uids:
            self._release_internal(uid)
        return stale_uids

    def snapshot(self) -> list[dict]:
        return [
            {
                "uid_hint": slot.uid[:6] + "…",
                "carrier": slot.carrier.value,
                "session_id": slot.session_id,
                "claimed_at": slot.claimed_at,
                "last_heartbeat": slot.last_heartbeat,
                "idle_seconds": round(time.time() - slot.last_heartbeat, 1),
            }
            for slot in self._slots.values()
        ]

    def _release_internal(self, uid: str) -> None:
        slot = self._slots.pop(uid, None)
        if slot is None:
            return
        owner = self._carrier_owners.get(slot.carrier)
        if owner == uid:
            self._carrier_owners.pop(slot.carrier, None)


slot_manager = SlotManager()
