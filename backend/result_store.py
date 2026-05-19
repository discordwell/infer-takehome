from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from .models import Carrier, Document, SessionState, StatusEvent

RESULTS_DIR = Path(__file__).resolve().parent.parent / "storage" / "results"


def _uid_index_dir() -> Path:
    # Computed via RESULTS_DIR each call so tests that monkeypatch RESULTS_DIR
    # also redirect the uid index.
    return RESULTS_DIR / "_uid_index"


def save_done(
    *,
    session_id: str,
    carrier: Carrier,
    username: str,
    docs: list[Document],
    doc_bytes: dict[str, bytes],
    timings_ms: dict[str, int] | None = None,
    uid: str | None = None,
) -> None:
    session_dir = _session_dir(session_id)
    docs_dir = session_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    for doc in docs:
        body = doc_bytes.get(doc.id)
        if body is None:
            continue
        (docs_dir / _doc_filename(doc.id)).write_bytes(body)

    metadata = {
        "session_id": session_id,
        "carrier": carrier.value,
        "username_hash": _username_hash(username),
        "state": SessionState.DONE.value,
        "saved_at": time.time(),
        "docs": [doc.model_dump(mode="json") for doc in docs],
        "timings_ms": timings_ms,
    }
    (session_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    if uid:
        _record_uid_session(uid, carrier, session_id)


def load_status(session_id: str) -> StatusEvent | None:
    metadata = _load_metadata(session_id)
    if metadata is None:
        return None
    try:
        docs = [Document.model_validate(doc) for doc in metadata.get("docs", [])]
        return StatusEvent(
            event="docs_ready",
            state=SessionState.DONE,
            docs=docs,
            timings_ms=metadata.get("timings_ms"),
            server_ts_ms=int(time.time() * 1000),
        )
    except Exception:
        return None


def load_doc_bytes(session_id: str, doc_id: str) -> bytes | None:
    path = _session_dir(session_id) / "docs" / _doc_filename(doc_id)
    try:
        return path.read_bytes()
    except OSError:
        return None


def load_doc(session_id: str, doc_id: str) -> Document | None:
    metadata = _load_metadata(session_id)
    if metadata is None:
        return None
    for raw_doc in metadata.get("docs", []):
        try:
            doc = Document.model_validate(raw_doc)
        except Exception:
            continue
        if doc.id == doc_id:
            return doc
    return None


def exists(session_id: str) -> bool:
    return (_session_dir(session_id) / "metadata.json").exists()


def prune(ttl_seconds: int) -> None:
    if ttl_seconds <= 0 or not RESULTS_DIR.exists():
        return
    cutoff = time.time() - ttl_seconds
    for metadata_path in RESULTS_DIR.glob("*/metadata.json"):
        try:
            saved_at = float(json.loads(metadata_path.read_text())["saved_at"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            saved_at = 0
        if saved_at < cutoff:
            shutil.rmtree(metadata_path.parent, ignore_errors=True)


def _load_metadata(session_id: str) -> dict | None:
    try:
        return json.loads((_session_dir(session_id) / "metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _session_dir(session_id: str) -> Path:
    return RESULTS_DIR / _safe_id(session_id)


def _doc_filename(doc_id: str) -> str:
    return f"{_safe_id(doc_id)}.bin"


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value)[:120] or "id"


def _username_hash(username: str) -> str:
    import hashlib

    return hashlib.sha256(username.encode()).hexdigest()


def _uid_index_path(uid: str) -> Path:
    return _uid_index_dir() / f"{_safe_id(uid)}.json"


def _record_uid_session(uid: str, carrier: Carrier, session_id: str) -> None:
    _uid_index_dir().mkdir(parents=True, exist_ok=True)
    path = _uid_index_path(uid)
    try:
        index = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        index = {"uid_hash": _safe_id(uid), "carriers": {}}
    index.setdefault("carriers", {})[carrier.value] = {
        "session_id": session_id,
        "saved_at": time.time(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2))
    os.replace(tmp, path)


def latest_for_uid(uid: str) -> list[dict]:
    """Return per-carrier most-recent completed sessions for this uid.

    Drops entries whose backing session dir has been pruned.
    """
    path = _uid_index_path(uid)
    try:
        index = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    carriers = index.get("carriers") or {}
    changed = False
    for carrier_value, entry in list(carriers.items()):
        session_id = entry.get("session_id")
        if not session_id or not exists(session_id):
            carriers.pop(carrier_value, None)
            changed = True
            continue
        metadata = _load_metadata(session_id) or {}
        docs = metadata.get("docs") or []
        out.append(
            {
                "carrier": carrier_value,
                "session_id": session_id,
                "saved_at": entry.get("saved_at") or metadata.get("saved_at"),
                "doc_count": len(docs),
            }
        )
    if changed:
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(index, indent=2))
            os.replace(tmp, path)
        except OSError:
            pass
    out.sort(key=lambda e: e.get("saved_at") or 0, reverse=True)
    return out
