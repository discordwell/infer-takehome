from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .models import Carrier, Document, SessionState, StatusEvent

RESULTS_DIR = Path(__file__).resolve().parent.parent / "storage" / "results"


def save_done(
    *,
    session_id: str,
    carrier: Carrier,
    username: str,
    docs: list[Document],
    doc_bytes: dict[str, bytes],
    timings_ms: dict[str, int] | None = None,
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
