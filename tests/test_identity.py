"""Tests for cookie-based uid issuance."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import Request, Response

from backend import identity


def _request_with_cookies(cookies: dict[str, str], scheme: str = "http"):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "scheme": scheme,
        "headers": [
            (
                b"cookie",
                "; ".join(f"{k}={v}" for k, v in cookies.items()).encode("latin-1"),
            )
        ]
        if cookies
        else [],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
    }
    return Request(scope)


def test_ensure_uid_issues_new_cookie_when_absent():
    req = _request_with_cookies({})
    resp = Response()
    uid = identity.ensure_uid(req, resp)
    assert uid
    set_cookies = resp.headers.getlist("set-cookie")
    assert any(identity.COOKIE_NAME in v for v in set_cookies)
    assert any("HttpOnly" in v for v in set_cookies)


def test_ensure_uid_reuses_existing_cookie():
    req = _request_with_cookies({identity.COOKIE_NAME: "abc123"})
    resp = Response()
    uid = identity.ensure_uid(req, resp)
    assert uid == "abc123"
    # No Set-Cookie when one already exists.
    assert resp.headers.getlist("set-cookie") == []


def test_get_uid_returns_none_when_absent():
    req = _request_with_cookies({})
    assert identity.get_uid(req) is None


def test_secure_flag_set_when_forwarded_proto_is_https():
    req = MagicMock(spec=Request)
    req.cookies = {}
    req.headers = {"x-forwarded-proto": "https"}
    req.url = MagicMock()
    req.url.scheme = "http"
    resp = Response()
    identity.ensure_uid(req, resp)
    set_cookies = resp.headers.getlist("set-cookie")
    assert any("Secure" in v for v in set_cookies)


def test_secure_flag_unset_for_http():
    req = MagicMock(spec=Request)
    req.cookies = {}
    req.headers = {}
    req.url = MagicMock()
    req.url.scheme = "http"
    resp = Response()
    identity.ensure_uid(req, resp)
    set_cookies = resp.headers.getlist("set-cookie")
    assert all("Secure" not in v for v in set_cookies)


def test_two_calls_generate_distinct_uids():
    req1 = _request_with_cookies({})
    req2 = _request_with_cookies({})
    uid1 = identity.ensure_uid(req1, Response())
    uid2 = identity.ensure_uid(req2, Response())
    assert uid1 != uid2
