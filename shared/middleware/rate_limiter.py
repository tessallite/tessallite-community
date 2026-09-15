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
* The client IP is the real caller, not the reverse proxy in front of the
  service: ``X-Forwarded-For`` is read only on a placement that declares how
  many reverse-proxy hops sit in front of it (``TRUSTED_PROXY_HOPS``; the
  model-service behind the shipped nginx declares 1, the gateway whose ports
  are published directly declares 0), optionally restricted to named proxy
  addresses (``TRUSTED_PROXY_IPS``), so the header cannot be spoofed by a
  direct caller (Bug-9164, F-R4-01).
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

import ipaddress
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

# Parsed form of one TRUSTED_PROXY_IPS string. ``None`` means the operator named
# no proxy address, so every peer of a placement that declares proxy hops is
# accepted as its proxy; an empty tuple (the literal ``none``) accepts no peer.
#
# Runtime-state declaration: durable authority is the ``TRUSTED_PROXY_IPS``
# setting (process environment); the key is that raw string, so a changed
# setting parses afresh and never reads a stale list; it is deployment-wide
# configuration, not tenant state, so there is nothing to isolate; it is bounded
# at ``_TRUSTED_PROXY_CACHE_MAX`` entries and cleared wholesale on overflow
# (a process sees one value, tests see a handful); it is per-process, rebuilt on
# restart, and identical on every replica because the input is identical.
_TRUSTED_PROXY_CACHE_MAX = 32
_trusted_proxy_cache: dict[str, tuple[ipaddress._BaseNetwork, ...] | None] = {}

_XFF_HEADER = "x-forwarded-for"


def _is_internal_request(request: Request) -> bool:
    return is_internal_request_header(request.headers.get(INTERNAL_BYPASS_HEADER))


def _parse_address(value: str) -> ipaddress._BaseAddress | None:
    """Return the IP in ``value``, or ``None`` when it is not an address.

    Accepts the bracketed IPv6 form a proxy may emit. Anything else — a
    hostname, an obfuscated identifier, ``unknown``, a port suffix, junk — is
    rejected, so an attacker-supplied header can never become a bucket key.
    """
    candidate = value.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _trusted_proxy_hops() -> int:
    """How many reverse-proxy hops this placement declares in front of it.

    ``0`` (the default) means the peer IS the client and ``X-Forwarded-For`` is
    never believed. A value that is not a non-negative integer is treated as
    ``0``: a misconfiguration must never widen trust.
    """
    raw = getattr(get_settings(), "TRUSTED_PROXY_HOPS", 0)
    try:
        hops = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "TRUSTED_PROXY_HOPS value %r is not an integer — treating it as 0; "
            "X-Forwarded-For will not be trusted",
            raw,
        )
        return 0
    return hops if hops > 0 else 0


def _trusted_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...] | None:
    """Proxy addresses the operator named in ``TRUSTED_PROXY_IPS``.

    Blank (the default) returns ``None``: no address restriction, the hop
    count alone decides. The literal ``none`` returns an empty tuple: no peer
    is a proxy. An unparsable entry is dropped with a warning rather than
    widening or emptying the list silently.
    """
    raw = (get_settings().TRUSTED_PROXY_IPS or "").strip()
    if raw in _trusted_proxy_cache:
        return _trusted_proxy_cache[raw]

    parsed: tuple[ipaddress._BaseNetwork, ...] | None
    if not raw:
        parsed = None
    elif raw.lower() == "none":
        parsed = ()
    else:
        networks: list[ipaddress._BaseNetwork] = []
        for entry in (part.strip() for part in raw.split(",") if part.strip()):
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                logger.warning(
                    "TRUSTED_PROXY_IPS entry %r is not an IP address or CIDR network "
                    "— ignoring it; X-Forwarded-For from that hop will not be trusted",
                    entry,
                )
        parsed = tuple(networks)
    if len(_trusted_proxy_cache) >= _TRUSTED_PROXY_CACHE_MAX:
        _trusted_proxy_cache.clear()
    _trusted_proxy_cache[raw] = parsed
    return parsed


def _peer_is_declared_proxy(address: ipaddress._BaseAddress | None) -> bool:
    """Is this peer one of the proxy hops the placement declares?

    A clientless request (no peer address) is never a proxy. With no named
    proxy addresses every real peer qualifies — the hop count is the trust
    decision; with a named list only those addresses qualify.
    """
    if address is None:
        return False
    networks = _trusted_proxy_networks()
    if networks is None:
        return True
    return any(address in network for network in networks)


def client_address(request: Request) -> str:
    """Return the address the rate-limit bucket should be keyed on.

    Bug-9164: ``get_remote_address`` returns the IMMEDIATE PEER. Behind the
    shipped nginx that peer is the proxy container, so every token-less request
    from every real client collapsed into ONE bucket keyed on the proxy address
    and a single client's login burst produced 429s for everybody.

    ``X-Forwarded-For`` carries the client address, but it is client-supplied:
    honoured unconditionally it lets a caller mint a fresh bucket per request
    and evade the limit entirely. Trust is therefore a HOP COUNT the placement
    declares (``TRUSTED_PROXY_HOPS``), not an address range: F-R4-01 showed
    that a range default (loopback plus RFC1918) named exactly the ranges the
    client population lives in, so office-LAN users behind nginx were skipped
    as "proxies" and collapsed onto one bucket, while a LAN peer of a directly
    published gateway port was itself "a proxy" and could choose its bucket.

    nginx sets ``X-Forwarded-For: $proxy_add_x_forwarded_for``, which APPENDS
    the address it saw to whatever the client sent — so a caller sending
    ``X-Forwarded-For: 1.2.3.4`` arrives as ``1.2.3.4, <real client>``. With
    ``N`` declared hops the ``N``-th entry from the RIGHT is the address the
    outermost trusted proxy observed; everything left of it is client-supplied
    and never read. Entries are never skipped by address range. When the
    placement declares no hops, the peer is not a declared proxy address, the
    chain is shorter than the hop count, or the chosen entry is not a valid
    address, the peer address is used — never an attacker-chosen string.
    """
    # The bucket key keeps the previous value for every untrusted peer, including
    # slowapi's synthetic "127.0.0.1" for a request that carries no client at
    # all. The TRUST decision is made on the real peer only: a clientless request
    # must not let its header pick the key.
    peer = get_remote_address(request)
    hops = _trusted_proxy_hops()
    if hops == 0:
        return peer
    peer_address = _parse_address(
        request.client.host if request.client and request.client.host else ""
    )
    if not _peer_is_declared_proxy(peer_address):
        return peer

    forwarded = request.headers.get(_XFF_HEADER)
    if not forwarded:
        return peer
    entries = [part for part in forwarded.split(",") if part.strip()]
    if len(entries) < hops:
        # Fewer hops reported than declared: a proxy in the chain did not add
        # itself, so the client cannot be identified from here.
        return peer
    candidate = _parse_address(entries[-hops])
    if candidate is None:
        # The trusted hop reported something that is not an address.
        return peer
    return str(candidate)


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
    Returns the client address (:func:`client_address`, which resolves the real
    client behind a trusted proxy) as fallback when no usable token is present.
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

    return client_address(request)


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
                        # Bug-9164: the real client behind a trusted proxy, so a
                        # login burst from one user cannot throttle everyone.
                        key = client_address(request)
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
