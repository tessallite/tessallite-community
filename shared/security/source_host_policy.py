"""Egress policy for tenant-supplied source-database hosts (Bug-6216).

"Test connection" and "Discover tables" take a host, port and database typed
into a form and open a real TCP connection from inside the platform's own
network. The response distinguishes "connection refused" from "timed out"
from "authentication failed", so it is a working port scanner: point it at
``169.254.169.254``, at ``127.0.0.1``, or at another tenant's database host
and read the answer off the error message.

The platform cannot simply refuse tenant-named hosts — connecting to the
customer's database IS the product. What it can do is refuse the hosts that
are never a customer database and are always somebody's infrastructure:

* loopback — the service's own process and anything else on that container;
* link-local, including the ``169.254.169.254`` cloud metadata endpoint that
  hands out instance credentials;
* the metadata service hostnames the major clouds publish;
* unspecified / multicast / reserved space.

Private RFC1918 ranges are ALLOWED by default and gated behind
``SOURCE_HOST_BLOCK_PRIVATE``. This is deliberate: a self-hosted or
docker-compose deployment reaches its source database over exactly such an
address, so blocking them by default would break the shipped local install.
A managed multi-tenant deployment, where a tenant's source database is always
somewhere on the public internet, should set it.

``SOURCE_HOST_ALLOWLIST`` names hosts that are exempt, for the deployment
whose legitimate source really is on loopback.

Where each form of the check runs:

* ``check_source_host_literal`` -- synchronous, no DNS. Runs on the query hot
  path (``source_executor``) at the moment a host becomes a socket.
* ``assert_source_host_allowed`` -- resolves and judges every returned address.
  Runs where a host ENTERS the system (connection create/update) and on the
  introspection endpoints.

Residual limitation, stated rather than hidden: the drivers (asyncpg, pyhive,
aioodbc) resolve the hostname again when they connect, so a host that resolved
to an allowed address at write time and to a blocked one at query time is a
DNS-rebinding race this check does not close. Pinning the validated IP into the
driver would break TLS hostname verification against the customer's database
certificate, which is a worse trade. Every resolved address is validated (not
just the first), so rebinding must win a race rather than simply return two
records.

Note this is genuinely a RACE, and only since R5. Before the write path
resolved, a name that statically resolves to loopback -- ``127.0.0.1.nip.io``,
``localtest.me`` -- passed every check that actually ran, with no race at all.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class SourceHostBlockedError(ValueError):
    """Raised when a tenant-supplied source host violates the egress policy."""


# Names that are never a customer database, whatever they resolve to. The
# address check below is the real guard; these give a clear, immediate error
# at the form instead of a DNS round trip.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    # GCP
    "metadata", "metadata.google", "metadata.google.internal",
    # AWS
    "instance-data", "instance-data.ec2.internal",
    # Azure
    "metadata.azure.internal",
    # Oracle Cloud
    "metadata.oraclecloud.com",
    # DigitalOcean
    "metadata.digitalocean.com",
})

# Addresses that are NEVER an acceptable egress target, whatever the private-
# range policy says. Cloud instance-metadata endpoints hand out credentials, so
# reaching one must not be gated behind a tunable: ``fd00:ec2::254`` (the AWS
# IPv6 metadata address) is unique-local, which Python classifies only as
# ``is_private``, and private space is permitted by default.
_ALWAYS_BLOCKED_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # GCP / AWS / Azure IMDS
    ipaddress.ip_address("169.254.170.2"),     # ECS task metadata
    ipaddress.ip_address("fd00:ec2::254"),     # AWS IMDS over IPv6
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
})

# Connectors whose endpoint is a tenant-supplied network address.
HOST_BEARING_CONNECTORS = frozenset({
    "postgresql", "redshift", "hadoop_spark", "sqlserver",
})

# Connectors deliberately exempt, and why. BigQuery reaches fixed Google API
# endpoints; Snowflake takes an account identifier the driver suffixes onto
# ``snowflakecomputing.com``. Neither lets a tenant name an arbitrary host in
# the connection form. Declared next to HOST_BEARING_CONNECTORS so a connector
# added to the dispatch tables must be classified into one set or the other --
# ``test_every_dispatched_connector_is_classified`` fails closed otherwise.
# NOTE: BigQuery's exemption covers the CONNECTION FORM only. The uploaded
# service-account JSON carries its own ``token_uri``/``auth_uri``, which are
# tenant-controlled outbound URLs; those are policed by
# ``assert_service_account_endpoints_allowed`` below.
EXPLICITLY_HOSTLESS_CONNECTORS = frozenset({"bigquery", "snowflake"})


def _normalise(host: str) -> str:
    return str(host).strip().strip("[]").rstrip(".").lower()


def _policy() -> tuple[bool, frozenset[str]]:
    from shared.config.settings import get_settings
    settings = get_settings()
    block_private = bool(getattr(settings, "SOURCE_HOST_BLOCK_PRIVATE", False))
    raw = getattr(settings, "SOURCE_HOST_ALLOWLIST", "") or ""
    allowlist = frozenset(
        _normalise(entry) for entry in raw.split(",") if entry.strip()
    )
    return block_private, allowlist


def _classify(addr: ipaddress._BaseAddress, block_private: bool) -> str | None:
    """Return a rejection reason for *addr*, or None when it is acceptable."""
    # IPv4-mapped / 6to4-style wrappers must be judged on the address they
    # actually reach, or ``::ffff:169.254.169.254`` walks straight through.
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped or getattr(addr, "sixtofour", None)
        if mapped is not None:
            return _classify(mapped, block_private)

    if addr in _ALWAYS_BLOCKED_ADDRESSES:
        return "a cloud instance-metadata endpoint"
    if addr.is_loopback:
        return "a loopback address"
    if addr.is_link_local:
        return "a link-local address (cloud instance metadata lives here)"
    if addr.is_unspecified:
        return "an unspecified address"
    if addr.is_multicast:
        return "a multicast address"
    if addr.is_reserved:
        return "a reserved address"
    if block_private and addr.is_private:
        return "a private address, which this deployment does not allow"
    return None


def _resolve_timeout_seconds() -> float:
    """Seconds to wait for a source-host DNS lookup before giving up.

    Tenant-supplied names are resolved on write and introspection paths, so the
    lookup is attacker-influenced and must not be unbounded.
    """
    from shared.config.settings import get_settings

    try:
        return float(getattr(get_settings(), "SOURCE_HOST_RESOLVE_TIMEOUT_SEC", 5.0))
    except (TypeError, ValueError):
        return 5.0


def _parse_address(normalised: str) -> ipaddress._BaseAddress | None:
    """Parse *normalised* as an IP literal in any form a resolver would accept.

    ``ipaddress.ip_address`` takes strict dotted-quad only, so ``2130706433``,
    ``0x7f000001``, ``127.1`` and ``0177.0.0.1`` all fell through as "must be a
    hostname" -- and every one of them is ``127.0.0.1`` to ``getaddrinfo``.
    That made the write-path literal check, which promises a loopback host
    "cannot be persisted at all", trivially bypassable (R2 finding 7).

    Returns ``None`` only when the value is genuinely a name.
    """
    try:
        return ipaddress.ip_address(normalised)
    except ValueError:
        pass
    # inet_aton accepts the historical forms (decimal, octal, hex, 2- and
    # 3-part). It rejects real hostnames, so a None here means "a name".
    try:
        packed = socket.inet_aton(normalised)
    except (OSError, UnicodeEncodeError, ValueError):
        # ValueError covers inet_aton's "embedded null character". It must not
        # escape: SourceHostBlockedError subclasses ValueError, so the callers'
        # ``except SourceHostBlockedError`` would miss a bare one and a
        # credentials field would surface as an HTTP 500 with a traceback.
        return None
    return ipaddress.ip_address(packed)


def check_source_host_literal(host: str, *, connector: str) -> None:
    """Synchronous pre-flight: reject blocked names and literal IPs.

    Cheap enough to run on a write path (connection create/update) where no
    DNS round trip is wanted. Does not resolve names — use
    :func:`assert_source_host_allowed` before actually connecting.
    """
    if connector not in HOST_BEARING_CONNECTORS:
        return
    normalised = _normalise(host)
    if not normalised:
        raise SourceHostBlockedError("A source host is required.")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in normalised):
        raise SourceHostBlockedError(
            f"'{host}' contains whitespace or control characters and cannot be "
            "used as a source database host."
        )

    block_private, allowlist = _policy()
    if normalised in allowlist:
        return

    if normalised in _BLOCKED_HOSTNAMES:
        raise SourceHostBlockedError(
            f"'{host}' is a reserved internal hostname and cannot be used as a "
            "source database host."
        )

    addr = _parse_address(normalised)
    if addr is None:
        return
    reason = _classify(addr, block_private)
    if reason is not None:
        raise SourceHostBlockedError(
            f"'{host}' is {reason} and cannot be used as a source database host."
        )


# The OAuth endpoints a Google service-account credential is allowed to name.
# ``google.oauth2.service_account.Credentials`` takes its token endpoint from
# the ``token_uri`` field of the uploaded JSON and POSTs the signed assertion
# there on the first refresh, so a tenant who uploads a key file with
# ``"token_uri": "http://169.254.169.254/..."`` gets an outbound request from
# inside the platform network with the transport error echoed back through the
# connection-test detail. The connection FORM has no host field for BigQuery,
# which is why the connector is otherwise exempt -- the credential blob is the
# hole (Bug-6216 R2 finding 4).
_SERVICE_ACCOUNT_URL_FIELDS = (
    "token_uri", "auth_uri", "auth_provider_x509_cert_url",
    "client_x509_cert_url",
)
# Not a URL but a bare domain. google-auth >= 2.23 reads it from the key file
# and google-api-core derives service endpoints from it, so it is the same
# tenant-controlled-egress shape as token_uri and is policed the same way.
# (This has NOT been executed against a real google-auth on this workstation —
# the package is not installed here — so it is defence in depth pending a live
# check, not a verified exploit path.)
_SERVICE_ACCOUNT_DOMAIN_FIELDS = ("universe_domain",)
_ALLOWED_SERVICE_ACCOUNT_HOSTS = frozenset({
    "oauth2.googleapis.com",
    "accounts.google.com",
    "www.googleapis.com",
    "googleapis.com",
    "sts.googleapis.com",
})


def assert_service_account_endpoints_allowed(sa_info: dict | None) -> None:
    """Refuse a service-account credential that points its OAuth endpoints
    somewhere other than Google.

    Allowlist, not policy-classification: there is exactly one legitimate set
    of hosts here, and an operator has no reason to change them. A tenant is
    uploading this file, so anything else is either a mistake or an attempt to
    aim the platform's outbound request at an internal address.
    """
    if not isinstance(sa_info, dict):
        return
    for field in _SERVICE_ACCOUNT_URL_FIELDS:
        raw = sa_info.get(field)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            # Fail closed: this module's posture is that an unrecognised shape
            # is a refusal, not a pass. A list/dict here is not something the
            # Google tooling emits.
            raise SourceHostBlockedError(
                f"The service-account key's '{field}' must be a string URL, "
                f"got {type(raw).__name__}."
            )
        parsed = urlparse(raw.strip())
        if parsed.scheme not in ("https", ""):
            raise SourceHostBlockedError(
                f"The service-account key's '{field}' must use https, "
                f"got {parsed.scheme or 'no scheme'!r}."
            )
        host = _normalise(parsed.hostname or "")
        if not host:
            raise SourceHostBlockedError(
                f"The service-account key's '{field}' has no hostname."
            )
        if host in _ALLOWED_SERVICE_ACCOUNT_HOSTS:
            continue
        if host.endswith(".googleapis.com") or host.endswith(".google.com"):
            continue
        raise SourceHostBlockedError(
            f"The service-account key's '{field}' points at '{host}', which is "
            "not a Google OAuth endpoint. Upload the key file exactly as "
            "Google issued it."
        )

    for field in _SERVICE_ACCOUNT_DOMAIN_FIELDS:
        raw = sa_info.get(field)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            raise SourceHostBlockedError(
                f"The service-account key's '{field}' must be a string."
            )
        domain = _normalise(raw)
        if domain not in ("googleapis.com", "google.com") and not (
            domain.endswith(".googleapis.com") or domain.endswith(".google.com")
        ):
            raise SourceHostBlockedError(
                f"The service-account key's '{field}' is '{domain}', which is "
                "not a Google service domain. Upload the key file exactly as "
                "Google issued it."
            )


async def assert_source_host_allowed(host: str, *, connector: str) -> None:
    """Full check: literal pre-flight, then validate every resolved address.

    Raises :class:`SourceHostBlockedError` when the host is not an acceptable
    egress target. Fails CLOSED on a name that does not resolve — the
    connection was going to fail anyway, and reporting it here keeps the
    resolution failure from being read as a policy pass.
    """
    if connector not in HOST_BEARING_CONNECTORS:
        return

    check_source_host_literal(host, connector=connector)

    normalised = _normalise(host)
    block_private, allowlist = _policy()
    if normalised in allowlist:
        return
    if _parse_address(normalised) is not None:
        return  # A literal, in any encoding, was already fully judged above.

    loop = asyncio.get_running_loop()
    try:
        # R5 review finding 4: bound it. This runs on a tenant-facing write
        # endpoint with a tenant-chosen name, and glibc will spend seconds per
        # nameserver against a tarpitting authoritative server -- on the update
        # path, while a tenant DB transaction is open. A timeout is reported as
        # "could not be resolved" so callers keep their existing three-way
        # handling (blocked / unresolved / clean).
        infos = await asyncio.wait_for(
            loop.getaddrinfo(normalised, None, proto=socket.IPPROTO_TCP),
            timeout=_resolve_timeout_seconds(),
        )
    except asyncio.TimeoutError as exc:
        raise SourceHostBlockedError(
            f"Source host '{host}' could not be resolved (DNS timed out after "
            f"{_resolve_timeout_seconds()}s)."
        ) from exc
    except socket.gaierror as exc:
        raise SourceHostBlockedError(
            f"Source host '{host}' could not be resolved ({exc})."
        ) from exc

    if not infos:
        raise SourceHostBlockedError(
            f"Source host '{host}' did not resolve to any address."
        )

    for *_unused, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        reason = _classify(addr, block_private)
        if reason is not None:
            logger.warning(
                "Blocked source-host egress: %r resolves to %s (%s)",
                host, addr, reason,
            )
            raise SourceHostBlockedError(
                f"Source host '{host}' resolves to {addr}, which is {reason}. "
                "It cannot be used as a source database host."
            )
