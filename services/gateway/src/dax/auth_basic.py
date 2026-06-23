import base64
import binascii
import logging
from defusedxml import DefusedXmlException, ElementTree as ET

import httpx
from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.dax import credential_cache
from src.router_client import login_discover, login_for_token

logger = logging.getLogger(__name__)


def _is_credential_failure(exc: BaseException) -> bool:
    """Return True if *exc* is a legitimate "wrong credentials" response
    from the upstream auth service. Everything else (DB outage, encoding
    error, timeout) should NOT be silently masked as an auth failure.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code == 401
    # login_discover raises a plain ValueError when every tenant rejects
    # the credentials. That's the only other "legitimate failure" shape.
    return isinstance(exc, ValueError)


def _is_unknown_tenant(exc: BaseException) -> bool:
    """Return True if *exc* indicates the Catalog is not a valid tenant.

    F-002-15: for Excel server-endpoint connections the SOAP Catalog is a
    *model* slug, not a tenant, so a tenant-scoped login against it returns
    404/422 (no such tenant) rather than 401 (wrong password). That is not an
    operational error and not a credential failure — it just means "this
    Catalog is not a tenant", so we fall through to cross-tenant discovery.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code in (404, 422)
    return False


def _extract_catalog_from_soap(body: bytes) -> str:
    """Parse SOAP body and return the Catalog property value, or ''."""
    try:
        root = ET.fromstring(body)
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag == "Catalog":
                return (el.text or "").strip()
    except (ET.ParseError, DefusedXmlException):
        pass
    return ""


def _has_existing_session(body: bytes) -> bool:
    """Return True if the SOAP body carries an existing ``Session`` header.

    F-002-06: once Excel has authenticated and the gateway has issued a
    SessionId, MSOLAP may send follow-up requests carrying only the
    ``<Session SessionId="...">`` header and no Authorization header. The
    handler consults the persisted session store for these, so the middleware
    must let them through instead of forcing a 401 + re-login on every request.
    ``BeginSession`` is NOT a resumed session (no SessionId yet) and still
    requires credentials, so it is excluded.
    """
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, DefusedXmlException):
        return False
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Session" and (el.get("SessionId") or "").strip():
            return True
    return False


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Only enforce on XMLA endpoints
        if not request.url.path.startswith("/api/v1/xmla"):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")

        # F-002-07: Bearer-JWT path. API clients authenticate with a
        # pre-obtained JWT instead of Basic credentials. The downstream handler
        # already validates the token via ``verify_jwt_token``; the middleware
        # must hand it through rather than 401-ing every non-Basic header.
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if not token:
                return self._unauthorized()
            request.state.jwt_token = token
            request.state.username = ""
            return await call_next(request)

        # F-002-06: session-resume path. A request with no Authorization header
        # but a live ``Session`` header is a follow-up to an established
        # session; let it reach the handler so the persisted session store can
        # supply the cached JWT. The handler 401s (and clears the session) if
        # the session is unknown or expired, so this does not open an
        # unauthenticated hole.
        if not auth_header or not auth_header.startswith("Basic "):
            body = await request.body()
            if not auth_header and _has_existing_session(body):
                return await call_next(request)
            # MSOLAP/Excel sends the first request without auth and expects a
            # 401 carrying ``WWW-Authenticate: Basic`` before it will resend
            # with credentials (HTTP Basic challenge-response).
            return self._unauthorized()

        try:
            encoded = auth_header[6:]
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            logger.warning("Malformed Basic auth header: %s", exc)
            return self._unauthorized()

        # Read SOAP body to extract Catalog (tenant slug) if present
        body = await request.body()
        catalog = _extract_catalog_from_soap(body)

        # F-002-06: short-TTL credential cache. Excel fires dozens of requests
        # per pivot interaction with the same credentials; serve the cached JWT
        # for the burst instead of re-logging-in each time. The handler still
        # re-validates the JWT's ``exp``, so a stale token is rejected.
        cached = credential_cache.get(catalog, username, password)
        if cached:
            request.state.jwt_token = cached
            request.state.username = username
            return await call_next(request)

        jwt_token = None

        # 1) If Catalog present, try tenant-specific login. Only fall through on
        #    a legitimate credential failure or an "unknown tenant" response
        #    (F-002-15: the Catalog is a model slug, not a tenant, for Excel
        #    server-endpoint connections). Any other exception is an
        #    operational problem (DB outage, timeout) and must surface.
        if catalog:
            try:
                jwt_token = await login_for_token(catalog, username, password)
                logger.info("Authenticated %s against tenant %s", username, catalog)
            except Exception as exc:
                if _is_credential_failure(exc) or _is_unknown_tenant(exc):
                    logger.info(
                        "Tenant-specific login did not apply for %s on catalog %r "
                        "(%s) — will try cross-tenant discovery",
                        username, catalog, type(exc).__name__,
                    )
                else:
                    logger.warning(
                        "Tenant-specific login for %s on catalog %r raised %s: %s",
                        username, catalog, type(exc).__name__, exc,
                    )
                    return self._unauthorized()

        # 2) Cross-tenant discovery login (searches all tenants). Same narrow
        #    exception handling — don't swallow operational errors.
        if not jwt_token:
            try:
                jwt_token = await login_discover(username, password)
                logger.info("Authenticated %s via cross-tenant discovery", username)
            except Exception as exc:
                if _is_credential_failure(exc):
                    logger.info(
                        "Cross-tenant discovery login failed for %s (wrong credentials)",
                        username,
                    )
                else:
                    logger.warning(
                        "Cross-tenant discovery for %s raised %s: %s",
                        username, type(exc).__name__, exc,
                    )
                return self._unauthorized()

        credential_cache.put(catalog, username, password, jwt_token)
        request.state.jwt_token = jwt_token
        request.state.username = username
        return await call_next(request)

    def _unauthorized(self) -> Response:
        return Response(
            content="Unauthorized",
            status_code=401,
            headers={
                "WWW-Authenticate": 'Basic realm="Analysis Services"',
                "Connection": "keep-alive",
            },
        )
