"""SAML 2.0 Service Provider backend.

Provides SP metadata generation, AuthnRequest initiation (redirect to IdP),
and Assertion Consumer Service (ACS) processing.  Uses python3-saml
(OneLogin) under the hood.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpcore
import httpx

from shared.auth.backend import UserIdentity
from shared.config.settings import get_settings
from shared.webhooks.ssrf import validate_webhook_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SamlAssertionResult:
    """F-021-03: validated SAML assertion plus the fields the ACS needs to
    enforce the replay ledger.

    ``identity`` is the resolved user. ``assertion_id`` is the unique ID of the
    processed assertion (the replay-ledger key). ``not_on_or_after`` is the
    earliest SubjectConfirmationData/Conditions NotOnOrAfter — the horizon past
    which the assertion is time-invalid anyway, used to bound the ledger row.
    """

    identity: UserIdentity
    assertion_id: str | None
    not_on_or_after: datetime | None

# Bug-7778: outbound IdP-metadata fetch timeout. Mirrors the OIDC discovery
# fetch timeout convention in ``oidc_backend.py`` (operational constant, not a
# tenant-tunable value). A metadata endpoint that does not answer within this
# window fails the settings build with a clear error rather than hanging the
# login flow.
_METADATA_FETCH_TIMEOUT_SECONDS = 10.0

# Bug-7778: cap on the metadata document size we will read into memory before
# parsing. IdP metadata XML is small (kilobytes); a hostile or misconfigured
# endpoint streaming an unbounded body must not be able to exhaust memory.
_METADATA_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB

# Bug-7855: bounded LRU + TTL cache for fetched IdP-metadata XML, keyed by the
# configured metadata URL. Mirrors the OIDC discovery cache in ``oidc_backend``.
# The Bug-7778 fix made URL mode functional by fetching+parsing on EVERY
# ``_get_saml_settings`` call; the unauthenticated ``/saml/metadata`` and
# ``/saml/login`` routes hit that path per request, so an anonymous client could
# drive one live outbound GET to the IdP per hit (latency + outbound-request
# amplification). Caching the fetched document collapses those to at most one
# outbound GET per URL per TTL window. Only the raw fetch is cached: on a cache
# miss the full SSRF pre-flight + connect-time-pinned fetch runs, and the
# OneLogin parse of the returned XML runs on every ``_get_saml_settings`` call
# regardless — so caching adds no bypass of any security control, it only
# suppresses the redundant network round-trip. A short TTL bounds staleness
# against IdP signing-key rotation. The cache is process-local; this is a
# synchronous code path
# (called from the SAML settings builder), so it uses a threading.Lock rather
# than the async lock the OIDC cache uses.
_METADATA_CACHE_MAX_SIZE = 16
_METADATA_CACHE_TTL_SECONDS = 300  # 5 minutes

_metadata_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_metadata_cache_lock = threading.Lock()


def _reset_metadata_cache() -> None:
    """Clear the IdP-metadata cache. Test-support only (Bug-7855)."""
    with _metadata_cache_lock:
        _metadata_cache.clear()


class MetadataFetchError(Exception):
    """Raised when the IdP metadata URL cannot be fetched or parsed.

    Bug-7778: surfaced instead of returning a silently-broken settings dict, so
    a URL-mode misconfiguration fails the login with a clear, logged error
    rather than producing a non-functional backend.
    """


class _SSRFSafeSyncBackend(httpcore.NetworkBackend):
    """Connect-time IP-pinning network backend (sync counterpart of
    ``shared/webhooks/ssrf._SSRFSafeBackend``).

    Resolves DNS, validates every resolved address is globally routable, then
    connects to the first validated IP directly — closing the DNS-rebinding
    TOCTOU gap. Rejects the whole connection if *any* resolved address is
    non-global or multicast (a mixed result set is a rebinding signal). The
    original hostname is preserved on the httpcore Origin so TLS SNI and
    certificate verification still use the name.

    We need a synchronous backend (not the shared async one) because the SAML
    settings path is called from synchronous helpers invoked inside the running
    event loop of the async ``sso.py`` routes, where ``asyncio.run`` is illegal.
    """

    def __init__(self) -> None:
        from httpcore._backends.sync import SyncBackend
        self._inner = SyncBackend()

    def connect_tcp(
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

        return self._inner.connect_tcp(
            validated_ip, port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError(
            "Unix socket connections not allowed for SAML metadata fetch"
        )

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _ssrf_safe_sync_transport() -> httpx.HTTPTransport:
    """Build a sync httpx transport that blocks connections to non-global IPs.

    Synchronous counterpart of ``shared/webhooks/ssrf.ssrf_safe_transport``.
    Asserts the private ``_pool`` assignment took effect so a future httpx
    upgrade that renames/removes ``_pool`` fails loudly rather than silently
    falling back to the default (unguarded) pool.
    """
    pool = httpcore.ConnectionPool(network_backend=_SSRFSafeSyncBackend())
    transport = httpx.HTTPTransport()
    transport._pool = pool  # type: ignore[attr-defined]
    if getattr(transport, "_pool", None) is not pool:
        raise RuntimeError(
            "SSRF guard: httpx.HTTPTransport._pool assignment did not take "
            "effect — the SSRF-safe network backend is NOT active. Pin httpx "
            "or update the SSRF transport integration."
        )
    return transport


def _fetch_idp_metadata_xml(url: str) -> str:
    """Fetch raw IdP metadata XML from ``url`` under the SSRF guard.

    Bug-7778: python3-saml's ``OneLogin_Saml2_IdPMetadataParser.parse_remote``
    performs its own ``urllib`` fetch, which bypasses every SSRF control in the
    codebase. We fetch the document ourselves through an IP-pinning, connect-time
    validating transport and then hand the raw XML to the same ``.parse`` path
    the inline-XML branch uses — so URL mode and XML mode converge on an
    identical parsed ``idp`` dict.

    Two layers, defence in depth (same policy as the webhook guard):

    1. ``validate_webhook_url`` — reused pre-flight from ``shared/webhooks/ssrf``
       (never a second copy that could drift). Rejects non-http(s) schemes,
       blocked internal hostnames (cloud metadata services, localhost), and
       literal non-global IPs. Honours the ``WEBHOOK_ALLOW_HTTP`` bootstrap
       setting so plaintext is refused by default.
    2. ``_SSRFSafeSyncBackend`` — connect-time DNS resolution + non-global
       address rejection. Closes the DNS-rebinding TOCTOU gap.

    Redirects are disabled: an IdP metadata endpoint that 30x-redirects to an
    internal address would otherwise re-open the SSRF hole one hop later.

    Raises ``MetadataFetchError`` on any refusal, network failure, non-2xx
    response, empty/oversized body, or decode error.

    Bug-7855: a bounded LRU + TTL cache keyed by ``url`` suppresses the redundant
    outbound GET the unauthenticated ``/saml/metadata`` and ``/saml/login``
    routes would otherwise drive on every request.
    """
    now = time.monotonic()
    with _metadata_cache_lock:
        cached = _metadata_cache.get(url)
        if cached is not None:
            xml, cached_at = cached
            if now - cached_at < _METADATA_CACHE_TTL_SECONDS:
                _metadata_cache.move_to_end(url)
                return xml
            # TTL expired — drop the stale entry and fall through to re-fetch.
            del _metadata_cache[url]

    try:
        safe_url = validate_webhook_url(url)
    except ValueError as exc:
        raise MetadataFetchError(
            f"SAML IdP metadata URL rejected by SSRF pre-flight: {exc}"
        ) from exc

    transport = _ssrf_safe_sync_transport()
    try:
        with httpx.Client(
            transport=transport, timeout=_METADATA_FETCH_TIMEOUT_SECONDS,
        ) as client:
            with client.stream("GET", safe_url, follow_redirects=False) as resp:
                if resp.status_code // 100 != 2:
                    raise MetadataFetchError(
                        f"SAML IdP metadata fetch returned HTTP {resp.status_code}"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _METADATA_MAX_BYTES:
                        raise MetadataFetchError(
                            f"SAML IdP metadata exceeds {_METADATA_MAX_BYTES} "
                            "bytes; refusing to load"
                        )
                    chunks.append(chunk)
    except MetadataFetchError:
        raise
    except Exception as exc:  # network / TLS / SSRF connect-time rejection
        raise MetadataFetchError(f"SAML IdP metadata fetch failed: {exc}") from exc

    body = b"".join(chunks)
    if not body.strip():
        raise MetadataFetchError("SAML IdP metadata response was empty")
    try:
        xml = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetadataFetchError(
            f"SAML IdP metadata is not valid UTF-8: {exc}"
        ) from exc

    # Bug-7855: cache the successfully fetched document with LRU eviction.
    with _metadata_cache_lock:
        if url not in _metadata_cache:
            while len(_metadata_cache) >= _METADATA_CACHE_MAX_SIZE:
                _metadata_cache.popitem(last=False)
        _metadata_cache[url] = (xml, time.monotonic())
        _metadata_cache.move_to_end(url)
    return xml


def _merge_parsed_idp_metadata(
    saml_cfg: dict[str, Any], idp_data: dict[str, Any]
) -> None:
    """Merge parsed IdP metadata into ``saml_cfg`` WITHOUT clobbering our posture.

    Bug-7778 (adversarial-review finding): ``dict.update(idp_data)`` is a shallow
    merge — it replaces whole sub-dicts. ``OneLogin_Saml2_IdPMetadataParser.parse``
    returns a ``security`` block (only ``authnRequestsSigned``) whenever the IdP
    metadata declares ``WantAuthnRequestsSigned`` — which Azure AD / Okta / ADFS
    all emit — and an ``sp`` block (only ``NameIDFormat``) when the metadata
    declares an IDPSSODescriptor NameIDFormat. A shallow update would then:

      * drop ``security.wantAssertionsSigned = True`` (python3-saml defaults it to
        False), silently downgrading the SP to ACCEPT UNSIGNED assertions — an
        assertion-forgery / auth-integrity hole; and
      * drop the base ``sp.entityId`` and ``sp.assertionConsumerService`` (ACS
        URL), yielding a configured-but-dead backend that never completes SSO.

    We adopt the parsed ``idp`` block wholesale (it is the IdP's coordinates,
    ours to take), but merge any parser-provided ``sp`` / ``security`` keys INTO
    our base sub-dicts so the SP identity, ACS binding, and the
    ``wantAssertionsSigned`` guarantee are never lost. Our explicit security
    posture wins on conflicting keys (``setdefault`` keeps the base value), so
    attacker-influenced metadata cannot flip an SP-side crypto setting such as
    ``authnRequestsSigned``; parser-only keys (e.g. a suggested NameIDFormat)
    are additive only where we have not set them.
    """
    for key, value in idp_data.items():
        if key in ("sp", "security") and isinstance(value, dict):
            base = saml_cfg.setdefault(key, {})
            if isinstance(base, dict):
                # Base (our explicit posture) wins on conflicts; parser-only
                # keys are added.
                for sub_key, sub_val in value.items():
                    base.setdefault(sub_key, sub_val)
            else:  # pragma: no cover - base is always a dict here
                saml_cfg[key] = value
        else:
            saml_cfg[key] = value


def _get_saml_settings(base_url: str) -> dict[str, Any] | None:
    """Build python3-saml settings dict from bootstrap config plus tenant overlay."""
    settings = get_settings()
    ov: dict[str, Any] = {}
    try:
        from src.auth.sso_overlay import current_overlay
        ov = current_overlay().get("saml") or {}
    except Exception:
        ov = {}
    metadata_url = (ov.get("idp_metadata_url") or settings.SAML_IDP_METADATA_URL or "").strip()
    metadata_xml = (ov.get("idp_metadata_xml") or settings.SAML_IDP_METADATA_XML or "").strip()
    if not metadata_url and not metadata_xml:
        return None

    sp_entity_id = settings.SAML_SP_ENTITY_ID or f"{base_url}/api/v1/auth/saml/metadata"
    acs_url = f"{base_url}/api/v1/auth/saml/acs"

    # Bug-7316: parse the optional allowlist of trusted IdP entityIDs.
    allowed_entity_ids: set[str] = set()
    if settings.SAML_ALLOWED_ENTITY_IDS:
        allowed_entity_ids = {
            eid.strip()
            for eid in settings.SAML_ALLOWED_ENTITY_IDS.split(",")
            if eid.strip()
        }

    saml_cfg: dict[str, Any] = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sp_entity_id,
            "assertionConsumerService": {
                "url": acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress",
        },
        "security": {
            "authnRequestsSigned": False,
            "wantAssertionsSigned": True,
            "wantNameIdEncrypted": False,
        },
    }

    sp_cert = (getattr(settings, "SAML_SP_X509CERT", "") or "").strip()
    sp_key = (getattr(settings, "SAML_SP_PRIVATE_KEY", "") or "").strip()
    if sp_cert and sp_key:
        saml_cfg["sp"]["x509cert"] = sp_cert
        saml_cfg["sp"]["privateKey"] = sp_key
        saml_cfg["security"]["authnRequestsSigned"] = True

    # Bug-7778: both configuration modes must produce the same parsed ``idp``
    # dict that python3-saml's settings loader consumes. The previous URL branch
    # set an ``idp_metadata_url`` key the loader never reads, producing a
    # silently non-functional backend. We now fetch the metadata ourselves under
    # the SSRF guard and feed the raw XML into the SAME ``.parse`` path the
    # inline-XML branch uses, so URL mode and XML mode converge.
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

    if metadata_url:
        try:
            idp_xml = _fetch_idp_metadata_xml(metadata_url)
        except MetadataFetchError:
            # Never return a broken-but-silent backend: log and refuse.
            logger.exception(
                "SAML IdP metadata URL %r could not be fetched; SAML backend "
                "is unconfigured for this request (Bug-7778)",
                metadata_url,
            )
            return None
        try:
            idp_data = OneLogin_Saml2_IdPMetadataParser.parse(idp_xml)
        except Exception:
            logger.exception(
                "SAML IdP metadata fetched from %r could not be parsed "
                "(Bug-7778)",
                metadata_url,
            )
            return None
        parsed_idp_entity_id = idp_data.get("idp", {}).get("entityId")
        if not (parsed_idp_entity_id and str(parsed_idp_entity_id).strip()):
            logger.error(
                "SAML IdP metadata fetched from %r yielded no idp entityId; "
                "refusing to build a non-functional backend (Bug-7778)",
                metadata_url,
            )
            return None
        _merge_parsed_idp_metadata(saml_cfg, idp_data)
    elif metadata_xml:
        idp_data = OneLogin_Saml2_IdPMetadataParser.parse(metadata_xml)
        _merge_parsed_idp_metadata(saml_cfg, idp_data)

    # Bug-7316: validate the parsed IdP entityID against the allowlist.
    # If the allowlist is configured, reject metadata whose entityID is
    # not explicitly trusted -- prevents metadata-swap / DNS-hijack attacks.
    # Bug-7778: both XML and URL modes now parse the metadata into an ``idp``
    # dict here, so this check applies uniformly to both paths (the URL path no
    # longer defers entityID resolution). A configured allowlist with no parsed
    # entityID is treated as a hard failure rather than a permissive fall-through.
    if allowed_entity_ids:
        parsed_entity_id = (
            saml_cfg.get("idp", {}).get("entityId", "")
        )
        if not parsed_entity_id:
            logger.error(
                "SAML_ALLOWED_ENTITY_IDS is set but the parsed IdP metadata "
                "has no entityId; refusing to build an unvalidated backend "
                "(defence-in-depth, Bug-7316)",
            )
            return None
        if parsed_entity_id not in allowed_entity_ids:
            logger.error(
                "SAML IdP entityID %r not in SAML_ALLOWED_ENTITY_IDS; "
                "rejecting metadata (defence-in-depth, Bug-7316)",
                parsed_entity_id,
            )
            return None

    return saml_cfg


def get_sp_metadata(base_url: str) -> str | None:
    """Generate SP metadata XML for the IdP to consume."""
    saml_cfg = _get_saml_settings(base_url)
    if saml_cfg is None:
        return None
    try:
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
        sp_settings = OneLogin_Saml2_Settings(saml_cfg, custom_base_path=None)
        metadata = sp_settings.get_sp_metadata()
        errors = sp_settings.validate_metadata(metadata)
        if errors:
            logger.warning("SAML SP metadata validation errors: %s", errors)
        return metadata.decode() if isinstance(metadata, bytes) else metadata
    except Exception:
        logger.exception("Failed to generate SAML SP metadata")
        return None


def build_authn_request(
    base_url: str, relay_state: str | None = None,
) -> tuple[str, str | None] | None:
    """Build a SAML AuthnRequest.

    Returns ``(redirect_url, request_id)`` or None on failure. F-021-03: the
    ``request_id`` is the AuthnRequest ID the caller must persist with the SSO
    flow state, so the ACS callback can require the IdP's ``InResponseTo`` to
    match this exact request (request-binding / replay defence).
    """
    saml_cfg = _get_saml_settings(base_url)
    if saml_cfg is None:
        return None
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        request_data = {
            "https": "on" if base_url.startswith("https") else "off",
            "http_host": base_url.split("//", 1)[1].split("/", 1)[0],
            "script_name": "/api/v1/auth/saml/acs",
            "get_data": {},
            "post_data": {},
        }
        auth = OneLogin_Saml2_Auth(request_data, saml_cfg)
        redirect_url = auth.login(return_to=relay_state)
        request_id = auth.get_last_request_id()
        return redirect_url, request_id
    except Exception:
        logger.exception("Failed to build SAML AuthnRequest")
        return None


def _earliest_not_on_or_after(auth: Any) -> datetime | None:
    """Return the earliest NotOnOrAfter the SAML library extracted, as an aware
    UTC datetime, or None if unavailable.

    python3-saml exposes ``get_last_assertion_not_on_or_after()`` returning a
    unix timestamp (seconds) for the SubjectConfirmationData NotOnOrAfter. We
    convert it to a timezone-aware datetime so it can bound the replay-ledger row.
    """
    try:
        ts = auth.get_last_assertion_not_on_or_after()
    except Exception:
        return None
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def process_saml_response(
    base_url: str,
    saml_response: str,
    relay_state: str | None = None,
    expected_request_id: str | None = None,
) -> SamlAssertionResult | None:
    """Parse and validate a SAML response from the IdP ACS POST.

    F-021-03: ``expected_request_id`` is the AuthnRequest ID this login flow
    issued. It is passed to python3-saml as ``request_id`` so the library
    validates the assertion's ``InResponseTo`` matches (request-binding). An
    assertion that was not produced for this exact request is rejected.

    Returns a ``SamlAssertionResult`` (identity + assertion id + not_on_or_after
    for the caller's replay ledger) on success, or None on failure.
    """
    settings_obj = get_settings()
    saml_cfg = _get_saml_settings(base_url)
    if saml_cfg is None:
        return None

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        request_data = {
            "https": "on" if base_url.startswith("https") else "off",
            "http_host": base_url.split("//", 1)[1].split("/", 1)[0],
            "script_name": "/api/v1/auth/saml/acs",
            "get_data": {},
            "post_data": {"SAMLResponse": saml_response},
        }
        if relay_state:
            request_data["post_data"]["RelayState"] = relay_state

        auth = OneLogin_Saml2_Auth(request_data, saml_cfg)
        # F-021-03: request-binding — python3-saml rejects the response when its
        # InResponseTo does not equal ``request_id``. We pass the AuthnRequest ID
        # persisted with the flow state. ``reject_deprecated_alg`` is left at the
        # library default; strict settings already require signed assertions.
        auth.process_response(request_id=expected_request_id)

        if auth.get_errors():
            logger.warning("SAML response errors: %s — reason: %s",
                           auth.get_errors(), auth.get_last_error_reason())
            return None

        if not auth.is_authenticated():
            logger.warning("SAML response: user not authenticated")
            return None

        attrs = auth.get_attributes()
        email_attr = settings_obj.SAML_ATTR_EMAIL
        display_attr = settings_obj.SAML_ATTR_DISPLAY_NAME
        groups_attr = settings_obj.SAML_ATTR_GROUPS

        email = attrs.get(email_attr, [None])[0] or auth.get_nameid()
        if not email:
            logger.warning("SAML response: no email found in attributes or NameID")
            return None

        display_name = (attrs.get(display_attr, [""]) or [""])[0]
        # F-021-04: distinguish an absent groups attribute (indeterminate) from a
        # present-but-empty one (authoritative "no groups" → de-provisioning).
        groups_claim_present = groups_attr in attrs
        groups = attrs.get(groups_attr, [])

        identity = UserIdentity(
            email=email,
            display_name=display_name,
            groups=groups,
            source_backend="saml",
            raw_claims=dict(attrs),
            groups_claim_present=groups_claim_present,
        )

        # F-021-03: surface the assertion id + validity horizon so the ACS can
        # atomically record it in the replay ledger and reject a second use.
        try:
            assertion_id = auth.get_last_assertion_id()
        except Exception:
            assertion_id = None
        not_on_or_after = _earliest_not_on_or_after(auth)

        return SamlAssertionResult(
            identity=identity,
            assertion_id=assertion_id,
            not_on_or_after=not_on_or_after,
        )
    except Exception:
        logger.exception("Failed to process SAML response")
        return None
