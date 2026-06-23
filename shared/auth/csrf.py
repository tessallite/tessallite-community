"""Double-submit cookie CSRF protection middleware.

For every state-changing request (POST, PUT, PATCH, DELETE) that relies on
cookie-based auth, the client must send an ``X-CSRF-Token`` header whose
value matches the ``csrf_token`` cookie.

CSRF validation activates only when:
- The request is a state-changing method (POST/PUT/PATCH/DELETE), AND
- The request carries an ``access_token`` cookie (browser cookie auth), AND
- The request does NOT carry an ``Authorization`` header (inter-service/API).

Requests without an ``access_token`` cookie are not cookie-authenticated,
so CSRF does not apply (they will be authenticated or rejected by the auth
dependency chain instead).
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validate CSRF double-submit cookie on cookie-authenticated writes."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        if request.headers.get("authorization"):
            return await call_next(request)

        if not request.cookies.get("access_token"):
            return await call_next(request)

        cookie_val = request.cookies.get("csrf_token")
        header_val = request.headers.get("x-csrf-token")

        if not cookie_val or not header_val or cookie_val != header_val:
            logger.warning(
                "CSRF validation failed: path=%s method=%s",
                request.url.path, request.method,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF validation failed"},
            )

        return await call_next(request)
