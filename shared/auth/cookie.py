"""httpOnly cookie helpers for JWT auth.

Sets two cookies on login:
- ``access_token`` — httpOnly, invisible to JavaScript, carries the JWT.
- ``csrf_token``   — JS-readable, used by the double-submit CSRF pattern.

The CSRF token is a random value independent of the JWT. It exists solely
so the browser-side code can echo it in the ``X-CSRF-Token`` header,
proving the request originated from our SPA (not a cross-site form).
"""
from __future__ import annotations

import secrets

from fastapi.responses import JSONResponse, Response

from shared.config.settings import get_settings


def _cookie_secure() -> bool:
    return get_settings().COOKIE_SECURE


def set_auth_cookies(
    response: Response,
    token: str,
    *,
    max_age_seconds: int,
) -> None:
    """Attach ``access_token`` and ``csrf_token`` cookies to *response*."""
    secure = _cookie_secure()

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age_seconds,
        path="/",
    )
    response.set_cookie(
        key="csrf_token",
        value=secrets.token_urlsafe(32),
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=max_age_seconds,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    """Remove both auth cookies from the browser."""
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("csrf_token", path="/")


def login_response(
    *,
    token: str,
    role: str | None,
    tenant_id: str | None,
    max_age_seconds: int,
) -> JSONResponse:
    """Build a ``JSONResponse`` with auth cookies set.

    The JSON body returns ``access_token``, ``role``, ``tenant_id``, and
    ``expires_in``.  The SPA ignores the token (it uses the httpOnly
    cookie) but non-browser clients like the Excel add-in need it.
    """
    body: dict = {"access_token": token, "expires_in": max_age_seconds}
    if role:
        body["role"] = role
    if tenant_id:
        body["tenant_id"] = tenant_id

    resp = JSONResponse(content=body)
    set_auth_cookies(resp, token, max_age_seconds=max_age_seconds)
    return resp
