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


def test_uid_index_records_and_returns_per_carrier(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path)
    docs = [Document(id="doc-1", name="Policy.pdf", size_bytes=8)]

    result_store.save_done(
        session_id="s-usaa",
        carrier=Carrier.USAA,
        username="alice",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
        uid="uid-alice",
    )
    result_store.save_done(
        session_id="s-geico",
        carrier=Carrier.GEICO,
        username="alice",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
        uid="uid-alice",
    )
    result_store.save_done(
        session_id="s-other",
        carrier=Carrier.USAA,
        username="bob",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
        uid="uid-bob",
    )

    alice = result_store.latest_for_uid("uid-alice")
    carriers = {e["carrier"] for e in alice}
    assert carriers == {"usaa", "geico"}
    # Each entry carries doc_count so the boring UI can preview without
    # reading metadata.json.
    assert all(e["doc_count"] == 1 for e in alice)

    bob = result_store.latest_for_uid("uid-bob")
    assert [e["session_id"] for e in bob] == ["s-other"]


def test_uid_index_skips_pruned_sessions(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path)
    docs = [Document(id="doc-1", name="Policy.pdf", size_bytes=8)]
    result_store.save_done(
        session_id="will-be-gone",
        carrier=Carrier.USAA,
        username="alice",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
        uid="uid-alice",
    )
    # Simulate the session dir being pruned out from under us.
    import shutil

    shutil.rmtree(tmp_path / "will-be-gone")

    assert result_store.latest_for_uid("uid-alice") == []


def test_save_done_without_uid_skips_index(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path)
    docs = [Document(id="doc-1", name="Policy.pdf", size_bytes=8)]
    result_store.save_done(
        session_id="anon-1",
        carrier=Carrier.USAA,
        username="alice",
        docs=docs,
        doc_bytes={"doc-1": b"%PDF-1.7"},
    )
    # No uid → no index dir created.
    assert not (tmp_path / "_uid_index").exists()


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
