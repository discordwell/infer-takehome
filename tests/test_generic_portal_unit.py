from backend.carriers.generic_portal import (
    GenericPortalFlow,
    MercuryFlow,
    MERCURY_SPEC,
    ProgressiveFlow,
    PROGRESSIVE_SPEC,
)
from backend.carriers.registry import supported_carriers
from backend.models import Carrier


def test_generic_document_body_validation():
    assert GenericPortalFlow._is_document_body(b"%PDF-1.7\nbody", "application/pdf")
    assert GenericPortalFlow._is_document_body(
        b"binary body", "application/octet-stream"
    )
    assert not GenericPortalFlow._is_document_body(
        b"<!doctype html><html></html>", "application/pdf"
    )
    assert not GenericPortalFlow._is_document_body(b"", "application/pdf")


def test_generic_name_from_headers_decodes_filename():
    name = GenericPortalFlow._name_from_headers(
        {"content-disposition": "attachment; filename*=UTF-8''Policy%20Dec.pdf"},
        "https://example.test/doc",
        "fallback",
    )

    assert name == "Policy Dec.pdf"


def test_experimental_carriers_are_registered():
    assert {
        Carrier.USAA,
        Carrier.GEICO,
        Carrier.PROGRESSIVE,
        Carrier.ALLSTATE,
        Carrier.STATE_FARM,
        Carrier.MERCURY,
    }.issubset(set(supported_carriers()))


def test_progressive_spec_has_login_and_document_urls():
    assert PROGRESSIVE_SPEC.login_url.startswith("https://")
    assert PROGRESSIVE_SPEC.document_urls


def test_progressive_otp_selector_covers_angular_mfa_field():
    selector = ProgressiveFlow._otp_selector()

    assert "formcontrolname='otp'" in selector
    assert "data-pgr-id='inputOtp'" in selector
    assert "maxlength='6'" in selector


def test_mercury_spec_points_at_customer_portal():
    assert MERCURY_SPEC.login_url == "https://cp.mercuryinsurance.com/"
    assert any("download-id-cards" in url for url in MERCURY_SPEC.document_urls)


def test_generic_url_scheme_helpers():
    assert GenericPortalFlow._is_http_url("https://cp.mercuryinsurance.com/customer")
    assert not GenericPortalFlow._is_http_url("javascript:void(0)")
    assert GenericPortalFlow._is_blob_url(
        "blob:https://cp.mercuryinsurance.com/7e556b3f"
    )
    assert not GenericPortalFlow._is_blob_url("https://cp.mercuryinsurance.com/")


def test_mercury_rejects_oversized_declaration_pdf():
    assert MercuryFlow._is_plausible_mercury_declarations_pdf(b"%PDF-1.7")
    assert not MercuryFlow._is_plausible_mercury_declarations_pdf(
        b"%PDF-1.7" + (b"x" * 750_001)
    )
