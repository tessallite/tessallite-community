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

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from shared.config.settings import get_settings

_SPA_CLIENTS = frozenset({"spa", "web"})
_TOKEN_CLIENTS = frozenset({"excel", "plugin", "pat", "api"})


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


def omit_access_token_in_body(request: Request | None) -> bool:
    """True when the JSON login body must not echo the JWT (F-021-05).

    The SPA authenticates via the httpOnly cookie. Putting ``access_token`` in
    JSON duplicates the secret into JS-accessible memory. Excel/plugin clients
    that cannot use cookies still receive the token when they identify as such.
    """
    if request is None:
        return False
    client = (request.headers.get("x-tessallite-client") or "").strip().lower()
    if client in _TOKEN_CLIENTS:
        return False
    if client in _SPA_CLIENTS:
        return True
    xhr = (request.headers.get("x-requested-with") or "").strip().lower()
    return xhr in ("xmlhttprequest", "tessallitespa")


def login_response(
    *,
    token: str,
    role: str | None,
    tenant_id: str | None,
    max_age_seconds: int,
    request: Request | None = None,
) -> JSONResponse:
    """Build a ``JSONResponse`` with auth cookies set.

    The JSON body always returns ``role``, ``tenant_id``, and ``expires_in``.
    ``access_token`` is omitted for the SPA (cookie is the session) and included
    for Excel/plugin/API clients (F-021-05).
    """
    body: dict = {"expires_in": max_age_seconds}
    if not omit_access_token_in_body(request):
        body["access_token"] = token
    if role:
        body["role"] = role
    if tenant_id:
        body["tenant_id"] = tenant_id

    resp = JSONResponse(content=body)
    set_auth_cookies(resp, token, max_age_seconds=max_age_seconds)
    return resp
