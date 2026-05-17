from __future__ import annotations

import os

from ..models import Carrier
from .base import CarrierFlow
from .geico import GeicoFlow
from .mock import MockFlow
from .usaa import UsaaFlow


def _build() -> dict[Carrier, CarrierFlow]:
    if os.getenv("CARRIER_MOCK") == "1":
        return {
            Carrier.GEICO: MockFlow(Carrier.GEICO),
            Carrier.USAA: MockFlow(Carrier.USAA),
        }
    return {
        Carrier.GEICO: GeicoFlow(),
        Carrier.USAA: UsaaFlow(),
    }


_FLOWS: dict[Carrier, CarrierFlow] = _build()


def get_flow(carrier: Carrier) -> CarrierFlow:
    flow = _FLOWS.get(carrier)
    if flow is None:
        raise ValueError(f"Carrier {carrier.value!r} is not wired up yet")
    return flow


def supported_carriers() -> list[Carrier]:
    return list(_FLOWS.keys())
