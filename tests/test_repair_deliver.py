"""Tests for the disk→set_docs bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import repair_deliver
from backend.models import Carrier, SessionState
from backend.session_manager import manager


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.setattr(repair_deliver, "REPAIR_ROOT", tmp_path)
    sess = manager.create(Carrier.MERCURY, "alice@example.com", uid="uid-alice")
    sess.state = SessionState.ERROR  # simulating the post-orchestrator-fail state
    return sess


def _write_pdf(path: Path, marker: bytes = b"hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + marker)


def test_deliver_present_publishes_docs(session, tmp_path):
    delivered = tmp_path / session.id / "delivered"
    _write_pdf(delivered / "Declarations Page.pdf", b"dec")
    _write_pdf(delivered / "Auto ID Card.pdf", b"idc")

    ok = repair_deliver.deliver_if_present(session.id)

    assert ok is True
    assert session.state == SessionState.DONE
    assert {d.name for d in session.docs} == {
        "Declarations Page.pdf",
        "Auto ID Card.pdf",
    }
    assert all(b.startswith(b"%PDF") for b in session.doc_bytes.values())


def test_deliver_no_dir_returns_false(session):
    assert repair_deliver.deliver_if_present(session.id) is False


def test_deliver_idempotent_via_marker(session, tmp_path):
    delivered = tmp_path / session.id / "delivered"
    _write_pdf(delivered / "doc.pdf")
    assert repair_deliver.deliver_if_present(session.id) is True
    # Second call: marker is set, no re-delivery.
    assert repair_deliver.deliver_if_present(session.id) is False


def test_deliver_skips_non_pdf_files(session, tmp_path):
    delivered = tmp_path / session.id / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    (delivered / "not_a_pdf.pdf").write_bytes(b"<html>oops</html>")
    _write_pdf(delivered / "real.pdf")

    ok = repair_deliver.deliver_if_present(session.id)
    assert ok is True
    assert [d.name for d in session.docs] == ["real.pdf"]


def test_deliver_no_valid_pdfs_returns_false(session, tmp_path):
    delivered = tmp_path / session.id / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    (delivered / "junk.pdf").write_bytes(b"not a pdf")

    assert repair_deliver.deliver_if_present(session.id) is False
    assert session.state == SessionState.ERROR  # unchanged
