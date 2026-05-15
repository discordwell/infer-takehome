from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage" / "sessions"


def _key(carrier: str, username: str) -> str:
    return hashlib.sha256(f"{carrier}:{username}".encode()).hexdigest()


def _path(carrier: str, username: str) -> Path:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return STORAGE_DIR / f"{_key(carrier, username)}.json"


def save(carrier: str, username: str, storage_state: dict[str, Any]) -> None:
    payload = {"saved_at": time.time(), "storage_state": storage_state}
    _path(carrier, username).write_text(json.dumps(payload))


def load(carrier: str, username: str) -> dict[str, Any] | None:
    """Return the saved Playwright storage_state, or None if absent/corrupt."""
    p = _path(carrier, username)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data["storage_state"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def saved_at(carrier: str, username: str) -> float | None:
    p = _path(carrier, username)
    if not p.exists():
        return None
    try:
        return float(json.loads(p.read_text())["saved_at"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def delete(carrier: str, username: str) -> None:
    _path(carrier, username).unlink(missing_ok=True)
