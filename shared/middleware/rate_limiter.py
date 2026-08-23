"""
Per-tenant rate limiting for Tessallite services.

Uses slowapi's storage/strategy engine (a FastAPI-compatible wrapper around
the ``limits`` library) with a Tessallite-owned enforcement middleware.

Enforcement model (F-021-01):

* Every user-facing HTTP request is counted against a per-tenant bucket
  (``rate_limit.per_minute``); the tenant key is extracted from the JWT
  payload without verification — real auth runs separately in
  ``get_current_user``. Requests without a JWT fall back to client IP.
* Login routes get a stricter, dedicated per-client bucket
  (``rate_limit.login_per_minute``) to slow password brute-force.
* Exceeding either limit returns HTTP 429 with a JSON body and a
  ``Retry-After`` header (``rate_limit.retry_after_seconds``).
* All limit values are read from the system settings snapshot on every
  request — no hardcoded numbers; changes take effect without a restart.
* Internal service-to-service traffic (gateway -> model-service,
  scheduler sweeps, agent pipeline metadata reads) is exempt: callers
  attach the header from :func:`internal_request_headers`, whose value
  is an HMAC derived from the shared ``JWT_SECRET_KEY``. Rate limits
  scope to user-facing ingress only — throttling the internal pipeline
  would break query routing for BI clients.
* ``/health`` and ``/metrics`` probes are exempt (Docker healthchecks,
  Prometheus scrapes).

Usage in a FastAPI app:

    from shared.middleware.rate_limiter import build_limiter, attach_limiter

    limiter = build_limiter()
    attach_limiter(app, limiter)

Internal callers (httpx) add the bypass header:

    from shared.middleware.rate_limiter import internal_request_headers

    headers = {**auth_headers, **internal_request_headers()}
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from limits import RateLimitItem, parse as parse_limit  # type: ignore
from slowapi import Limiter  # type: ignore
from slowapi.util import get_remote_address  # type: ignore
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.middleware.internal_bypass import (  # noqa: F401 — re-exported
    INTERNAL_BYPASS_HEADER,
    internal_request_headers,
    is_internal_request_header,
)

logger = logging.getLogger(__name__)

# Paths exempt from rate limiting: liveness probes, metrics scrapes, and
# the XMLA wire-protocol endpoint.  BI clients (Excel, Power BI) fire
# dozens of rapid-fire SOAP requests for a single PivotTable operation —
# session handshakes, metadata discovery, data queries — each doubled by
# HTTP Basic Auth 401 challenges.  The 60/min API limit blocks them mid-
# pivot.  XMLA is already guarded by per-request authentication.
EXEMPT_PATHS = frozenset({"/health", "/metrics", "/api/v1/xmla/"})

# Login routes get the stricter ``rate_limit.login_per_minute`` bucket.
LOGIN_PATH_SUFFIXES = (
    "/auth/login",
    "/auth/login/discover",
    "/auth/system/login",
)

_parsed_limit_cache: dict[str, RateLimitItem] = {}


def _is_internal_request(request: Request) -> bool:
    return is_internal_request_header(request.headers.get(INTERNAL_BYPASS_HEADER))


def _extract_tenant_key(request: Request) -> str:
    """
    Extract tenant_id from a *signature-verified* JWT for per-tenant
    rate limiting.

    The tenant claim is only trusted after the token signature checks out
    (F-H27R1-03). An unsigned/forged token cannot mint a fresh per-tenant
    bucket: unverifiable tokens fall back to the client IP, so a single
    client cannot rotate buckets by forging distinct ``tenant_id`` claims,
    and the in-memory keyspace cannot be inflated with attacker-chosen keys.

    Real authentication still happens separately in ``get_current_user``;
    this verification exists solely to make the limiter key trustworthy.
    Returns client IP as fallback when no usable token is present.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        try:
            # Verify the signature before trusting the tenant claim. The
            # platform secret is available to the middleware, so keying on
            # a verified claim is cheap and closes the fake-tenant rotation.
            from shared.auth.jwt import decode_access_token

            payload = decode_access_token(token)
            tenant_id = payload.get("tenant_id")
            if tenant_id:
                return str(tenant_id)
        except Exception:
            # Bad signature / expired / malformed — do NOT trust the claim.
            pass  # fall through to IP-based key

    return get_remote_address(request) or "unknown"


def build_limiter() -> Limiter:
    """Return a configured Limiter instance (storage + strategy engine).

    Storage backend (F-H27R1-02): the limiter uses an in-memory store by
    default (``memory://``), which is **per replica** — each uvicorn worker
    or service replica holds an independent set of buckets. Under multiple
    replicas (e.g. GCP Cloud Run scale-out) the effective ceiling is N× the
    configured ``rate_limit.per_minute`` / ``rate_limit.login_per_minute``,
    and buckets reset on restart/scale events.

    To enforce a single shared ceiling across all replicas, an operator sets
    ``RATE_LIMIT_STORAGE_URI`` in the environment to a shared store that the
    underlying ``limits`` library already supports (e.g.
    ``redis://host:6379/0`` or ``memcached://host:11211``). No new dependency
    is introduced here — only a connection URI is passed through. When unset,
    the default ``memory://`` preserves the existing single-process semantics.
    """
    storage_uri = (get_settings().RATE_LIMIT_STORAGE_URI or "").strip()
    if storage_uri:
        return Limiter(key_func=_extract_tenant_key, storage_uri=storage_uri)
    return Limiter(key_func=_extract_tenant_key)


def _parse_limit_cached(limit_str: str) -> RateLimitItem:
    item = _parsed_limit_cache.get(limit_str)
    if item is None:
        item = parse_limit(limit_str)
        _parsed_limit_cache[limit_str] = item
    return item


def _rate_limit_response(limit_str: str) -> Response:
    """Structured JSON 429 with a Retry-After header."""
    retry_after = str(int(system_snapshot_get("rate_limit.retry_after_seconds")))
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded",
            "limit": limit_str,
            "retry_after": retry_after,
        },
        headers={"Retry-After": retry_after},
    )


class TenantRateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce config-driven per-tenant rate limits on user-facing ingress.

    Reads ``rate_limit.*`` from the system snapshot per request, so the
    settings UI controls enforcement without service restarts. Internal
    service calls (verified bypass header) and health/metrics probes are
    never throttled.

    Multi-replica behaviour (Bug-7410): buckets are per-process by default
    (``memory://``). Under N replicas the effective ceiling is N x the
    configured limit and buckets reset on restart/scale events. This is
    bounded and documented -- Tessallite scales UP (single replica, larger
    CPU/RAM) not OUT; a scale-out deployment that needs a shared ceiling
    sets ``RATE_LIMIT_STORAGE_URI`` (e.g. ``redis://<host>:6379``) to route
    all replicas through a single store, with no code or dependency change.

    ``login_only=True`` (model-service placement, architecture_rate-limit-
    placement.md) throttles ONLY the login paths — brute-force protection — and
    passes the operational/metadata API through untouched. The default (full)
    mode additionally throttles the per-tenant ``api`` bucket (the gateway).
    """

    def __init__(self, app, *, login_only: bool = False) -> None:
        super().__init__(app)
        self.login_only = login_only

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # RL-CG-R1-01: this try must only DECIDE whether to reject; it must NEVER
        # dispatch the downstream app from inside it. If call_next() ran here and
        # raised, the broad `except` below would swallow it and the final
        # call_next() would REPLAY the request — double-executing a non-idempotent
        # POST/PATCH/DELETE. So call_next() is invoked exactly once, OUTSIDE this
        # try, for every pass-through path (disabled, exempt, internal, login-only,
        # and under-limit). The limiter still fails OPEN on its own internal error.
        try:
            limiter: Limiter | None = getattr(request.app.state, "limiter", None)
            if limiter is not None and bool(system_snapshot_get("rate_limit.enabled")):
                path = request.url.path
                if path not in EXEMPT_PATHS and not _is_internal_request(request):
                    per_minute: int | None
                    if path.endswith(LOGIN_PATH_SUFFIXES):
                        per_minute = int(system_snapshot_get("rate_limit.login_per_minute"))
                        scope = "login"
                        # Login requests carry no JWT — key by client address.
                        key = get_remote_address(request) or "unknown"
                    elif self.login_only:
                        # login-only placement (model-service): the operational/
                        # metadata API is deliberately NOT request-rate throttled
                        # here — that load is bounded by fixing the N+1 fan-out, not
                        # a per-tenant bucket (architecture_rate-limit-placement.md).
                        # per_minute=None => skip the bucket; the request passes
                        # through via the single call_next() below.
                        per_minute = None
                    else:
                        per_minute = int(system_snapshot_get("rate_limit.per_minute"))
                        scope = "api"
                        key = _extract_tenant_key(request)

                    if per_minute is not None:
                        limit_str = f"{per_minute}/minute"
                        item = _parse_limit_cached(limit_str)
                        if not limiter.limiter.hit(item, key, scope):
                            logger.warning(
                                "rate limit %s exceeded: key=%s scope=%s path=%s",
                                limit_str, key, scope, path,
                            )
                            return _rate_limit_response(limit_str)
        except Exception:  # defensive: never let the limiter break requests
            logger.exception("Rate limiter check failed — request allowed through")

        return await call_next(request)


def attach_limiter(app: FastAPI, limiter: Limiter, *, login_only: bool = False) -> None:
    """
    Attach the Limiter and enforcement middleware to the app.

    Call this after creating the FastAPI app, before adding routers.
    The middleware is always attached; the per-request enabled check
    (``rate_limit.enabled``) governs enforcement, so toggling the switch
    in system settings takes effect without a restart.

    ``login_only=True`` restricts enforcement to the login paths (brute-force
    protection) and leaves the operational/metadata API un-throttled — the
    model-service placement per architecture_rate-limit-placement.md. The
    default (full) mode also throttles the per-tenant ``api`` bucket and is
    what the gateway uses.
    """
    app.state.limiter = limiter
    app.add_middleware(TenantRateLimitMiddleware, login_only=login_only)
    logger.info(
        "Rate limiting middleware attached (enabled=%s, mode=%s, %s requests/minute per tenant, "
        "%s logins/minute per client)",
        bool(system_snapshot_get("rate_limit.enabled")),
        "login-only" if login_only else "full",
        int(system_snapshot_get("rate_limit.per_minute")),
        int(system_snapshot_get("rate_limit.login_per_minute")),
    )
