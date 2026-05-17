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
