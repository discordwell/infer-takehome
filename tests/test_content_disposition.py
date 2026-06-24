"""Unit tests for ``main._content_disposition``.

The document-serving endpoint builds a ``Content-Disposition`` header from a
carrier doc name scraped off a portal page. Those names are untrusted text and
routinely carry characters that can't go into an HTTP header verbatim — most
importantly non-latin-1 runes (an em-dash in "Auto Policy – Declarations"),
which made Starlette's latin-1 header encoding raise and 500 the PDF fetch.
These pin the safe-encoding contract.
"""

from __future__ import annotations

import pytest
from fastapi.responses import Response

from backend.main import _content_disposition


def _raw_header_value(name: str) -> bytes:
    """The exact bytes Starlette would put on the wire for this doc name.

    Constructing the Response is the operative check: it latin-1-encodes header
    values, which is precisely where an un-sanitized non-latin-1 name used to
    raise ``UnicodeEncodeError`` (→ HTTP 500).
    """
    resp = Response(
        content=b"%PDF",
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(name)},
    )
    return next(v for k, v in resp.raw_headers if k == b"content-disposition")


class TestSimpleNames:
    @pytest.mark.parametrize(
        "name",
        ["Declarations.pdf", "doc-0.pdf", "id_card.pdf", "a.b-c_d~e.pdf"],
    )
    def test_pure_unreserved_ascii_uses_plain_filename(self, name):
        # No special characters → the simple, maximally compatible form, with
        # no filename* extension.
        assert _content_disposition(name) == f'inline; filename="{name}"'

    def test_empty_name_falls_back_to_document(self):
        assert _content_disposition("") == 'inline; filename="document"'

    def test_disposition_is_configurable(self):
        assert _content_disposition("x.pdf", disposition="attachment") == (
            'attachment; filename="x.pdf"'
        )


class TestUnsafeNames:
    def test_em_dash_does_not_raise_and_round_trips_utf8(self):
        # The headline bug: a U+2013 em-dash used to 500 the doc fetch.
        name = "Auto Policy – Declarations.pdf"
        raw = _raw_header_value(name)
        # Survives the wire encoding and carries the original name, UTF-8
        # percent-encoded, in filename*.
        assert b"filename*=utf-8''" in raw
        assert b"%E2%80%93" in raw  # the em-dash, percent-encoded

    def test_embedded_quote_is_neutralized(self):
        # A quote in the ASCII fallback would break the quoted-string; it must
        # be stripped there and percent-encoded in filename*. Pin the exact
        # header so any regression in either half is caught.
        assert _content_disposition('weird"name.pdf') == (
            'inline; filename="weirdname.pdf"; filename*=utf-8\'\'weird%22name.pdf'
        )

    def test_lone_surrogate_does_not_raise(self):
        # quote() raises on lone surrogates; the helper scrubs them so it never
        # re-introduces the 500 it exists to prevent.
        raw = _raw_header_value("doc\ud800.pdf")
        assert raw.startswith(b"inline; filename=")
        assert b"\r" not in raw and b"\n" not in raw

    def test_crlf_cannot_inject_a_header(self):
        # The real injection vector is the CR/LF itself: strip it and the
        # leftover text is just harmless filename content inside the
        # quoted-string. The invariant is that no bare CR/LF reaches the wire,
        # so a second header line can never be smuggled in.
        raw = _raw_header_value("safe\r\nInjected-Header: pwned")
        assert b"\r" not in raw and b"\n" not in raw
        assert b"%0D%0A" in raw  # the CRLF survives only percent-encoded

    @pytest.mark.parametrize(
        "name",
        [
            "Renters Policy (2024).pdf",  # spaces + parens
            "déclaration.pdf",  # accented latin-1-ish but still needs filename*
            "保险单.pdf",  # CJK
            'a"b\r\n–c.pdf',  # everything at once
        ],
    )
    def test_unsafe_names_never_raise_on_the_wire(self, name):
        raw = _raw_header_value(name)
        assert raw.startswith(b"inline; filename=")
        assert b"\r" not in raw and b"\n" not in raw
