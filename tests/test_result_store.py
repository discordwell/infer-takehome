import json
import time
from pathlib import Path

from backend import result_store
from backend.models import Carrier, Document, SessionState


def test_result_store_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path)
    docs = [Document(id="doc-1", name="Policy.pdf", size_bytes=8)]

    result_store.save_done(
        session_id="session-1",
        carrier=Carrier.PROGRESSIVE,
        username="user@example.com",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
        timings_ms={"docs_ready_publish": 123},
    )

    status = result_store.load_status("session-1")
    assert status is not None
    assert status.event == "docs_ready"
    assert status.state == SessionState.DONE
    assert status.docs == docs
    assert status.timings_ms == {"docs_ready_publish": 123}
    assert result_store.load_doc("session-1", "doc-1") == docs[0]
    assert result_store.load_doc_bytes("session-1", "doc-1") == b"%PDF-1.7"


def test_result_store_prunes_old_results(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path)
    docs = [Document(id="doc-1", name="Policy.pdf", size_bytes=8)]
    result_store.save_done(
        session_id="old",
        carrier=Carrier.MERCURY,
        username="user@example.com",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
    )
    metadata = tmp_path / "old" / "metadata.json"
    data = json.loads(metadata.read_text())
    data["saved_at"] = time.time() - 1000
    metadata.write_text(json.dumps(data))

    result_store.prune(ttl_seconds=10)

    assert not (tmp_path / "old").exists()
