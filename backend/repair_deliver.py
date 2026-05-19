"""Bridge: PDFs Claude wrote to disk during auto-repair → user's live session.

Claude (inside its repair subprocess) navigates the carrier site, downloads
candidate PDFs into `storage/repair/<session_id>/delivered/`, and writes
`STATUS=DONE`. The controller (`auto_repair._check_done`) calls
`deliver_if_present` after parsing STATUS — if the dir has PDFs, we load
them and publish via `session_manager.set_docs` so the user's existing SSE
delivers `docs_ready` and the frontend renders them in place.

The Claude subprocess can't reach the FastAPI session_manager singleton
directly (different process); this disk-handoff is the IPC.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from .models import Document
from .session_manager import SessionNotFoundError, manager

log = logging.getLogger(__name__)

REPAIR_ROOT = Path("storage/repair")
DELIVERED_SUBDIR = "delivered"
DELIVERED_MARKER = "delivered.json"  # written after successful set_docs


def deliver_if_present(session_id: str) -> bool:
    """Look for PDFs in storage/repair/<sid>/delivered/ and ship them.

    Returns True if any docs were delivered to the session. Idempotent — once
    delivered, a marker file prevents re-delivery on the next cadence tick.
    """
    delivered_dir = REPAIR_ROOT / session_id / DELIVERED_SUBDIR
    marker = REPAIR_ROOT / session_id / DELIVERED_MARKER
    if marker.exists():
        return False  # already delivered for this repair turn
    if not delivered_dir.is_dir():
        return False

    pdfs = sorted(
        p for p in delivered_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )
    if not pdfs:
        return False

    docs: list[Document] = []
    doc_bytes: dict[str, bytes] = {}
    started = time.time()
    for path in pdfs:
        try:
            body = path.read_bytes()
        except OSError as e:
            log.warning("delivery: could not read %s: %s", path, e)
            continue
        if not body.startswith(b"%PDF"):
            log.warning(
                "delivery: %s is not a PDF (missing %%PDF header); skipping",
                path,
            )
            continue
        doc_id = uuid.uuid4().hex[:12]
        docs.append(
            Document(
                id=doc_id,
                name=path.name,
                content_type="application/pdf",
                size_bytes=len(body),
            )
        )
        doc_bytes[doc_id] = body

    if not docs:
        log.info(
            "delivery: session=%s found delivered/ but no valid PDFs",
            session_id,
        )
        return False

    try:
        session = manager.get(session_id)
    except SessionNotFoundError:
        log.warning(
            "delivery: session %s gone; can't ship %d docs", session_id, len(docs)
        )
        return False

    timings = {
        "repair_delivered_ms": int((time.time() - started) * 1000),
        "repair_doc_count": len(docs),
    }
    if session.timings_ms:
        timings = {**session.timings_ms, **timings}

    manager.set_docs(session_id, docs, doc_bytes, timings_ms=timings)

    try:
        marker.write_text(
            json.dumps(
                {
                    "delivered_at": time.time(),
                    "doc_count": len(docs),
                    "doc_names": [d.name for d in docs],
                },
                indent=2,
            )
        )
    except OSError as e:
        log.warning("delivery: could not write marker: %s", e)

    log.info(
        "delivery OK session=%s docs=%d total_bytes=%d",
        session_id,
        len(docs),
        sum(d.size_bytes for d in docs),
    )
    return True
