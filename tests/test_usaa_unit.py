from backend.carriers import usaa
from backend.carriers.usaa import UsaaFlow
from backend.config import settings


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


def test_usaa_discard_stale_state_moves_profile(tmp_path, monkeypatch):
    profile = tmp_path / "usaa-chrome"
    profile.mkdir()
    (profile / "Preferences").write_text("{}")
    monkeypatch.setattr(usaa, "USAA_CHROME_PROFILE_DIR", profile)
    monkeypatch.setattr(settings, "usaa_login_driver", "playwright")
    monkeypatch.setattr(usaa.time, "time", lambda: 12345)

    UsaaFlow().discard_stale_state("u")

    moved = tmp_path / "stale" / "usaa-chrome-12345"
    assert not profile.exists()
    assert (moved / "Preferences").read_text() == "{}"


def test_usaa_context_options_default_to_os_browser_profile(tmp_path, monkeypatch):
    profile = tmp_path / "os-profile"
    monkeypatch.setattr(settings, "usaa_login_driver", "os_browser")
    monkeypatch.setattr(settings, "usaa_os_browser_profile_dir", str(profile))

    options = UsaaFlow().context_options()

    assert options["_launch_chrome_cdp"] is True
    assert options["_chrome_profile_dir"] == str(profile)


def test_usaa_context_options_can_use_playwright_profile(tmp_path, monkeypatch):
    profile = tmp_path / "usaa-cdp"
    monkeypatch.setattr(settings, "usaa_login_driver", "playwright")
    monkeypatch.setattr(usaa, "USAA_CHROME_PROFILE_DIR", profile)

    options = UsaaFlow().context_options()

    assert options["_chrome_profile_dir"] == str(profile)


def test_usaa_invalid_login_driver_rejected(monkeypatch):
    monkeypatch.setattr(settings, "usaa_login_driver", "bogus")

    try:
        UsaaFlow().context_options()
    except RuntimeError as e:
        assert "USAA_LOGIN_DRIVER" in str(e)
    else:
        raise AssertionError("invalid driver should raise")


def test_usaa_unavailable_block_detection():
    body = (
        "We are unable to complete your request. "
        "Our system is currently unavailable. Please try again later."
    ).lower()

    assert UsaaFlow._is_unavailable_block_text(body)
    assert not UsaaFlow._is_unavailable_block_text("password mismatch")


def test_usaa_debug_html_redacts_password_values():
    html = (
        '<input name="memberId" value="cordwell">'
        '<input name="password" type="password" value="secret">'
    )

    sanitized = UsaaFlow._sanitize_debug_html(html)

    assert 'value="cordwell"' in sanitized
    assert "secret" not in sanitized
    assert 'value="[redacted]"' in sanitized
