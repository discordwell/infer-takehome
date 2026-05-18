from backend.carriers.usaa import UsaaFlow


def test_usaa_document_body_validation():
    assert UsaaFlow._is_document_body(b"%PDF-1.7\nbody", "application/pdf")
    assert UsaaFlow._is_document_body(b"binary body", "application/octet-stream")
    assert not UsaaFlow._is_document_body(
        b"<!doctype html><html></html>", "application/pdf"
    )
    assert not UsaaFlow._is_document_body(b"", "application/pdf")


def test_usaa_single_document_publishes_first_pdf_bytes():
    flow = UsaaFlow()
    docs, doc_bytes = flow._single_document(
        b"%PDF-1.7\nbody", "application/pdf", "Policy Declaration"
    )

    assert len(docs) == 1
    assert docs[0].id == "usaa-doc-0"
    assert docs[0].name == "Policy Declaration.pdf"
    assert docs[0].size_bytes == len(b"%PDF-1.7\nbody")
    assert doc_bytes == {"usaa-doc-0": b"%PDF-1.7\nbody"}


def test_usaa_merge_documents_dedupes_and_renumbers():
    flow = UsaaFlow()
    target_docs = []
    target_bytes = {}
    seen = set()

    docs1, bytes1 = flow._single_document(b"%PDF one", "application/pdf", "One")
    docs2, bytes2 = flow._single_document(b"%PDF one", "application/pdf", "Duplicate")
    docs3, bytes3 = flow._single_document(b"%PDF two", "application/pdf", "Two")

    flow._merge_documents(target_docs, target_bytes, seen, docs1, bytes1)
    flow._merge_documents(target_docs, target_bytes, seen, docs2, bytes2)
    flow._merge_documents(target_docs, target_bytes, seen, docs3, bytes3)

    assert [d.id for d in target_docs] == ["usaa-doc-0", "usaa-doc-1"]
    assert [d.name for d in target_docs] == ["One.pdf", "Two.pdf"]
    assert target_bytes == {
        "usaa-doc-0": b"%PDF one",
        "usaa-doc-1": b"%PDF two",
    }


def test_usaa_timing_snapshot_uses_first_label_occurrence():
    flow = UsaaFlow()
    flow._timings = [
        ("mfa_code_received", 0.0),
        ("doc_pdf_bytes", 1.234),
        ("doc_pdf_bytes", 2.0),
        ("docs_ready_publish", 2.5),
    ]

    assert flow.timing_snapshot() == {
        "mfa_code_received": 0,
        "doc_pdf_bytes": 1234,
        "docs_ready_publish": 2500,
    }
