import base64
import binascii
import logging
import re
from defusedxml import DefusedXmlException, ElementTree as ET

import httpx
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from shared.config.settings import get_settings
from src.dax import credential_cache
from src.jdbc.throttle import get_governor
from src.router_client import login_discover, login_for_token

logger = logging.getLogger(__name__)

_settings = get_settings()

# Bug-6943: redact SessionId values before DEBUG logging so session-resume
# credentials are not persisted to log sinks.
_SESSION_ID_RE = re.compile(
    rb'SessionId\s*=\s*(?:"[^"]*"|\'[^\']*\')',
    re.IGNORECASE,
)


def _is_xmla_endpoint(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return (
        normalized.startswith("/api/v1/xmla")
        or normalized.startswith("/xmla")
        or normalized in {"/msmdpump.dll", "/api/v1/msmdpump.dll"}
    )


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


def _is_credential_rejection(exc: BaseException) -> bool:
    """Return True ONLY for a genuine wrong-password rejection (HTTP 401).

    Bug-8143: the failed-login THROTTLE must count real credential guesses,
    not operational faults. ``login_for_token`` / ``login_discover`` raise
    ``httpx.HTTPStatusError`` 401 for a rejected password, but a plain
    ``ValueError`` for an operational fault (a 200 response missing the
    ``access_token`` cookie — ``router_client._extract_token_from_response``).
    ``_is_credential_failure`` intentionally treats that ValueError as an auth
    failure for cache invalidation, but feeding it into the throttle would let a
    misconfigured upstream (repeated missing-cookie 200s) lock out every XMLA
    client. The throttle therefore keys off this stricter 401-only test.
    """
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response is not None
        and exc.response.status_code == 401
    )


def _throttle_key(client_ip: str, username: str) -> str:
    """Per-IP-and-identity key for the shared failed-login governor.

    Bug-8143: JDBC keys the governor on the raw TCP peer IP, but the XMLA
    surface sits behind the nginx reverse proxy, so ``get_remote_address``
    collapses every real client to the proxy's IP. Keying the throttle on IP
    alone there would (a) let one attacker lock out ALL Excel/Power BI logins on
    that route and (b) let any one successful login clear the shared window and
    reset an attacker's counter. Qualifying the key with the (case-normalised)
    username makes the control per-identity: repeated guesses against one
    account are throttled without affecting other users, and a success clears
    only that account's window. The ``xmla:`` prefix namespaces these buckets
    away from JDBC's raw-IP buckets in the shared governor. The identity is
    length-bounded so a client-supplied username cannot inflate per-key memory
    (bucket COUNT is bounded separately by the governor's tracked-key cap); a
    real email is far shorter than the limit, so legitimate keys are unaffected.
    """
    ident = username.strip().lower()[:128]
    return f"xmla:{client_ip}:{ident}"


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


def _extract_request_type(body: bytes) -> str:
    """Return the XMLA RequestType (or method local-name) for diagnostics."""
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, DefusedXmlException):
        return ""
    method = ""
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "RequestType":
            return (el.text or "").strip()
        if tag in ("Discover", "Execute", "BeginSession", "EndSession") and not method:
            method = tag
    return method


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
        if not _is_xmla_endpoint(request.url.path):
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
            has_session = _has_existing_session(body)
            if not auth_header and has_session:
                return await call_next(request)
            # MSOLAP/Excel sends the first request without auth and expects a
            # 401 carrying ``WWW-Authenticate: Basic`` before it will resend
            # with credentials (HTTP Basic challenge-response). Log the scheme
            # only (never header contents — NTLM/Negotiate blobs carry identity
            # material) so rejected non-Basic legs are diagnosable.
            scheme = auth_header.split(" ", 1)[0] if auth_header else "<none>"
            logger.debug(
                "xmla auth challenge: scheme=%s path=%s body_bytes=%d session_header=%s request=%r",
                scheme, request.url.path, len(body), has_session,
                _extract_request_type(body),
            )
            # Bug-6943: redact SessionId values from the body before logging.
            # A SessionId is a session-resume credential (the session store
            # exchanges it for a cached JWT), so log access could enable
            # session hijacking.  Also cap the dump to the opt-in raw-payload
            # gate when the body contains query content.
            if body and len(body) <= 4096:
                redacted = _SESSION_ID_RE.sub(b'SessionId="[REDACTED]"', body)
                logger.debug("xmla challenge body: %s", redacted.decode("utf-8", "replace"))
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

        # Bug-8143: the XMLA password-login path must receive the SAME per-IP
        # failed-login throttle the SPA and JDBC logins get. The gateway login
        # relay deliberately adds the signed internal bypass header
        # (router_client), so model-service's dedicated login bucket is skipped
        # for this internal call; JDBC compensates with JdbcConnectionGovernor
        # but XMLA never consulted it. Consume that SAME process-wide governor
        # here (JDBC listener and this FastAPI app share one process, so they
        # share the singleton) so repeated credential failures over XMLA are
        # rate-limited identically to JDBC — same thresholds and window. Only a
        # genuine cache-miss credential exchange is gated; a cached burst above
        # is never throttled, and a successful login clears the window so a
        # legitimate user is never locked out by their own valid volume. A
        # threshold of 0 disables the control (governor opt-out), so this is
        # revert-safe by configuration.
        client_ip = get_remote_address(request) or "unknown"
        throttle_key = _throttle_key(client_ip, username)
        governor = get_governor()
        if governor.is_throttled(throttle_key):
            logger.warning(
                "xmla login throttled: ip=%s user=%s (too many recent auth failures)",
                client_ip, username,
            )
            return self._too_many_requests()

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
                    # Bug-6309: the authority rejected these credentials (wrong /
                    # changed password, or a disabled account). Purge any JWT
                    # still cached for this user under earlier auth material so a
                    # subsequent request cannot be served a stale token from a
                    # prior cache hit.
                    credential_cache.invalidate(catalog, username)
                    # Bug-8143: record this as a failed-login for the shared
                    # throttle, but ONLY on a genuine 401 rejection — not on an
                    # operational ValueError (missing-cookie) that
                    # _is_credential_failure also captures for cache purposes.
                    # This is the single terminal credential failure for an XMLA
                    # attempt (a tenant-specific 401 falls through to discovery
                    # rather than terminating), so exactly one failure is counted
                    # per rejected attempt — aligned with the JDBC path.
                    if _is_credential_rejection(exc):
                        governor.record_auth_failure(throttle_key)
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
        # Bug-8143: a successful credential exchange clears THIS identity's
        # failure window so a legitimate user who mistyped once is not held
        # under the throttle after they authenticate correctly (matches JDBC
        # success). The identity-qualified key means one user's success cannot
        # clear an attacker's window for a different account behind the proxy.
        governor.record_auth_success(throttle_key)
        request.state.jwt_token = jwt_token
        request.state.username = username
        return await call_next(request)

    def _too_many_requests(self) -> Response:
        # Bug-8143: hard back-off response when the per-IP failed-login throttle
        # has fired. A 429 (rather than another 401 challenge) tells the client
        # to stop retrying and does not re-arm MSOLAP's credential prompt loop.
        retry_after = max(1, int(_settings.GATEWAY_JDBC_AUTH_FAILURE_WINDOW_SECONDS))
        return Response(
            content="Too Many Requests",
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
                "Connection": "keep-alive",
            },
        )

    def _unauthorized(self) -> Response:
        return Response(
            content="Unauthorized",
            status_code=401,
            headers={
                "WWW-Authenticate": 'Basic realm="Analysis Services"',
                "Connection": "keep-alive",
            },
        )
