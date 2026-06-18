"""Robust truthy parsing for stringly-typed boolean environment flags.

A handful of toggles are read straight from the environment at *call time*
rather than through the frozen ``settings`` singleton, because tests and the
local demo flip them per process *after* import (e.g. the mock-carrier flags
the integration suite monkeypatches, or the ``REPAIR_ENABLED`` kill switch).

This module is the single place that decides what counts as "true", so any of
``1`` / ``true`` / ``yes`` / ``on`` (in any case) work, matching both the
booleans written in ``.env.example`` (``CARRIER_MOCK=false``,
``MOCK_QUICK_PATH_OK=true`` …) and ordinary pydantic-settings / Docker
conventions.

History: each reader used to hard-code ``os.getenv(name) == "1"``. That made a
documented ``CARRIER_MOCK=true`` silently do nothing (it isn't the literal
``"1"``), and made ``.env.example``'s own ``MOCK_QUICK_PATH_OK=true`` evaluate
to *false* — a confusing trap on the exact no-credentials path reviewers use.
``auto_repair.is_enabled()`` already parsed ``REPAIR_ENABLED`` the tolerant
way; this generalizes that one good implementation.
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def env_truthy(name: str, default: bool = False) -> bool:
    """Return the boolean value of environment variable ``name``.

    - Unset or empty -> ``default``.
    - Recognized spellings (case-insensitive): truthy ``1/true/yes/on``,
      falsy ``0/false/no/off``.
    - Any other non-empty value -> ``default`` (a typo never silently flips a
      flag the opposite way).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if not val:
        return default
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    return default
