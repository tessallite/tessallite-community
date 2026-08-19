"""SSRF guard for outbound webhook delivery (F-022-05).

Single source of truth for "is this URL safe to POST a signed payload to".
Both the platform-wide dispatcher (``shared/webhooks/dispatcher.py``) and the
scheduler's inline webhook path use this so the guard cannot drift.

Two layers, defence in depth:

1. ``validate_webhook_url`` — a fast pre-flight that rejects non-http(s)
   schemes, empty hosts, and known-internal hostnames before any network
   call. Cheap to run at create/update time so a bad URL is refused at the
   admin surface, not silently at send time.

2. ``ssrf_safe_transport`` — an httpx transport whose network backend
   resolves DNS at connect time, rejects any address that is not globally
   routable (loopback / private / link-local / metadata 169.254.169.254 /
   multicast), and connects to the validated IP directly. This closes the
   DNS-rebinding TOCTOU gap that a name-only check cannot: a hostname that
   passed the pre-flight but resolves to a private address at connect time
   is still blocked. The original hostname is preserved on the httpcore
   Origin so TLS SNI and certificate verification still use the name.

Fail-closed: a host that does not resolve, or resolves only to non-global
addresses, raises ``ConnectError`` and the delivery is not attempted.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import typing
from urllib.parse import urlparse

import httpcore
import httpx

logger = logging.getLogger(__name__)

# Hostnames that must never be the target of an outbound webhook, regardless
# of what they resolve to. The IP-level check below is the real guard; this is
# a cheap early rejection at the admin surface.
# Bug-6764: defence-in-depth: add AWS, Azure, Oracle, and DigitalOcean
# metadata service hostnames. The real guard is _SSRFSafeBackend which
# blocks non-global IPs at connect time (169.254.169.254 etc.), but these
# hostname blocks catch the obvious cases at the admin surface before any
# DNS resolution, giving the operator a clear validation error.
_BLOCKED_HOSTS = frozenset({
    "localhost",
    # GCP
    "metadata.google.internal",
    "metadata.google",
    "metadata",
    # AWS
    "instance-data",
    "instance-data.ec2.internal",
    # Azure
    "metadata.azure.internal",
    # Oracle Cloud
    "metadata.oraclecloud.com",
    # DigitalOcean
    "metadata.digitalocean.com",
})


def validate_webhook_url(url: str, *, allow_http: bool | None = None) -> str:
    """Pre-flight check. Returns ``url`` if acceptable, else raises ``ValueError``.

    Rejects non-http(s) schemes, empty hosts, blocked internal hostnames, and
    any host given as a literal non-global IP address. This runs at webhook
    create/update time so a bad URL is refused before it is ever stored.

    Bug-7339: ``allow_http`` now defaults to ``None``, which means "read the
    ``WEBHOOK_ALLOW_HTTP`` bootstrap setting". When the setting is False
    (the default), only HTTPS URLs are accepted — the HMAC signature and
    payload are never exposed on plaintext transport. Callers can still
    force ``allow_http=True/False`` for tests or internal use.
    """
    if allow_http is None:
        from shared.config.settings import get_settings
        allow_http = getattr(get_settings(), "WEBHOOK_ALLOW_HTTP", False)
    parsed = urlparse(url)
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in allowed_schemes:
        raise ValueError(
            f"Webhook URL must use {' or '.join(allowed_schemes)}, "
            f"got {parsed.scheme!r}"
        )

    # Bug-8349 R2 HIGH — ``parsed.port`` raises ``ValueError`` for a
    # malformed port (e.g. ``https://host:notaport/hook``). This function
    # never used to touch ``.port`` at all, so a URL like that sailed
    # through validation at both config-write time and dispatch time, then
    # blew up at request time as ``httpx.InvalidURL`` — an exception that is
    # NOT a subclass of ``httpx.HTTPError`` and so escaped the dispatcher's
    # transport-error handling uncaught, losing the event with zero DLQ
    # record. Reject it HERE, at config-write time, with a clear error, so
    # it can never reach dispatch as a live URL in the first place.
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"Webhook URL has an invalid port: {exc}") from exc

    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("Webhook URL has no hostname")

    normalised = hostname.lower().rstrip(".")
    if normalised in _BLOCKED_HOSTS:
        raise ValueError(f"Webhook URL host {hostname!r} is not allowed")

    # If the host is a literal IP, reject non-global ones up front (the
    # connect-time backend would catch it anyway, but failing at validation
    # gives the admin an immediate, clear error).
    try:
        addr = ipaddress.ip_address(normalised)
    except ValueError:
        addr = None
    if addr is not None and (not addr.is_global or addr.is_multicast):
        raise ValueError(
            f"Webhook URL host {hostname!r} resolves to non-global address {addr}"
        )

    return url


class _SSRFSafeBackend(httpcore.AsyncNetworkBackend):
    """Network backend that validates and pins resolved IPs at connect time.

    Resolves DNS, validates every address is globally routable, then connects
    to the first validated IP directly — eliminating the DNS-rebinding TOCTOU
    gap. The original hostname is preserved in the httpcore Origin for TLS SNI
    and certificate verification (httpcore passes ``server_hostname`` from the
    Origin to ``start_tls``, not from ``connect_tcp``).
    """

    def __init__(self) -> None:
        from httpcore._backends.anyio import AnyIOBackend
        self._inner = AnyIOBackend()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None,
    ):
        try:
            # Bug-5744: use async DNS resolution to avoid blocking the event
            # loop. asyncio.get_running_loop().getaddrinfo delegates to the
            # executor and returns the same tuple format as socket.getaddrinfo.
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                str(host), port, proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror:
            raise httpcore.ConnectError(f"SSRF: host {host!r} does not resolve")

        validated_ip: str | None = None
        for _fam, _type, _proto, _canon, sockaddr in infos:
            addr = ipaddress.ip_address(sockaddr[0])
            if not addr.is_global or addr.is_multicast:
                raise httpcore.ConnectError(
                    f"SSRF blocked: {host!r} resolves to non-global address {addr}"
                )
            if validated_ip is None:
                validated_ip = sockaddr[0]

        if validated_ip is None:
            raise httpcore.ConnectError(f"SSRF: host {host!r} has no usable address")

        return await self._inner.connect_tcp(
            validated_ip, port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix socket connections not allowed for webhooks")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _AsyncStreamWrapper(httpx.AsyncByteStream):
    """Thin wrapper adapting an httpcore async-iterable stream to httpx's
    ``AsyncByteStream`` protocol."""

    def __init__(self, httpcore_stream: typing.AsyncIterable[bytes]) -> None:
        self._stream = httpcore_stream

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()


class _SSRFSafeTransport(httpx.AsyncBaseTransport):
    """httpx async transport that enforces SSRF protection (Bug-6765).

    Replaces the previous ``_pool`` monkeypatch approach which relied on a
    private httpx attribute and risked failing OPEN on an httpx upgrade.
    This subclass uses httpx's public ``AsyncBaseTransport`` contract
    (``handle_async_request``), which is the supported extension point and
    cannot silently degrade. The SSRF-safe network backend is wired via
    httpcore's public ``AsyncConnectionPool(network_backend=...)`` API.

    Fail-closed: if httpx changes the ``AsyncBaseTransport`` contract (the
    method signature or semantics), this code either raises ``TypeError`` at
    call time or fails to construct — never silently falls back to an
    unguarded transport.
    """

    def __init__(self) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_SSRFSafeBackend()
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        resp = await self._pool.handle_async_request(req)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_AsyncStreamWrapper(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        """Close the owned connection pool, releasing sockets and resources.

        Called by ``httpx.AsyncClient.__aexit__`` when the client is used as
        an async context manager (which is the standard dispatcher pattern).
        Without this, the pool's connections leak until GC.
        """
        await self._pool.aclose()


def ssrf_safe_transport() -> httpx.AsyncBaseTransport:
    """Build an httpx transport that blocks connections to non-global addresses.

    Bug-6765: now uses ``AsyncBaseTransport`` (httpx's public extension
    point) instead of monkeypatching ``AsyncHTTPTransport._pool``. The
    ``_SSRFSafeTransport`` owns its own httpcore pool with the SSRF-safe
    network backend wired in at construction. No private attributes are
    accessed, so an httpx upgrade cannot silently disable the guard.
    """
    return _SSRFSafeTransport()
