"""Cookie-based user identity for slot/cache scoping.

A long-lived `demo_uid` cookie identifies a "user" across requests. Different
browsers (even behind the same NAT) get different uids, so the per-uid slot
and per-uid result cache don't collide.

This is not authentication. Anyone who steals the cookie inherits the slot
and cache. For the demo that's fine; real auth is out of scope.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response

COOKIE_NAME = "demo_uid"
COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days


def mint_uid() -> str:
    return secrets.token_urlsafe(18)


def get_uid(request: Request) -> str | None:
    """Return the uid from the request cookie, or None if absent."""
    return request.cookies.get(COOKIE_NAME)


def set_uid_cookie(response: Response, uid: str, request: Request) -> None:
    """Stamp the demo_uid cookie on `response`."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=uid,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_should_secure(request),
        path="/",
    )


def ensure_uid(request: Request, response: Response) -> str:
    """Return existing uid or mint a new one and set the cookie on `response`."""
    uid = get_uid(request)
    if uid:
        return uid
    uid = mint_uid()
    set_uid_cookie(response, uid, request)
    return uid


def _should_secure(request: Request) -> bool:
    """Set Secure cookie flag when the request came in over HTTPS.

    We're behind Caddy which terminates TLS; honor `X-Forwarded-Proto`. The
    first hop wins — split on comma to handle proxy chains, and compare
    exactly so 'httpsx' doesn't false-match.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        first = forwarded.split(",")[0].strip().lower()
        return first == "https"
    return request.url.scheme == "https"
