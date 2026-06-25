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

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpcore
import httpx

logger = logging.getLogger(__name__)

# Hostnames that must never be the target of an outbound webhook, regardless
# of what they resolve to. The IP-level check below is the real guard; this is
# a cheap early rejection at the admin surface.
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.google",
    "metadata",
})


def validate_webhook_url(url: str, *, allow_http: bool = True) -> str:
    """Pre-flight check. Returns ``url`` if acceptable, else raises ``ValueError``.

    Rejects non-http(s) schemes, empty hosts, blocked internal hostnames, and
    any host given as a literal non-global IP address. This runs at webhook
    create/update time so a bad URL is refused before it is ever stored.
    """
    parsed = urlparse(url)
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in allowed_schemes:
        raise ValueError(
            f"Webhook URL must use {' or '.join(allowed_schemes)}, "
            f"got {parsed.scheme!r}"
        )

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
            infos = socket.getaddrinfo(str(host), port, proto=socket.IPPROTO_TCP)
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


def ssrf_safe_transport() -> httpx.AsyncHTTPTransport:
    """Build an httpx transport that blocks connections to non-global addresses."""
    pool = httpcore.AsyncConnectionPool(network_backend=_SSRFSafeBackend())
    transport = httpx.AsyncHTTPTransport()
    transport._pool = pool  # type: ignore[attr-defined]
    return transport
