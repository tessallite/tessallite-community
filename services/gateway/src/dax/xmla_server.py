"""
XMLA-over-HTTP server.

POST /xmla/{tenant_slug}
POST /xmla/ (server endpoint - tenant resolved from Catalog property)

Excel / Power BI Desktop connects to this endpoint using Analysis Services
XMLA protocol. The gateway:

  1. Parses the SOAP/XMLA envelope.
  2. DISCOVER requests  → returns synthetic MDSCHEMA rowsets from model metadata.
  3. EXECUTE requests   → extracts the DAX statement, translates to a
                          query-router call, and wraps the result as an
                          XMLA MDDataSet response.

Authentication:
  - Basic auth  (Excel / Power BI): Authorization: Basic base64(email:password)
    The gateway exchanges credentials for a JWT via model-service /auth/login.
  - Bearer JWT  (API clients):       Authorization: Bearer <jwt>
    The JWT is validated directly.

For server endpoint (/xmla/):
  - Tenant is resolved from Catalog property in XMLA Properties
  - DISCOVER_DATASOURCES and MDSCHEMA_CATALOGS list all accessible tenants

For tenant endpoint (/xmla/{tenant}):
  - Tenant is specified in URL path (Power BI / API style)
  - Kept for backwards compatibility
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import gzip
import logging
import os
import re
# threading import removed: Bug-6937 — the inflight-task registry is
# guarded by asyncio.Lock (correct for single-threaded event loop),
# not threading.Lock (wrong primitive for asyncio concurrency).
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import unquote
from defusedxml import DefusedXmlException, ElementTree as ET

from fastapi import APIRouter, Request, Response

from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings as _get_settings
from shared.connector_qualify import quote_identifier as _qi
from shared.connector_qualify import quote_literal as _ql
from shared.semantic.hierarchy_resolver import resolve_hierarchy_dimension_map as _build_hierarchy_dimension_map
from src.auth.base import validate_session_upstream, verify_jwt_token
from src.dax import member_cache
from src.dax import session_store
from src.dax.cube_model import (
    STANDALONE_GROUP_NAME,
    build_cube_dimensions,
    filter_cube_dimensions_by_persona,
    is_standalone_attribute,
)
from src.dax.adapter import XmlaAdapter
from src.dax.constants import SERVER_NAME
from src.dax.dax_parser import translate_dax, find_kpi_member_functions
from src.dax.drillthrough_handler import handle_drillthrough
from src.dax.kpi_persona_filter import (
    filter_kpis_for_persona,
    _kpi_lineage_measure_ids as kpi_lineage_measure_ids,
    _measure_name_to_id as kpi_persona_measure_name_to_id,
)
from src.dax.mdx_validators import (
    check_unsupported_mdx_constructs as _check_unsupported_mdx_constructs,
)
from src.dax.mdschema import (
    _escape_mdx_bracket as _escape_mdx_bracket_name,
    build_discover_response,
    kpi_goal_static_value,
    kpi_goal_support_measure_name,
    kpi_goal_synthetic_measures,
    kpi_status_needs_support_measure,
    kpi_status_support_measure_name,
    kpi_status_synthetic_measures,
)
from src.dax.member_uname import (
    KEY_PATH,
    KEYS_OR_CAPTION,
    first_bracket_body,
    parse_member_keys,
    parse_member_uname,
)
from src.dax.mdx_execute import (
    build_real_execute_response,
)
from src.dax.ts_mdx_parser import (
    MDXParserUnavailableError,
    ParsedMDX,
    parse_mdx as parse_mdx_statement,
)
from src.router_client import (
    GatewayQueryRateLimitExceeded,
    QueryByteCeilingExceeded,
    QueryRouterError,
    evaluate_kpi_batch,
    evaluate_kpi_governed,
    execute_query,
    get_model_dimensions,
    get_model_hierarchies,
    get_hierarchy_preview,
    get_model_kpis,
    get_model_measures,
    get_model_named_sets,
    get_model_parameters,
    get_model_personas,
    get_model_snapshot,
    get_model_version_snapshot,
    get_deployed_named_queries,
    get_dimension_members,
    list_all_models_for_tenant,
    list_models_for_tenant,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _discovery_httpx():
    """``httpx`` module, imported lazily (this file's convention for it)."""
    import httpx as _httpx

    return _httpx


def _deployed_snapshot_fault_message(catalog: str) -> str:
    """One wording for the Bug-8384 DEPLOYED_SNAPSHOT_INVALID fault.

    Shared by the Discover and Execute handlers so the two BI surfaces cannot
    describe the same broken state differently. Deliberately does not enumerate
    which metadata family failed — the same 409 is reachable from the named-set
    and KPI fetches on three different request types, and the actionable half is
    the same in every case.
    """
    return (
        "DEPLOYED_SNAPSHOT_INVALID: the deployed model snapshot for catalog "
        f"'{catalog}' is missing or malformed, so this model cannot be served. "
        "Redeploy the model."
    )


_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_XMLA_NS = "urn:schemas-microsoft-com:xml-analysis"
_CONTENT_TYPE = "text/xml; charset=utf-8"

# Bug-5436b: per-request client Accept-Encoding. Set once at the HTTP entry
# point and read inside ``_soap_response`` so every XMLA response is compressed
# without threading a parameter through ~8 call sites. A ContextVar is the
# correct primitive here: each ASGI request runs in its own context, so there
# is no cross-request bleed even under concurrency.
_accept_encoding: contextvars.ContextVar[str] = contextvars.ContextVar(
    "xmla_accept_encoding", default=""
)

# Responses smaller than this (bytes) are not worth compressing — the gzip
# header overhead can make tiny payloads larger, and the CPU is wasted.
_COMPRESS_MIN_BYTES = 512

# Bug-6950: configurable XMLA request-body size cap (bytes).
_XMLA_MAX_REQUEST_BYTES = int(
    os.environ.get("XMLA_MAX_REQUEST_BYTES", str(10 * 1024 * 1024))
)
_XMLA_TRUST_LOOKUP_TIMEOUT_SECONDS = float(
    os.environ.get("XMLA_TRUST_LOOKUP_TIMEOUT_SECONDS", "0.5")
)

# Bug-8048: name of the Tessallite DRILLTHROUGH keyset-pagination extension.
# Used for BOTH the inbound Execute PropertyList entry and the outbound
# continuation element, so request and response never drift apart. The XMLA
# spec allows provider-specific properties; clients that do not know this one
# never send it and therefore never receive it back.
_DRILLTHROUGH_CURSOR_PROPERTY = "DrillthroughCursor"

# Bug-6945: collision-safe sentinel for timeline range filters.
# The previous ``__BETWEEN__<start>__<end>`` encoding used ``__`` as both
# prefix and separator, so member keys containing ``__`` (e.g. ``FY__2024``)
# were misparsed by ``bv.split("__")``.  A null byte cannot appear in
# XML/SOAP member-key strings, making ``\x00`` a collision-proof delimiter.
_RANGE_PREFIX = "\x00BETWEEN\x00"
_RANGE_SEP = "\x00"

# Bug-5888: registry of in-flight Execute tasks keyed by XMLA SessionId,
# mirroring the JDBC CancelRequest pattern (`jdbc/server.py:_inflight_tasks`,
# Bug-5188). A <Cancel> command carries the same SessionId as the Execute it
# targets (Excel/Power BI reuse one session across requests), so a Cancel on
# that session can look up and cancel the real in-flight query-router call
# instead of returning an unconditional, untruthful empty-success response.
# Only the most recently started task per session is tracked; concurrent
# multi-statement Execute on a single session is not a supported XMLA usage
# pattern for our BI clients, so this is a proportionate simplification.
_xmla_inflight_tasks: dict[str, asyncio.Task] = {}
# Bug-6937 (CF-002-DS-F00201): the previous ``threading.Lock`` was the wrong
# synchronisation primitive — it guards an ``asyncio.Task`` dict mutated only
# by coroutines on the single-threaded event loop. ``threading.Lock`` is
# semantically incorrect here (it serialises OS threads, not coroutine
# scheduling points) and can mask concurrency bugs.  ``asyncio.Lock``
# serialises at the coroutine-scheduling level, matching the execution model.
_xmla_inflight_lock = asyncio.Lock()


def _parse_accept_encoding(accept_encoding: str) -> dict[str, float]:
    """Parse an ``Accept-Encoding`` header into ``{coding: qvalue}`` (RFC 7231 §5.3).

    Bug-6951: previously the picker used a bare ``"gzip" in header`` substring
    test, which ignored quality weights entirely — so a client sending
    ``gzip;q=0`` (an explicit REFUSAL of gzip) was still served gzip because the
    substring ``gzip`` was present. Each coding may carry a ``;q=<weight>``
    (0.0-1.0; absent means 1.0); ``q=0`` means "not acceptable". A ``*`` token
    sets the default weight for codings not otherwise named.
    """
    weights: dict[str, float] = {}
    for part in (accept_encoding or "").split(","):
        token = part.strip().lower()
        if not token:
            continue
        coding, _, params = token.partition(";")
        coding = coding.strip()
        if not coding:
            continue
        q = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 1.0
                break
        # Clamp to the RFC range; malformed high/low values fold to bounds.
        weights[coding] = min(1.0, max(0.0, q))
    return weights


def _pick_content_encoding(accept_encoding: str) -> str:
    """Return the response Content-Encoding to use for a client Accept-Encoding.

    Honors gzip and deflate (the two encodings MSOLAP/Power BI advertise),
    respecting the ``Accept-Encoding`` quality weights (Bug-6951): a coding with
    ``q=0`` is refused, and among acceptable codings the higher q wins (gzip
    breaks a tie, matching the historical preference). Returns "" when neither
    gzip nor deflate is acceptable (identity) — including when the client sent
    ``gzip;q=0`` / ``deflate;q=0`` or only ``identity``.
    """
    weights = _parse_accept_encoding(accept_encoding)
    # A wildcard sets the default weight for any coding not explicitly listed.
    star_q = weights.get("*")
    gzip_q = weights.get("gzip", star_q if star_q is not None else 0.0)
    deflate_q = weights.get("deflate", star_q if star_q is not None else 0.0)
    # Prefer gzip on a tie; only pick a coding that is actually acceptable (q>0).
    if gzip_q > 0 and gzip_q >= deflate_q:
        return "gzip"
    if deflate_q > 0:
        return "deflate"
    return ""


def _tenant_from_jwt(jwt_token: str) -> str:
    """Extract tenant_id from a real JWT. Returns '' on failure."""
    try:
        payload = verify_jwt_token(jwt_token)
        return payload.tenant_id or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Tenant resolver for server endpoint
# ---------------------------------------------------------------------------

async def _resolve_tenant_from_catalog(xml_root: ET.Element, username: str, jwt_token: str) -> Optional[str]:
    """
    Resolve tenant slug from Catalog property in XMLA Properties.
    Used by the server endpoint (/xmla/) to support Excel's connection flow.

    OlaPy (confirmed working with Excel) does not validate catalog names
    against any backend — it simply accepts whatever the client sends.
    We do the same: if a Catalog is provided, return it directly.
    """
    properties = _parse_properties(xml_root)
    catalog = properties.get("Catalog", "").strip()

    if not catalog:
        return None

    # Accept the catalog name as-is — no model-service validation needed
    # for the XMLA discovery protocol.  Validation happens later when
    # an actual DAX/MDX query is executed.
    return catalog


# ---------------------------------------------------------------------------
# XMLA server endpoint (SSAS-style - Excel)
# ---------------------------------------------------------------------------

@router.api_route("/xmla", methods=["GET", "POST"])
@router.api_route("/xmla/", methods=["GET", "POST"])
@router.api_route("/xmla/msmdpump.dll", methods=["GET", "POST"])
@router.api_route("/msmdpump.dll", methods=["GET", "POST"])
async def xmla_server_endpoint(request: Request) -> Response:
    """
    XMLA-over-HTTP server endpoint (SSAS-style for Excel).
    Excel connects to single server URL, tenant resolved from Catalog property.
    """
    if request.method == "GET":
        # GET probe required by MSOLAP. Middleware already verified auth.
        return Response(status_code=200, media_type="text/plain")

    # Bug-5436b: record the client's Accept-Encoding for this request so
    # ``_soap_response`` can gzip/deflate the response body.
    _accept_encoding.set(request.headers.get("accept-encoding", ""))

    # Access authenticated user and JWT token set by BasicAuthMiddleware
    username = getattr(request.state, "username", "")
    jwt_token = getattr(request.state, "jwt_token", "")

    # Bug-6950: reject oversized XMLA request bodies before buffering.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _XMLA_MAX_REQUEST_BYTES:
        return Response(
            status_code=413,
            content="Request body too large",
            media_type="text/plain",
        )
    body_bytes = await request.body()
    if len(body_bytes) > _XMLA_MAX_REQUEST_BYTES:
        return Response(
            status_code=413,
            content="Request body too large",
            media_type="text/plain",
        )
    body_bytes = XmlaAdapter.normalize_inbound(body_bytes)

    # Phase F9 of the code-review remediation: do NOT log the raw SOAP body
    # by default — it may contain credentials or query content. Structured
    # debug log records the user, size, and no payload. Set
    # DEBUG_XMLA_RAW=1 in the environment to re-enable raw payload dump.
    if os.environ.get("DEBUG_XMLA_RAW") == "1":
        logger.debug(
            "xmla soap request: user=%r raw=%s",
            username,
            body_bytes[:1000].decode("utf-8", "replace"),
        )
    else:
        logger.debug("xmla soap request: user=%r bytes=%d", username, len(body_bytes))

    # Parse SOAP envelope and extract session info (critical for Excel handshake)
    try:
        root = ET.fromstring(body_bytes)
    except (ET.ParseError, DefusedXmlException) as exc:
        return _soap_fault(f"Malformed SOAP envelope: {exc}", "Client", status_code=400)

    # Session management — Excel requires SessionId echoed in every response
    session_id = _extract_session_action(root)

    # Check session cache if no header token. The cache is persisted
    # to disk so sessions survive ``docker restart`` without Excel
    # having to re-authenticate (Bug F2).
    if not jwt_token and session_id:
        cached = await session_store.get(session_id)
        if cached:
            jwt_token = cached

    # Auth gate: reject unauthenticated requests before any tenant
    # resolution to prevent tenant slug leaks in error messages.
    if not jwt_token:
        if session_id:
            await session_store.delete(session_id)
        return _soap_fault("Authentication required.", "Client", status_code=401)

    try:
        verify_jwt_token(jwt_token)
    except Exception:
        return _soap_fault("Authentication required.", "Client", status_code=401)

    # Bug-7322 (gateway consumer half): validate that the session has not
    # been revoked (deactivated user, role demotion, stale token_version).
    try:
        await validate_session_upstream(jwt_token)
    except ValueError:
        if session_id:
            await session_store.delete(session_id)
        return _soap_fault("Authentication required.", "Client", status_code=401)

    catalog_name = await _resolve_tenant_from_catalog(root, username, jwt_token)
    jwt_tenant = _tenant_from_jwt(jwt_token) if jwt_token else ""

    # Success: store session if established. ``put`` also refreshes
    # the TTL, so a long-running pivot keeps its session alive.
    if session_id:
        existed = await session_store.contains(session_id)
        await session_store.put(session_id, jwt_token)
        if not existed:
            logger.debug("xmla session created: %s", session_id)

    request_type = _find_text(root, "RequestType")
    method_name = _local_name(_find_method(root).tag) if _find_method(root) else None
    # Normalize endpoint URL — strip msmdpump.dll so responses always point
    # to the clean server endpoint (MSOLAP appends msmdpump.dll to HTTP URLs)
    endpoint_url = re.sub(r'/msmdpump\.dll\b', '/', str(request.url))
    if not endpoint_url.endswith('/'):
        endpoint_url += '/'

    logger.debug(
        "xmla request: catalog=%r tenant=%r type=%s method=%s",
        catalog_name,
        jwt_tenant,
        request_type,
        method_name,
    )

    # Handle Execute requests (e.g. empty MSOLAP handshake or DAX queries)
    if method_name == "Execute":
        return await _handle_execute(_find_method(root), jwt_tenant, jwt_token, session_id)

    # Server-level discovery requests (no catalog specified)
    if not catalog_name:
        if request_type in ("DISCOVER_DATASOURCES", "MDSCHEMA_CATALOGS", "DBSCHEMA_CATALOGS",
                            "DISCOVER_SCHEMA_ROWSETS"):
            return await _handle_discover_server_level(root, username, jwt_token, endpoint_url, session_id)
        else:
            logger.debug("xmla server-level request: %s (tenant=%r)", request_type, jwt_tenant)
            method_el = _find_method(root)
            if method_el is None:
                return _soap_fault("Could not locate XMLA method in SOAP body", "Client")
            return await _handle_discover(method_el, jwt_tenant, jwt_token, endpoint_url, session_id)

    # Catalog specified — route to tenant-specific handler
    return await _handle_xmla_request(jwt_tenant, username, jwt_token, body_bytes, request)


async def _handle_discover_server_level(
    xml_root: ET.Element, username: str, jwt_token: str, endpoint_url: str,
    session_id: str = "",
) -> Response:
    """Handle DISCOVER requests that arrive without a URL-path catalog.

    The caller at ``dispatch_xmla`` guarantees ``request_type`` is one of:

    - ``DISCOVER_SCHEMA_ROWSETS`` — enumerate available schemas. Always
      server-level; the SOAP ``<Catalog>`` property is ignored.
    - ``DISCOVER_DATASOURCES`` — list one SSAS-compatible datasource for
      the server. Describes the server itself; catalog is ignored.
    - ``MDSCHEMA_CATALOGS`` / ``DBSCHEMA_CATALOGS`` — list tenant catalogs.
      If the client passed a SOAP ``<Catalog>`` property, we narrow to
      that catalog via ``_handle_discover``; otherwise we return the
      full set via ``_build_catalogs_all_tenants``.

    CR-002 Finding 10: the function used to contain dead branches for
    ``DISCOVER_PROPERTIES`` (never reached given the caller's type
    filter) and for tenant-specific requests without a catalog (never
    reached for the same reason). The body is now a clean enumeration
    of the four legitimate request types with a logged ``_soap_fault``
    fallback if the caller ever violates its contract.
    """
    request_type = _find_text(xml_root, "RequestType")
    properties = _parse_properties(xml_root)
    restrictions = _parse_restrictions(xml_root)
    catalog_value = properties.get("Catalog", "").strip()
    jwt_tenant = _tenant_from_jwt(jwt_token)

    logger.debug(
        "xmla server-level discover: type=%r soap_catalog=%r tenant=%r",
        request_type,
        catalog_value,
        jwt_tenant,
    )

    if request_type == "DISCOVER_SCHEMA_ROWSETS":
        method_el = _find_method(xml_root)
        if method_el is None:
            return _soap_fault("Could not locate XMLA method in SOAP body", "Client")
        return await _handle_discover(
            method_el, jwt_tenant, jwt_token, endpoint_url, session_id
        )

    if request_type == "DISCOVER_DATASOURCES":
        return _build_datasources_server(endpoint_url, session_id)

    if request_type in ("MDSCHEMA_CATALOGS", "DBSCHEMA_CATALOGS"):
        if catalog_value:
            # Client narrowed the catalog list via the SOAP property.
            # F-002-15: the SOAP Catalog is a MODEL/catalog slug, not a tenant
            # slug. Pass the authenticated tenant (from the JWT) in the
            # tenant_slug parameter — _handle_discover reads the catalog itself
            # from the SOAP PropertyList. Passing catalog_value here put a model
            # slug where a tenant slug was expected (harmless only because the
            # downstream lookup keys off the JWT, but it surfaced the wrong
            # tenant in fault messages and would misroute if tenant plumbing
            # were re-enabled).
            method_el = _find_method(xml_root)
            if method_el is None:
                return _soap_fault("Could not locate XMLA method in SOAP body", "Client")
            return await _handle_discover(
                method_el, jwt_tenant, jwt_token, endpoint_url, session_id
            )
        return await _build_catalogs_all_tenants(
            jwt_token, endpoint_url, restrictions, session_id
        )

    # Caller contract violation — the outer dispatch should never
    # route a different request_type to this function.
    logger.warning(
        "xmla server-level discover called with unexpected request_type=%r",
        request_type,
    )
    return _soap_fault(
        f"Unexpected request type {request_type!r} at server-level discover",
        "Server",
    )


def _build_datasources_server(endpoint_url: str, session_id: str = "") -> Response:
    """
    Build DISCOVER_DATASOURCES response as a single SSAS-compatible server.
    Excel expects ONE datasource representing the server.  Catalogs (databases)
    are listed separately via MDSCHEMA_CATALOGS.
    """
    # Strip any trailing tenant slug from the URL so it points to the server root
    server_url = re.sub(r'/xmla/.*$', '/xmla/', endpoint_url)

    rows = [{
        "DataSourceName": SERVER_NAME,
        "DataSourceDescription": "Tessallite Semantic Aggregation Layer",
        "URL": server_url,
        "DataSourceInfo": "-",
        "ProviderName": SERVER_NAME,
        "ProviderType": "MDP",
        "AuthenticationMode": "Authenticated",
    }]

    col_defs = [
        {"name": "DataSourceName", "type": "string", "required": True},
        {"name": "DataSourceDescription", "type": "string"},
        {"name": "URL", "type": "string"},
        {"name": "DataSourceInfo", "type": "string"},
        {"name": "ProviderName", "type": "string"},
        {"name": "ProviderType", "type": "string", "required": True, "maxOccurs": "unbounded"},
        {"name": "AuthenticationMode", "type": "string", "required": True},
    ]
    from src.dax.mdschema import _build_rowset_xml

    xml_body = _build_rowset_xml(col_defs, rows)
    full_response = (
        f'<tns:DiscoverResponse>\n'
        f'  {xml_body}\n'
        f'</tns:DiscoverResponse>'
    )
    return _soap_response(full_response, session_id=session_id)


async def _build_catalogs_all_tenants(
    jwt_token: str, endpoint_url: str, restrictions: dict,
    session_id: str = "",
) -> Response:
    """
    Build MDSCHEMA_CATALOGS response listing all accessible models as catalogs.

    Excel expects this to list all databases (models) the user can access.
    Uses the tenant_id from the JWT to list models for the authenticated tenant.

    Phase 8 persona-as-catalog: each model is exposed as the business
    base catalog ``<slug>`` plus one sibling catalog per persona named
    ``<slug>_<persona.slug>``. The seeded ``technical`` persona
    reproduces the previous ``_technical`` variant; other personas
    surface as their own catalogs so Excel users can pick the shape
    they need from the connect dialog.
    """
    import asyncio as _aio_cat
    jwt_tenant = _tenant_from_jwt(jwt_token)
    rows = []
    try:
        models = await list_all_models_for_tenant(jwt_tenant, jwt_token)
        persona_tasks = [
            get_model_personas(
                str(model["id"]), jwt_tenant, jwt_token,
                project_id=str(model.get("project_id", "")),
            )
            for model in models
        ]
        persona_results = await _aio_cat.gather(*persona_tasks, return_exceptions=True)
        for model, personas_or_exc in zip(models, persona_results):
            base_slug = model.get("slug") or str(model["id"])
            display = model.get("display_name", "")
            personas = personas_or_exc if isinstance(personas_or_exc, list) else []
            variants: list[tuple[str, str]] = [("", "")]
            for persona in personas:
                pslug = persona.get("slug")
                if not pslug:
                    continue
                pname = persona.get("description") or persona.get("name") or pslug
                variants.append((f"_{pslug}", f" ({pname})"))
            for suffix, label_suffix in variants:
                rows.append({
                    "CATALOG_NAME": f"{base_slug}{suffix}",
                    "DESCRIPTION": f"{display}{label_suffix}".strip(),
                    "ROLES": "",
                    "DATE_MODIFIED": str(system_snapshot_get("xmla.metadata_modified_at")),
                    "COMPATIBILITY_LEVEL": "1600",
                    "TYPE": "1",
                })
    except Exception as exc:
        logger.warning("Failed to list models for catalogs: %s", exc)

    col_defs = [
        {"name": "CATALOG_NAME", "type": "string"},
        {"name": "DESCRIPTION", "type": "string"},
        {"name": "ROLES", "type": "string"},
        {"name": "DATE_MODIFIED", "type": "dateTime"},
        {"name": "COMPATIBILITY_LEVEL", "type": "int"},
        {"name": "TYPE", "type": "int"},
    ]
    from src.dax.mdschema import _build_rowset_xml

    xml_body = _build_rowset_xml(col_defs, rows)
    full_response = (
        f'<tns:DiscoverResponse>\n'
        f'  {xml_body}\n'
        f'</tns:DiscoverResponse>'
    )
    return _soap_response(full_response, session_id=session_id)


# ---------------------------------------------------------------------------
# XMLA tenant endpoint (Power BI / API style)
# ---------------------------------------------------------------------------

# Mirrors TenantCreate's slug pattern (^[a-z0-9_-]+$). A malformed path
# segment (e.g. "acme-demo," from a copy-paste with a trailing comma) used to
# half-work — cross-tenant auth succeeded and discovery answered — which hid
# the typo from the BI client instead of surfacing it (Bug-5534 diagnosis).
_TENANT_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


@router.api_route("/xmla/{tenant_slug}", methods=["GET", "POST"])
async def xmla_tenant_endpoint(tenant_slug: str, request: Request) -> Response:
    """
    XMLA-over-HTTP endpoint with tenant in path (Power BI / API style).
    Kept for backwards compatibility with Power BI and direct API users.
    """
    if not _TENANT_SLUG_RE.fullmatch(tenant_slug):
        logger.warning(
            "xmla tenant endpoint called with malformed tenant slug %r", tenant_slug
        )
        return Response(
            status_code=404,
            content=(
                f"Unknown workspace path segment {tenant_slug!r}. Use the "
                "workspace slug only, e.g. /api/v1/xmla/acme-demo"
            ),
            media_type="text/plain",
        )

    # Bug-6948 (CF-002-GPT-F00201): handle authenticated GET probes
    # consistently with the server endpoint (/xmla).  MSOLAP and Power BI
    # issue a GET to discover whether the endpoint is live before sending
    # XMLA POST traffic.  The server endpoint returns 200; the tenant
    # endpoint must do the same.  Middleware has already verified auth.
    if request.method == "GET":
        return Response(status_code=200, media_type="text/plain")

    username = getattr(request.state, "username", "")
    jwt_token = getattr(request.state, "jwt_token", "")
    # Bug-5436b: record the client's Accept-Encoding for response compression.
    _accept_encoding.set(request.headers.get("accept-encoding", ""))
    # Bug-6950: reject oversized XMLA request bodies.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _XMLA_MAX_REQUEST_BYTES:
        return Response(
            status_code=413,
            content="Request body too large",
            media_type="text/plain",
        )
    body_bytes = await request.body()
    if len(body_bytes) > _XMLA_MAX_REQUEST_BYTES:
        return Response(
            status_code=413,
            content="Request body too large",
            media_type="text/plain",
        )
    body_bytes = XmlaAdapter.normalize_inbound(body_bytes)

    logger.debug("xmla tenant resolved: tenant=%r user=%r", tenant_slug, username)
    return await _handle_xmla_request(tenant_slug, username, jwt_token, body_bytes, request)


# Bug-5534 (remaining Power BI Desktop half): the PBI data-source dialog can
# nest the WHOLE gateway URL after the base path, so the request line becomes
#   POST /api/v1/xmlahttps%3A//sql.cloud.tessallite.io%3A8080/api/v1/xmla
# (verbatim from the live GCP gateway log, 2026-06-25). Note there is no
# separator between ``xmla`` and ``https`` — the appended text is glued to the
# base path — so neither ``/xmla`` nor ``/xmla/{tenant_slug}`` matches it and
# FastAPI answered a bare 404 "Not Found". The user sees a connection failure
# with nothing pointing at the URL they typed, which is why this half of
# Bug-5534 stayed open through several rounds of live diagnosis.
#
# This route recognises the shape and answers with the SAME actionable 404 the
# malformed-slug guard above returns. It deliberately does NOT silently
# dispatch the request to a tenant guessed out of the appended text: the
# recorded Bug-5534 decision (see ``_TENANT_SLUG_RE``) is that a malformed
# XMLA URL must be surfaced, because the earlier half-working behaviour hid
# the typo from the BI client instead of getting it corrected. Recovering here
# would leave a broken connection string in place to fail again elsewhere.
_XMLA_APPENDED_URL_HINT_RE = re.compile(r"https?://", re.IGNORECASE)
# ``scheme://user:password@host`` — a pasted connection URL can carry embedded
# credentials, so the userinfo is stripped before the path is logged OR echoed.
# Greedy up to the LAST ``@`` before the next path separator: a decoded
# userinfo can itself contain an ``@`` (``admin@acme-demo.com:pw@host``), and a
# lazy match would leave the password behind.
#
# The scheme alternation accepts percent-encoded separators (``https%3A//``,
# ``https%3A%2F%2F``) because that is the shape Power BI Desktop actually
# appended in the live Bug-5534 log, and because this pattern is applied to the
# RAW path first — see ``_safe_malformed_path``.
_URL_USERINFO_RE = re.compile(
    r"(https?(?::|%3A)(?:/|%2F){2})[^/\s]*@", re.IGNORECASE,
)
# Last-resort net: everything from the scheme to the LAST ``@`` in the string.
# Only used when the precise pattern above provably left a userinfo behind (see
# ``_residual_userinfo``), because it can also swallow a ``@`` that belongs to
# the path. Losing diagnostic detail is always preferable to echoing a secret.
_URL_ANY_USERINFO_RE = re.compile(r"(https?://).*@", re.IGNORECASE)
_REDACTION = "[REDACTED]@"
# Route segment this catch-all is mounted on, used to recover the raw appended
# tail from the ASGI ``raw_path``.
_XMLA_ROUTE_SEGMENT = "/xmla"
# Cap on how much of a malformed path is repeated back, so a pasted blob does
# not become an unbounded log line or response body.
_MALFORMED_PATH_ECHO_LIMIT = 120


def _residual_userinfo(text: str) -> bool:
    """True when a ``scheme://...@`` userinfo survived redaction unredacted.

    Compares the whole userinfo against the marker rather than testing a
    suffix: a password crafted to END in ``[REDACTED]`` would otherwise pass
    the check and leave the rest of the credential in place. Any ``@`` between
    the scheme and the host that is not exactly the marker is treated as an
    unredacted userinfo — including a benign ``@`` in the path, which is then
    over-redacted. Losing a host name from a 404 message is always cheaper
    than echoing a secret.
    """
    m = _URL_ANY_USERINFO_RE.search(text)
    if not m:
        return False
    return m.group(0)[len(m.group(1)):] != _REDACTION


def _raw_appended_path(request: Request, decoded_fallback: str) -> str:
    """The still percent-encoded appended path, taken off the ASGI scope.

    ``raw_path`` is optional in the ASGI spec, so the decoded path parameter
    remains the fallback; ``_safe_malformed_path`` stays safe on either input.
    """
    raw = request.scope.get("raw_path")
    if not isinstance(raw, (bytes, bytearray)):
        return decoded_fallback
    text = raw.decode("latin-1", "replace")
    idx = text.find(_XMLA_ROUTE_SEGMENT)
    if idx < 0:
        return decoded_fallback
    return text[idx + len(_XMLA_ROUTE_SEGMENT):]


def _safe_malformed_path(raw_path: str) -> str:
    """Redact embedded credentials and cap the length of a path we echo.

    Takes the RAW, still percent-encoded path. sol review F-CR-03: redacting
    after decoding is unsound, because decoding destroys the authority boundary
    the pattern relies on — ``pw%2Ftail@host`` becomes ``pw/tail@host`` and
    ``[^/\\s]*@`` then stops at the decoded ``/`` before it ever reaches the
    ``@``, substituting nothing and echoing the password verbatim. Percent-
    encoded whitespace failed identically. In the encoded form a ``/`` or a
    space inside the userinfo is necessarily escaped, so the first literal
    ``/`` really is the path separator and the match is correct.

    Three passes, each strictly a net under the previous one:
    1. the precise pattern on the raw path (the sound case);
    2. the same pattern after decoding, for a credential only revealed by
       decoding (an escaped scheme with an otherwise clean userinfo) — the
       substitution is idempotent, so this can never un-redact;
    3. the blunt scheme-to-last-``@`` pattern, only if a userinfo demonstrably
       survived — which is possible when ``raw_path`` is unavailable and the
       decoded credential contains a delimiter.
    """
    redacted = _URL_USERINFO_RE.sub(rf"\1{_REDACTION}", raw_path)
    redacted = _URL_USERINFO_RE.sub(rf"\1{_REDACTION}", unquote(redacted))
    if _residual_userinfo(redacted):
        redacted = _URL_ANY_USERINFO_RE.sub(rf"\1{_REDACTION}", redacted)
    if len(redacted) > _MALFORMED_PATH_ECHO_LIMIT:
        redacted = redacted[:_MALFORMED_PATH_ECHO_LIMIT] + "..."
    return redacted


@router.api_route("/xmla{appended_path:path}", methods=["GET", "POST"])
async def xmla_malformed_path_endpoint(appended_path: str, request: Request) -> Response:
    """Explain a malformed XMLA URL instead of returning a bare 404.

    Only reached when no concrete XMLA route matched, because every concrete
    route (``/xmla``, ``/xmla/``, ``/xmla/msmdpump.dll``, ``/xmla/{tenant}``)
    is registered ahead of this one and Starlette matches in registration
    order.
    """
    # The hint check runs on the decoded path (an escaped ``https%3A//`` is
    # still an appended URL); the echoed path is redacted from the RAW path,
    # because decoding first would hide the credential boundary (F-CR-03).
    looks_appended = bool(_XMLA_APPENDED_URL_HINT_RE.search(unquote(appended_path)))
    safe_path = _safe_malformed_path(_raw_appended_path(request, appended_path))
    logger.warning(
        "xmla malformed path: path=%r url_appended=%s", safe_path, looks_appended,
    )
    detail = (
        "A full URL appears to have been appended to the XMLA endpoint. "
        if looks_appended else ""
    )
    return Response(
        status_code=404,
        content=(
            f"Malformed XMLA endpoint path {safe_path!r}. {detail}"
            "Use the server URL on its own — /api/v1/xmla for Excel (pick the "
            "workspace as the catalog), or /api/v1/xmla/<workspace> for Power "
            "BI Desktop, e.g. /api/v1/xmla/acme-demo. Do not paste the whole "
            "address a second time into the server field."
        ),
        media_type="text/plain",
    )


# ---------------------------------------------------------------------------
# Common XMLA request handler
# ---------------------------------------------------------------------------

async def _handle_xmla_request(tenant_slug: str, username: str, jwt_token: str, body_bytes: bytes, request: Request) -> Response:
    """
    Common handler for XMLA requests (shared by server and tenant endpoints).
    """
    root = None
    session_id = ""
    if body_bytes:
        try:
            root = ET.fromstring(body_bytes)
            session_id = _extract_session_action(root)
        except (ET.ParseError, DefusedXmlException):
            pass

    # Check session cache if no header token. Allows requests
    # without Authorization to succeed as long as a prior request
    # on the same session already authenticated. Persisted via
    # ``session_store`` so it survives ``docker restart`` (Bug F2).
    if not jwt_token and session_id:
        cached = await session_store.get(session_id)
        if cached:
            jwt_token = cached

    logger.debug(
        "xmla auth: tenant=%r user=%r session=%r",
        tenant_slug,
        username,
        session_id,
    )

    if not jwt_token:
        return _soap_fault("Authentication required.", "Client", status_code=401)

    try:
        verify_jwt_token(jwt_token)
    except Exception:
        return _soap_fault("Authentication required.", "Client", status_code=401)

    # Bug-7322 (gateway consumer half): validate that the session has not
    # been revoked (deactivated user, role demotion, stale token_version).
    try:
        await validate_session_upstream(jwt_token)
    except ValueError:
        if session_id:
            await session_store.delete(session_id)
        return _soap_fault("Authentication required.", "Client", status_code=401)

    # Success: store session if established (store token as plain string).
    if session_id:
        existed = await session_store.contains(session_id)
        await session_store.put(session_id, jwt_token)
        if not existed:
            logger.debug("xmla session created: %s", session_id)

    # Ensure we have a valid parsed body to continue
    if root is None:
        try:
            root = ET.fromstring(body_bytes)
        except (ET.ParseError, DefusedXmlException) as exc:
            return _soap_fault(f"Malformed SOAP envelope: {exc}", "Client", status_code=400)

    # Extract the XMLA method element (DISCOVER or EXECUTE) from SOAP Body
    method_el = _find_method(root)
    if method_el is None:
        return _soap_fault("Could not locate XMLA method in SOAP body", "Client")

    local_name = _local_name(method_el.tag)
    logger.debug("xmla method: %s", local_name)

    try:
        if local_name == "Discover":
            return await _handle_discover(method_el, tenant_slug, jwt_token, str(request.url), session_id)
        if local_name == "Execute":
            return await _handle_execute(method_el, tenant_slug, jwt_token, session_id)
    except Exception as exc:
        # Bug-6651: propagation of 401 from downstream services.
        # Previously used `"401" in str(exc)` which matched ANY error
        # whose text happened to contain "401" (e.g. "invoice #401
        # failed"). Now uses typed status inspection on httpx
        # HTTPStatusError and QueryRouterError.
        import httpx as _httpx
        from src.router_client import QueryRouterError as _QRE
        _is_downstream_401 = (
            (isinstance(exc, _httpx.HTTPStatusError) and exc.response.status_code == 401)
            or (isinstance(exc, _QRE) and exc.status_code == 401)
        )
        if _is_downstream_401:
            logger.warning("xmla downstream 401: %s", exc)
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Analysis Services"'},
                content=f"Downstream authentication required: {exc}",
                media_type="text/plain",
            )
        raise

    return _soap_fault(f"Unsupported XMLA method: {local_name}", "Client")


# ---------------------------------------------------------------------------
# DISCOVER handler
# ---------------------------------------------------------------------------

def _normalize_restrictions(restrictions: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in restrictions.items():
        out[key.upper()] = value
    return out


def _member_page_limit() -> int:
    """Bounded first-page size for a whole-level member enumeration (Bug-6602).

    ``MEMBER_DISCOVERY_LIMIT`` (default 100000, Bug-5436a) is a *completeness*
    cap for filter dropdowns, not a page size: materialising 100k members into
    a synchronous SOAP rowset is tens of MB that Excel parses on its UI thread
    (the freeze in Fable diagnostic §3). For a browse gesture (select/expand a
    dimension) we emit at most a bounded first page instead. Configurable via
    ``XMLA_MEMBER_PAGE_LIMIT`` and never larger than ``MEMBER_DISCOVERY_LIMIT``.
    TREE_OP children / specific-member drills are already parent-bounded and are
    not subject to this page cap.
    """
    discovery_cap = int(_get_settings().MEMBER_DISCOVERY_LIMIT)
    raw = os.environ.get("XMLA_MEMBER_PAGE_LIMIT", "").strip()
    page = 10000
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                page = parsed
        except ValueError:
            pass
    return max(1, min(page, discovery_cap))


def _hier_level_names(dimension: dict[str, Any]) -> list[str]:
    levels = dimension.get("levels") or []
    if not levels:
        return [dimension.get("name", "")]
    if isinstance(levels[0], dict):
        ordered = sorted(levels, key=lambda item: int(item.get("ordinal", 0)))
        return [str(item.get("name", "")).strip() for item in ordered if str(item.get("name", "")).strip()]
    return [str(item).strip() for item in levels if str(item).strip()]


def _build_discover_dimensions(
    raw_dimensions: list[dict[str, Any]],
    hierarchy_defs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map model metadata to the ordered XMLA cube-dimension list (Bug-6603).

    Delegates to ``cube_model.build_cube_dimensions`` — the single source of the
    cube shape — which (unlike the previous inline merge) preserves each
    hierarchy's id / caption / time typing and marks flat dimensions as
    attribute hierarchies. Every ``mdschema._rows_*`` builder and the member
    discovery path consume this one list, so DISCOVER emits a consistent
    dimension -> hierarchy -> level graph.
    """
    return build_cube_dimensions(raw_dimensions, hierarchy_defs)


def _extract_member_name(member_unique_name: str | None) -> str | None:
    """Deepest member key of a MEMBER_UNIQUE_NAME, via the single grammar parser.

    Bug-3617 (Phase 1): routed through ``parse_member_uname`` so a canonical
    name (``[Dim].[Hier].[Level].&[2025]&[4]``) yields the member key ``4`` — the
    old flat regex returned the LEVEL name (``Level``). Caption form is unchanged
    (``[Dim].[Hier].[4]`` -> ``4``). The ``(All)`` member still resolves to
    ``"All"`` so the All self-only short-circuit in ``_load_hierarchy_member_data``
    is preserved; Measures/invalid return None (as the old three-bracket regex did).
    """
    if not member_unique_name:
        return None
    _hier, _level, grammar, key_path = parse_member_uname(member_unique_name)
    if grammar == "all":
        return "All"
    if grammar in ("invalid", "measure") or not key_path:
        return None
    return key_path[-1]


def _level_index_from_unique_name(level_unique_name: str | None, dimension: dict[str, Any]) -> int | None:
    if not level_unique_name:
        return None
    level_names = _hier_level_names(dimension)
    dim_name = dimension.get("name", "")
    hier = f"[{dim_name}].[{dim_name}]"
    if level_unique_name == f"{hier}.[(All)]":
        return -1
    for idx, level_name in enumerate(level_names):
        if level_unique_name == f"{hier}.[{level_name}]":
            return idx
    return None


def _to_preview_member_row(member: dict[str, Any], ordinal: int) -> dict[str, Any]:
    # Bug-3617 (Phase 0.5a/c): carry the member KEY and CAPTION separately so
    # the XMLA member-identity layer can emit MEMBER_KEY/MEMBER_UNIQUE_NAME from
    # the key and MEMBER_CAPTION from the caption. ``name`` stays = key_value for
    # back-compat with the current caption-form emit/match (migrated in Phase 2);
    # ``key_path`` (when the preview supplies it) carries the ancestor key path
    # for parent-less whole-level enumeration (Phase 0.5b).
    key_value = str(member.get("key_value", ""))
    caption = member.get("caption")
    raw_path = member.get("key_path")
    return {
        "name": key_value,
        "ordinal": ordinal,
        "parent": str(member.get("parent_key") or ""),
        "level": str(member.get("level_name") or ""),
        "key": key_value,
        "caption": str(caption) if caption is not None else key_value,
        "key_path": [str(k) for k in raw_path] if isinstance(raw_path, list) else None,
    }


async def _load_hierarchy_member_data(
    *,
    model_id: str,
    project_id: str,
    dimension: dict[str, Any],
    tenant_slug: str,
    jwt_token: str,
    restrictions: dict[str, list[str]],
    persona_id: str | None = None,
) -> dict[str, Any]:
    hierarchy_id = str(dimension.get("hierarchy_id") or "")
    if not hierarchy_id:
        return {"members": [], "levels": _hier_level_names(dimension), "members_by_level": {}}

    norm = _normalize_restrictions(restrictions)
    level_names = _hier_level_names(dimension)
    level_count = len(level_names)
    member_filter = (norm.get("MEMBER_UNIQUE_NAME") or [None])[0]
    level_filter = (norm.get("LEVEL_UNIQUE_NAME") or [None])[0]
    tree_op_raw = (norm.get("TREE_OP") or [None])[0]
    tree_op = int(tree_op_raw) if tree_op_raw and str(tree_op_raw).isdigit() else 0
    parsed_member = _extract_member_name(member_filter)

    members_by_level: dict[str, list[dict[str, Any]]] = {}

    # All-member self-only requests need no preview query.
    if parsed_member and parsed_member.lower() == "all" and tree_op and (tree_op & 8) and not (tree_op & 1 or tree_op & 16):
        return {"members": [], "levels": level_names, "members_by_level": members_by_level}

    # Specific-member self-only requests can be answered without parent discovery.
    if parsed_member and parsed_member.lower() != "all" and (not tree_op or ((tree_op & 8) and not (tree_op & 1 or tree_op & 16))):
        current_level = _level_index_from_unique_name(level_filter, dimension)
        if current_level is None or current_level < 0:
            current_level = 0
        if current_level < level_count:
            members_by_level[str(current_level)] = [{
                "name": parsed_member,
                "ordinal": 0,
                "parent": "",
                "level": level_names[current_level],
            }]
        return {
            "members": members_by_level.get("0", []),
            "levels": level_names,
            "members_by_level": members_by_level,
        }

    # Bug-5431: TREE_OP parent(2)/ancestors(32). The canonical member_filter
    # carries the full ancestor key path, so parent/ancestor members are
    # synthesised straight from it (no source query). Skipped when children/
    # descendants are also requested (those need the live fetch below).
    if (
        parsed_member and parsed_member.lower() != "all"
        and tree_op and (tree_op & 2 or tree_op & 32)
        and not (tree_op & 1 or tree_op & 16)
    ):
        _, _, _, filt_path = parse_member_uname(member_filter)
        if filt_path:
            current_level = _level_index_from_unique_name(level_filter, dimension)
            if current_level is None or current_level < 0:
                current_level = min(len(filt_path) - 1, max(level_count - 1, 0))
            first = 0 if (tree_op & 32) else max(current_level - 1, 0)
            wanted = list(range(first, current_level))
            if tree_op & 8:  # self also requested
                wanted.append(current_level)
            for lvl in wanted:
                if 0 <= lvl < len(filt_path) and lvl < level_count:
                    members_by_level[str(lvl)] = [{
                        "name": filt_path[lvl],
                        "ordinal": 0,
                        "parent": filt_path[lvl - 1] if lvl > 0 else "",
                        "level": level_names[lvl],
                        "key": filt_path[lvl],
                        "key_path": list(filt_path[: lvl + 1]),
                    }]
            return {
                "members": members_by_level.get("0", []),
                "levels": level_names,
                "members_by_level": members_by_level,
            }

    expand_level = 0
    parent_key: str | None = None
    sibling_mode = False
    if parsed_member and parsed_member.lower() != "all":
        current_level = _level_index_from_unique_name(level_filter, dimension)
        if current_level is None or current_level < 0:
            current_level = 0
        if tree_op & 1 or tree_op & 16:
            expand_level = min(current_level + 1, max(level_count - 1, 0))
            parent_key = parsed_member
        elif tree_op & 4:
            # Bug-5431: siblings = the member's own parent's children.
            expand_level = current_level
            _, _, _, _fp = parse_member_uname(member_filter)
            if len(_fp) >= 2:
                parent_key = _fp[-2]
                sibling_mode = True
        else:
            expand_level = current_level
    elif level_filter:
        idx = _level_index_from_unique_name(level_filter, dimension)
        if idx is not None and idx >= 0:
            expand_level = idx

    # Bug-6602: a whole-level enumeration (no parent_key -> a browse of the
    # entire level, the case that streams a multi-MB payload) is bounded to a
    # first page; TREE_OP drills (parent_key set) are already parent-bounded and
    # keep the full completeness cap so no child is lost.
    if parent_key is None:
        sample_size = _member_page_limit()
    else:
        sample_size = _get_settings().MEMBER_DISCOVERY_LIMIT
    try:
        preview = await get_hierarchy_preview(
            model_id=model_id,
            hierarchy_id=hierarchy_id,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            project_id=project_id,
            # Bug-5436a: discovery member cap is configurable (was a silent 1000);
            # truncation is logged below so large hierarchies never lose members
            # without a trace.
            sample_size=sample_size,
            expand_level=expand_level,
            parent_key=parent_key,
            persona_id=persona_id,
            # Bug-3617 (Phase 0.5b): ask for ancestor key paths; the model-service
            # only returns them for the parent-less single-table enumeration case
            # (target_level>0, no parent_key), so this is a no-op on the root and
            # drill paths and safe to pass unconditionally.
            include_key_path=True,
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch hierarchy preview for model=%s hierarchy=%s: %s",
            model_id,
            hierarchy_id,
            exc,
        )
        return {"members": [], "levels": level_names, "members_by_level": {}}

    preview_members = preview.get("members") or []
    # Bug-5436a: never truncate member discovery silently — warn if the result
    # filled the cap (the client's filter dropdown is then incomplete).
    # Bug-6602: the effective cap is the ``sample_size`` actually requested
    # (the bounded page for a whole-level browse, the full completeness cap for
    # a drill), so the warning reports the true truncation point.
    _limit = sample_size
    if len(preview_members) >= _limit:
        logger.warning(
            "Member discovery hit the cap (%d) for model=%s hierarchy=%s level=%s — "
            "result may be truncated; raise XMLA_MEMBER_PAGE_LIMIT / "
            "MEMBER_DISCOVERY_LIMIT if this dimension is legitimately larger.",
            _limit, model_id, hierarchy_id, expand_level,
        )
    preview_rows = [
        _to_preview_member_row(member, idx)
        for idx, member in enumerate(preview_members)
    ]
    # F-2: the model-service preview REMOVES levels whose key attribute is
    # excluded by the persona and indexes the served level into that FILTERED
    # list, so the level it actually returns can differ from ``expand_level``
    # (computed here against this hierarchy's UNFILTERED level list — the two
    # sides diverge for a privileged caller whose auto-resolved persona differs
    # from the catalog persona, e.g. an admin browsing a persona-variant
    # catalog). Label the returned members by the level the PREVIEW reports
    # (matched by level name against this hierarchy's levels) so Month members
    # are never filed under the Quarter level. Falls back to ``expand_level``
    # when the preview reports no recognised level name, so the common,
    # non-skewed path is unchanged (there the served level == ``expand_level``).
    served_level = expand_level
    if preview_rows:
        _level_index = {str(n): i for i, n in enumerate(level_names)}
        served_name = str(preview_rows[0].get("level") or "")
        mapped = _level_index.get(served_name)
        if mapped is not None:
            served_level = mapped
    members_by_level[str(served_level)] = preview_rows
    if sibling_mode:
        # Bug-5431: siblings sit at the filter member's level sharing its parent;
        # stamp the canonical ancestor path (parent path + own key) so the matcher
        # resolves each sibling's identity correctly.
        _, _, _, _sfp = parse_member_uname(member_filter)
        if len(_sfp) >= 1:
            for _row in members_by_level.get(str(served_level), []):
                _row["key_path"] = list(_sfp[:-1]) + [_row.get("key") or _row.get("name")]
    root_members = members_by_level.get("0", [])
    return {
        "members": root_members,
        "levels": level_names,
        "members_by_level": members_by_level,
    }


async def _load_discover_member_data(
    *,
    model_id: str,
    project_id: str,
    dimensions: list[dict[str, Any]],
    tenant_slug: str,
    jwt_token: str,
    restrictions: dict[str, list[str]],
    persona_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load member data for MDSCHEMA_MEMBERS discovery.

    Bug-5189: ``persona_id`` is threaded through to
    ``get_dimension_members`` so the query-router enforces persona-level
    dimension allow-lists and row-level security. Without this a
    restricted persona could enumerate all members of a dimension it
    should not see.
    """
    import asyncio

    norm = _normalize_restrictions(restrictions)
    dim_filter = (norm.get("DIMENSION_UNIQUE_NAME") or [None])[0]
    hier_filter = (norm.get("HIERARCHY_UNIQUE_NAME") or [None])[0]
    member_filter = (norm.get("MEMBER_UNIQUE_NAME") or [None])[0]
    level_filter = (norm.get("LEVEL_UNIQUE_NAME") or [None])[0]
    tree_op = (norm.get("TREE_OP") or [None])[0]

    # Bug-6602: restriction-aware narrowing. Previously ``dims_to_fetch`` was
    # narrowed ONLY by DIMENSION/HIERARCHY_UNIQUE_NAME, so an Excel expand --
    # which restricts by MEMBER_UNIQUE_NAME (+TREE_OP) or LEVEL_UNIQUE_NAME
    # alone -- fanned a full source scan out to EVERY dimension in the model.
    # A member/level restriction identifies exactly one owning dimension via the
    # ``[Dim].[Hier]`` prefix, so derive it here and fetch only that dimension.
    # ``first_bracket_body`` is bracket-aware (handles dimension names that carry
    # a literal ``.`` or an escaped ``]]``) -- a plain ``split(".")`` would
    # mis-parse a dotted name and return zero members for that dimension.
    target_dim_name: str | None = None
    if dim_filter:
        target_dim_name = first_bracket_body(dim_filter)
    elif hier_filter:
        target_dim_name = first_bracket_body(hier_filter)
    else:
        ref = member_filter or level_filter
        if ref:
            hier_bracket, _lvl, grammar, _kp = parse_member_uname(ref)
            if hier_bracket and grammar != "invalid":
                target_dim_name = first_bracket_body(hier_bracket)

    # Bug-6603: the standalone-attribute field-list group node [Dimensions] is a
    # containing dimension, not a real Tessallite dimension, so a DIMENSION_UNIQUE_NAME
    # restriction of [Dimensions] must NOT narrow to a (non-existent) dimension named
    # "Dimensions" — that would fetch zero members. Fall back to per-hierarchy /
    # per-member narrowing; absent a finer ref, scope to the STANDALONE dims of the
    # group (not every dimension) so the Bug-6602 fan-out bound is preserved.
    group_browse = False
    if target_dim_name == STANDALONE_GROUP_NAME:
        target_dim_name = None
        group_browse = True
        ref = member_filter or level_filter or hier_filter
        if ref:
            hier_bracket, _lvl, grammar, _kp = parse_member_uname(ref)
            if hier_bracket and grammar != "invalid":
                target_dim_name = first_bracket_body(hier_bracket)
            elif hier_filter:
                target_dim_name = first_bracket_body(hier_filter)
            if target_dim_name is not None:
                group_browse = False

    if target_dim_name is not None:
        dims_to_fetch = [d for d in dimensions if d.get("name") == target_dim_name]
    elif group_browse:
        # Exactly the members of the [Dimensions] group — the standalone flat
        # attributes, matching the group node's DIMENSION_UNIQUE_NAME.
        dims_to_fetch = [d for d in dimensions if is_standalone_attribute(d)]
    elif dim_filter or hier_filter or member_filter or level_filter:
        # Bug-6802: a restriction WAS supplied but resolved to no owning
        # dimension — e.g. LEVEL_UNIQUE_NAME=[Measures] (a legitimate MSOLAP
        # probe that `parse_member_uname` classifies "invalid" because it is not
        # [Dim].[Hier]...), or any restriction the grammar cannot parse. The
        # old fallthrough fanned a full DISTINCT source scan out to EVERY
        # dimension (N scans) for a request that identified NO real dimension.
        # Fail NARROW: a restriction that names no Tessallite dimension owns no
        # members, so fetch none. Only the genuinely restriction-less browse
        # below fans out to the whole cube.
        logger.debug(
            "Member discovery restriction resolved to no owning dimension "
            "(dim=%r hier=%r member=%r level=%r) for model=%s; returning no "
            "members instead of fanning out to every dimension (Bug-6802).",
            dim_filter, hier_filter, member_filter, level_filter, model_id,
        )
        dims_to_fetch = []
    else:
        dims_to_fetch = dimensions

    page_limit = _member_page_limit()
    # Bug-6602: the per-caller fingerprint scopes the member cache to the
    # security context that actually filters the members. Member discovery
    # compiles ROW-LEVEL SECURITY from the caller's Principal (not the persona),
    # so users sharing a persona (incl. every business-base user) get different
    # filtered lists; keying on persona alone would leak one user's rows to
    # another. The persona still keys allow-list/persona scoping separately.
    principal_key = member_cache.principal_fingerprint(jwt_token)

    # Bug-6602: distinguish a specific-member lookup (Excel validating/resolving
    # one selected member) from a whole-level browse. Only a browse can stream a
    # multi-MB payload and is subject to the first-page cap; a specific-member
    # request must NOT be capped, or a flat member beyond the page boundary would
    # silently vanish from the pivot. (Flat dims have no parent pushdown, so the
    # whole level is fetched and re-filtered downstream to the one member.)
    is_specific_member = False
    if member_filter:
        _mhb, _mlv, _mgr, _mkp = parse_member_uname(member_filter)
        is_specific_member = _mgr in ("key", "caption")
    apply_flat_cap = not is_specific_member

    def _cap_flat(result: dict[str, Any], *, log: bool, dname: str) -> dict[str, Any]:
        """Bound a whole-level flat enumeration to the first page (browse only)."""
        if not isinstance(result, dict):
            return result
        members = result.get("members")
        if isinstance(members, list) and len(members) > page_limit:
            if log:
                logger.warning(
                    "Member discovery for model=%s dimension=%s returned %d "
                    "members; emitting the bounded first page of %d "
                    "(raise XMLA_MEMBER_PAGE_LIMIT if a full browse list is "
                    "required for this dimension).",
                    model_id, dname, len(members), page_limit,
                )
            return {**result, "members": members[:page_limit]}
        return result

    tasks = []
    dim_names = []
    dim_is_hier: list[bool] = []
    cache_keys: list[str] = []
    member_data: dict[str, dict[str, Any]] = {}
    for d in dims_to_fetch:
        dname = d.get("name", "")
        if not dname:
            continue
        is_hier = d.get("source") == "hierarchy"
        # Bug-6602: the cache key is scoped by persona AND per-caller principal
        # (security contexts). Flat dimensions enumerate the whole level
        # irrespective of the member/level restriction, so the cached value is
        # the FULL level (constant shape) and the browse page cap is applied
        # AFTER the cache read; hierarchy dimensions vary by member/level/tree-op.
        if is_hier:
            restriction_shape = f"{member_filter or ''}|{level_filter or ''}|{tree_op or ''}"
        else:
            restriction_shape = ""
        ckey = member_cache.member_key(
            model_id=model_id,
            persona_id=persona_id,
            principal_key=principal_key,
            dimension_name=dname,
            source_type="hierarchy" if is_hier else "flat",
            restriction_shape=restriction_shape,
        )
        cached = member_cache.get_member_data(ckey)
        if cached is not None:
            if not is_hier and apply_flat_cap:
                member_data[dname] = _cap_flat(cached, log=False, dname=dname)
            else:
                member_data[dname] = cached
            continue
        dim_names.append(dname)
        dim_is_hier.append(is_hier)
        cache_keys.append(ckey)
        if is_hier:
            # Bug-5424: persona_id is now threaded through to
            # get_hierarchy_preview so the model-service applies both
            # hierarchy/level visibility and row-level security
            # filtering when previewing members under a persona.
            tasks.append(
                _load_hierarchy_member_data(
                    model_id=model_id,
                    project_id=project_id,
                    dimension=d,
                    tenant_slug=tenant_slug,
                    jwt_token=jwt_token,
                    restrictions=restrictions,
                    persona_id=persona_id,
                )
            )
        else:
            tasks.append(get_dimension_members(
                model_id, dname, tenant_slug, jwt_token,
                persona_id=persona_id,
            ))

    if not tasks:
        return member_data

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for dname, is_hier, ckey, result in zip(dim_names, dim_is_hier, cache_keys, results):
        if not isinstance(result, dict):
            continue
        # Cache the FULL (uncapped) flat level so a browse and a specific-member
        # lookup share one entry; the browse page cap is a per-request
        # presentation step applied below.
        member_cache.put_member_data(ckey, result)
        if not is_hier and apply_flat_cap:
            member_data[dname] = _cap_flat(result, log=True, dname=dname)
        else:
            member_data[dname] = result
    return member_data


async def _restore_empty_axis_members(
    *,
    dax_statement: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    measures_meta: list[dict[str, Any]],
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    persona_id: str | None = None,
) -> list[dict[str, Any]]:
    """Bug-6658 (F-002-04): restore zero-fact axis members when NON EMPTY is absent.

    SSAS's "Show items with no data" renders every member of an axis level, even
    ones with no facts, unless NON EMPTY prunes them. The fact-driven GROUP BY the
    translator emits only returns members present in facts, so a plain (no
    NON EMPTY) axis previously rendered identically to a NON EMPTY one — silently
    dropping zero-activity members.

    This fetches the member domain for each axis dimension whose axis omits
    NON EMPTY and unions the ABSENT members into ``rows`` with empty (NULL) measure
    cells, so the member appears with blank cells. NON EMPTY thereby becomes an
    explicit pruning operation (keep the fact-driven behaviour) rather than the
    only behaviour.

    Scope guard: applies only when the whole pivot uses at most ONE flat axis
    dimension per axis (the common "products/periods with no activity" case). A
    multi-dimension axis would need the FULL member cross-product, which can
    explode; that case is left fact-driven (a documented limitation) rather than
    risk an unbounded result. Only the axis (row/col) dimensions are considered;
    slicer/WHERE dims are unaffected.
    """
    if not rows and not columns:
        return rows
    if not model_id:
        return rows

    col_expr = _mdx_axis_expr(dax_statement, 0)
    row_expr = _mdx_axis_expr(dax_statement, 1)

    # Resolve the axis dim columns present in the result, per axis.
    def _axis_dims(expr: str) -> list[str]:
        return _mdx_extract_dimensions(
            expr, dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )

    col_dims = [d for d in _axis_dims(col_expr) if d in columns]
    row_dims = [d for d in _axis_dims(row_expr) if d in columns]

    measure_names = {str(m.get("name") or "") for m in measures_meta if m.get("name")}
    result_measures = [c for c in columns if c in measure_names]

    # Fable R1 F3: an explicit enumerated member set on an axis (e.g.
    # ``{[Product].&[A],[Product].&[B]}`` ON ROWS) restricts the level to exactly
    # those members. Restoration must NOT add back filtered-out members, so exclude
    # dims whose axis carries an explicit member filter.
    axis_text = _mdx_axis_expr(dax_statement, 0) + " " + _mdx_axis_expr(dax_statement, 1)
    axis_member_filters = _mdx_extract_axis_member_filters(
        axis_text, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    filtered_dims = set(axis_member_filters.keys())

    # Only the dims on an axis that OMITS NON EMPTY and has no explicit member
    # filter are eligible for restoration.
    eligible: list[str] = []
    if col_dims and not _mdx_axis_has_non_empty(dax_statement, 0):
        eligible.extend(d for d in col_dims if d not in filtered_dims)
    if row_dims and not _mdx_axis_has_non_empty(dax_statement, 1):
        eligible.extend(d for d in row_dims if d not in filtered_dims)
    if not eligible:
        return rows

    # Scope guard: at most one flat dim per eligible axis. A multi-dim axis needs
    # the member cross-product, which we do not synthesise here (fail-safe: leave
    # fact-driven, never emit an unbounded/incorrect axis).
    elig_col = [d for d in eligible if d in col_dims]
    elig_row = [d for d in eligible if d in row_dims]
    if len(elig_col) > 1 or len(elig_row) > 1:
        logger.debug(
            "Bug-6658: multi-dimension axis omits NON EMPTY; leaving fact-driven "
            "(empty-member restoration is scoped to single flat axis dims)."
        )
        return rows

    # Fable R1 F4: to avoid injecting a phantom "(blank)" member on the OTHER
    # axis (build_real_execute_response normalises None -> "(blank)" for all dim
    # cols), restored rows are synthesised against each EXISTING other-axis member
    # rather than setting other dims to None. For a measures-only other axis (no
    # dims), one empty-cell row per restored member suffices.
    other_dims_map: dict[str, list] = {}
    for dim in eligible:
        others = [d for d in (col_dims + row_dims) if d != dim and d in columns]
        if others:
            # Collect distinct tuples of the other-axis dims present in fact rows.
            seen: dict[str, list] = {}
            for r in rows:
                key = str(r.get(others[0], "")) if len(others) == 1 else str(tuple(str(r.get(o, "")) for o in others))
                if key not in seen:
                    seen[key] = [r.get(o) for o in others]
            other_dims_map[dim] = list(seen.values())
        else:
            other_dims_map[dim] = [[]]  # one empty row per restored member

    added: list[dict[str, Any]] = []
    for dim in eligible:
        present = {str(r.get(dim)) for r in rows if r.get(dim) is not None}
        others = [d for d in (col_dims + row_dims) if d != dim and d in columns]
        try:
            member_payload = await get_dimension_members(
                model_id, dim, tenant_slug, jwt_token, persona_id=persona_id,
            )
        except Exception as exc:  # best-effort: a fetch failure keeps fact rows
            logger.warning(
                "Bug-6658: member-domain fetch for %s failed: %s", dim, exc,
            )
            continue
        for m in member_payload.get("members", []):
            key = m.get("key_value")
            if key is None:
                key = m.get("key")
            if key is None:
                continue
            if str(key) in present:
                continue
            # Synthesise one empty-cell row per existing other-axis member so the
            # restored member appears with blank cells without injecting a phantom
            # "(blank)" member on the other axis.
            for other_vals in other_dims_map.get(dim, [[]]):
                new_row: dict[str, Any] = {}
                for c in columns:
                    new_row[c] = None
                new_row[dim] = key
                for meas in result_measures:
                    new_row[meas] = None
                for i, o in enumerate(others):
                    if i < len(other_vals):
                        new_row[o] = other_vals[i]
                added.append(new_row)
            present.add(str(key))

    if not added:
        return rows
    return list(rows) + added


async def _load_model_metadata_cached(
    *,
    model_id: str,
    project_id: str,
    tenant_slug: str,
    jwt_token: str,
    persona_id: str | None = None,
    deployed_version_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch ``(measures, dimensions, hierarchy_defs)`` for a model, short-TTL
    cached and shared by every XMLA metadata consumer (Bug-6602).

    Excel / Power BI / "Analyze in Excel" burst dozens of Discover AND
    Execute/DMV requests during connect; each otherwise re-fetched measures +
    dimensions + hierarchies-WITH-DETAILS (one HTTP call PER hierarchy — the
    N+1). The cache is keyed by (tenant, model, PRINCIPAL fingerprint,
    persona_id): the model-service list endpoints auto-resolve the caller's
    persona from the JWT and apply persona allow-list + CLS
    restricted-column trimming BEFORE responding
    (``resolve_effective_persona`` / Bug-6141), so the lists are
    caller-DEPENDENT — an admin-primed unfiltered snapshot must never be served
    to a restricted viewer, nor a viewer-primed trimmed snapshot to an admin
    (Bug-6602 / Fable F-1). The per-caller fingerprint isolates security
    contexts while a single caller's connect burst still de-duplicates (same
    JWT), preserving the N+1 elimination. The gateway applies its own
    catalog-persona allow-list + ``is_hidden`` trimming AFTER this returns. Deep
    copies are returned (and stored) so a caller's in-place edit — including
    nested ``levels`` lists on hierarchy defs — can never poison the snapshot.

    Bug-6628: ``persona_id`` is forwarded to the model-service metadata
    endpoints (same shape as the Bug-6263 named-sets fix). Without it,
    ``resolve_effective_persona`` 403s for multi-persona users ('multiple
    personas — select one') and the metadata swallowed into empty lists
    makes Excel show an empty model. persona_id is included in the cache
    key so different persona views for the same principal are not mixed.

    Bug-7959: ``deployed_version_id`` pins ``effective_description`` to the
    deployed snapshot.  The live model-service routes compute
    effective_description from the CURRENT glossary state (pre-deploy edits
    leak); the serialiser now bakes the approved glossary definitions into
    the dimension/measure snapshot rows at deploy time.  When a
    deployed_version_id is available, this function fetches the deployed
    snapshot and overlays its ``effective_description`` values onto the live
    metadata — so XMLA and JDBC both serve the same deployment-pinned
    descriptions.

    Exception-safe: like the original inline fetch, a failure part-way keeps
    what already succeeded (e.g. measures + dimensions when only the hierarchy
    fetch fails) rather than discarding all three. Only a COMPLETE fetch is
    cached, so a transient upstream failure is never frozen into the cache for
    the TTL (and a partially-degraded hierarchy-detail fetch that raises is not
    cached either); the next request re-tries. Callers receive whatever was
    fetched and never need their own try/except.

    Bug-6628 fail-loud: an ``httpx.HTTPStatusError`` with status 403 is NOT
    swallowed — it is re-raised so the caller surfaces a proper XMLA fault
    instead of silently degrading to an empty catalogue. Other exceptions
    are still caught (partial preservation).
    """
    import httpx as _httpx  # local import to avoid top-level dep

    # F-1: scope the cache to the calling principal — the model-service filters
    # the lists by the JWT-auto-resolved persona/CLS, so a shared (tenant, model)
    # entry would leak one persona's visible NAMES to another within the TTL.
    # Bug-6628: persona_id is part of the key so connecting to modelx_business
    # and modelx_technical within one session gets separate cached snapshots.
    principal_key = member_cache.principal_fingerprint(jwt_token)
    persona_suffix = str(persona_id) if persona_id else ""
    meta_key = member_cache.metadata_key(
        tenant_slug=tenant_slug, model_id=model_id,
        principal_key=f"{principal_key}\x01{persona_suffix}",
    )
    cached = member_cache.get_metadata(meta_key)
    if cached is not None:
        _m, _d, _h = cached
        return copy.deepcopy(_m), copy.deepcopy(_d), copy.deepcopy(_h)
    measures: list[dict[str, Any]] = []
    raw_dimensions: list[dict[str, Any]] = []
    hierarchy_defs: list[dict[str, Any]] = []
    complete = False
    try:
        measures = await get_model_measures(
            model_id, tenant_slug, jwt_token, project_id=project_id,
            persona_id=persona_id,
        )
        raw_dimensions = await get_model_dimensions(
            model_id, tenant_slug, jwt_token, project_id=project_id,
            persona_id=persona_id,
        )
        hierarchy_defs = await get_model_hierarchies(
            model_id, tenant_slug, jwt_token, project_id=project_id,
            include_details=True, persona_id=persona_id,
        )
        complete = True
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            # Bug-6628: a 403 from the model-service means the user has
            # multiple personas and none was specified. Re-raise so the
            # caller can surface a proper XMLA fault (fail LOUD) instead
            # of silently returning an empty catalogue.
            logger.warning(
                "Metadata fetch 403 (model %s, persona_id=%s): %s — "
                "re-raising for XMLA fault.",
                model_id, persona_id, exc,
            )
            raise
        logger.warning("Failed to fetch model metadata: %s", exc)
    except Exception as exc:
        logger.warning("Failed to fetch model metadata: %s", exc)

    # Bug-7959: overlay deployed snapshot effective_description onto live
    # metadata so XMLA serves the same deployment-pinned descriptions as
    # JDBC.  If the deployed snapshot is unavailable or has no
    # effective_description fields (pre-fix snapshot), the live values are
    # kept — matching the additive-field fallback contract.
    if deployed_version_id and (measures or raw_dimensions):
        try:
            deployed_snap = await get_model_version_snapshot(
                model_id, deployed_version_id, tenant_slug, jwt_token,
                project_id=project_id,
            )
            _overlay_deployed_effective_descriptions(
                measures, raw_dimensions, deployed_snap,
            )
        except Exception as _snap_exc:
            # Non-fatal: if the snapshot overlay fails, XMLA degrades to
            # live effective_description rather than failing the request.
            logger.warning(
                "Bug-7959: deployed snapshot overlay failed for model %s "
                "(version %s): %s — falling back to live descriptions.",
                model_id, deployed_version_id, _snap_exc,
            )

    if complete:
        member_cache.put_metadata(
            meta_key,
            (
                copy.deepcopy(measures),
                copy.deepcopy(raw_dimensions),
                copy.deepcopy(hierarchy_defs),
            ),
        )
    return measures, raw_dimensions, hierarchy_defs


def _overlay_deployed_effective_descriptions(
    measures: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    deployed_snapshot: dict[str, Any],
) -> None:
    """Replace live ``effective_description`` with the deployed snapshot value.

    Bug-7959: the live model-service routes compute effective_description from
    the CURRENT glossary state, which means a glossary edit appears in XMLA
    metadata BEFORE a deploy.  The serialiser now bakes the approved glossary
    definitions into dimension/measure snapshot rows at deploy time.  This
    helper overlays those deployed values onto the live metadata so XMLA
    serves the same pinned descriptions as JDBC.

    Fallback: if the deployed snapshot lacks ``effective_description`` (pre-fix
    snapshot format), the live value is kept.  This ensures backward
    compatibility with snapshots created before Bug-7959 was fixed.
    """
    if not deployed_snapshot:
        return

    # Build id -> effective_description lookups from the deployed snapshot.
    snap_dims = {
        str(d.get("id") or ""): d.get("effective_description")
        for d in (deployed_snapshot.get("dimensions") or [])
    }
    snap_meas = {
        str(m.get("id") or ""): m.get("effective_description")
        for m in (deployed_snapshot.get("measures") or [])
    }

    for dim in dimensions:
        dim_id = str(dim.get("id") or "")
        pinned = snap_dims.get(dim_id)
        if pinned is not None:
            dim["effective_description"] = pinned

    for meas in measures:
        meas_id = str(meas.get("id") or "")
        pinned = snap_meas.get(meas_id)
        if pinned is not None:
            meas["effective_description"] = pinned


async def _handle_discover(
    method_el: ET.Element,
    tenant_slug: str,
    jwt_token: str,
    endpoint_url: str = "",
    session_id: str = "",
) -> Response:
    request_type = _find_text(method_el, "RequestType")
    if not request_type:
        return _soap_fault("RequestType element missing", "Client")

    properties = _parse_properties(method_el)
    catalog = properties.get("Catalog", "")
    logger.debug("xmla discover: type=%r catalog=%r", request_type, catalog)

    # Resolve model + persona from the catalog name.
    model_id, project_id, persona, deployed_version_id = await _resolve_model_id(
        catalog, tenant_slug, jwt_token,
    )
    is_technical_view = _persona_includes_hidden(persona)

    # Bug-XMLA-002 + Bug-XMLA-005 fix: validate the Catalog property at
    # the Discover entry point. Previously, an unknown catalog resolved
    # to ``model_id = None`` and the handler emitted an empty metadata
    # response, so Excel "connected" but then failed on the first real
    # MDX query with a confusing deferred error. We reject unknown
    # catalogs with a proper XMLA Fault so the connect handshake itself
    # fails fast and Excel surfaces "catalog not found" in its error UI.
    #
    # Exemption policy (Bug-XMLA-005):
    # - DISCOVER_DATASOURCES, MDSCHEMA_CATALOGS, DBSCHEMA_CATALOGS are
    #   legitimate catalog-listing requests. Passing a Catalog property
    #   narrows the result set; an unknown catalog returns an empty
    #   list rather than a fault. These are exempt.
    # - DISCOVER_PROPERTIES and DISCOVER_SCHEMA_ROWSETS used to be on
    #   the exemption list because they describe the server itself.
    #   BUT Excel's Connection.Open issues DISCOVER_PROPERTIES *with*
    #   the target Catalog in PropertyList — when the caller passes a
    #   non-empty catalog on these requests, they are implicitly
    #   asserting "I'm connecting to this catalog", and the handshake
    #   must fail fast if the catalog is unknown. So these are only
    #   exempt when the catalog is empty.
    _catalog_listing_requests = {
        "DISCOVER_DATASOURCES",
        "MDSCHEMA_CATALOGS",
        "DBSCHEMA_CATALOGS",
    }
    if (
        catalog
        and model_id is None
        and request_type not in _catalog_listing_requests
    ):
        return _soap_fault(
            f"Catalog {catalog!r} not found for tenant {tenant_slug!r}.",
            "Client",
            status_code=404,
        )

    # Bug-6628: extract persona_id before the metadata cache call so the
    # model-service receives the resolved persona and does not trip
    # resolve_effective_persona's "select one" 403 for multi-persona users.
    # Previously persona_id was extracted only after the metadata fetch, so
    # the metadata call went without persona_id.
    persona_id = str(persona["id"]) if persona and persona.get("id") else None

    # Bug-9178 remediation: Named Queries carry no per-object persona allow-list
    # AND the deployed snapshot does not record which dimensions a Named Query
    # projects, so the catalogue cannot prove a given NQ is within a restricted
    # persona's dimension scope (unlike named sets, which persona-scope by
    # referenced-dimension id per Bug-5963). Until per-NQ dimension provenance
    # exists, NQs are advertised ONLY on catalogue surfaces where the persona
    # narrows NO dimensions (full-visibility); on a surface where the persona
    # hides at least one dimension they are suppressed, so a restricted persona
    # can never enumerate a Named Query that might reference a hidden dimension.
    # Set True below iff filter_cube_dimensions_by_persona actually drops a dim.
    _nq_persona_narrows_dimensions = False

    measures: list[dict[str, Any]] = []
    raw_dimensions: list[dict[str, Any]] = []
    hierarchy_defs: list[dict[str, Any]] = []
    discover_dimensions: list[dict[str, Any]] = []
    if model_id:
        # Bug-6602 / F-1: the metadata is short-TTL cached per (tenant, model,
        # PRINCIPAL, persona_id) — NOT (tenant, model) alone. The model-service
        # list endpoints apply persona/CLS trimming before responding, so the
        # snapshot is caller-dependent and must never be shared across principals
        # or persona views. The gateway's catalog-persona allow-list + is_hidden
        # trimming is applied BELOW, after this read. Kills the per-Discover
        # hierarchy-detail N+1 for a single caller's connect burst.
        try:
            measures, raw_dimensions, hierarchy_defs = await _load_model_metadata_cached(
                model_id=model_id, project_id=project_id,
                tenant_slug=tenant_slug, jwt_token=jwt_token,
                persona_id=persona_id,
                deployed_version_id=deployed_version_id,
            )
        except Exception as _meta_exc:
            # Bug-6628: a 403 from the model-service (multi-persona user
            # without persona_id) must surface as a clear XMLA fault.
            import httpx as _httpx
            if (
                isinstance(_meta_exc, _httpx.HTTPStatusError)
                and _meta_exc.response.status_code == 403
            ):
                return _soap_fault(
                    "This user account has multiple personas. "
                    "Connect to a persona-specific catalog "
                    f"(e.g. '{catalog}_business' or '{catalog}_technical') "
                    "instead of the base catalog.",
                    "Client",
                    status_code=403,
                )
            raise
        try:
            discover_dimensions = _build_discover_dimensions(raw_dimensions, hierarchy_defs)
        except Exception:
            # build_cube_dimensions is pure dict work, so a failure here is a
            # real bug — log loud (with traceback) rather than silently degrade.
            # The attribute-dimension fallback below still yields a usable (if
            # hierarchy-less) catalogue instead of faulting the whole Discover.
            logger.exception("Failed to build discover dimensions")
    if not discover_dimensions:
        discover_dimensions = list(raw_dimensions)

    # Fetch all tenant models once; reused for trust_meta lookup,
    # catalog enumeration, and persona enrichment below.
    _all_models: list[dict[str, Any]] = []
    try:
        _all_models = await list_all_models_for_tenant(tenant_slug, jwt_token)
    except Exception as exc:
        logger.warning("Failed to list tenant models: %s", exc)

    trust_meta: dict[str, Any] = {}
    if model_id:
        for m in _all_models:
            if str(m.get("id", "")) == str(model_id):
                trust_meta = m.get("trust_meta") or {}
                break

    # Phase 8 persona-as-catalog: the business base drops every dimension
    # and measure flagged is_hidden (cascaded from ModelColumn.is_hidden).
    # A persona whose includes_hidden_columns is true keeps them all, but
    # clears the flag so the downstream mdschema row builders treat every
    # item as visible. A persona with populated include_*_ids further
    # trims the lists to that allow list.
    if is_technical_view:
        def _unhide(item: dict[str, Any]) -> dict[str, Any]:
            return {**item, "is_hidden": False}

        measures = [_unhide(m) for m in measures]
        raw_dimensions = [_unhide(d) for d in raw_dimensions]
        discover_dimensions = [_unhide(d) for d in discover_dimensions]
    else:
        measures = [m for m in measures if not m.get("is_hidden")]
        raw_dimensions = [d for d in raw_dimensions if not d.get("is_hidden")]
        discover_dimensions = [d for d in discover_dimensions if not d.get("is_hidden")]

    if persona:
        # Only the persona-scoped ``measures`` is consumed downstream; the
        # dimension surface served to clients is ``discover_dimensions``
        # (filtered source-aware just below), so the narrowed raw-dimension
        # list is intentionally discarded here.
        measures, _, _ = _apply_persona_allow_lists(
            persona,
            measures=measures,
            dimensions=raw_dimensions,
        )
        # Bug-6603: the discover list mixes attribute dimensions and hierarchy
        # dimensions, so it must be scoped source-aware — attribute dims by
        # included_dimension_ids, hierarchies by included_hierarchy_ids.
        # Filtering the whole list by included_dimension_ids alone (the old
        # behaviour) silently deleted every hierarchy from a persona that
        # populated only that list (Fable symptom 2, finding b).
        _nq_dims_before_persona = {
            str(d.get("name") or "") for d in discover_dimensions
        }
        discover_dimensions = filter_cube_dimensions_by_persona(
            discover_dimensions, persona,
        )
        # Bug-9178 remediation: this persona narrowed the visible dimension set,
        # so it is a restricted surface — Named Queries are suppressed below.
        if {
            str(d.get("name") or "") for d in discover_dimensions
        } != _nq_dims_before_persona:
            _nq_persona_narrows_dimensions = True

    # Phase 5 of the semantic-layer plan: append synthetic trust-signal
    # measures so Excel users can drag freshness / source / owner straight
    # into a pivot. They live under the "Info" display folder so the
    # business measures list stays clean.
    measures = list(measures) + _build_trust_info_measures()

    if not measures and not discover_dimensions and not model_id:
        logger.warning("No model found for catalog=%r tenant=%r", catalog, tenant_slug)

    # For catalog-enumeration requests, fetch all tenant models so the wizard
    # can show the real list of databases. Phase 8 persona-as-catalog: each
    # model is enriched with its personas so the row builders can emit one
    # catalog per persona alongside the business base.
    tenant_models: list[dict[str, Any]] = []
    if request_type.upper() in (
        "MDSCHEMA_CATALOGS",
        "DBSCHEMA_CATALOGS",
        "MDSCHEMA_CUBES",
    ):
        import asyncio as _aio_disc
        try:
            tenant_models = await list_models_for_tenant(tenant_slug, jwt_token)
            _persona_tasks = [
                get_model_personas(
                    str(m.get("id", "")), tenant_slug, jwt_token,
                    project_id=str(m.get("project_id", "")),
                )
                for m in tenant_models
            ]
            _persona_results = await _aio_disc.gather(*_persona_tasks, return_exceptions=True)
            for m, p_or_exc in zip(tenant_models, _persona_results):
                m["personas"] = p_or_exc if isinstance(p_or_exc, list) else []
        except Exception as exc:
            logger.warning("Failed to list tenant models: %s", exc)

    restrictions = _parse_restrictions(method_el)
    # Bug-5189 / Bug-6628: persona_id extracted above (before the metadata
    # cache call) so member enumeration is scoped to the resolved persona.
    member_data = {}
    if model_id and request_type.upper() == "MDSCHEMA_MEMBERS":
        member_data = await _load_discover_member_data(
            model_id=model_id,
            project_id=project_id,
            dimensions=discover_dimensions,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            restrictions=restrictions,
            persona_id=persona_id,
        )

    named_sets: list[dict[str, Any]] = []
    kpis_list: list[dict[str, Any]] = []
    named_queries: list[dict[str, Any]] = []
    # Bug-6888: MDSCHEMA_MEASURES also needs the KPI list so the synthetic
    # goal support measures ([Measures].[<KPI> Goal] for static targets) are
    # present as (hidden) measure rows — Excel resolves the member advertised
    # in MDSCHEMA_KPIS against the measures rowset.
    if model_id and request_type.upper() in (
        "MDSCHEMA_SETS", "MDSCHEMA_KPIS", "MDSCHEMA_MEASURES",
    ):
        try:
            if request_type.upper() == "MDSCHEMA_SETS":
                # Bug-6263: forward the persona resolved from the catalog name
                # so MDSCHEMA_SETS is persona-filtered consistently with the
                # measure/dimension/KPI surfaces (not empty for multi-persona
                # viewers, not over-broad on persona-variant catalogs).
                named_sets = await get_model_named_sets(
                    model_id, tenant_slug, jwt_token, project_id=project_id,
                    persona_id=persona_id,
                )
            else:
                kpis_list = await get_model_kpis(
                    model_id, tenant_slug, jwt_token, project_id=project_id,
                )
                # Bug-7227: filter KPIs BY LINEAGE for a measure-restricted
                # persona (was Bug-5587, which blanked the ENTIRE list — a
                # persona restricted to some measures then saw ZERO KPIs). A KPI
                # is advertised iff every measure in its transitive lineage is in
                # the persona allow-list; fail closed on unverifiable lineage.
                # Same policy the query-router $KPIs data path uses
                # (_kpi_allowed_by_persona), applied here to the XMLA catalogue.
                if persona and kpis_list:
                    allow_m = {
                        str(x)
                        for x in (persona.get("included_measure_ids") or [])
                    }
                    if allow_m:
                        kpis_list = filter_kpis_for_persona(
                            kpis_list, measures, allow_m,
                        )
        except _discovery_httpx().HTTPStatusError as exc:
            # Bug-8384: a 409 DEPLOYED_SNAPSHOT_INVALID means the model's
            # deployed serving authority is genuinely broken, not that a
            # transient fetch blipped. Swallowing it renders an EMPTY set/KPI
            # catalogue in Excel with no error — indistinguishable from "this
            # model has no named sets", which is the same deceptive-empty
            # failure Bug-7254 rejected on the Execute path (which re-raises for
            # exactly this reason). Fail loud so discovery and Execute agree.
            # Return a SOAP Fault, not a bare re-raise: an unhandled exception
            # leaves the route as an unformatted HTTP 500, which Excel shows as a
            # generic "connection lost" carrying none of the diagnosis. Loud is
            # only useful if it is also readable.
            if getattr(exc.response, "status_code", None) == 409:
                logger.error(
                    "Deployed snapshot is invalid for model %s; failing %s "
                    "rather than advertising an empty catalogue (Bug-8384): %s",
                    model_id, request_type, exc,
                )
                return _soap_fault(
                    _deployed_snapshot_fault_message(catalog), "Server",
                )
            logger.warning("Failed to load named_sets/kpis: %s", exc)
        except Exception as exc:
            logger.warning("Failed to load named_sets/kpis: %s", exc)

    if kpis_list and request_type.upper() == "MDSCHEMA_MEASURES":
        # Bug-6888: append hidden goal support measures for static-target KPIs.
        # Bug-8288: append hidden governed-status support measures so the
        # [Measures].[<caption> Status] member advertised in MDSCHEMA_KPIS exists
        # as a resolvable measure a native pivot can bind.
        measures = (
            list(measures)
            + kpi_goal_synthetic_measures(kpis_list, measures)
            + kpi_status_synthetic_measures(kpis_list, measures)
        )

    # Bug-9178: advertise deployed Named Queries as first-class ``@name``
    # tables in the DBSCHEMA_TABLES / DBSCHEMA_COLUMNS rowsets — the same
    # relation shape the JDBC catalogue registers, so Excel / Power BI table
    # enumeration can see and reference a Named Query. Definitions come from
    # the DEPLOYED snapshot only (invariant 7): an undeployed draft never
    # enters the catalogue.
    #
    # Bug-9178 PERSONA remediation: advertise Named Queries ONLY on catalogue
    # surfaces where the persona narrows no dimensions
    # (``_nq_persona_narrows_dimensions`` False). A restricted persona could
    # otherwise enumerate a Named Query — and its column list — that projects a
    # dimension outside its scope, defeating the persona dimension-scope
    # boundary every sibling BI surface enforces (measures / dimensions / KPIs /
    # hierarchies / named sets, Bug-6628/6263/5963/6800). Data rows stay
    # RLS/CLS-protected at query time regardless; this closes the CATALOGUE
    # (structure) leak. Full-visibility surfaces (persona=None or a persona that
    # hides nothing) still see every deployed Named Query. Per-NQ dimension-
    # scope filtering (so restricted personas see the NQs they ARE entitled to)
    # is tracked as a follow-up once the snapshot records NQ dimension refs.
    #
    # Bug-8384 parity: a 409 DEPLOYED_SNAPSHOT_INVALID fails LOUD as a SOAP
    # fault instead of rendering a deceptive empty table list — same policy
    # as the named-set / KPI fetches above.
    if (
        not _nq_persona_narrows_dimensions
        and model_id and deployed_version_id
        and request_type.upper() in ("DBSCHEMA_TABLES", "DBSCHEMA_COLUMNS")
    ):
        try:
            named_queries = await get_deployed_named_queries(
                model_id, deployed_version_id, tenant_slug, jwt_token,
                project_id=project_id,
            )
        except _discovery_httpx().HTTPStatusError as exc:
            if getattr(exc.response, "status_code", None) == 409:
                logger.error(
                    "Deployed snapshot is invalid for model %s; failing %s "
                    "rather than advertising an empty Named Query table list "
                    "(Bug-8384): %s", model_id, request_type, exc,
                )
                return _soap_fault(
                    _deployed_snapshot_fault_message(catalog), "Server",
                )
            logger.warning("Failed to load named_queries: %s", exc)
        except Exception as exc:
            logger.warning("Failed to load named_queries: %s", exc)

    xml_body = build_discover_response(
        request_type=request_type,
        catalog_name=catalog,
        model_id=model_id or "",
        measures=measures,
        dimensions=discover_dimensions,
        endpoint_url=endpoint_url,
        properties=properties,
        restrictions=restrictions,
        tenant_models=tenant_models,
        member_data=member_data,
        trust_meta=trust_meta,
        hierarchy_defs=hierarchy_defs,
        named_sets=named_sets,
        kpis=kpis_list,
        named_queries=named_queries,
    )

    full_response = (
        f'<tns:DiscoverResponse>\n'
        f'  {xml_body}\n'
        f'</tns:DiscoverResponse>'
    )

    # Log row count to confirm we're actually returning data
    row_count = full_response.count("<row>")
    # Response body not logged by default — may contain query results.
    # Summary only; set DEBUG_XMLA_RAW=1 to dump the body.
    if os.environ.get("DEBUG_XMLA_RAW") == "1":
        logger.debug(
            "xmla response: type=%s rows=%d body=%s",
            request_type,
            row_count,
            full_response[:4000],
        )
    else:
        logger.debug("xmla response: type=%s rows=%d", request_type, row_count)

    # Final pass for strict MSOLAP compatibility
    full_response = XmlaAdapter.normalize_outbound(full_response)

    return _soap_response(full_response, session_id=session_id)


# ---------------------------------------------------------------------------
# EXECUTE handler
# ---------------------------------------------------------------------------

async def _handle_tmschema_dmv(
    statement: str,
    catalog: str,
    tenant_slug: str,
    jwt_token: str,
    session_id: str,
) -> Response:
    """Answer a ``$SYSTEM.TMSCHEMA_*`` DMV Execute from model metadata (Bug-5430).

    Power BI / "Analyze in Excel" read the Tabular metadata surface via these
    DMVs. We resolve the catalog's model, fetch its measures / dimensions /
    hierarchies, and project the requested TMSCHEMA table as a flat XMLA Rowset
    (the same response shape DRILLTHROUGH uses). An unresolved catalog or an
    unknown TMSCHEMA table yields a conformant empty rowset rather than a fault,
    so the discovery sequence proceeds.
    """
    from src.dax.mdschema import _build_rowset_xml, build_tmschema_rowset

    table = _tmschema_table_name(statement)
    measures: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    hierarchy_defs: list[dict[str, Any]] = []

    model_id, project_id, persona, _dmv_dvid = await _resolve_model_id(
        catalog, tenant_slug, jwt_token,
    )
    # Bug-6628: thread persona_id into metadata fetches.
    _dmv_persona_id = str(persona["id"]) if persona and persona.get("id") else None
    if model_id:
        # Bug-6602: share the short-TTL metadata cache so a Power BI /
        # "Analyze in Excel" DMV burst does not re-run the hierarchy-detail
        # N+1 on every request. The helper is exception-safe (partial-preserving).
        measures, dimensions, hierarchy_defs = await _load_model_metadata_cached(
            model_id=model_id, project_id=project_id,
            tenant_slug=tenant_slug, jwt_token=jwt_token,
            persona_id=_dmv_persona_id,
            deployed_version_id=_dmv_dvid,
        )

    # Persona / hidden scoping for the TMSCHEMA DMV (Bug-5493).
    #
    # Two distinct restriction kinds must be handled differently because they
    # mean different things:
    #
    #   ACCESS  (persona allow lists + CLS restricted columns) — the requesting
    #           persona is NOT permitted to see the object at all. Such measures
    #           must be EXCLUDED so neither their name nor their DAX expression
    #           leaks. This mirrors the persona-as-catalog isolation the discovery
    #           path enforces.
    #
    #   CURATION (the hidden/visible flag) — the object exists for this persona
    #           but the modeller has hidden it from default field lists. The real
    #           SSAS Tabular contract still LISTS hidden measures in
    #           TMSCHEMA_MEASURES with IsHidden=true, so we include them with their
    #           expression and carry the IsHidden flag through. Hidden is curation,
    #           not an access boundary, and must not blank the DAX.
    #
    # A technical persona (includes_hidden_columns) sees every object as visible,
    # so its hidden flag is cleared; otherwise the original is_hidden is preserved
    # (NOT dropped) and surfaced via the IsHidden column.
    is_technical_view = _persona_includes_hidden(persona)
    if is_technical_view:
        def _unhide(item: dict[str, Any]) -> dict[str, Any]:
            return {**item, "is_hidden": False}

        measures = [_unhide(m) for m in measures]
        dimensions = [_unhide(d) for d in dimensions]

    if persona:
        # ACCESS boundary 1 — persona allow lists exclude non-permitted objects.
        measures, dimensions, hierarchy_defs = _apply_persona_allow_lists(
            persona,
            measures=measures,
            dimensions=dimensions,
            hierarchies=hierarchy_defs,
        )
        # ACCESS boundary 2 — CLS / persona-restricted source columns. A measure
        # or dimension bound to a restricted source column is excluded entirely
        # (name + DAX). Additionally, an INCLUDED measure whose DAX expression
        # references a restricted column NAME has its expression blanked so the
        # restricted column name cannot leak through the exposed DAX (covers
        # calculated measures, which reference columns only inside free-text DSL).
        restricted_column_names: set[str] = set()
        names_resolved = True
        cls_snapshot: dict[str, Any] | None = None
        if persona.get("restricted_column_ids") and model_id:
            restricted_column_names, names_resolved, cls_snapshot = (
                await _resolve_restricted_column_names(
                    persona, model_id, tenant_slug, jwt_token, project_id,
                )
            )
        measures, dimensions = _apply_cls_column_guard(
            persona,
            measures=measures,
            dimensions=dimensions,
            restricted_column_names=restricted_column_names,
            names_resolved=names_resolved,
            snapshot=cls_snapshot,
        )

    col_defs, rows = build_tmschema_rowset(
        table, catalog, measures, dimensions, hierarchy_defs,
    )
    xml_body = _build_rowset_xml(col_defs, rows)
    return _soap_response(
        f'<tns:ExecuteResponse>{xml_body}</tns:ExecuteResponse>',
        session_id=session_id,
    )


_LEADING_MDX_COMMENT_RE = re.compile(
    r"^\s*(?://[^\n]*|--[^\n]*|/\*.*?\*/)", re.DOTALL,
)


def _strip_leading_mdx_comments(text: str) -> str:
    """Remove leading whitespace + line/block comments from a statement.

    Used only to CLASSIFY a statement (DAX vs MDX) — SSAS/BI tools can prefix a
    statement with a ``//``, ``--`` or ``/* */`` comment, and the DAX detector
    (``^EVALUATE``/``DEFINE``) must see past it (WC3-U2). Mid-statement comments
    are untouched (the grammar handles those as extras).
    """
    s = text or ""
    while True:
        m = _LEADING_MDX_COMMENT_RE.match(s)
        if not m or m.end() == 0:
            break
        s = s[m.end():]
    return s.lstrip()


def _parse_mdx_for_execute(statement: str) -> ParsedMDX:
    """Parse MDX for Execute — the structured parser is the ADMISSION AUTHORITY.

    Wave C #3: the gateway FAILS CLOSED (raises ``ValueError`` → SOAP client fault)
    when, for ANY MDX statement class (normal SELECT, WITH MEMBER/SET, DRILLTHROUGH):

      * the structured MDX parser dependency is UNAVAILABLE — no structured proof
        the statement is well-formed; or
      * the structured parse reports a syntax/ERROR/MISSING node (``has_error``) —
        a malformed statement, or a construct outside the grammar's supported set.

    The regex/SQL translator runs ONLY after a clean structured parse: a malformed
    MDX statement can never reach execution via a fallback interpretation.

    The MDX grammar was fixed (Wave C, Bug-9443) so ``has_error`` is a RELIABLE
    signal: comments, ``Filter``/``Left``/``CurrentMember`` predicates, subselect
    FROM-clauses, and ``Generate``/``Ascendants`` inside WITH MEMBER/SET now parse
    cleanly, so the gate no longer rejects valid Excel/Power BI queries.

    Exemptions:
      * DAX (``EVALUATE`` / ``DEFINE``) is not MDX — the MDX grammar cannot parse
        it, so its ``has_error`` is meaningless here. The DAX translator
        (``_statement_to_sql`` → ``_dax_to_sql``) is its authority and raises its
        own client faults; DAX is admitted past the has_error gate.
      * Genuine non-MDX surfaces (empty connection handshakes, supported
        ``$SYSTEM.TMSCHEMA_*`` DMVs) are intercepted by the caller BEFORE this gate.
    """
    try:
        parsed = parse_mdx_statement(statement)
    except MDXParserUnavailableError as exc:
        raise ValueError(
            "XMLA Execute requires the structured MDX parser, but its "
            "dependencies are not available. The statement was refused rather "
            "than interpreted by a fallback translator. Install tree_sitter and "
            "ensure the MDX grammar can be built."
        ) from exc

    if re.match(r"^\s*(EVALUATE|DEFINE)\b",
                _strip_leading_mdx_comments(statement), re.IGNORECASE):
        # DAX, not MDX — admit past the MDX has_error gate; the DAX translator is
        # the authority and raises its own client faults. Leading comments are
        # stripped first so ``// x\nEVALUATE ...`` is still recognised as DAX
        # (WC3-U2).
        return parsed
    if parsed.has_error:
        raise ValueError(
            "The MDX statement could not be parsed by the structured MDX parser "
            "(syntax error or unsupported construct) and was refused. It was not "
            "interpreted by a fallback translator."
        )
    return parsed


async def _handle_execute(
    method_el: ET.Element,
    tenant_slug: str,
    jwt_token: str,
    session_id: str = "",
) -> Response:
    # Bug-5888 (fixes Bug-5436b's false-success gap): an XMLA Execute whose
    # Command is <Cancel> asks the server to abort an in-flight command on a
    # connection/session/SPID. Tessallite does not hold a server-side cursor,
    # but the in-flight query-router call for that session IS a real
    # cancellable asyncio task (registered in `_xmla_inflight_tasks` by the
    # Execute path below). Cancel now actually cancels it when one exists,
    # instead of unconditionally claiming success. This must run BEFORE
    # statement extraction: a Cancel command carries no <Statement>, so it
    # would otherwise fall into the empty-handshake path and (harmlessly but
    # incorrectly) be treated as a connection probe.
    if _is_cancel_command(method_el):
        cancelled = False
        async with _xmla_inflight_lock:
            task = _xmla_inflight_tasks.get(session_id) if session_id else None
        if task is not None and not task.done():
            task.cancel()
            cancelled = True
        logger.info(
            "xmla cancel command (session=%r): %s",
            session_id,
            "cancelled in-flight query" if cancelled else "no matching in-flight query",
        )
        return _soap_response(
            '<tns:ExecuteResponse>'
            '<return>'
            '<root xmlns="urn:schemas-microsoft-com:xml-analysis:empty"/>'
            '</return>'
            '</tns:ExecuteResponse>',
            session_id=session_id,
        )

    # Extract the DAX statement first — MSOLAP sends an empty Execute as a
    # connection handshake before any catalog is selected. Return an empty
    # success response so MSOLAP treats the connection as established.
    dax_statement = _find_command_statement(method_el)
    # Treat MDX empty-set expressions as a connection handshake — MSOLAP
    # sends "{}" or "{ }" as a schema probe before issuing real queries.
    if dax_statement and dax_statement.strip().strip("{}").strip() == "":
        dax_statement = None
    if not dax_statement:
        # MSOLAP sends an empty Execute as a connection handshake.
        # OlaPy returns a simple empty root — match exactly.
        return _soap_response(
            '<tns:ExecuteResponse>'
            '<return>'
            '<root xmlns="urn:schemas-microsoft-com:xml-analysis:empty"/>'
            '</return>'
            '</tns:ExecuteResponse>',
            session_id=session_id,
        )

    properties = _parse_properties(method_el)
    catalog = properties.get("Catalog", "")

    # Bug-5430: Power BI / Tabular clients issue DMV-style Execute statements
    # (``SELECT ... FROM $SYSTEM.TMSCHEMA_*``) to read the Tabular metadata
    # surface. Intercept before MDX translation — the parser does not
    # understand DMV SELECTs and would fault — and answer from model metadata.
    if _is_tmschema_dmv(dax_statement):
        return await _handle_tmschema_dmv(
            dax_statement, catalog, tenant_slug, jwt_token, session_id,
        )

    model_id, _project_id, persona, _exec_dvid = await _resolve_model_id(
        catalog, tenant_slug, jwt_token,
    )
    is_technical_view = _persona_includes_hidden(persona)
    persona_id = str(persona["id"]) if persona and persona.get("id") else None

    if not model_id:
        return _soap_fault(
            f"Model not found for catalog '{catalog or tenant_slug}'. "
            "Verify the model exists in the tenant.",
            "Client",
        )

    # Fetch model metadata for classifying result columns (Bug-6602: shared
    # short-TTL cache — an Execute burst reuses the Discover metadata instead of
    # re-running the hierarchy-detail N+1).
    # Bug-6628: thread persona_id so multi-persona users get correct metadata.
    try:
        measures_meta, dimensions_meta, hierarchy_defs = await _load_model_metadata_cached(
            model_id=model_id, project_id=_project_id,
            tenant_slug=tenant_slug, jwt_token=jwt_token,
            persona_id=persona_id,
            deployed_version_id=_exec_dvid,
        )
    except Exception as _exec_meta_exc:
        import httpx as _httpx
        if (
            isinstance(_exec_meta_exc, _httpx.HTTPStatusError)
            and _exec_meta_exc.response.status_code == 403
        ):
            return _soap_fault(
                "This user account has multiple personas. "
                "Connect to a persona-specific catalog "
                f"(e.g. '{catalog}_business' or '{catalog}_technical') "
                "instead of the base catalog.",
                "Client",
                status_code=403,
            )
        raise

    # Bug-5499: fetch saved named sets and inline their expressions into the
    # MDX statement BEFORE any axis extraction, SQL translation, or response
    # building. When a BI tool places a saved named set onto an axis, the MDX
    # references the set by name (e.g. `{[Top Customers]} ON ROWS`). Without
    # inlining, the axis extractors see a bare name — not a dimension or
    # measure reference — so the axis renders empty.
    execute_named_sets: list[dict[str, Any]] = []
    try:
        # Bug-6263: scope Execute-time inlining to the resolved persona so a
        # restricted persona never has a set built on a dimension it cannot see
        # inlined into its query, and so multi-persona viewers keep working
        # inlining (the unscoped call tripped a "please select one" 403 that
        # left every set un-inlined). persona_id is resolved from the catalog
        # name above.
        execute_named_sets = await get_model_named_sets(
            model_id, tenant_slug, jwt_token, project_id=_project_id,
            persona_id=persona_id,
        )
    except Exception as exc:
        # Bug-7254: fail CLOSED on a named-set fetch failure. The previous
        # behavior silently swallowed the error (DEBUG log only) and proceeded
        # with no set inlining; any query referencing a named set then returned
        # empty axes that looked like "no data" -- a silent fail-open that
        # confused users into thinking the data was missing. Re-raising makes
        # the fetch failure visible as a query error (the BI client shows a
        # server error, not a deceptive empty result). Models with NO named
        # sets return an empty list (not an error), so this only fires on
        # actual API/network failures.
        logger.error(
            "Named-set fetch failed for Execute; failing the query to avoid "
            "returning misleading empty axes (Bug-7254): %s", exc,
        )
        # Bug-8384: sending ``deployed_only=true`` made 409
        # DEPLOYED_SNAPSHOT_INVALID a reachable outcome on THIS call, where it
        # previously could not occur. A bare re-raise leaves the route as an
        # unformatted HTTP 500 (``dispatch_xmla`` only converts a downstream
        # 401), so Excel shows a generic "connection lost" carrying none of the
        # diagnosis — while a fresh Discover against the SAME broken model
        # returns a clear fault. Return the same readable fault here so the two
        # surfaces agree instead of contradicting each other.
        if getattr(getattr(exc, "response", None), "status_code", None) == 409:
            return _soap_fault(_deployed_snapshot_fault_message(catalog), "Server")
        raise
    if execute_named_sets:
        dax_statement = _inline_named_sets(dax_statement, execute_named_sets)
        logger.debug(
            "[XMLA-EXEC] inlined %d named set(s) into MDX",
            len(execute_named_sets),
        )

    (
        dim_names,
        hierarchy_level_dim_map,
        hierarchy_default_dim_map,
    ) = _build_hierarchy_dimension_map(dimensions_meta, hierarchy_defs)

    # MDX DRILLTHROUGH — route through the semantic drill-through pipeline
    # instead of the normal MDX→SQL translation. Excel sends DRILLTHROUGH
    # on double-click; the response is a flat Rowset, not MDDataSet.
    # Wave C #3: the structured MDX parser is the ADMISSION AUTHORITY. A syntax
    # error / unsupported construct (has_error) or an unavailable parser fails
    # closed here as a SOAP client fault — a malformed statement never reaches the
    # regex translator via a "fallback interpretation". Normal SELECT now follows
    # the same law DRILLTHROUGH and WITH MEMBER/SET already do.
    try:
        parsed_mdx = _parse_mdx_for_execute(dax_statement)
    except ValueError as exc:
        return _soap_fault(str(exc), "Client")

    # Wave C #11: XMLA <Parameters> → model session_vars (app.<name>). Parse the
    # scalar parameter block, reject any duplicate / malformed / table-valued /
    # expression-valued / UNDECLARED parameter as a SOAP client fault, and map each
    # declared parameter to app.<name>. The mapping is passed into EVERY query this
    # Execute generates (detail, subtotal, grand-total, secondary-grain) through
    # the single ``_execute_query`` wrapper below, so a new sub-query branch cannot
    # silently omit it. User values are NEVER substituted into the MDX text; they
    # only scope the existing row-security / default-filter resolver.
    #
    # B1 (decision #11 gap): this MUST run BEFORE the DRILLTHROUGH branch below.
    # A DRILLTHROUGH Execute is result-bearing exactly like a SELECT, so its detail
    # query has to be scoped by the same declared parameters, and a malformed /
    # duplicate / undeclared / table-valued <Parameters> on a DRILLTHROUGH must
    # FAULT here — never be silently ignored. ``_param_session_vars`` is threaded
    # into ``handle_drillthrough`` so the drill path and the MDX path share one
    # parameter contract.
    try:
        _xmla_params = _parse_xmla_parameters(method_el)
    except ValueError as exc:
        return _soap_fault(str(exc), "Client")
    _param_session_vars: dict[str, str] | None = None
    if _xmla_params:
        try:
            _declared_params = await get_model_parameters(
                model_id, tenant_slug, jwt_token, project_id=_project_id,
            )
        except Exception as exc:
            logger.error(
                "XMLA parameter validation could not load declared model "
                "parameters: %s", exc,
            )
            return _soap_fault(
                "Could not validate the supplied XMLA parameters against the model.",
                "Server",
            )
        _declared_names = {
            str(p.get("name", "")).lstrip("@").strip().lower()
            for p in _declared_params if isinstance(p, dict)
        }
        _undeclared = sorted(n for n in _xmla_params if n not in _declared_names)
        if _undeclared:
            return _soap_fault(
                "Unknown model parameter(s): " + ", ".join(_undeclared)
                + ". Only parameters declared on the model may be supplied.",
                "Client",
            )
        _param_session_vars = {f"app.{n}": v for n, v in _xmla_params.items()}

    if parsed_mdx.is_drillthrough:
        # Bug-8048: DRILLTHROUGH pagination is an OPT-IN Tessallite extension.
        # A client that wants stable pages sends the ``DrillthroughCursor``
        # Execute property — empty on the first page, then the token from the
        # previous response. Only such a client gets the continuation element
        # back, so Excel/Power BI (which never send the property) see a byte-
        # identical ExecuteResponse and no unknown sibling element that a
        # strict MSOLAP parser could reject.
        wants_cursor = _DRILLTHROUGH_CURSOR_PROPERTY in properties
        cursor = properties.get(_DRILLTHROUGH_CURSOR_PROPERTY) or None
        try:
            drill = await handle_drillthrough(
                parsed=parsed_mdx,
                tenant_slug=tenant_slug,
                jwt_token=jwt_token,
                measures_meta=measures_meta,
                dimensions_meta=dimensions_meta,
                hierarchy_defs=hierarchy_defs,
                persona_id=persona_id,
                cursor=cursor,
                session_vars=_param_session_vars,
            )
        except ValueError as exc:
            logger.warning("DRILLTHROUGH failed: %s", exc)
            return _soap_fault(str(exc), "Client")
        except Exception as exc:
            logger.error("DRILLTHROUGH error: %s", exc)
            return _soap_fault(str(exc), "Server")

        messages_xml = ""
        if drill.warnings:
            msgs = "".join(
                f'<Warning><Description>{_escape_xml(w)}</Description></Warning>'
                for w in drill.warnings
            )
            messages_xml = f"<Messages>{msgs}</Messages>"

        # The token is opaque: the client echoes it back verbatim as the next
        # request's ``DrillthroughCursor`` property. Absent element == no more
        # pages.
        cursor_xml = ""
        if wants_cursor and drill.next_cursor:
            cursor_xml = (
                f"<tns:{_DRILLTHROUGH_CURSOR_PROPERTY}>"
                f"{_escape_xml(drill.next_cursor)}"
                f"</tns:{_DRILLTHROUGH_CURSOR_PROPERTY}>"
            )

        return _soap_response(
            f'<tns:ExecuteResponse>'
            f'{drill.xml_body}{messages_xml}{cursor_xml}'
            f'</tns:ExecuteResponse>',
            session_id=session_id,
        )

    # Wave C #11 parameter parsing + validation now runs ABOVE the DRILLTHROUGH
    # branch (see the block after ``_parse_mdx_for_execute``) so both the drill
    # path and the MDX path share one parameter contract. ``_param_session_vars``
    # is already resolved here.
    async def _execute_query(**kwargs):
        # Wave C #11: the SINGLE funnel for every query this Execute generates. The
        # XMLA parameter -> session_vars mapping is injected here so no sub-query
        # branch (detail / subtotal / grand-total / secondary-grain) can omit it.
        if _param_session_vars and not kwargs.get("session_vars"):
            kwargs["session_vars"] = _param_session_vars
        return await execute_query(**kwargs)

    # Phase 5 of the semantic-layer plan: intercept MDX that only touches
    # the synthetic Info measures so the executor can return the real
    # trust values without going through the SQL router. If the user
    # dragged `[Measures].[_info_last_refreshed]` onto a pivot we don't
    # want to hand a meaningless SELECT to the query router — we want
    # the actual timestamp surfaced in a single-cell result.
    info_measure_values = await _maybe_resolve_info_measures(
        dax_statement, model_id, tenant_slug, jwt_token
    )
    if info_measure_values is not None:
        columns, rows = info_measure_values
        catalog_name = catalog or tenant_slug
        try:
            xml_body = build_real_execute_response(
                mdx=dax_statement,
                catalog=catalog_name,
                columns=columns,
                rows=rows,
                measures_meta=measures_meta + _build_trust_info_measures(),
                dimensions_meta=dimensions_meta,
                axis_format=properties.get("AxisFormat", ""),
                client_app_name=properties.get("SspropInitAppName", ""),
            )
        except ValueError as exc:
            logger.warning("Execute response build failed: %s", exc)
            return _soap_fault(str(exc), "Client")
        return _soap_response(
            f'<tns:ExecuteResponse>{xml_body}</tns:ExecuteResponse>',
            session_id=session_id,
        )

    # Bug-3657: intercept KPI member functions (KPIValue/KPIGoal/KPIStatus/
    # KPITrend) before the normal MDX→SQL translation. They resolve to the
    # KPI's published value/goal/status/trend rather than falling through to a
    # default measure and faulting. Returns None when no KPI function is present.
    try:
        # Bug-5587: pass persona measure allow-list so Execute path
        # refuses KPI member functions for restricted personas,
        # consistent with the Discover MDSCHEMA_KPIS filter.
        _persona_allow_m: set[str] | None = None
        if persona:
            _raw_ids = persona.get("included_measure_ids") or []
            _persona_allow_m = {str(x) for x in _raw_ids} or None
        kpi_cell = await _maybe_resolve_kpi_members(
            statement=dax_statement,
            model_id=model_id,
            project_id=_project_id,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            measures_meta=measures_meta,
            dimensions_meta=dimensions_meta,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
            dim_names=dim_names,
            model_slug=catalog or "",
            persona_id=persona_id,
            is_technical_view=is_technical_view,
            persona_included_measure_ids=_persona_allow_m,
        )
    except ValueError as exc:
        logger.warning("KPI member resolution failed: %s", exc)
        return _soap_fault(str(exc), "Client")
    except Exception as exc:
        logger.error("KPI member resolution error: %s", exc)
        return _soap_fault(str(exc), "Server")
    if kpi_cell is not None:
        columns, rows = kpi_cell
        catalog_name = catalog or tenant_slug
        try:
            xml_body = build_real_execute_response(
                mdx=dax_statement,
                catalog=catalog_name,
                columns=columns,
                rows=rows,
                measures_meta=measures_meta,
                dimensions_meta=dimensions_meta,
                axis_format=properties.get("AxisFormat", ""),
                client_app_name=properties.get("SspropInitAppName", ""),
            )
        except ValueError as exc:
            logger.warning("Execute response build failed: %s", exc)
            return _soap_fault(str(exc), "Client")
        return _soap_response(
            f'<tns:ExecuteResponse>{xml_body}</tns:ExecuteResponse>',
            session_id=session_id,
        )

    # Detect if this MDX requests hierarchy subtotals (.MEMBERS on a hierarchy)
    # before SQL generation so the detail query includes all hierarchy dims.
    from src.dax.subtotal_engine import (
        detect_subtotal_hierarchies as _detect_subtotals,
        build_subtotal_queries as _build_subtotal_queries,
        compute_last_non_empty_subtotals as _compute_lne_subtotals,
        merge_grain_results as _merge_grain_results,
        build_multi_subtotal_queries as _build_multi_subtotal_queries,
        compute_multi_lne_subtotals as _compute_multi_lne_subtotals,
        merge_multi_hierarchy_results as _merge_multi_hierarchy_results,
        GrainResult as _GrainResult,
        GrainQuery as _GrainQuery,
    )
    col_expr = _mdx_axis_expr(dax_statement, 0)
    row_expr = _mdx_axis_expr(dax_statement, 1)
    subtotal_hierarchies = _detect_subtotals(
        col_expr, row_expr, hierarchy_defs, hierarchy_level_dim_map,
    )

    # Bug-6888: resolve static-KPI goal support members ([Measures].[<KPI> Goal])
    # to their constant values. They are not SQL columns — drop them from SQL
    # resolution (constant_measure_names) and post-join the constants after the
    # rows return, mirroring the Info-measure contract.
    # Bug-8288 review R3 F1/F2: the KPI goal/status const blocks MUST resolve
    # against the SAME measure + KPI surface the Discover MDSCHEMA path advertises,
    # or Execute and the catalogue diverge (Bug-6702 / Bug-7227 parity). A
    # non-technical view hides is_hidden measures (so a hidden-backed KPI is withheld
    # from MDSCHEMA and must not be served here either — via a hand-written member),
    # and a measure-restricted persona sees only lineage-allowed KPIs. Both const
    # blocks below use this surface set for their visibility/collision checks and
    # persona-filter the loaded KPI list, exactly as Discover does.
    _kpi_surface_measures = (
        measures_meta if is_technical_view
        else [m for m in measures_meta if not m.get("is_hidden")]
    )

    def _persona_filter_kpis(_kpis: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if _persona_allow_m and _kpis:
            return filter_kpis_for_persona(_kpis, _kpi_surface_measures, _persona_allow_m)
        return _kpis

    kpi_goal_consts: dict[str, str] = {}
    if model_id and "Goal]" in dax_statement:
        try:
            _kpis_for_goals = await get_model_kpis(
                model_id, tenant_slug, jwt_token, project_id=_project_id,
            )
        except Exception as exc:
            logger.warning("Failed to load KPIs for goal member resolution: %s", exc)
            _kpis_for_goals = []
        # Bug-7227 parity: withhold a persona-excluded KPI's goal, same as Discover.
        _kpis_for_goals = _persona_filter_kpis(_kpis_for_goals)
        _mm_by_id = {str(m.get("id", "")): m for m in measures_meta}
        # Bug-6942 + Bug-6702 parity: collision check against the SURFACE-VISIBLE
        # measures so a hidden real measure does not diverge Execute from Discover.
        _real_measure_lower = {
            (m.get("name") or "").lower()
            for m in _kpi_surface_measures if m.get("name")
        }
        _seen_goal_supports: set[str] = set()
        for _kpi in _kpis_for_goals:
            _support = kpi_goal_support_measure_name(_kpi)
            # Bug-8288 review finding 2 (pre-existing Bug-6888 seam): match the FULL
            # advertised member unique name, not a bare ``[<support>]`` token. A bare
            # token also matches a same-named DIMENSION/hierarchy/member on an axis
            # (e.g. a dimension "aa Goal" next to a KPI "aa"), and the post-join then
            # OVERWRITES that dimension column on every row -> silently wrong numbers.
            # The unique name Excel binds is exactly ``[Measures].[<escaped support>]``.
            _goal_member = f"[Measures].[{_escape_mdx_bracket_name(_support)}]"
            if _support and _goal_member in dax_statement:
                # Bug-6942: skip constant substitution when the support name
                # collides with a real measure -- the real measure's aggregated
                # value must not be replaced with the KPI's static target.
                if _support.lower() in _real_measure_lower:
                    continue
                # Review R2 finding 1 (goal parity with the status path): two KPIs
                # sharing a caption produce the same goal support member. Refuse
                # loudly rather than silently serving the last KPI's target for both.
                if _support in _seen_goal_supports:
                    return _soap_fault(
                        f"KPI goal member '{_support}' is ambiguous: more than one "
                        "KPI resolves to this goal member. Rename the KPIs so their "
                        "captions are unique.",
                        "Client",
                    )
                _goal_val = kpi_goal_static_value(_kpi, _mm_by_id)
                if _goal_val:
                    _seen_goal_supports.add(_support)
                    kpi_goal_consts[_support] = _goal_val

    # Bug-8288: resolve synthetic governed-status support members
    # ([Measures].[<KPI> Status]) to the governed −1/0/1 RAG verdict. A native
    # Excel pivot "Status" checkbox binds this member directly (no KPIStatus()
    # token), so it would otherwise resolve the raw value member through the SQL
    # path. Like the goal constants, the member is NOT a SQL column: drop it from
    # SQL resolution (constant_measure_names) and post-join the governed verdict
    # after the rows return. The governed authority is model-wide — the single
    # /evaluate route takes no runtime slice — so a status member requested WITH a
    # dimension breakdown / slicer cannot be governed-sliced. Refuse it with a
    # clear client fault rather than repeat one model-wide verdict across every
    # slice (which would be silently wrong). A sliced governed status needs
    # model-service /evaluate slice support (Bug-8287, cross-service).
    kpi_status_consts: dict[str, int | None] = {}
    if model_id and "Status]" in dax_statement:
        try:
            _kpis_for_status = await get_model_kpis(
                model_id, tenant_slug, jwt_token, project_id=_project_id,
            )
        except Exception as exc:
            logger.warning("Failed to load KPIs for status member resolution: %s", exc)
            _kpis_for_status = []
        # Bug-7227 parity: withhold a persona-excluded KPI's status, as Discover does.
        _kpis_for_status = _persona_filter_kpis(_kpis_for_status)
        # Bug-6702 parity: collision check against the SURFACE-VISIBLE measures.
        _real_measure_lower_s = {
            (m.get("name") or "").lower()
            for m in _kpi_surface_measures if m.get("name")
        }
        _status_members: list[tuple[str, dict[str, Any]]] = []
        _seen_status_supports: set[str] = set()
        for _kpi in _kpis_for_status:
            _support = kpi_status_support_measure_name(_kpi)
            if not _support:
                continue
            # Bug-8288 review finding 1/3: match the FULL advertised member unique
            # name (``[Measures].[<escaped support>]``, exactly what MDSCHEMA_KPIS
            # advertises and Excel binds), NOT a bare ``[<support>]`` token. A bare
            # token also matches a same-named DIMENSION on an axis (e.g. a dimension
            # "aa Status" next to a KPI "aa"), which would spuriously trip the
            # dimension-breakdown fail-loud and deny an innocent pivot.
            _status_member = f"[Measures].[{_escape_mdx_bracket_name(_support)}]"
            if _status_member not in dax_statement:
                continue
            # Bug-6942 parity: never hijack a real measure of the same name.
            if _support.lower() in _real_measure_lower_s:
                continue
            # Bug-6702 parity: resolve the value against the SURFACE-VISIBLE set so a
            # hidden-backed KPI (withheld from MDSCHEMA on a non-technical view) is
            # not served here either — it fails need-support and falls through.
            if not kpi_status_needs_support_measure(_kpi, _kpi_surface_measures):
                continue
            # Review finding 4: two KPIs sharing a caption produce the same support
            # member. Refuse loudly rather than silently letting one verdict win.
            if _support in _seen_status_supports:
                return _soap_fault(
                    f"KPI status member '{_support}' is ambiguous: more than one "
                    "KPI resolves to this status member. Rename the KPIs so their "
                    "captions are unique.",
                    "Client",
                )
            _seen_status_supports.add(_support)
            _status_members.append((_support, _kpi))
        if _status_members:
            # Governed KPI status cannot produce one cell per axis member, so a
            # dimension breakdown remains unsupported. A WHERE slicer, however,
            # is a supported governed batch context (Bug-8383) when every MDX
            # member maps to a model dimension filter.
            _status_axis_dims = _mdx_extract_dimensions(
                col_expr + " " + row_expr,
                dim_names=dim_names,
                hierarchy_level_dim_map=hierarchy_level_dim_map,
                hierarchy_default_dim_map=hierarchy_default_dim_map,
            )
            _status_where = _mdx_where_expr(dax_statement)
            _status_where_filters = _mdx_extract_where_filters(
                _status_where, dim_names,
                hierarchy_level_dim_map=hierarchy_level_dim_map,
                hierarchy_default_dim_map=hierarchy_default_dim_map,
            ) if _status_where else {}
            if _status_axis_dims:
                _rep = _status_members[0][0]
                return _soap_fault(
                    f"KPI status member '{_rep}' was requested with a dimension "
                    "breakdown. Governed KPI status returns one value per KPI, so "
                    "query the status without a dimension breakdown, or use the "
                    "underlying measure.",
                    "Client",
                )
            try:
                _status_batch_filters = _translate_kpi_slicer_filters(
                    _status_where,
                    _status_where_filters,
                    dimensions_meta,
                    dim_names,
                    hierarchy_level_dim_map=hierarchy_level_dim_map,
                    hierarchy_default_dim_map=hierarchy_default_dim_map,
                )
            except ValueError as exc:
                return _soap_fault(str(exc), "Client")
            _status_batch: dict[str, dict[str, Any]] = {}
            if _status_batch_filters:
                _status_ids = [
                    str(_kpi.get("id") or "") for _support, _kpi in _status_members
                ]
                if not all(_status_ids):
                    return _soap_fault(
                        "A governed KPI status member has no id for batch "
                        "evaluation.",
                        "Client",
                    )
                try:
                    _status_batch = await evaluate_kpi_batch(
                        _status_ids,
                        model_id=model_id,
                        project_id=_project_id,
                        tenant_slug=tenant_slug,
                        jwt_token=jwt_token,
                        filters=_status_batch_filters,
                        persona_id=persona_id,
                    )
                except ValueError as exc:
                    logger.warning("KPI status batch resolution failed: %s", exc)
                    return _soap_fault(str(exc), "Client")
                except Exception as exc:
                    logger.error("KPI status batch resolution error: %s", exc)
                    return _soap_fault(str(exc), "Server")
            for _support, _kpi in _status_members:
                _kpi_id = str(_kpi.get("id") or "")
                if not _kpi_id:
                    return _soap_fault(
                        f"KPI status member '{_support}' cannot be evaluated: the "
                        "KPI has no id for the governed evaluation authority.",
                        "Client",
                    )
                try:
                    if _status_batch_filters:
                        _ev = _status_batch.get(_kpi_id)
                        if _ev is None:
                            return _soap_fault(
                                f"KPI status member '{_support}' was not returned "
                                "by governed batch evaluation; refusing to serve "
                                "an unfiltered value.",
                                "Server",
                            )
                    else:
                        _ev = await evaluate_kpi_governed(
                            kpi_id=_kpi_id,
                            model_id=model_id,
                            project_id=_project_id,
                            tenant_slug=tenant_slug,
                            jwt_token=jwt_token,
                            persona_id=persona_id,
                        )
                except ValueError as exc:
                    logger.warning("KPI status member resolution failed: %s", exc)
                    return _soap_fault(str(exc), "Client")
                except Exception as exc:
                    # R4 finding 2: a transport failure (model-service down /
                    # timeout) must degrade to a SOAP Server fault, the same as the
                    # KPI member-function path — not escape as a raw HTTP 500.
                    logger.error("KPI status member resolution error: %s", exc)
                    return _soap_fault(str(exc), "Server")
                _st = _ev.get("status")
                kpi_status_consts[_support] = int(_st) if _st is not None else None

    # Bug-8288 review R2 finding 2+3: when the statement references ONLY constant
    # members (KPI goal/status support members) and no axis dimension, every measure
    # has been dropped from SQL resolution -> _mdx_to_sql would expand to ALL model
    # measures (a needless full scan, and a spurious "LAST_NON_EMPTY requires a
    # DATE/TIME grain" fault on models carrying an LNE measure). Short-circuit to a
    # single synthetic grand-total cell — the same pattern as the info-measure and
    # KPI member-function paths — instead of inventing a measure set. This also
    # guarantees the goal + status columns are both populated on the single cell
    # even when no real measure (and thus no router row) is present.
    if kpi_goal_consts or kpi_status_consts:
        _const_supports = set(kpi_goal_consts) | set(kpi_status_consts)
        # Bug-8751 consumer alignment (deep-review R2 finding 6): the FIFTH
        # derivation of the SQL measure set. Using the raw axis extraction here
        # counted a WITH-declared calc member as a real measure, so the
        # short-circuit did not fire and _mdx_to_sql fell through to its
        # all-model-measures expansion — the exact needless full scan (and
        # spurious LNE-grain fault) this block exists to prevent. Same helper as
        # the other four consumers; it already drops the constants.
        _non_const_measures = _sql_measure_set(
            dax_statement,
            col_expr + " " + row_expr + " " + _mdx_where_expr(dax_statement),
            constant_measure_names=_const_supports,
        )
        _consts_only_axis_dims = _mdx_extract_dimensions(
            col_expr + " " + row_expr,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        if not _non_const_measures and not _consts_only_axis_dims:
            # Preserve the MDX axis order of the members for the response.
            def _member_pos(_s: str) -> int:
                _m = f"[Measures].[{_escape_mdx_bracket_name(_s)}]"
                _idx = dax_statement.find(_m)
                return _idx if _idx >= 0 else 1 << 30
            _cc_columns = sorted(_const_supports, key=_member_pos)
            _cc_row: dict[str, Any] = {}
            for _s in _cc_columns:
                if _s in kpi_status_consts:
                    _cc_row[_s] = kpi_status_consts[_s]
                else:
                    _graw = kpi_goal_consts[_s]
                    try:
                        _cc_row[_s] = float(_graw)
                    except (TypeError, ValueError):
                        _cc_row[_s] = _graw
            _cc_measures = list(measures_meta) + [
                {"name": _s, "display_name": _s, "default_agg": "max",
                 "is_hidden": False}
                for _s in _cc_columns
            ]
            try:
                xml_body = build_real_execute_response(
                    mdx=dax_statement,
                    catalog=catalog or tenant_slug,
                    columns=_cc_columns,
                    rows=[_cc_row],
                    measures_meta=_cc_measures,
                    dimensions_meta=dimensions_meta,
                    axis_format=properties.get("AxisFormat", ""),
                    client_app_name=properties.get("SspropInitAppName", ""),
                )
            except ValueError as exc:
                logger.warning("Execute response build failed: %s", exc)
                return _soap_fault(str(exc), "Client")
            return _soap_response(
                f'<tns:ExecuteResponse>{xml_body}</tns:ExecuteResponse>',
                session_id=session_id,
            )

    # Translate Execute statement (MDX or DAX) -> SQL for query-router execution.
    try:
        sql, protocol = _statement_to_sql(
            dax_statement,
            measures_meta,
            dimensions_meta,
            hierarchy_meta=hierarchy_defs,
            model_slug=catalog or "",
            subtotal_hierarchies=subtotal_hierarchies,
            constant_measure_names=set(kpi_goal_consts) | set(kpi_status_consts),
        )
    except ValueError as exc:
        logger.warning("Execute translation failed: %s", exc)
        return _soap_fault(str(exc), "Client")
    logger.info("[XMLA-EXEC] stmt=%r -> SQL=%r protocol=%s", dax_statement[:200], sql[:200], protocol)

    # Bug-5888: run the router call as a task registered under this session's
    # SessionId so a same-session <Cancel> can actually cancel it (see the
    # registry docstring above and the Cancel handling near the top of this
    # function). Sessionless requests (session_id == "") are not trackable
    # and simply run un-cancellable, as before.
    cancel_key = session_id or None
    # Bug-8285: ask the router to project friendly member captions for any axis
    # dimension that declares a distinct display column, so the Execute axis can
    # render display names instead of raw keys (the consumer,
    # _normalize_member_captions, reads the returned <dim>__caption column).
    _caption_dims = _caption_dimension_names(dimensions_meta)
    inflight = asyncio.ensure_future(
        _execute_query(
            model_id=model_id,
            sql=sql,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            protocol=protocol,
            include_hidden=is_technical_view,
            persona_id=persona_id,
            caption_dimensions=_caption_dims or None,
        )
    )
    if cancel_key:
        async with _xmla_inflight_lock:
            _xmla_inflight_tasks[cancel_key] = inflight
    try:
        result = await inflight
    except asyncio.CancelledError:
        logger.info(
            "XMLA Execute cancelled by client Cancel request (session=%r)",
            session_id,
        )
        return _soap_fault("Query cancelled by client request.", "Client")
    except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded) as exc:
        logger.warning("Bug-7745: gateway resource limit: %s", exc)
        return _soap_fault(str(exc), "Client")
    except QueryRouterError as exc:
        # Wave C #5: a 403 from the query-router execute path is an ACCESS DENIAL
        # (persona / CLS / RLS), including a query that references a CLS-blocked
        # column. Surface it as an XMLA access-denied SOAP CLIENT fault, not a
        # generic Server fault — the uniform CLS contract across surfaces (REST
        # 403 / JDBC 42501 / XMLA access-denied). Never a partial/redacted result.
        if exc.status_code == 403:
            logger.info("XMLA Execute access denied (403): %s", exc.detail)
            return _soap_fault(exc.detail, "Client", status_code=403)
        logger.error("Query execution failed: %s", exc)
        return _soap_fault(str(exc), "Server")
    except Exception as exc:
        logger.error("Query execution failed: %s", exc)
        return _soap_fault(str(exc), "Server")
    finally:
        if cancel_key:
            async with _xmla_inflight_lock:
                if _xmla_inflight_tasks.get(cancel_key) is inflight:
                    _xmla_inflight_tasks.pop(cancel_key, None)

    columns = result.get("columns", [])
    rows = result.get("rows", [])

    flat_axis_dims = _mdx_extract_dimensions(
        col_expr + " " + row_expr,
        dim_names=dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    flat_measure_canonical = {
        str(m.get("name") or "").lower(): str(m.get("name") or "")
        for m in measures_meta
        if m.get("name")
    }
    # Bug-8751 review finding 1: derive the measure set through the SAME helper
    # ``_mdx_to_sql`` used. A LAST_NON_EMPTY measure referenced only from the
    # WITH prelude makes ``_mdx_to_sql`` append a HIDDEN time grain to the GROUP
    # BY; if this consumer computes a narrower (axis-only) set it decides there
    # is no LNE measure, never collapses that grain back out, and the pivot
    # renders NO cells at all with a 200 and no fault.
    flat_mdx_measures = _sql_measure_set(
        dax_statement,
        col_expr + " " + row_expr + " " + _mdx_where_expr(dax_statement),
        constant_measure_names=set(kpi_goal_consts) | set(kpi_status_consts),
    )
    flat_lne_measures = _lne_measures_for_mdx(
        flat_mdx_measures, measures_meta, flat_measure_canonical,
    )
    hidden_lne_time_dim = _flat_lne_hidden_time_dim(
        mdx_dims=flat_axis_dims,
        lne_measures=flat_lne_measures,
        dimensions_meta=dimensions_meta,
        subtotal_hierarchies=subtotal_hierarchies,
    )
    columns, rows = _collapse_flat_lne_rows(
        columns=columns,
        rows=rows,
        hidden_time_dim=hidden_lne_time_dim,
        lne_measures=flat_lne_measures,
        measures_meta=measures_meta,
    )

    # Bug-6658 (F-002-04): "Show items with no data". SSAS renders every member
    # of an axis level — including members with zero facts — when NON EMPTY is
    # ABSENT. The fact-driven GROUP BY only returns members present in facts, so
    # without this the pivot silently drops zero-activity members (a planner
    # cannot see a product/period/entity with no activity). When an axis omits
    # NON EMPTY, fetch the member domain and union the absent members in with
    # empty (NULL) cells, making NON EMPTY an explicit PRUNING operation rather
    # than the only behaviour. Scoped to plain flat pivots (no subtotal
    # hierarchies) so the cost and cross-product stay bounded.
    if not subtotal_hierarchies:
        rows = await _restore_empty_axis_members(
            dax_statement=dax_statement,
            columns=columns,
            rows=rows,
            dimensions_meta=dimensions_meta,
            measures_meta=measures_meta,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
            model_id=model_id,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            persona_id=persona_id,
        )

    subtotal_info = None
    # Bug-6946: collect names of grain queries that fail so a SOAP <Warning>
    # can surface the degrade to the client instead of silently omitting
    # subtotal/grand-total rows.
    _failed_grain_labels: list[str] = []
    if subtotal_hierarchies and rows:
        subtotal_info = subtotal_hierarchies[0] if len(subtotal_hierarchies) == 1 else None

        mdx_dims = _mdx_extract_dimensions(
            col_expr + " " + row_expr,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        all_text = col_expr + " " + row_expr + " " + _mdx_where_expr(dax_statement)
        # Same shared derivation as the detail SQL (Bug-8751 review finding 1):
        # the GRAIN queries must project the same measure columns the detail
        # query does, or the merged result carries a measure at detail grain that
        # the subtotal rows are missing.
        mdx_measures = _sql_measure_set(
            dax_statement, all_text,
            constant_measure_names=set(kpi_goal_consts) | set(kpi_status_consts),
        )
        where_filters = _mdx_extract_where_filters(
            _mdx_where_expr(dax_statement), dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        # B8 round-3 fix (Bug-1050): merge subselect slicer filters into the
        # grain-path filters, exactly as the detail-SQL path (_mdx_to_sql)
        # and the calc-member requery path do. Excel emits subselects for
        # pivot keep-only filters; without this merge the subtotal GRAIN
        # queries ran unfiltered — filtered detail cells under an
        # unfiltered grand total in one response.
        _grain_sub_filters = _mdx_extract_subselect_filters(
            dax_statement, dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        for dim, vals in _grain_sub_filters.items():
            existing = where_filters.get(dim, [])
            for v in vals:
                if v not in existing:
                    existing.append(v)
            where_filters[dim] = existing
        # Bug-5548: an enumerated member set on the axis must restrict the
        # subtotal/grand-total GRAIN queries too, otherwise filtered detail rows
        # would sit beneath an unfiltered subtotal (the Bug-1050 class of fault).
        # Mirrors the detail-SQL merge in _mdx_to_sql; a bare .Members expansion
        # adds nothing.
        _grain_axis_filters = _mdx_extract_axis_member_filters(
            col_expr + " " + row_expr, dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        for dim, vals in _grain_axis_filters.items():
            existing = where_filters.get(dim, [])
            for v in vals:
                if v not in existing:
                    existing.append(v)
            where_filters[dim] = existing
        measure_canonical: dict[str, str] = {}
        for m in measures_meta:
            mname = m.get("name", "")
            if mname:
                measure_canonical[mname.lower()] = mname

        _subtotal_dim_types = _dim_type_map_from_meta(dimensions_meta)
        where_sql = _build_where_sql_clauses(
            where_filters, lambda n: _qi("postgresql", n),
            dim_type_map=_subtotal_dim_types,
        )
        # F-002-02: the subtotal / grand-total GRAIN queries must honour the
        # same label filters (Begins/Ends-With, Contains) the detail SQL path
        # applies — otherwise an expanded pivot with a label filter shows
        # filtered detail rows beneath an unfiltered subtotal/grand total.
        # Subselect slicers are already merged above (Bug-1050); label filters
        # live in the axis expressions, not the WHERE clause, so extract them
        # from col_expr + row_expr here.
        # Bug-8925/Bug-8926: the SAME translator as the detail-SQL path, so a
        # label-filter shape supported by one path can never be silently ignored
        # by another. The rendered clause is reused verbatim.
        _subtotal_label_filters = _translate_label_filter_calls(
            col_expr + " " + row_expr,
            dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
            quote_fn=lambda n: _qi("postgresql", n),
        )
        for _lf in _subtotal_label_filters:
            where_sql.append(_lf.sql_clause)

        # F-002-01: a Top-N (TopCount/BottomCount) pivot applies its ranking as an
        # ``ORDER BY <measure> ... LIMIT N`` on the DETAIL query only. The subtotal
        # / grand-total grain queries below are built from ``where_sql`` and would
        # otherwise aggregate every member, so a Top-5 pivot showing five rows can
        # print a grand total that includes the hidden sixth-and-later members — a
        # silently wrong board-pack number. Resolve the ranked member set ONCE from
        # the detail result (already limited to the survivors) and constrain every
        # grain query to exactly those surviving detail-grain member tuples, so the
        # visible members and every subtotal / grand total agree.
        _topn_axis_text = col_expr + " " + row_expr
        _subtotal_topn = _extract_topn_spec(_topn_axis_text)
        if _subtotal_topn is not None:
            # Constrain by the FULL detail grain: the detail query's LIMIT N was
            # applied to the hierarchy-EXPANDED grain (all levels), so the
            # surviving member set is the set of distinct dimension-column tuples
            # actually present in the detail result. Derive the grain columns from
            # the result columns (every dimension column the rows carry), not from
            # the un-expanded axis dims, so a Top-N over an expanded hierarchy is
            # constrained at the same grain the survivors were ranked at.
            _topn_grain_cols = [c for c in columns if c in dim_names]
            _topn_pred = _topn_member_predicate(
                detail_rows=rows,
                grain_dim_cols=_topn_grain_cols,
                quote_fn=lambda n: _qi("postgresql", n),
                dim_type_map=_subtotal_dim_types,
            )
            if _topn_pred is not None:
                where_sql.append(_topn_pred)
            elif _topn_grain_cols:
                # Fail loud rather than emit unconstrained subtotals: a Top-N pivot
                # whose surviving member set cannot be resolved from the detail
                # result must not silently fall back to an all-member grand total.
                logger.warning(
                    "[XMLA-TOPN] could not resolve Top-N member set for grain "
                    "cols=%s (rows=%d); subtotals would be unconstrained",
                    _topn_grain_cols, len(rows),
                )
                return _soap_fault(
                    "Top-N pivot subtotals could not be constrained to the "
                    "ranked member set; refusing to return a grand total that "
                    "may include hidden members.",
                    "Server",
                )

        lne_measures = [
            m.get("name", "") for m in measures_meta
            if m.get("semi_additive_behavior") == "last_non_empty"
            and m.get("name", "") in {measure_canonical.get(ms.lower(), ms) for ms in mdx_measures}
        ]

        if len(subtotal_hierarchies) == 1:
            hierarchy = subtotal_hierarchies[0]
            subtotal_queries = _build_subtotal_queries(
                mdx_dims=mdx_dims,
                mdx_measures=mdx_measures,
                where_sql_clauses=where_sql,
                model_slug=catalog or "",
                measures_meta=measures_meta,
                hierarchy=hierarchy,
                measure_canonical=measure_canonical,
            )

            subtotal_results: list[_GrainResult] = []
            for sq in subtotal_queries:
                if not sq.sql:
                    continue
                try:
                    sr = await _execute_query(
                        model_id=model_id,
                        sql=sq.sql,
                        tenant_slug=tenant_slug,
                        jwt_token=jwt_token,
                        protocol=sq.protocol,
                        include_hidden=is_technical_view,
                        persona_id=persona_id,
                    )
                    subtotal_results.append(_GrainResult(
                        query=sq,
                        columns=sr.get("columns", []),
                        rows=sr.get("rows", []),
                    ))
                except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded):
                    raise  # Bug-7745: resource limits must propagate fail-closed
                except Exception as exc:
                    logger.warning("Subtotal query failed (grain=%s): %s", sq.level_name, exc)
                    _failed_grain_labels.append(sq.level_name)

            lne_overrides = {}
            if lne_measures:
                hier_dim_names = {lvl.dim_name for lvl in hierarchy.levels}
                non_hier = [d for d in mdx_dims if d not in hier_dim_names]
                lne_overrides = _compute_lne_subtotals(
                    rows, hierarchy, lne_measures, non_hier_dims=non_hier,
                )

            hier_dim_set = {lvl.dim_name for lvl in hierarchy.levels}
            detail_dim_cols = [c for c in columns if c not in hier_dim_set] + [
                lvl.dim_name for lvl in hierarchy.levels if lvl.dim_name in columns
            ]
            detail_grain = _GrainQuery(
                sql=sql, protocol=protocol,
                grain_ordinal=hierarchy.levels[-1].ordinal if hierarchy.levels else 999,
                level_name="detail", dim_cols=detail_dim_cols,
            )
            detail_result = _GrainResult(
                query=detail_grain, columns=columns, rows=rows,
            )

            columns, rows = _merge_grain_results(
                detail_result, subtotal_results, hierarchy,
                lne_overrides=lne_overrides,
            )

            logger.info(
                "[XMLA-SUBTOTAL] hierarchy=%s levels=%d subtotal_queries=%d merged_rows=%d",
                hierarchy.hierarchy_name, len(hierarchy.levels),
                len(subtotal_results), len(rows),
            )
        else:
            subtotal_queries = _build_multi_subtotal_queries(
                mdx_dims=mdx_dims,
                mdx_measures=mdx_measures,
                where_sql_clauses=where_sql,
                model_slug=catalog or "",
                measures_meta=measures_meta,
                hierarchies=subtotal_hierarchies,
                measure_canonical=measure_canonical,
            )

            async def _exec_grain(sq: _GrainQuery) -> _GrainResult | None:
                if not sq.sql:
                    return None
                try:
                    sr = await _execute_query(
                        model_id=model_id,
                        sql=sq.sql,
                        tenant_slug=tenant_slug,
                        jwt_token=jwt_token,
                        protocol=sq.protocol,
                        include_hidden=is_technical_view,
                        persona_id=persona_id,
                    )
                    return _GrainResult(
                        query=sq,
                        columns=sr.get("columns", []),
                        rows=sr.get("rows", []),
                    )
                except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded):
                    raise  # Bug-7745: resource limits must propagate fail-closed
                except Exception as exc:
                    logger.warning(
                        "Multi-subtotal query failed (grain=%s): %s",
                        sq.level_name, exc,
                    )
                    _failed_grain_labels.append(sq.level_name)
                    return None

            grain_results = await _gather_bounded(
                [lambda sq=sq: _exec_grain(sq) for sq in subtotal_queries],
                _subtotal_grain_concurrency(),
            )
            subtotal_results = [r for r in grain_results if r is not None]

            lne_overrides_multi = {}
            if lne_measures:
                lne_overrides_multi = _compute_multi_lne_subtotals(
                    rows, subtotal_hierarchies, lne_measures,
                    subtotal_queries=subtotal_queries,
                )

            all_hier_dims: set[str] = set()
            for h in subtotal_hierarchies:
                for lvl in h.levels:
                    all_hier_dims.add(lvl.dim_name)
            detail_dim_cols = (
                [c for c in columns if c not in all_hier_dims]
                + [
                    lvl.dim_name
                    for h in subtotal_hierarchies
                    for lvl in h.levels
                    if lvl.dim_name in columns
                ]
            )
            detail_grain = _GrainQuery(
                sql=sql, protocol=protocol,
                grain_ordinal=sum(
                    h.levels[-1].ordinal for h in subtotal_hierarchies
                ),
                level_name="detail", dim_cols=detail_dim_cols,
            )
            detail_result = _GrainResult(
                query=detail_grain, columns=columns, rows=rows,
            )

            columns, rows = _merge_multi_hierarchy_results(
                detail_result, subtotal_results, subtotal_hierarchies,
                lne_overrides=lne_overrides_multi,
            )

            logger.info(
                "[XMLA-SUBTOTAL-MULTI] hierarchies=%d subtotal_queries=%d merged_rows=%d",
                len(subtotal_hierarchies),
                len(subtotal_results), len(rows),
            )

    axis_aliases = _extract_axis_hierarchy_dimension_aliases(
        dax_statement,
        dim_names=dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    columns, rows, dimensions_meta = _alias_result_dimensions_for_hierarchy_axes(
        columns=columns,
        rows=rows,
        dimensions_meta=dimensions_meta,
        alias_to_source=axis_aliases,
    )

    # F-002-13: a pivot that mixes a real measure with an info/trust measure
    # (e.g. [Measures].[Sales] and [Measures].[Last Refreshed]) cannot resolve
    # the info measure via the SQL router — it is not a real column, so the
    # mixed query ran the SQL for the real measure and left the info column
    # blank with no warning. Post-join the constant trust value onto every
    # result row so the column is populated (the info-only case is still
    # short-circuited above by _maybe_resolve_info_measures).
    info_refs = _referenced_info_measures(dax_statement)

    # Bug-8381 [silent wrong numbers]: EVERY post-joined constant below is
    # written by keying a row column on a NAME. If the router already returned a
    # column of that name, the write DESTROYS it on every row. One guard, run
    # once, before any of the three writers (info measures, KPI goal, KPI
    # status) touches ``rows`` -- so it sees the router's columns and not our
    # own additions, and so a new constant writer added later inherits it.
    #
    # ``columns`` is the authoritative answer to "what did this query actually
    # project?": it needs no re-derivation of the axis/slicer dimension set and
    # it covers every column source, not just the ones a detector knows to look
    # for. The earlier collision guards compare only against MEASURE names, and
    # a support member is deliberately dropped from SQL resolution, so any
    # same-named column coming back from the router belongs to something else --
    # a dimension, an attribute, a caption. There is no correct value to put in
    # one column for both, so refuse rather than overwrite.
    def _const_column_collision(const_name: str) -> str | None:
        target = const_name.strip().lower()
        for existing in columns:
            if str(existing).strip().lower() == target:
                return str(existing)
        return None

    for _const_name in (
        list(info_refs) + list(kpi_goal_consts) + list(kpi_status_consts)
    ):
        _clash = _const_column_collision(_const_name)
        if _clash is not None:
            return _soap_fault(
                f"Constant member '{_const_name}' collides with the column "
                f"'{_clash}' this query already returns (a dimension or "
                "attribute of the same name). The constant would overwrite that "
                "column on every row. Rename the KPI or the dimension, or query "
                "them separately.",
                "Client",
            )

    if info_refs:
        info_vals = await _fetch_trust_values(model_id, tenant_slug, jwt_token)
        for internal in info_refs:
            if internal not in columns:
                columns = list(columns) + [internal]
            const_val = info_vals.get(internal, "")
            for r in rows:
                r[internal] = const_val
        measures_meta = measures_meta + [
            m for m in _build_trust_info_measures()
            if m.get("name") in info_refs
        ]

    # Bug-6888: post-join static KPI goal constants (same contract as the info
    # measures above — the member was dropped from SQL resolution and its
    # constant value fills the column on every row).
    if kpi_goal_consts:
        for _support, _goal_raw in kpi_goal_consts.items():
            try:
                _goal_val: Any = float(_goal_raw)
            except (TypeError, ValueError):
                _goal_val = _goal_raw
            if _support not in columns:
                columns = list(columns) + [_support]
            for r in rows:
                r[_support] = _goal_val
        measures_meta = measures_meta + [
            {
                "name": _support,
                "display_name": _support,
                "default_agg": "max",
                "is_hidden": False,
            }
            for _support in kpi_goal_consts
        ]

    # Bug-8288: post-join governed KPI status constants (same contract as the goal
    # constants above — the synthetic status member was dropped from SQL resolution
    # and its governed −1/0/1 verdict fills the column on every row of the single-
    # cell result). No-data / no-target yields None, which stays blank rather than
    # reading as a 0 verdict.
    if kpi_status_consts:
        # A status-only single-cell query (Excel "Status" ticked, no real measure)
        # drops every measure to a constant, so the router may return no rows. The
        # governed status is model-wide (independent of pivot facts) and there is no
        # dimension breakdown here (refused above), so a single synthetic cell is
        # the correct grand-total shape.
        if not rows:
            rows = [{}]
        for _support, _st_val in kpi_status_consts.items():
            # Bug-8381: the collision guard above already refused any support
            # name the router returned a column for, so this append can never
            # shadow a real column. The ``not in`` check remains only to stop
            # two CONSTANT writers that happen to share a name (the guard runs
            # once, before all three, so it cannot see our own appends) from
            # adding the column twice.
            if _support not in columns:
                columns = list(columns) + [_support]
            for r in rows:
                r[_support] = _st_val
        measures_meta = measures_meta + [
            {
                "name": _support,
                "display_name": _support,
                "default_agg": "max",
                "is_hidden": False,
            }
            for _support in kpi_status_consts
        ]

    catalog_name = catalog or tenant_slug

    # Pre-compute re-query results for non-composable aggregations (Bug-575)
    requery_results: dict[tuple, Any] | None = None
    denom_requery_results: dict[tuple, Any] | None = None
    # F-002-03: a re-query is REQUIRED to render its cell correctly. If it fails
    # for any reason other than a resource limit, we must fail the Execute rather
    # than let the evaluator paint a swallowed ``None`` as legitimate no-data
    # (which shows a blank cell that looks like a real zero/absent value while
    # the leaf rows look fine — a silent wrong-number defect). This list collects
    # the (calc, measure) of every failed required re-query; a non-empty list
    # raises a client fault below.
    _rq_failures: list[str] = []
    # R1 finding 1: True once re-query specs are planned, so a block-level
    # exception (which leaves _rq_failures empty) still fails closed instead of
    # rendering a blank/mis-aggregated cell.
    _rq_planned = False
    try:
        # F-002-08: reuse the parse computed at the top of _handle_execute
        # rather than parsing the same statement a second time.
        if parsed_mdx.with_members:
            from src.dax.mdx_calc_members import (
                parse_calc_members as _parse_cm,
                plan_aggregate_requeried as _plan_rq,
                build_requery_sql as _build_rq_sql,
                plan_denominator_requeried as _plan_denom_rq,
                build_denominator_requery_sql as _build_denom_rq_sql,
            )
            dim_names_set = {(d.get("name") or "") for d in (dimensions_meta or [])}
            _dim_cols = [c for c in columns if c in dim_names_set]
            _cms = _parse_cm(parsed_mdx.with_members)
            _rq_axis_text = (
                _mdx_axis_expr(dax_statement, 0) + " "
                + _mdx_axis_expr(dax_statement, 1) + " "
                + _mdx_where_expr(dax_statement)
            )
            # Bug-8751 consumer alignment (deep-review R2 finding 1): this is the
            # FOURTH derivation of "which measures does this statement need", and
            # it gates the custom-group re-query
            # (``plan_aggregate_requeried(queried_measures=...)`` ->
            # ``measure_names & queried_measures``). Before Bug-8751 every
            # projected measure appeared on an axis, so the axis text was a valid
            # proxy; now a calc member's NON-ADDITIVE input measure can be
            # referenced only from the WITH prelude. Deriving this set from the
            # axis alone then plans NO re-query for it, ``_eval_aggregate_set``
            # writes ``None`` into the custom-group row, and the group cell
            # renders blank in Excel with a 200 and no warning. Same shared helper
            # as the other three consumers.
            _rq_queried = set(_sql_measure_set(
                dax_statement, _rq_axis_text,
                constant_measure_names=set(kpi_goal_consts) | set(kpi_status_consts),
            ))
            # R3 adversarial finding F3: mark planned BEFORE invoking either
            # planner whenever any calc member could require a re-query
            # (aggregate custom group, or % of Grand Total / Parent over a
            # potentially non-additive measure). A raise inside EITHER planner
            # then still fails closed via the block-level except reading
            # _rq_planned — closing the denom-only fail-open where the aggregate
            # planner returned empty and the denom planner raised.
            if any(
                c.calc_type in (
                    "aggregate_set", "pct_grand_total", "pct_parent",
                    "pct_row_total", "pct_col_total", "pct_axis_total",
                )
                for c in _cms
            ):
                _rq_planned = True
            # Bug-8206: row/col axis dim split for % of Row/Column Total denominator
            # re-queries. A dim is on the column axis if its bracketed name appears
            # in the ON COLUMNS expr, on the row axis if in ON ROWS.
            _rq_col_axis_expr = _mdx_axis_expr(dax_statement, 0)
            _rq_row_axis_expr = _mdx_axis_expr(dax_statement, 1)
            _rq_row_axis_dims: list[str] = []
            _rq_col_axis_dims: list[str] = []
            for _dc in _dim_cols:
                # Opus R1 F4: escape ``]`` in dim names (``]]`` in MDX brackets)
                # to match the evaluator's ``_axis_dim_split`` exactly.
                _tok = f"[{_dc.replace(']', ']]')}]"
                _in_col = _tok in _rq_col_axis_expr
                _in_row = _tok in _rq_row_axis_expr
                if _in_col and not _in_row:
                    _rq_col_axis_dims.append(_dc)
                elif _in_row and not _in_col:
                    _rq_row_axis_dims.append(_dc)
            specs = _plan_rq(_cms, measures_meta, catalog or "", _dim_cols, rows, queried_measures=_rq_queried or None)
            # F-002-04: denominator re-queries for non-additive % of Grand Total /
            # Parent members (sum-of-averages is mathematically wrong).
            # Bug-8206: same for non-additive % of Row/Column Total.
            denom_specs = _plan_denom_rq(
                _cms, measures_meta, catalog or "", _dim_cols, rows,
                row_axis_dims=_rq_row_axis_dims,
                col_axis_dims=_rq_col_axis_dims,
            )
            # Bug-8327: the partition pins the planners just built carry POST-alias
            # axis identifiers; translate them alias->source before the re-query
            # SQL is built, so an aliased pivot's re-query binds (mirrors the
            # Bug-8283 survivor-predicate translation below).
            _translate_requery_partition_identifiers(
                agg_specs=specs, denom_specs=denom_specs, axis_aliases=axis_aliases,
            )
            if specs or denom_specs:
                _rq_planned = True

                # Bug-582/587: include MDX WHERE + subselect slicer filters
                _rq_where_expr = _mdx_where_expr(dax_statement)
                _rq_where_filters = _mdx_extract_where_filters(
                    _rq_where_expr, dim_names,
                    hierarchy_level_dim_map=hierarchy_level_dim_map,
                    hierarchy_default_dim_map=hierarchy_default_dim_map,
                ) if _rq_where_expr else {}
                _rq_sub_filters = _mdx_extract_subselect_filters(
                    dax_statement, dim_names,
                    hierarchy_level_dim_map=hierarchy_level_dim_map,
                    hierarchy_default_dim_map=hierarchy_default_dim_map,
                )
                for dim, vals in _rq_sub_filters.items():
                    existing = _rq_where_filters.get(dim, [])
                    for v in vals:
                        if v not in existing:
                            existing.append(v)
                    _rq_where_filters[dim] = existing
                # Bug-8272: an enumerated member set on ROWS/COLUMNS
                # (e.g. ``{[Product].&[A],[Product].&[B]}``) restricts the main
                # detail SQL (Bug-5548) and the subtotal/grain queries
                # (Bug-5548 grain merge) to exactly those members, but the
                # calc-member DENOMINATOR / aggregate-set re-queries were built
                # only from the WHERE/subselect/label slicers above — never the
                # axis member set. A keep-only axis selection therefore made the
                # denominator (% of Grand/Parent/Row/Column Total) aggregate over
                # the FULL unfiltered level, silently returning a wrong ratio
                # (the numerator is over the two kept members, the denominator
                # over all members). Merge the enumerated axis members in here
                # with the SAME pattern as the main-SQL / grain paths so every
                # re-query family (pct_grand_total / pct_parent denom AND the
                # aggregate_set custom-group total) is scoped to the kept members.
                # A bare ``.Members`` / ``.Children`` / ``.AllMembers`` expansion
                # adds nothing (exclude_level_expansions), so a full-level pivot
                # is unaffected.
                _rq_axis_member_filters = _mdx_extract_axis_member_filters(
                    _mdx_axis_expr(dax_statement, 0) + " "
                    + _mdx_axis_expr(dax_statement, 1),
                    dim_names,
                    hierarchy_level_dim_map=hierarchy_level_dim_map,
                    hierarchy_default_dim_map=hierarchy_default_dim_map,
                )
                for dim, vals in _rq_axis_member_filters.items():
                    existing = _rq_where_filters.get(dim, [])
                    for v in vals:
                        if v not in existing:
                            existing.append(v)
                    _rq_where_filters[dim] = existing
                _rq_where_sql = _build_where_sql_clauses(
                    _rq_where_filters, lambda n: _qi("postgresql", n),
                    dim_type_map=_dim_type_map_from_meta(dimensions_meta),
                ) if _rq_where_filters else []

                # Bug-5191: propagate label filters (Begins/Ends-With,
                # Contains) into the AVG/COUNT_DISTINCT re-query so the
                # re-query is filtered identically to the main SQL path.
                # Previously label_filter_specs were extracted (~2895)
                # but never forwarded here, causing re-queries to ignore
                # the active label filter and return unfiltered
                # aggregates.
                # Bug-8925/Bug-8926: "identically" is now structural — this is
                # the SAME translator the detail-SQL path used, producing the
                # same rendered clause. A second, differently-shaped extraction
                # here is what would let the main query gain a supported variant
                # the re-query silently drops.
                _rq_label_filters = _translate_label_filter_calls(
                    _mdx_axis_expr(dax_statement, 0) + " "
                    + _mdx_axis_expr(dax_statement, 1),
                    dim_names,
                    hierarchy_level_dim_map=hierarchy_level_dim_map,
                    hierarchy_default_dim_map=hierarchy_default_dim_map,
                    quote_fn=lambda n: _qi("postgresql", n),
                )
                for _lf in _rq_label_filters:
                    _rq_where_sql.append(_lf.sql_clause)

                # Bug-8283: a calculated-member denominator/aggregate re-query on
                # a Top-N (TopCount/BottomCount) pivot MUST be constrained to the
                # surviving ranked member set, exactly as the subtotal/grand-total
                # GRAIN queries are (F-002-01, ~2596). The detail SQL applies the
                # Top-N as ``ORDER BY <measure> ... LIMIT N`` on the detail query
                # only; the re-query denominator is built independently from the
                # WHERE/subselect/label slicers above and would otherwise aggregate
                # ALL members, over-counting the denominator over hidden members.
                #
                # This re-query path only fires for a NON-ADDITIVE base measure
                # (avg / count_distinct / min / max — ``plan_denominator_requeried``
                # skips additive sum/count, whose grand total is the sum of the
                # already-displayed survivor cells and is therefore correct without
                # this fix) and for the aggregate_set custom-group total. Concrete
                # wrong number for an AVG-Sales "% of Grand Total": a Top-5 pivot
                # showing {100,90,80,70,60} (hidden 6th=50) re-aggregates the
                # denominator from fact grain; unconstrained it averages all six
                # (avg 75 over 6) instead of the five survivors — a silently wrong
                # board-pack percentage. (The 100/400=25% vs 100/450=22.2%
                # illustration is the additive analogue of the same over-count.)
                # Every denominator kind that re-queries (pct_grand_total,
                # pct_parent, pct_axis_total) AND the aggregate_set custom-group
                # total flow through ``specs`` / ``denom_specs`` and receive
                # ``_rq_where_sql`` below, so appending the ranked member-set
                # predicate here scopes them all to the surviving members. This
                # block runs whether or not the pivot also has a subtotal hierarchy
                # — a flat Top-N pivot with a Show-Values-As member never enters the
                # subtotal block that resolves the grain predicate, so it must be
                # resolved here independently.
                _rq_topn_spec = _extract_topn_spec(
                    _mdx_axis_expr(dax_statement, 0) + " "
                    + _mdx_axis_expr(dax_statement, 1)
                )
                if _rq_topn_spec is not None and rows:
                    # Constrain by the FULL detail grain actually present in the
                    # result rows (mirrors the grain-query path at ~2615): the
                    # surviving member set is the distinct tuples of dimension
                    # columns carried by the detail rows. The derivation (detail-
                    # only filter + post-alias grain cols + source-name translation
                    # + predicate build) is a PURE function so it can be pinned by a
                    # revert-guarding unit test on merged/aliased inputs.
                    _rq_topn_pred, _rq_topn_grain_cols = (
                        _topn_requery_survivor_predicate(
                            rows=rows,
                            columns=columns,
                            dim_names_set=dim_names_set,
                            axis_aliases=axis_aliases,
                            dimensions_meta=dimensions_meta,
                        )
                    )
                    if _rq_topn_pred is not None:
                        _rq_where_sql.append(_rq_topn_pred)
                    elif _rq_topn_grain_cols:
                        # Fail loud rather than emit an all-member denominator: a
                        # Top-N pivot whose surviving member set cannot be resolved
                        # from the detail result must not silently produce a
                        # denominator over every member (a wrong board-pack %).
                        logger.warning(
                            "[XMLA-TOPN] could not resolve Top-N member set for "
                            "calc-member re-query denominator (grain cols=%s, "
                            "rows=%d); denominator would be unconstrained",
                            _rq_topn_grain_cols, len(rows),
                        )
                        return _soap_fault(
                            "Top-N pivot calculated-member denominator could not "
                            "be constrained to the ranked member set; refusing to "
                            "return a percentage computed over hidden members.",
                            "Server",
                        )

                for sp in specs:
                    sp.extra_where = _rq_where_sql
                for sp in denom_specs:
                    # The denominator re-query is scoped to the same MDX WHERE /
                    # subselect / label slicer as the main query so the total is
                    # over exactly the rows the pivot shows. Its own partition
                    # filters (parent-dim pins) are appended by the SQL builder.
                    sp.extra_where = list(_rq_where_sql)

                requery_results = {}
                denom_requery_results = {}

                # F-002-03: sentinel distinguishing a re-query FAILURE (fail
                # closed) from a legitimately empty result (value stays None but
                # is not a failure). ``_FAIL`` is only ever produced by a caught
                # non-resource exception on a required re-query.
                _FAIL = object()

                async def _exec_one(build_sql, calc_name, measure_name, part_key):
                    # R1 finding 1: build the SQL INSIDE the try so a builder
                    # exception is captured by the _FAIL sentinel too, instead of
                    # escaping to the block-level swallow (which would render a
                    # blank/wrong cell with _rq_failures empty).
                    try:
                        rq_sql = build_sql()
                        rq_result = await _execute_query(
                            sql=rq_sql, model_id=model_id or "",
                            tenant_slug=tenant_slug, jwt_token=jwt_token,
                            protocol="jdbc",
                            include_hidden=is_technical_view,
                            persona_id=persona_id,
                        )
                        rq_rows = rq_result.get("rows", [])
                        if rq_rows:
                            return (calc_name, measure_name, part_key), rq_rows[0].get(measure_name)
                        # Empty result: a real (non-error) absence.
                        return (calc_name, measure_name, part_key), None
                    except (QueryByteCeilingExceeded, GatewayQueryRateLimitExceeded):
                        # Bug-7745: resource limits fail closed. R1 finding 1:
                        # do NOT re-raise — a raise escapes _gather_bounded to the
                        # block-level swallow, leaving _rq_failures empty and the
                        # Execute rendering anyway. Return _FAIL so it faults.
                        logger.warning(
                            "Re-query for %s/%s hit a resource limit; failing closed.",
                            calc_name, measure_name,
                        )
                        return (calc_name, measure_name, part_key), _FAIL
                    except Exception as exc:
                        # F-002-03: a required re-query failed. Record the fault
                        # so the Execute fails closed instead of rendering blank.
                        logger.warning(
                            "Re-query for %s/%s failed: %s", calc_name, measure_name, exc,
                        )
                        return (calc_name, measure_name, part_key), _FAIL

                async def _exec_requery(sp):
                    return await _exec_one(
                        lambda: _build_rq_sql(sp), sp.calc_name, sp.measure_name, sp.partition_key,
                    )

                async def _exec_denom_requery(sp):
                    return await _exec_one(
                        lambda: _build_denom_rq_sql(sp), sp.calc_name, sp.measure_name, sp.partition_key,
                    )

                rq_results_list = await _gather_bounded(
                    [lambda sp=sp: _exec_requery(sp) for sp in specs]
                    + [lambda sp=sp: _exec_denom_requery(sp) for sp in denom_specs],
                    _subtotal_grain_concurrency(),
                )
                _denom_keys = {
                    (sp.calc_name, sp.measure_name, sp.partition_key)
                    for sp in denom_specs
                }
                for key, val in rq_results_list:
                    if val is _FAIL:
                        _rq_failures.append(f"{key[0]}/{key[1]}")
                        continue
                    if val is None:
                        continue
                    if key in _denom_keys:
                        # Re-key to (calc_name, partition_key) for the evaluator.
                        denom_requery_results[(key[0], key[2])] = val
                    else:
                        requery_results[key] = val
                if not requery_results:
                    requery_results = None
                if not denom_requery_results:
                    denom_requery_results = None
    except Exception as exc:
        # R1 finding 1: a re-query was planned (calc members present) but the
        # pre-computation block raised. This is NOT a safe no-op — the required
        # aggregate/denominator values are missing, so rendering anyway would
        # emit a blank aggregate cell (F-002-03) or a sum-of-cells non-additive
        # denominator (F-002-04). Fail closed.
        logger.warning("Re-query pre-computation failed: %s", exc)
        if _rq_planned:
            return _soap_fault(
                "Calculated-member re-query pre-computation failed; refusing to "
                "return a result with a silently blank or mis-aggregated cell. "
                "Please retry.",
                "Server",
            )

    # F-002-03: fail the Execute closed when a required re-query failed, rather
    # than letting the evaluator render a swallowed None as legitimate no-data.
    if _rq_failures:
        return _soap_fault(
            "A calculated-member re-query failed for "
            f"{', '.join(sorted(set(_rq_failures)))}; refusing to return a result "
            "with a silently blank aggregate cell. Please retry.",
            "Server",
        )

    # F-002-10: the CubeInfo LastDataUpdate must reflect the model's real data
    # refresh time (trust_meta.last_refreshed_at), not a static system config
    # stamp, so Excel / Power BI do not treat stale (aggregate-served) pivots as
    # fresh. A trust-lookup failure degrades to the system stamp (best effort).
    _last_data_update = ""
    try:
        if model_id:
            _trust_vals = await _fetch_trust_values(model_id, tenant_slug, jwt_token)
            _last_data_update = _trust_vals.get("_info_last_refreshed", "") or ""
    except Exception as exc:
        logger.warning("CubeInfo last-refresh lookup failed: %s", exc)

    # Use MDDataSet format (required by MSOLAP/Excel for Execute responses)
    try:
        xml_body = build_real_execute_response(
            mdx=dax_statement,
            catalog=catalog_name,
            columns=columns,
            rows=rows,
            measures_meta=measures_meta,
            dimensions_meta=dimensions_meta,
            axis_format=properties.get("AxisFormat", ""),
            client_app_name=properties.get("SspropInitAppName", ""),
            subtotal_hierarchy=subtotal_info,
            requery_results=requery_results,
            subtotal_hierarchies=subtotal_hierarchies if subtotal_hierarchies and len(subtotal_hierarchies) > 1 else None,
            hierarchy_defs=hierarchy_defs,
            denom_requery_results=denom_requery_results,
            last_data_update=_last_data_update,
        )
    except ValueError as exc:
        logger.warning("Execute response build failed: %s", exc)
        return _soap_fault(str(exc), "Client")
    # Execute response body not logged by default — may contain tabular
    # query results. Summary only; set DEBUG_XMLA_RAW=1 to dump the body.
    if os.environ.get("DEBUG_XMLA_RAW") == "1":
        logger.debug(
            "xmla execute response: columns=%r rows=%d body=%r",
            columns,
            len(rows),
            xml_body[:12000],
        )
    else:
        logger.debug("xmla execute response: columns=%r rows=%d", columns, len(rows))

    # Bug-6946: surface failed subtotal/grand-total grain queries as a SOAP
    # <Warning> so the client sees that some aggregation levels are missing,
    # rather than rendering silently incomplete totals.
    _all_warnings = [
        "Subtotal grain query failed for level: " + lbl
        for lbl in _failed_grain_labels
    ]
    _grain_messages_xml = ""
    if _all_warnings:
        _grain_msgs = "".join(
            f'<Warning><Description>{_escape_xml(w)}</Description></Warning>'
            for w in _all_warnings
        )
        _grain_messages_xml = f"<Messages>{_grain_msgs}</Messages>"

    return _soap_response(
        f'<tns:ExecuteResponse>{xml_body}{_grain_messages_xml}</tns:ExecuteResponse>',
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_model_id(
    catalog: str,
    tenant_slug: str,
    jwt_token: str,
) -> tuple[Optional[str], str, Optional[dict[str, Any]], Optional[str]]:
    """
    Resolve a catalog name to a ``(model_id, project_id, persona,
    deployed_version_id)`` tuple.

    The catalog name is one of:
    - ``<uuid>`` — direct model id (persona is always None).
    - ``<slug>`` — business base view; persona is None.
    - ``<slug>_<persona.slug>`` — persona-bound catalog; persona is the
      full dict fetched from model-service (id, slug, name,
      included_*_ids, includes_hidden_columns, ...).

    Bug-7959: ``deployed_version_id`` is returned so callers can pin
    catalogue metadata (effective descriptions) to the deployed snapshot.

    Returns ``(None, "", None, None)`` when the catalog cannot be matched.
    """
    if not catalog:
        return None, "", None, None

    is_uuid = bool(re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        catalog,
        re.IGNORECASE,
    ))

    try:
        models = await list_all_models_for_tenant(tenant_slug, jwt_token)
    except Exception as exc:
        logger.warning("Model lookup failed for catalog '%s': %s", catalog, exc)
        return None, "", None, None

    def _dvid(model: dict) -> Optional[str]:
        vid = model.get("deployed_version_id")
        return str(vid) if vid else None

    if is_uuid:
        # Validate the UUID matches a *deployed* model (list_all_models_for_tenant
        # filters undeployed). If it doesn't appear in the deployed list, refuse.
        for model in models:
            if str(model["id"]).lower() == catalog.lower():
                return catalog, str(model.get("project_id", "")), None, _dvid(model)
        return None, "", None, None

    lc = catalog.lower()

    # Prefer an exact slug/display-name match — this is the business
    # base catalog for that model.
    for model in models:
        slug = (model.get("slug") or "").lower()
        name = (model.get("display_name") or "").lower()
        if slug == lc or name == lc:
            return str(model["id"]), str(model.get("project_id", "")), None, _dvid(model)

    # Try <model-slug>_<persona-slug>. Find the longest model slug that
    # is a prefix of the catalog with an underscore delimiter, then
    # match the suffix against one of that model's personas.
    candidates = [
        m for m in models
        if (m.get("slug") or "").lower()
        and lc.startswith((m["slug"] or "").lower() + "_")
    ]
    candidates.sort(
        key=lambda m: len((m.get("slug") or "")),
        reverse=True,
    )
    for model in candidates:
        m_slug = (model.get("slug") or "").lower()
        persona_suffix = lc[len(m_slug) + 1:]
        mid = str(model["id"])
        pid = str(model.get("project_id", ""))
        try:
            personas = await get_model_personas(
                mid, tenant_slug, jwt_token, project_id=pid,
            )
        except Exception as exc:
            logger.warning("Persona lookup failed for model %s: %s", mid, exc)
            personas = []
        for persona in personas:
            if (persona.get("slug") or "").lower() == persona_suffix:
                return mid, pid, persona, _dvid(model)

    return None, "", None, None


def _persona_includes_hidden(persona: Optional[dict[str, Any]]) -> bool:
    """True when the resolved persona carries includes_hidden_columns.

    Phase 8 persona-as-catalog — replaces the old _catalog_is_technical
    check. A None persona (business base) never includes hidden columns.
    """
    return bool(persona and persona.get("includes_hidden_columns"))


def _apply_persona_allow_lists(
    persona: Optional[dict[str, Any]],
    *,
    measures: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    hierarchies: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter measure / dimension / hierarchy lists by a persona's allow
    lists. An empty allow list is a no-op (unrestricted). A None persona
    also no-ops.

    Returns ``(measures, dimensions, hierarchies)`` narrowed to the
    persona's scope.
    """
    if hierarchies is None:
        hierarchies = []
    if not persona:
        return list(measures), list(dimensions), list(hierarchies)

    def _as_ids(key: str) -> set[str]:
        return {str(x) for x in (persona.get(key) or [])}

    allow_m = _as_ids("included_measure_ids")
    allow_d = _as_ids("included_dimension_ids")
    allow_h = _as_ids("included_hierarchy_ids")

    if allow_m:
        measures = [m for m in measures if str(m.get("id", "")) in allow_m]
    if allow_d:
        dimensions = [d for d in dimensions if str(d.get("id", "")) in allow_d]
    if allow_h:
        hierarchies = [h for h in hierarchies if str(h.get("id", "")) in allow_h]

    return measures, dimensions, hierarchies


async def _resolve_restricted_column_names(
    persona: dict[str, Any],
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str,
) -> tuple[set[str], bool, dict[str, Any] | None]:
    """Resolve a persona's ``restricted_column_ids`` to the corresponding source
    column NAMES via the model snapshot (Bug-5493). Names feed the CLS column-
    disclosure guard so a restricted column name referenced inside a measure's
    DAX expression can be detected and blanked.

    Returns ``(names, resolved, snapshot)`` — the snapshot dict is passed through
    to the CLS guard (F-008-04) so the XMLA/Excel catalogue applies the SAME
    transitive restricted-column closure as the JDBC catalogue and the runtime
    serving gate (variant/UDA/transitive-calc channels, not only direct
    columns). ``snapshot`` is ``None`` when the fetch failed (the guard then
    falls back to the direct-membership + name-scan rules, still fail-closed via
    ``names_resolved``). ``resolved`` is False whenever resolution is
    not provably complete, so the caller can FAIL CLOSED (blank all expressions)
    rather than skip the expression scan and risk leaking a restricted column name
    through an unscanned expression. ``resolved`` is False when:
      - the snapshot fetch fails; or
      - any restricted column id is absent from the snapshot columns; or
      - any restricted column resolves to a blank/empty name.
    Only when EVERY restricted id maps to a non-blank name do we have the full set
    of names to scan for, and ``resolved`` is True. When there are no restricted
    columns, returns ``(set(), True)`` — nothing to resolve.
    """
    restricted_ids = {str(x) for x in (persona.get("restricted_column_ids") or [])}
    if not restricted_ids:
        return set(), True, None
    try:
        snapshot = await get_model_snapshot(
            model_id, tenant_slug, jwt_token, project_id=project_id,
        )
    except Exception as exc:
        logger.warning(
            "TMSCHEMA CLS column-name resolution failed (snapshot fetch): %s", exc,
        )
        return set(), False, None
    names: set[str] = set()
    resolved_ids: set[str] = set()
    for col in snapshot.get("columns") or []:
        col_id = str(col.get("id", ""))
        if col_id in restricted_ids:
            # Snapshot columns are serialised from ModelColumn (ORM), whose name
            # field is `column_name` (see shared/db/models.py:ModelColumn and the
            # router_client snapshot consumers). Read that canonical key.
            name = str(col.get("column_name") or "").strip()
            if name:
                names.add(name)
                resolved_ids.add(col_id)
    # If any restricted id did not resolve to a non-blank name, the name set is
    # incomplete — fail closed so the caller blanks every surviving expression.
    resolved = resolved_ids >= restricted_ids
    return names, resolved, snapshot


def _expression_references_name(expression: str, name: str) -> bool:
    """True when *name* appears in *expression* as a whole token (case-
    insensitive). Used by the CLS column-disclosure guard so a restricted source
    column name embedded inside a larger identifier (e.g. ``salary_band`` vs
    ``salary``) does not over-match, while a real reference does.
    """
    if not expression or not name:
        return False
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                     expression, flags=re.IGNORECASE) is not None


def _apply_cls_column_guard(
    persona: Optional[dict[str, Any]],
    *,
    measures: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    restricted_column_names: set[str] | None = None,
    names_resolved: bool = True,
    snapshot: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Enforce CLS / persona column restrictions on the TMSCHEMA projection
    (Bug-5493). ``restricted_column_ids`` is the same persona allow-list machinery
    the catalogue-metadata path uses (router_client._fetch_model_metadata).

    Two ACCESS rules, both honouring the persona's restricted source columns:

      1. EXCLUSION — a measure or dimension that reaches a restricted source
         column is dropped entirely so neither its name nor its DAX expression
         reaches the client. F-008-04: when ``snapshot`` is supplied this uses
         the SHARED transitive closure (``catalogue_cls.object_hidden_by_cls``),
         so a variant of a restricted base, a UDA-backed object, or a transitive
         calc chain is excluded too — matching the JDBC catalogue and the
         runtime serving gate. Without a snapshot it falls back to direct
         ``source_column_id`` membership.

      2. EXPRESSION BLANKING (column-disclosure guard) — for an INCLUDED measure
         whose DAX ``expression`` text references a restricted column NAME, the
         expression is blanked (the row + measure name remain, the DAX text is
         dropped). A restricted column name can only be disclosed if it literally
         appears in the exposed expression text, so scanning the expression for
         restricted column names is an exact guard against name disclosure — not a
         heuristic. This covers *calculated* measures, which carry no
         ``source_column_id`` and reference columns only inside free-text DSL.

    ``restricted_column_names`` is the set of source column NAMES corresponding to
    the persona's ``restricted_column_ids`` (resolved by the caller from the model
    snapshot). ``names_resolved`` reports whether that resolution succeeded.

    FAIL CLOSED: when the persona HAS restricted columns but their names could not
    be resolved (``names_resolved is False`` — e.g. the snapshot fetch failed),
    we cannot scan expressions for the restricted names, so we blank the
    expression of EVERY surviving included measure rather than risk leaking a
    restricted column name through an unscanned expression. Rule 1 (exclusion by
    bound column) still applies in that case.

    Returns the narrowed ``(measures, dimensions)`` (measure dicts are shallow-
    copied before blanking so the caller's source lists are not mutated).
    """
    if not persona:
        return list(measures), list(dimensions)

    restricted_column_ids = {
        str(x) for x in (persona.get("restricted_column_ids") or [])
    }
    if not restricted_column_ids:
        return list(measures), list(dimensions)

    names = {n for n in (restricted_column_names or set()) if n}
    # Fail-closed trigger: restricted columns exist but their names are unknown.
    blank_all = not names_resolved

    # F-008-04: apply the SHARED transitive closure over the STRUCTURAL channels
    # (direct source/display column, variant-of chain, UDA refs, calc-DIMENSION
    # expression) so the XMLA/Excel catalogue hides variant/UDA/transitive
    # objects exactly like the JDBC catalogue and the runtime serving gate — not
    # only objects bound DIRECTLY to a restricted source column.
    #
    # DAX ``expression`` on a calculated MEASURE is deliberately NOT fed to the
    # closure: the shared calc-measure branch parses the SEMANTIC calc grammar
    # (``measure("name")`` refs), not TMSCHEMA DAX (``SUM(salary)``), so passing
    # DAX there would fail-closed-drop a calc measure that Rule 2 handles more
    # precisely by BLANKING its expression (Bug-5493 keep-name-blank-DAX
    # contract). The closure sees each object with ``measure_type``/``expression``
    # masked; the variant/UDA/direct channels still fire, and Rule 2 below blanks
    # any surviving DAX that names a restricted column.
    def _structural_view(obj: dict[str, Any]) -> dict[str, Any]:
        # Mask the DAX-expression / calc-measure fields so the closure uses only
        # the structural channels (see the note above).
        return {
            k: v for k, v in obj.items()
            if k not in ("measure_type", "expression")
        }

    _cls_ctx = None
    if snapshot is not None:
        from src.catalogue_cls import build_closure_context
        # Structural-masked measures so variant-base resolution inside the
        # context never re-parses DAX as a semantic calc expression.
        _cls_ctx = build_closure_context(
            measures=[_structural_view(m) for m in measures],
            snapshot=snapshot,
            restricted_column_ids=restricted_column_ids,
        )

    def _is_restricted(obj: dict[str, Any]) -> bool:
        if _cls_ctx is not None:
            from src.catalogue_cls import object_hidden_by_cls
            return object_hidden_by_cls(
                _structural_view(obj), restricted_column_ids, _cls_ctx,
            )
        col_id = obj.get("source_column_id")
        return col_id is not None and str(col_id) in restricted_column_ids

    kept_measures: list[dict[str, Any]] = []
    for m in measures:
        if _is_restricted(m):
            # Rule 1 — bound to a restricted column: drop row (name + DAX).
            continue
        # Rule 2 — included measure whose expression names a restricted column:
        # blank the expression so the restricted column NAME cannot leak. When
        # the restricted names are unresolved, blank every expression (fail
        # closed) since any of them could reference a restricted column.
        expr = str(m.get("expression") or "")
        if expr and (blank_all or any(
            _expression_references_name(expr, n) for n in names
        )):
            m = {**m, "expression": ""}
        kept_measures.append(m)

    kept_dimensions = [d for d in dimensions if not _is_restricted(d)]

    return kept_measures, kept_dimensions


_INFO_MEASURES = {
    "_info_last_refreshed": "last_refreshed_at",
    "_info_source_system": "source_system",
    "_info_owner": "owner",
}


async def _fetch_trust_values(
    model_id: str, tenant_slug: str, jwt_token: str
) -> dict[str, str]:
    """Fetch the model's trust metadata (freshness / source / owner) and
    return a dict keyed by info-measure internal name.

    Phase 5 of the semantic-layer plan — the same trust_meta pipeline
    that feeds column footers is reused to fill the cell values for
    `[Measures].[Last Refreshed]`, `[Measures].[Source System]` and
    `[Measures].[Owner]`.
    """
    try:
        models = await asyncio.wait_for(
            list_all_models_for_tenant(tenant_slug, jwt_token),
            timeout=_XMLA_TRUST_LOOKUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out after %.3fs listing models for trust lookup",
            _XMLA_TRUST_LOOKUP_TIMEOUT_SECONDS,
        )
        return {}
    except Exception as exc:
        logger.warning("Failed to list models for trust lookup: %s", exc)
        return {}
    for m in models:
        if str(m.get("id", "")) != str(model_id):
            continue
        trust = m.get("trust_meta") or {}
        return {
            "_info_last_refreshed": str(trust.get("last_refreshed_at") or ""),
            "_info_source_system":  str(trust.get("source_system") or ""),
            "_info_owner":          str(trust.get("owner") or ""),
        }
    return {}


_INFO_DISPLAY_TO_INTERNAL: dict[str, str] = {
    "last refreshed": "_info_last_refreshed",
    "source system": "_info_source_system",
    "owner": "_info_owner",
}


def _referenced_info_measures(mdx: str) -> list[str]:
    """Return the internal names of info/trust measures referenced in *mdx*,
    in order of first appearance with duplicates removed.

    Accepts both internal names (`_info_last_refreshed`) and display names
    (`Last Refreshed`) so either MDX form is recognised (F-002-13).
    """
    if not mdx:
        return []
    found: list[str] = []
    for ref in re.findall(r"\[Measures\]\.\[([^\]]+)\]", mdx, flags=re.IGNORECASE):
        ref_lower = ref.lower()
        internal: str | None = None
        if ref_lower in _INFO_MEASURES:
            internal = ref_lower
        elif ref_lower in _INFO_DISPLAY_TO_INTERNAL:
            internal = _INFO_DISPLAY_TO_INTERNAL[ref_lower]
        if internal and internal not in found:
            found.append(internal)
    return found


async def _maybe_resolve_info_measures(
    mdx: str, model_id: str, tenant_slug: str, jwt_token: str
) -> Optional[tuple[list, list]]:
    """Return (col_names, rows) when the MDX statement only references
    synthetic info measures, otherwise None so the caller can fall back
    to the normal SQL router path.

    Returns columns as list[str] and rows as list[dict] so the result is
    compatible with build_real_execute_response directly.

    Accepts both internal names (_info_last_refreshed) and display names
    (Last Refreshed) so Excel MSOLAP clients using either form are handled.
    """
    if not mdx or not model_id:
        return None

    # Display-name → internal-name for caption-style MDX references (Bug-121)
    _display_to_internal = _INFO_DISPLAY_TO_INTERNAL

    measure_refs = re.findall(r"\[Measures\]\.\[([^\]]+)\]", mdx, flags=re.IGNORECASE)
    if measure_refs:
        referenced_internals: list[str] = []
        for ref in measure_refs:
            ref_lower = ref.lower()
            if ref_lower in _INFO_MEASURES:
                referenced_internals.append(ref_lower)
            elif ref_lower in _display_to_internal:
                referenced_internals.append(_display_to_internal[ref_lower])
            else:
                return None  # mixed query with real measures — fall back to SQL router
    else:
        # Bracket notation absent; check for bare internal names in text
        text = mdx.upper()
        referenced_internals = [k for k in _INFO_MEASURES if k.upper() in text]
        if not referenced_internals:
            return None

    values = await _fetch_trust_values(model_id, tenant_slug, jwt_token)
    col_names = list(dict.fromkeys(referenced_internals))  # deduplicate, preserve order
    row_dict = {internal: values.get(internal, "") for internal in col_names}
    return col_names, [row_dict]


def _reject_unrepresentable_kpi_range_unions(
    where_expr: str,
    where_filters: dict[str, list[str]],
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
) -> None:
    """Reject KPI range unions that cannot be represented by one filter.

    ``_mdx_extract_where_filters`` intentionally gives a range precedence over
    ordinary members on the same dimension for the general SQL path.  That is
    not safe for governed KPI translation: a range plus another member is an
    OR-shaped slicer, while the batch API receives separate predicates and
    evaluates them as AND.  Two ranges have the same ambiguity.  Keep the
    general extractor unchanged and inspect the raw KPI slicer here, refusing
    only the shapes that would otherwise change the requested set.
    """
    hierarchy_level_dim_map = hierarchy_level_dim_map or {}
    hierarchy_default_dim_map = hierarchy_default_dim_map or {}
    range_dimensions = {
        str(dimension).casefold()
        for dimension, values in where_filters.items()
        if any(str(value).startswith(_RANGE_PREFIX) for value in values)
    }
    if not range_dimensions:
        return

    range_pattern = (
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.'
        + KEYS_OR_CAPTION
        + r'\s*:\s*'
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.'
        + KEYS_OR_CAPTION
    )
    range_spans: list[tuple[int, int]] = []
    matched_ranges: dict[str, int] = {}
    for match in re.finditer(range_pattern, where_expr):
        target = _resolve_hierarchy_dimension_name(
            dim_name=match.group(1).strip(),
            hierarchy_name=match.group(2).strip(),
            level_name=match.group(3).strip(),
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        if target is None:
            continue
        target_key = str(target).casefold()
        if target_key not in range_dimensions:
            continue
        range_spans.append(match.span())
        matched_ranges[target_key] = matched_ranges.get(target_key, 0) + 1

    for dimension, values in where_filters.items():
        range_count = sum(
            1 for value in values if str(value).startswith(_RANGE_PREFIX)
        )
        if range_count > 1 or matched_ranges.get(str(dimension).casefold(), 0) > 1:
            raise ValueError(
                f"KPI slicer range union for dimension '{dimension}' has no "
                "single request-level filter equivalent; refusing to evaluate "
                "an unsliced KPI."
            )

    if not range_spans:
        return
    residual = where_expr
    for start, end in reversed(range_spans):
        residual = residual[:start] + residual[end:]
    ordinary_filters = _mdx_extract_where_filters(
        residual,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    for dimension in ordinary_filters:
        if str(dimension).casefold() in range_dimensions:
            raise ValueError(
                f"KPI slicer range union for dimension '{dimension}' has no "
                "single request-level filter equivalent; refusing to evaluate "
                "an unsliced KPI."
            )


def _translate_kpi_slicer_filters(
    where_expr: str,
    where_filters: dict[str, list[str]],
    dimensions_meta: list[dict[str, Any]],
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Translate an MDX KPI slicer into the batch-evaluation filter schema.

    The model-service batch API consumes dimension IDs, while the MDX parser
    deliberately returns the semantic dimension names used by the XMLA
    catalogue. Resolve every captured name against the same metadata snapshot
    and fail closed if a member reference cannot be represented. An empty or
    unknown filter must never become an unfiltered governed KPI evaluation.
    """
    # KPI member interception runs before the normal MDX -> SQL translator,
    # whose validator would otherwise reject set operations and unsupported
    # slicer functions. Keep the same fail-loud boundary here: extracting the
    # literal members from ``Except(...)`` or ``Filter(...)`` and sending them
    # as a plain IN filter changes the requested set instead of preserving its
    # semantics (Bug-8383).
    _check_unsupported_mdx_constructs(
        where_expr, "KPI slicer", allow_range=True,
    )
    if re.search(r"\bStrTo(?:Set|Member)\s*\(", where_expr, re.IGNORECASE):
        raise ValueError(
            "Dynamic STRTOSET/STRTOMEMBER KPI slicers have no request-level "
            "filter equivalent; refusing to evaluate an unsliced KPI."
        )
    if re.search(r"(?<![\w\[\"'])@\s*[A-Za-z_][A-Za-z0-9_]*", where_expr):
        raise ValueError(
            "Parameterized KPI slicers have no request-level filter "
            "equivalent; refusing to evaluate an unsliced KPI."
        )
    _assert_where_members_applied(
        where_expr,
        where_filters,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    extracted_dimensions = set(_mdx_extract_dimensions(
        where_expr,
        dim_names=dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    ))
    untranslated_dimensions = sorted(
        dimension for dimension in extracted_dimensions
        if dimension not in where_filters
    )
    if untranslated_dimensions:
        raise ValueError(
            "KPI slicer dimensions "
            f"{', '.join(untranslated_dimensions)!r} have no supported "
            "filter equivalent; refusing to evaluate an unsliced KPI."
        )
    _reject_unrepresentable_kpi_range_unions(
        where_expr,
        where_filters,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    if not where_filters:
        return []

    dimension_ids: dict[str, str] = {}
    for dimension in dimensions_meta:
        if not isinstance(dimension, dict):
            continue
        dimension_id = dimension.get("id") or dimension.get("dimension_id")
        dimension_name = dimension.get("name") or dimension.get("display_name")
        if dimension_id is not None and dimension_name:
            dimension_ids[str(dimension_name).casefold()] = str(dimension_id)

    translated: list[dict[str, Any]] = []
    for dimension_name, values in where_filters.items():
        dimension_id = dimension_ids.get(str(dimension_name).casefold())
        if not dimension_id:
            raise ValueError(
                f"KPI slicer dimension '{dimension_name}' has no model filter "
                "equivalent; refusing to evaluate an unsliced KPI."
            )
        normal_values: list[str] = []
        for value in values:
            if str(value).startswith(_RANGE_PREFIX):
                payload = str(value)[len(_RANGE_PREFIX):]
                try:
                    start, end = payload.split(_RANGE_SEP, 1)
                except ValueError as exc:
                    raise ValueError(
                        f"KPI slicer for '{dimension_name}' has an invalid "
                        "range and cannot be translated."
                    ) from exc
                translated.append({
                    "dimension_id": dimension_id,
                    "operator": "between",
                    "values": [start, end],
                })
            else:
                normal_values.append(str(value))
        if len(normal_values) == 1:
            translated.append({
                "dimension_id": dimension_id,
                "operator": "eq",
                "value": normal_values[0],
            })
        elif normal_values:
            translated.append({
                "dimension_id": dimension_id,
                "operator": "in",
                "values": normal_values,
            })
    if not translated:
        raise ValueError(
            "The KPI slicer has no supported filter equivalent; refusing to "
            "evaluate an unfiltered KPI."
        )
    return translated


async def _maybe_resolve_kpi_members(
    *,
    statement: str,
    model_id: str,
    project_id: str,
    tenant_slug: str,
    jwt_token: str,
    measures_meta: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
    dim_names: set[str],
    model_slug: str,
    persona_id: str | None,
    is_technical_view: bool,
    persona_included_measure_ids: set[str] | None = None,
) -> Optional[tuple[list[str], list[dict[str, Any]]]]:
    """Resolve KPI member functions (KPIValue/KPIGoal/KPIStatus/KPITrend).

    Returns ``(columns, rows)`` for a single-cell-per-KPI-function MDDataSet
    response, or ``None`` when the statement contains no KPI member function so
    the caller falls through to the normal SQL path (Bug-3657).

    The value reference is resolved to the KPI's value MEASURE (queryable),
    goal to its published goal (literal or measure), status to the
    direction-aware -1/0/1 evaluation, and trend to the published trend or
    blank. KPI functions only appear in the WHERE slicer of a measureless
    ``SELECT FROM [cube]`` (Excel's CUBEKPIMEMBER cell); a dimension slicer
    alongside the KPI is honoured as a filter.
    """
    found = find_kpi_member_functions(statement)
    if not found:
        return None

    # Bug-6702 (Codex R2 finding 2): the Discover path trims is_hidden measures
    # BEFORE `_rows_kpis` builds the catalogue (xmla_server `_handle_discover`),
    # so a KPI whose value resolves to a hidden measure advertises KPI_VALUE=""
    # for a non-technical catalog. This Execute path received the RAW cached
    # measure list, so the same KPI's KPIValue()/KPIStatus() still resolved and
    # ran the hidden backing measure — catalogue and Execute disagreed. Apply the
    # SAME visibility rule here (a technical-view catalog keeps hidden measures
    # on both surfaces) so the two surfaces resolve KPIs against the same set:
    # a hidden-backed KPI now fails loud in Execute exactly where the catalogue
    # advertises no value member.
    if not is_technical_view:
        measures_meta = [m for m in measures_meta if not m.get("is_hidden")]

    kpis = await get_model_kpis(
        model_id, tenant_slug, jwt_token, project_id=project_id,
    )
    # Bug-7227: filter KPIs BY LINEAGE for a measure-restricted persona (was
    # Bug-5587, which blanked ALL KPIs so a KPIValue()/KPIStatus() cell for an
    # ALLOWED KPI faulted). Keep exactly the KPIs whose transitive measure
    # lineage is inside the allow-list — the SAME set the Discover MDSCHEMA_KPIS
    # surface advertises — so the catalogue and Execute agree. Fail closed on
    # unverifiable lineage. `measures_meta` here is the persona-scoped executable
    # measure set, so a KPI over a non-allowed measure fails lineage resolution
    # and is withheld.
    if persona_included_measure_ids:
        kpis = filter_kpis_for_persona(
            kpis, measures_meta, persona_included_measure_ids,
        )
    kpi_by_caption: dict[str, dict[str, Any]] = {}
    for k in kpis:
        cap = (k.get("display_name") or k.get("name") or "").strip().lower()
        if cap:
            kpi_by_caption.setdefault(cap, k)
        nm = (k.get("name") or "").strip().lower()
        if nm:
            kpi_by_caption.setdefault(nm, k)

    # Dimension slicer filters that accompany the KPI (KPI function stripped).
    # Bug-8383: a governed KPI slicer uses the batch route, whose request-level
    # filters are compiled into the KPI's SQL evaluation. Translate names to
    # dimension IDs against this same metadata snapshot and fail loud before any
    # evaluation when the MDX shape has no equivalent filter.
    where_expr = _mdx_where_expr(statement)
    where_filters = _mdx_extract_where_filters(
        where_expr, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    ) if where_expr else {}
    kpi_slicer_filters = _translate_kpi_slicer_filters(
        where_expr,
        where_filters,
        dimensions_meta,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )

    columns: list[str] = []
    row: dict[str, Any] = {}

    # Bug-6608 (un-gated 2026-07-21): every KPI member function resolves through
    # the SINGLE governed model-service ``/evaluate`` authority — the SAME pipeline
    # the SPA scorecard and the Excel custom function consume — so all surfaces
    # return one result. This fixes:
    #   * F-025-01 — KPIStatus now returns the governed −1/0/1 RAG verdict
    #     (``kpi_threshold.evaluate_threshold``), identical to the scorecard and the
    #     Excel custom function, instead of the raw value the Bug-6608 by-design
    #     decision served. The report-builder traffic-light iconSet (calibrated for
    #     the −1/0/1 domain, useExcel.ts ``kpiIconCriteria``) now colours correctly.
    #   * G-002-01 — composite / ratio value expressions and expression / prior-period
    #     goals resolve (the pipeline compiles the DSL), where the gateway's own
    #     single-measure ``_measure_cell`` resolution could not.
    # A per-KPI evaluation is cached within this call. Sliced members share one
    # batch request so KPIValue+KPIGoal+KPIStatus for one KPI receive the exact
    # same translated filter context.
    eval_cache: dict[str, dict[str, Any]] = {}
    batch_eval_cache: dict[str, dict[str, Any]] = {}

    # Bug-6702 visibility gate (preserved): `measures_meta` is already trimmed of
    # is_hidden measures on a non-technical view (above). A KPI whose transitive
    # measure lineage references a measure NOT in this trimmed set is hidden-backed
    # on THIS surface — the Discover catalogue advertises no value member for it, so
    # the Execute path must fail loud here too rather than resolving the hidden
    # backing measure through the governed authority (which applies persona scope
    # but not the gateway's technical-view visibility rule). This keeps the
    # catalogue and Execute surfaces aligned exactly as before, while still letting
    # a composite KPI whose lineage measures ARE all visible resolve (G-002-01).
    _visible_measure_ids = {str(m.get("id")) for m in measures_meta if m.get("id")}
    _measure_name_to_id = kpi_persona_measure_name_to_id(measures_meta)
    _kpi_by_name: dict[str, dict[str, Any]] = {}
    _children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for _k in kpis:
        _nm = _k.get("name")
        if _nm:
            _kpi_by_name.setdefault(str(_nm), _k)
            _kpi_by_name.setdefault(str(_nm).lower(), _k)
        _pid = _k.get("parent_kpi_id")
        if _pid is not None and str(_pid).strip():
            _children_by_parent.setdefault(str(_pid), []).append(_k)

    def _kpi_is_surface_visible(kpi: dict[str, Any]) -> bool:
        lineage_ids, fully_resolved = kpi_lineage_measure_ids(
            kpi, _kpi_by_name, _measure_name_to_id, _children_by_parent,
        )
        # Fail closed on unverifiable lineage, and withhold when any lineage
        # measure is trimmed from this surface's executable set. An empty
        # verified lineage (no measure references at all) is also withheld:
        # the catalogue advertises KPI_VALUE="" for such a KPI, so Execute
        # must agree rather than proceeding to a governed call the catalogue
        # does not promise (Opus R1 finding 1 -- alignment).
        if not fully_resolved:
            return False
        return bool(lineage_ids) and all(
            mid in _visible_measure_ids for mid in lineage_ids
        )

    async def _governed_eval(kpi: dict[str, Any], caption: str) -> dict[str, Any]:
        kpi_id = str(kpi.get("id") or "")
        if not kpi_id:
            raise ValueError(
                f"KPI '{caption}' has no id and cannot be evaluated through the "
                "governed KPI authority."
            )
        if not _kpi_is_surface_visible(kpi):
            raise ValueError(
                f"KPI '{caption}' has no resolvable value measure on this surface "
                "(a measure in its lineage is hidden / not available); the "
                "catalogue advertises no value member for it."
            )
        cached = eval_cache.get(kpi_id)
        if cached is not None:
            return cached
        if kpi_slicer_filters:
            if not batch_eval_cache:
                requested_ids: list[str] = []
                for _matched, _fn, _caption in found:
                    _candidate = kpi_by_caption.get(
                        (_caption or "").strip().lower()
                    )
                    if _candidate is None:
                        raise ValueError(
                            f"KPI '{_caption}' is not a deployed KPI in this model."
                        )
                    if not _kpi_is_surface_visible(_candidate):
                        raise ValueError(
                            f"KPI '{_caption}' has no resolvable value measure on "
                            "this surface (a measure in its lineage is hidden / "
                            "not available); the catalogue advertises no value "
                            "member for it."
                        )
                    _candidate_id = str(_candidate.get("id") or "")
                    if _candidate_id and _candidate_id not in requested_ids:
                        requested_ids.append(_candidate_id)
                batch_eval_cache.update(await evaluate_kpi_batch(
                    requested_ids,
                    model_id=model_id,
                    project_id=project_id,
                    tenant_slug=tenant_slug,
                    jwt_token=jwt_token,
                    filters=kpi_slicer_filters,
                    persona_id=persona_id,
                ))
            result = batch_eval_cache.get(kpi_id)
            if result is None:
                raise ValueError(
                    f"KPI '{caption}' was not returned by the governed batch "
                    "evaluation; refusing to substitute an unfiltered value."
                )
            eval_cache[kpi_id] = result
            return result
        result = await evaluate_kpi_governed(
            kpi_id=kpi_id,
            model_id=model_id,
            project_id=project_id,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            persona_id=persona_id,
        )
        eval_cache[kpi_id] = result
        return result

    def _num(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    for matched_text, fn, caption in found:
        kpi = kpi_by_caption.get((caption or "").strip().lower())
        col_name = f"{caption} ({fn[3:]})"
        if kpi is None:
            raise ValueError(
                f"KPI '{caption}' is not a deployed KPI in this model."
            )

        ev = await _governed_eval(kpi, caption)

        if fn == "KPIValue":
            # No-data / NULL value stays blank (None) rather than reading as 0.
            row[col_name] = _num(ev.get("value"))
        elif fn == "KPIGoal":
            # ``target`` is the v2 field; ``goal`` is the v1-compat alias.
            goal = ev.get("target")
            if goal is None:
                goal = ev.get("goal")
            row[col_name] = _num(goal)
        elif fn == "KPIStatus":
            # Governed −1/0/1 RAG verdict (None → blank for no-data). This is the
            # SAME integer the scorecard and the Excel custom function return, so the
            # traffic-light iconSet the report builder applies renders the correct
            # colour. A KPI with no target / no data yields status None → blank.
            status = ev.get("status")
            row[col_name] = int(status) if status is not None else None
        elif fn == "KPITrend":
            # Governed period-over-period trend classification (−1/0/1). Falls back
            # to a modeller-authored literal ``trend_expression`` only when the
            # pipeline produced no trend (e.g. no time binding).
            trend = ev.get("trend")
            if trend is not None:
                row[col_name] = int(trend)
            else:
                literal = kpi.get("trend_expression") or None
                row[col_name] = literal if literal else 0
        else:
            raise ValueError(f"Unsupported KPI member function: {fn}")
        columns.append(col_name)

    return columns, [row]


def _build_trust_info_measures() -> list[dict[str, Any]]:
    """Phase 5 of the semantic-layer plan: synthetic info measures
    surfaced under the `Info` display folder of every cube.

    The values themselves are filled in lazily by the model-service trust
    metadata pipeline; here we only declare the measure names so they
    appear in the PivotTable Field List. When a user drops one onto a
    pivot, the executor returns a constant string for that measure.
    """
    folder = "Info"
    return [
        {
            "name": "_info_last_refreshed",
            "display_name": "Last Refreshed",
            "description": "Most recent aggregate refresh timestamp for this model.",
            "display_folder": folder,
            "default_agg": "min",
            "is_hidden": False,
        },
        {
            "name": "_info_source_system",
            "display_name": "Source System",
            "description": "Underlying connection type powering this model.",
            "display_folder": folder,
            "default_agg": "min",
            "is_hidden": False,
        },
        {
            "name": "_info_owner",
            "display_name": "Owner",
            "description": "Modeller responsible for this model.",
            "display_folder": folder,
            "default_agg": "min",
            "is_hidden": False,
        },
    ]


# ---------------------------------------------------------------------------
# MDX → SQL translation
# ---------------------------------------------------------------------------

def _caption_dimension_names(
    dimensions_meta: list[dict[str, Any]] | None,
) -> list[str]:
    """Bug-8285: dimension names that declare a distinct DISPLAY column.

    These are the dimensions the query-router must project a friendly
    ``<dim>__caption`` companion column for (via ExecuteRequest.caption_dimensions).
    The trigger condition mirrors the consumer's exactly
    (``mdx_execute.build_real_execute_response`` builds ``dim_caption_col_map``
    from the same ``display_column_name`` distinct-from-name test) so producer and
    consumer agree on which members get captions. Returns the pre-alias SOURCE
    dimension names, matching the names the router binds against.
    """
    names: list[str] = []
    for d in dimensions_meta or []:
        name = d.get("name") or ""
        disp = (d.get("display_column_name") or "").strip()
        if name and disp and disp != name:
            names.append(name)
    return names


def _statement_to_sql(
    statement: str,
    measures_meta: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_meta: list[dict[str, Any]] | None = None,
    model_slug: str = "",
    subtotal_hierarchies: list | None = None,
    constant_measure_names: set[str] | None = None,
) -> tuple[str, str]:
    text = _strip_leading_mdx_comments((statement or "").strip())
    if re.match(r"^EVALUATE\b", text, re.IGNORECASE):
        return _dax_to_sql(
            text,
            measures_meta,
            dimensions_meta,
            hierarchy_meta=hierarchy_meta,
            model_slug=model_slug,
        )
    return _mdx_to_sql(
        text,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
        model_slug=model_slug,
        subtotal_hierarchies=subtotal_hierarchies,
        constant_measure_names=constant_measure_names,
    )


def _resolve_case_insensitive_name(name: str, names: set[str]) -> str | None:
    lname = (name or "").strip().lower()
    if not lname:
        return None
    matches = [n for n in names if n.lower() == lname]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_dax_dimension_name(
    *,
    raw_name: str,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
    hierarchy_name_hint: str | None = None,
    strict: bool = False,
    context: str = "dimension",
) -> str | None:
    name = (raw_name or "").strip()
    if not name:
        if strict:
            raise ValueError(f"Missing DAX {context} reference.")
        return None

    hint = (hierarchy_name_hint or "").strip()
    if hint:
        hkey = hint.lower()
        by_level = hierarchy_level_dim_map.get(hkey)
        if by_level:
            resolved = by_level.get(name.lower())
            if resolved:
                return resolved
            if strict:
                raise ValueError(f"Unknown DAX {context} reference '{hint}[{name}]'.")

        if hkey in hierarchy_default_dim_map and name.lower() in {"all", "(all)"}:
            return hierarchy_default_dim_map[hkey]

    if name in dim_names:
        return name

    ci = _resolve_case_insensitive_name(name, dim_names)
    if ci:
        return ci

    # DAX refs often look like Geography[Region] where "Geography" is hierarchy
    # and "Region" is a level; parser emits "Region", so resolve by unique level.
    level_key = name.lower()
    level_candidates: set[str] = set()
    matching_hierarchies: set[str] = set()
    for hierarchy_name, by_level in hierarchy_level_dim_map.items():
        resolved = by_level.get(level_key)
        if resolved:
            level_candidates.add(resolved)
            matching_hierarchies.add(hierarchy_name)
    if len(level_candidates) == 1:
        return next(iter(level_candidates))
    if len(level_candidates) > 1:
        if strict:
            ordered_hiers = ", ".join(sorted(matching_hierarchies))
            raise ValueError(
                f"Ambiguous DAX {context} reference '{name}': matches multiple hierarchies ({ordered_hiers})."
            )
        return None

    # Allow direct hierarchy-name references to map to hierarchy default level.
    if name.lower() in hierarchy_default_dim_map:
        return hierarchy_default_dim_map[name.lower()]

    if strict:
        raise ValueError(f"Unknown DAX {context} reference '{name}'.")
    return None


def _sql_literal(value: Any, *, is_string: bool | None = None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    # F-002-12: a DAX literal known to be a STRING (the parser saw it quoted)
    # must always be emitted quoted, even when it looks numeric ("00123") or
    # boolean ("true"). Inferring the type from the text shape strips string
    # typing and compares zero-padded codes / boolean-looking strings against
    # the wrong literal. Only fall back to text-shape inference when the parser
    # did not record the literal kind (regex fallback parser).
    # Bug-6634: route every STRING literal branch through the shared
    # connector_qualify.quote_literal helper instead of hand-rolling ''-doubling.
    # The XMLA channel emits canonical PostgreSQL SQL that is transpiled per
    # connector downstream, so quote for "postgresql" here; the shared helper is
    # the single audited place for literal escaping (defense-in-depth, and it
    # keeps this consistent with every other SQL-generation site).
    if is_string:
        return _ql("postgresql", text)
    if is_string is False:
        # Parser saw an unquoted (numeric/boolean) literal — emit it verbatim
        # when it is a clean numeric, else quote defensively.
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            return text
        lowered = text.lower()
        if lowered in {"true", "false"}:
            return lowered.upper()
        return _ql("postgresql", text)
    # is_string is None — unknown provenance (regex fallback parser): infer
    # from the text shape (legacy behaviour).
    if re.fullmatch(r"-?\d+", text):
        return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        return text
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered.upper()
    return _ql("postgresql", text)


def _dax_to_sql(
    dax: str,
    measures_meta: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_meta: list[dict[str, Any]] | None = None,
    model_slug: str = "",
) -> tuple[str, str]:
    (
        dim_names,
        hierarchy_level_dim_map,
        hierarchy_default_dim_map,
    ) = _build_hierarchy_dimension_map(dimensions_meta, hierarchy_meta or [])

    measure_agg: dict[str, str] = {}
    measure_names: set[str] = set()
    measure_id_by_name: dict[str, str] = {}
    variant_lookup: dict[tuple[str, str], str] = {}
    for m in measures_meta:
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        measure_names.add(name)
        measure_agg[name] = str(m.get("default_agg") or "sum").upper()
        mid = str(m.get("id") or "")
        if mid:
            measure_id_by_name[name] = mid
        vk = m.get("variant_kind")
        base_id = str(m.get("variant_of_measure_id") or "")
        if vk and base_id:
            variant_lookup[(base_id, vk)] = name

    try:
        parsed = translate_dax(dax, model_id="")
    except Exception as exc:
        logger.warning("DAX parse failed; falling back to MDX translator: %s", exc)
        return _mdx_to_sql(
            dax,
            measures_meta,
            dimensions_meta,
            hierarchy_meta=hierarchy_meta,
            model_slug=model_slug,
        )

    # Bug-5886 (F-002-01): TREATAS filters are recognised by the parser but
    # silently dropped from the executed WHERE clause -- a valid-looking
    # result that ignores the requested filter is a wrong-number defect, not
    # a warning. Fail loud (SOAP fault) instead of executing an unfiltered
    # query. This is intentionally narrow: it only fires for the TREATAS
    # semantic-loss warning, not for the unrelated "partial parse" warning.
    treatas_warnings = [
        w for w in parsed.warnings if w.startswith("TREATAS ignored")
    ]
    if treatas_warnings:
        raise ValueError(
            "DAX TREATAS() is not supported by this gateway and was not "
            "applied to the query; refusing to return unfiltered results. "
            f"{treatas_warnings[0]}"
        )

    # Resolve time-variant hints: TOTALYTD([Revenue], ...) → Revenue_ytd
    if parsed.time_variant_hints:
        for base_name, variant_kind in parsed.time_variant_hints.items():
            base_id = measure_id_by_name.get(base_name, "")
            variant_name = variant_lookup.get((base_id, variant_kind))
            if variant_name:
                if base_name in parsed.measures:
                    idx = parsed.measures.index(base_name)
                    parsed.measures[idx] = variant_name
                logger.debug(
                    "DAX time-variant resolved: %s(%s) → %s",
                    variant_kind, base_name, variant_name,
                )
            else:
                # Bug-5887 (F-002-02): a missing time-intelligence variant
                # used to fall back to the base (non-time-filtered) measure,
                # a silent wrong-number defect (e.g. YTD returns the base
                # period's total). Fail loud instead: the client asked for a
                # specific time-intelligence calculation the model cannot
                # provide, so surface a client-visible fault rather than a
                # plausible-looking wrong answer.
                raise ValueError(
                    f"DAX time-intelligence function {variant_kind}({base_name}) "
                    "requires a matching time-variant measure that is not "
                    "defined on this model; refusing to fall back to the "
                    "base measure."
                )

    resolved_dims: list[str] = []
    for idx, raw_dim in enumerate(parsed.dimensions):
        hint = (
            parsed.dimension_hierarchy_hints[idx]
            if idx < len(parsed.dimension_hierarchy_hints)
            else None
        )
        resolved = _resolve_dax_dimension_name(
            raw_name=raw_dim,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
            hierarchy_name_hint=hint,
            strict=True,
            context="dimension",
        )
        if resolved and resolved not in resolved_dims:
            resolved_dims.append(resolved)

    resolved_measures: list[str] = []
    for raw_measure in parsed.measures:
        candidate = (raw_measure or "").strip()
        if not candidate:
            continue
        resolved = candidate if candidate in measure_names else _resolve_case_insensitive_name(candidate, measure_names)
        if not resolved:
            raise ValueError(f"Unknown DAX measure reference '{candidate}'.")
        if resolved not in resolved_measures:
            resolved_measures.append(resolved)

    if not parsed.measures and not resolved_dims and measure_names:
        # Keep backward compatibility with previous Execute behavior.
        resolved_measures = [m.get("name", "") for m in measures_meta if m.get("name")]

    def _q(name: str) -> str:
        return _qi("postgresql", name)

    select_parts: list[str] = []
    for dim in resolved_dims:
        if dim in dim_names:
            select_parts.append(_q(dim))
    for meas in resolved_measures:
        qm = _q(meas)
        agg = measure_agg.get(meas, "SUM")
        if agg == "COUNT_DISTINCT":
            select_parts.append(f"COUNT(DISTINCT {qm}) AS {qm}")
        elif agg == "COUNT":
            select_parts.append(f"COUNT({qm}) AS {qm}")
        else:
            select_parts.append(f"{agg}({qm}) AS {qm}")

    if not select_parts:
        raise ValueError("DAX translation produced an empty projection.")

    op_map = {
        "eq": "=",
        "neq": "!=",
        "lt": "<",
        "lte": "<=",
        "gt": ">",
        "gte": ">=",
    }
    where_clauses: list[str] = []
    for flt in parsed.filters:
        hint = str(flt.get("table") or "").strip() or None
        resolved_col = _resolve_dax_dimension_name(
            raw_name=str(flt.get("column") or ""),
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
            hierarchy_name_hint=hint,
            strict=True,
            context="filter",
        )
        op = op_map.get(str(flt.get("operator") or "eq"), "=")
        # F-002-12: honour the parser's record of whether the DAX literal was a
        # quoted string so numeric-looking codes keep their string type.
        where_clauses.append(
            f"{_q(resolved_col)} {op} "
            f"{_sql_literal(flt.get('value'), is_string=flt.get('value_is_string'))}"
        )

    from_table = _q(model_slug or "model_table")
    sql = f"SELECT {', '.join(select_parts)} FROM {from_table}"
    if where_clauses:
        sql += f" WHERE {' AND '.join(where_clauses)}"
    if resolved_dims:
        sql += f" GROUP BY {', '.join(_q(d) for d in resolved_dims)}"

    order_parts: list[str] = []
    for order in parsed.order_by:
        raw_col = str(order.get("column") or "").strip()
        hint = str(order.get("table") or "").strip() or None
        direction = "DESC" if str(order.get("direction") or "").upper() != "ASC" else "ASC"

        resolved_col = _resolve_dax_dimension_name(
            raw_name=raw_col,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
            hierarchy_name_hint=hint,
        )
        if not resolved_col:
            resolved_col = raw_col if raw_col in resolved_measures else _resolve_case_insensitive_name(raw_col, set(resolved_measures))
        if not resolved_col:
            raise ValueError(f"Unknown DAX ORDER BY reference '{raw_col}'.")
        order_parts.append(f"{_q(resolved_col)} {direction}")
    if order_parts:
        sql += f" ORDER BY {', '.join(order_parts)}"

    if parsed.limit is not None and parsed.limit >= 0:
        sql += f" LIMIT {parsed.limit}"

    return sql, "jdbc"


def _subtotal_grain_concurrency() -> int:
    """Configured cap on concurrent subtotal/re-query SQL fan-out (F-002-16)."""
    try:
        val = int(system_snapshot_get("gateway.subtotal_grain_max_concurrency"))
        return val if val > 0 else 4
    except (TypeError, ValueError):
        return 4


async def _gather_bounded(coro_factories: list, limit: int) -> list:
    """Run *coro_factories* (zero-arg callables returning coroutines) with at
    most *limit* in flight at once.

    F-002-16: a deep multi-hierarchy pivot fans out the cross-product of grain
    levels (e.g. two 4-level hierarchies → up to 24 grain queries) plus
    per-partition re-queries, each hitting the query-router and source DB. A
    bare ``asyncio.gather`` fired them all at once; this caps concurrency with a
    semaphore so one pivot cannot saturate the downstream.
    """
    import asyncio

    sem = asyncio.Semaphore(max(1, limit))

    async def _run(factory):
        async with sem:
            return await factory()

    return await asyncio.gather(*[_run(f) for f in coro_factories])


# Bug-5558: plain integer/decimal literal regex matching the Bug-5538 pattern
# in query-router conditions.py. Tight: no exponent, no leading ``+``, no
# surrounding whitespace, ASCII digits only.
_NUMERIC_LITERAL_RE = re.compile(r"^-?(\d+(\.\d+)?|\.\d+)\Z", re.ASCII)
_INTEGER_LITERAL_RE = re.compile(r"^-?\d+\Z", re.ASCII)
_INTEGER_TYPE_RE = re.compile(
    r"^(u?int(eger)?\d*|bigint|smallint|tinyint|byteint|long)\Z",
    re.ASCII,
)


def _dim_type_map_from_meta(
    dimensions_meta: list[dict[str, Any]],
) -> dict[str, str]:
    """Build a {dim_name: data_type} map from dimension metadata.

    Used by ``_build_where_sql_clauses`` to render type-aware WHERE literals
    (Bug-5558). Missing or None data_type entries are omitted so callers
    get an empty dict when no type info is available.
    """
    result: dict[str, str] = {}
    for d in dimensions_meta:
        name = d.get("name")
        dt = d.get("data_type")
        if name and dt:
            result[name] = dt
    return result


def _is_integer_type(col_type: str | None) -> bool:
    """Return True for connector-native integer type spellings."""
    if not col_type:
        return False
    normalized = str(col_type).strip().lower()
    if "(" in normalized:
        normalized = normalized.split("(", 1)[0].strip()
    return bool(_INTEGER_TYPE_RE.match(normalized))


def _where_literal(value: str, col_type: str | None) -> str:
    """Render a WHERE clause literal, emitting bare numerics for numeric columns.

    Bug-5558: BigQuery integer dimensions fail when a slicer value like
    ``2024`` is rendered as ``'2024'`` (STRING) against an INT64 column.
    When the dimension's ``data_type`` indicates a numeric family AND the
    value passes the strict numeric-literal validator, it is emitted bare.
    All other values (or unknown/text column types) use the safe
    single-quoted form with internal quote escaping.
    """
    from shared.type_family import is_numeric as _is_numeric_type
    if col_type and _is_numeric_type(col_type) and _NUMERIC_LITERAL_RE.match(value):
        if _is_integer_type(col_type) and not _INTEGER_LITERAL_RE.match(value):
            raise ValueError(
                f"Invalid integer slicer literal {value!r} for {col_type} column"
            )
        return value
    # Bug-6074: render the string literal through the sanctioned dialect-correct
    # helper instead of hand-rolled ``''`` doubling. The gateway emits canonical
    # PostgreSQL SQL (the query-router re-parses as postgres and transpiles to
    # the source), so ``postgresql`` is the correct literal dialect here; the
    # helper keeps a client value (incl. a trailing backslash) contained on every
    # downstream dialect and removes the last hand-escaped path feeding the
    # calc-member re-query WHERE clause.
    return _ql("postgresql", value)


def _build_where_sql_clauses(
    where_filters: dict[str, list[str]],
    quote_fn: Callable[[str], str],
    dim_type_map: dict[str, str] | None = None,
) -> list[str]:
    """Convert MDX-extracted where_filters into SQL WHERE clause parts.

    Bug-5558: when *dim_type_map* is provided, numeric dimension values are
    rendered as bare numeric literals (no single-quote wrapping) so BigQuery
    INT64/FLOAT64 columns receive the correct type instead of a STRING
    literal that triggers a type-mismatch error. Non-numeric or
    non-validating values always fall back to quoted string literals.
    """
    dim_types = dim_type_map or {}

    clauses: list[str] = []
    for dim, vals in where_filters.items():
        if not dim:
            continue
        qd = quote_fn(dim)
        col_type = dim_types.get(dim)
        between_vals = [v for v in vals if v.startswith(_RANGE_PREFIX)]
        normal_vals = [v for v in vals if not v.startswith(_RANGE_PREFIX)]
        for bv in between_vals:
            payload = bv[len(_RANGE_PREFIX):]
            start_key, end_key = payload.split(_RANGE_SEP, 1)
            start_lit = _where_literal(start_key, col_type)
            end_lit = _where_literal(end_key, col_type)
            clauses.append(f"{qd} BETWEEN {start_lit} AND {end_lit}")
        if normal_vals:
            if len(normal_vals) == 1:
                lit = _where_literal(normal_vals[0], col_type)
                clauses.append(f"{qd} = {lit}")
            else:
                in_list = ", ".join(
                    _where_literal(v, col_type) for v in normal_vals
                )
                clauses.append(f"{qd} IN ({in_list})")
    return clauses


def _topn_member_predicate(
    *,
    detail_rows: list[dict[str, Any]],
    grain_dim_cols: list[str],
    quote_fn: Callable[[str], str],
    dim_type_map: dict[str, str] | None = None,
) -> str | None:
    """Build a WHERE predicate constraining grain queries to the Top-N member set.

    F-002-01: a Top-N (TopCount/BottomCount) pivot applies ``ORDER BY <measure>
    ... LIMIT N`` to the DETAIL query only. The subtotal / grand-total grain
    queries are built independently from ``where_sql`` and never receive the
    ranked member set, so they aggregate ALL members (e.g. 6 rows) beneath a
    detail axis that shows only the surviving N (e.g. 5 rows) — a silently wrong
    subtotal / grand total.

    The detail result has already had the Top-N ``LIMIT`` applied, so its rows
    are exactly the surviving members. Every coarser subtotal is by definition
    the aggregate over those same surviving detail children, so constraining each
    grain query to the exact set of surviving detail-grain dimension tuples makes
    every requested grain agree with the visible members. Returns the SQL
    predicate (a single-column ``IN`` list, or an ``OR`` of composite tuple
    equalities for a multi-column grain), or ``None`` when there is nothing to
    constrain (no rows, or no grain dimension columns).
    """
    if not detail_rows or not grain_dim_cols:
        return None

    dim_types = dim_type_map or {}

    # Distinct surviving member tuples over the detail grain, preserving first
    # appearance order for deterministic SQL (and stable test assertions).
    seen: set[tuple] = set()
    tuples: list[tuple] = []
    for row in detail_rows:
        key = tuple(row.get(c) for c in grain_dim_cols)
        if key in seen:
            continue
        seen.add(key)
        tuples.append(key)

    if not tuples:
        return None

    def _lit(col: str, value: Any) -> str:
        col_type = dim_types.get(col)
        if value is None:
            # A NULL member value cannot participate in an equality/IN predicate;
            # match it explicitly so a surviving NULL-keyed member is not dropped
            # from the constrained subtotal (which would re-introduce the wrong
            # total this fix exists to prevent).
            return None
        return _where_literal(str(value), col_type)

    # Single-column grain: a compact IN (...) list, with an explicit NULL branch
    # when a surviving member key is NULL.
    if len(grain_dim_cols) == 1:
        col = grain_dim_cols[0]
        qc = quote_fn(col)
        has_null = any(t[0] is None for t in tuples)
        in_lits = [_lit(col, t[0]) for t in tuples if t[0] is not None]
        parts: list[str] = []
        if in_lits:
            parts.append(f"{qc} IN ({', '.join(in_lits)})")
        if has_null:
            parts.append(f"{qc} IS NULL")
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    # Multi-column grain: OR of composite tuple equalities. Each surviving tuple
    # becomes ``(col_a = v_a AND col_b = v_b ...)`` so the constraint matches the
    # exact member combinations that survived Top-N, not the cross-product.
    tuple_clauses: list[str] = []
    for t in tuples:
        conds: list[str] = []
        for col, value in zip(grain_dim_cols, t):
            qc = quote_fn(col)
            if value is None:
                conds.append(f"{qc} IS NULL")
            else:
                conds.append(f"{qc} = {_lit(col, value)}")
        tuple_clauses.append("(" + " AND ".join(conds) + ")")
    if not tuple_clauses:
        return None
    return "(" + " OR ".join(tuple_clauses) + ")"


def _topn_requery_survivor_predicate(
    *,
    rows: list[dict[str, Any]],
    columns: list[str],
    dim_names_set: set[str],
    axis_aliases: dict[str, str],
    dimensions_meta: list[dict[str, Any]],
) -> tuple[str | None, list[str]]:
    """Derive the Top-N survivor WHERE predicate for a calc-member re-query.

    Bug-8283: on a Top-N (TopCount/BottomCount) pivot the calc-member
    ("Show Values As") denominator / aggregate re-queries must aggregate ONLY
    the surviving ranked members. This is the PURE derivation used by
    ``_handle_execute`` — extracted so its correctness on merged / aliased
    inputs can be pinned by a revert-guarding unit test.

    Returns ``(predicate, grain_cols)`` where ``predicate`` is the SQL member-set
    constraint (or ``None`` if it cannot be built) and ``grain_cols`` is the
    post-alias grain column list (used by the caller's fail-loud guard: a
    non-empty ``grain_cols`` with a ``None`` predicate must SOAP-fault rather
    than emit an all-member denominator).

    Three properties this function guarantees, each guarding a distinct
    silent-wrong / hard-fail failure the review rounds found:

    1. DETAIL rows only (R3 finding 1). When the pivot also has a subtotal
       hierarchy, ``rows`` is the MERGED result — subtotal / grand-total rows
       (tagged ``SUBTOTAL_LEVEL_KEY != "detail"``) have ``None`` in their finer
       dimension columns and would inject spurious ``... IS NULL`` survivor
       branches matching hidden blank-member fact rows = contaminated
       denominator. Filter to detail rows, exactly as the evaluator does
       (``mdx_calc_members.py``). A flat pivot has no subtotal rows (no-op).
    2. POST-alias grain cols (R1 finding 1). This runs AFTER
       ``_alias_result_dimensions_for_hierarchy_axes`` renamed result columns to
       hierarchy-alias names, so grain cols are keyed on ``dim_names_set`` (built
       from post-alias ``dimensions_meta``), matching the post-alias ``columns``
       and the re-query specs' ``_dim_cols`` basis. Keying on the pre-alias set
       would find no grain cols -> None -> unconstrained denominator.
    3. SOURCE identifiers in the SQL (R4 finding 1). The re-query is bound by the
       query-router as canonical postgres, whose binder resolves SOURCE
       dimension / level names, not bare MDX hierarchy-alias names. Translate
       each grain col back to its source name via ``axis_aliases``
       (alias->source) for BOTH the quoted identifier and the row-value lookup
       (aliased detail rows retain both keys); this also lets the ``dim_type_map``
       recover the source dim's ``data_type`` (the appended alias meta entry has
       none), so a numeric member renders as a bare literal not a string.
    """
    from src.dax.subtotal_engine import select_detail_rows

    detail_rows = select_detail_rows(rows)
    # Grain cols are alias names post-rename; find them via the post-alias set.
    grain_cols = [c for c in columns if c in dim_names_set]
    # Translate to source names for the SQL (bindable identifiers + source row
    # key + source data_type). Non-aliased cols map to themselves.
    source_cols = [axis_aliases.get(c, c) for c in grain_cols]
    predicate = _topn_member_predicate(
        detail_rows=detail_rows,
        grain_dim_cols=source_cols,
        quote_fn=lambda n: _qi("postgresql", n),
        dim_type_map=_dim_type_map_from_meta(dimensions_meta),
    )
    return predicate, grain_cols


def _translate_requery_partition_identifiers(
    *,
    agg_specs: list,
    denom_specs: list,
    axis_aliases: dict[str, str],
) -> None:
    """Bug-8327: translate calc-member re-query partition-pin identifiers
    alias->source in place.

    The re-query partition pins (``ReQuerySpec.dim_col`` / ``partition_dims`` and
    ``DenomReQuerySpec.partition_dims``) are built by the planners from the
    POST-alias axis column names (``_dim_cols``, keyed on the post-alias
    ``dim_names_set``). But every calc-member re-query is executed through the
    query-router as canonical postgres, whose binder resolves SOURCE dimension /
    level names — not MDX hierarchy-alias names. On an ALIASED pivot the
    untranslated alias identifiers therefore fail to bind and FAULT the whole
    Execute re-query.

    Bug-8283 already fixed the Top-N SURVIVOR predicate this way (see
    ``_topn_requery_survivor_predicate`` — post-alias keying + alias->source
    identifier translation). The partition-pin path was left untranslated; this
    applies the SAME translation to it. Only the emitted IDENTIFIERS are rewritten
    — the partition VALUES were already read from the post-alias detail rows and
    are correct as-is. Non-aliased columns map to themselves, so this is a no-op
    on a flat (unaliased) pivot, which is what makes it revert-guardable by an
    aliased-pivot unit test.
    """
    if not axis_aliases:
        return
    for sp in agg_specs or []:
        sp.dim_col = axis_aliases.get(sp.dim_col, sp.dim_col)
        sp.partition_dims = [axis_aliases.get(d, d) for d in sp.partition_dims]
    for sp in denom_specs or []:
        sp.partition_dims = [axis_aliases.get(d, d) for d in sp.partition_dims]


class _TopNSpec:
    __slots__ = ("count", "measure", "descending")

    def __init__(self, count: int, measure: str, descending: bool) -> None:
        self.count = count
        self.measure = measure
        self.descending = descending


class _FilterCond:
    __slots__ = ("measure", "operator", "value", "coalesce_default")

    def __init__(
        self,
        measure: str,
        operator: str,
        value: str,
        coalesce_default: str | None = None,
    ) -> None:
        self.measure = measure
        self.operator = operator
        self.value = value
        # Bug-5523: when the MDX operand was `CoalesceEmpty([Measures].[m], d)`,
        # `coalesce_default` carries `d` so the HAVING can render
        # `COALESCE(AGG(m), d) op value` and reproduce MDX's treatment of empty/
        # null measure cells (which evaluate against `d`, not as unknown).
        self.coalesce_default = coalesce_default


class _FilterSpec:
    """A value filter over one or more measure-threshold predicates.

    The named-list compiler (`shared.named_list_compiler._compile_filter`) joins
    every condition with a single AND/OR inside one MDX `Filter(...)` call, e.g.
    ``Filter([cat].Members, [Measures].[a] > 1 AND [Measures].[b] < 2)``. The
    gateway must translate *all* predicates and the joining logic — dropping any
    of them would silently contradict the deployed named set and the model-
    service preview (`_build_filter_having`), which renders every condition.
    """

    __slots__ = ("conditions", "logic")

    def __init__(self, conditions: list[_FilterCond], logic: str) -> None:
        self.conditions = conditions
        self.logic = logic


def _extract_topn_spec(axis_text: str) -> _TopNSpec | None:
    """Extract TopCount or BottomCount from MDX axis text."""
    m = re.search(
        r'\b(TopCount|BottomCount)\s*\([^,]+,\s*(\d+)\s*,\s*\[Measures\]\.\[([^\]]+)\]',
        axis_text, re.IGNORECASE,
    )
    if m:
        func = m.group(1).lower()
        return _TopNSpec(
            count=int(m.group(2)),
            measure=m.group(3).strip(),
            descending=(func == "topcount"),
        )
    return None


_FILTER_PREDICATE = re.compile(
    r'\[Measures\]\.\[([^\]]+)\]\s*(>=|<=|<>|>|<|=)\s*([0-9.]+)',
    re.IGNORECASE,
)

# Bug-5505: comparison operators and a bare-measure reference shape, mirrored from
# mdx_execute's `_CMP_OP` / `_MEASURE_REF` (Bug-5495) so the two layers agree on
# which wrapped Filter() operands collapse to a bare measure predicate.
_FILTER_CMP_OP = r'(?:>=|<=|<>|>|<|=)'
# Bug-6717: accept ]] inside bracket bodies (MDX escaping of ]).
_FILTER_BRACKET_MEASURE = r'\[Measures\]\.\[(?:[^\]]|\]\])+\]'
# A measure operand wrapped only in balanced parentheses: `([Measures].[m])` or
# `(( [Measures].[m] ))`. Collapsed to the bare reference before predicate
# extraction so a paren-wrapped operand reads as the bare form.
_FILTER_PAREN_WRAPPED_MEASURE = re.compile(
    rf'\(+\s*({_FILTER_BRACKET_MEASURE})\s*\)+',
    re.IGNORECASE,
)


def _normalize_wrapped_filter_operands(cond_text: str) -> str:
    """Collapse *paren*-wrapped measure operands to the bare form.

    Bug-5505 / Bug-5523: Excel and the model-service preview emit Filter()
    conditions whose measure operand is wrapped in balanced parentheses —
    `([Measures].[net_sales]) > 5` or `(( [Measures].[m] )) >= 10`. A
    parenthesis group is *semantically identical* to its inner reference, so the
    bare predicate `[Measures].[m] > 5` produces an identical filter and these
    are collapsed here before predicate extraction. This keeps the numeric
    matcher and the fail-loud count guard (both written for the bare shape)
    working unchanged, and never touches a set-wrapped axis measure
    (`{[Measures].[m]}`) since that is never adjacent to a comparison operator.

    Scalar *function*-wrapped operands (`CoalesceEmpty([Measures].[m], 0) > 5`)
    are NOT collapsed here. A function such as ``CoalesceEmpty`` changes the
    measure's empty/null semantics, so stripping it to a bare reference would
    silently produce a different-cardinality result for some operators (Bug-5523:
    `= 0`, `<= 0`, `< d`). Those operands are matched by
    ``_FILTER_COALESCE_PREDICATE`` in ``_extract_filter_spec``, which preserves
    the coalesce default for the HAVING render. This function only normalizes the
    neutral paren form.
    """
    if not cond_text or "[Measures]" not in cond_text:
        return cond_text

    out = cond_text

    # Paren-wrapped operand: `([Measures].[m])` -> `[Measures].[m]`. Only when
    # adjacent to a comparison operator, so a slicer tuple `([Dim].[m])` that
    # happens to hold a measure is not disturbed — but in a Filter condition a
    # paren-wrapped measure beside a comparison is always a predicate operand.
    def _unwrap_paren(match: "re.Match") -> str:
        span_start, span_end = match.span()
        after = out[span_end:].lstrip()
        before = out[:span_start].rstrip()
        if re.match(_FILTER_CMP_OP, after) or re.search(_FILTER_CMP_OP + r'\s*$', before):
            return match.group(1)
        return match.group(0)

    out = _FILTER_PAREN_WRAPPED_MEASURE.sub(_unwrap_paren, out)
    return out


# Bug-5523: a `CoalesceEmpty([Measures].[m], <numeric default>) <op> <value>`
# predicate. The measure, default, comparison operator and threshold are all
# captured so the HAVING can render `COALESCE(AGG(m), default) op value` and
# faithfully reproduce MDX's empty/null handling (an empty measure cell is
# treated as the default, not as SQL NULL). The default is restricted to a
# numeric literal — the only form the aggregate HAVING can express — so a
# non-numeric default (`CoalesceEmpty([m], "x")`) falls through and defers to the
# fail-loud guard rather than being mistranslated.
_FILTER_COALESCE_PREDICATE = re.compile(
    r'CoalesceEmpty\s*\(\s*\[Measures\]\.\[([^\]]+)\]\s*,\s*(-?[0-9.]+)\s*\)'
    r'\s*(>=|<=|<>|>|<|=)\s*(-?[0-9.]+)',
    re.IGNORECASE,
)

def _filter_condition_text(axis_text: str) -> str | None:
    """Return the condition argument of the first ``Filter(set, <cond>)`` call.

    The body is bounded by matching the parenthesis depth from the ``Filter(``
    opener — NOT by ``rfind(')')`` — so an outer wrapper that carries its own
    later argument (e.g. ``OuterFn(Filter(set, p1), p2)``) cannot leak ``p2``
    into the captured condition text, and a trailing `` ) ON ROWS FROM [cube]``
    is excluded.
    """
    fm = re.search(r'\bFilter\s*\(', axis_text, re.IGNORECASE)
    if not fm:
        return None
    # Walk from the Filter's opening paren to its matching close.
    i = fm.end() - 1  # index of the '('
    depth = 0
    end = -1
    for j in range(i, len(axis_text)):
        ch = axis_text[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end == -1:
        return None
    inner = axis_text[i + 1:end]
    # Drop the set argument (everything up to the first top-level comma) so only
    # the condition text remains.
    depth = 0
    for k, ch in enumerate(inner):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[k + 1:].strip()
    return None


def _extract_filter_spec(
    axis_text: str, measure_names: set[str],
) -> _FilterSpec | None:
    """Extract ``Filter(set, [Measures].[M] op value [AND|OR ...])``.

    Captures *every* measure-threshold predicate inside the first `Filter(...)`
    condition argument and the single AND/OR that joins them, mirroring the
    compiler's output. Bare and paren-wrapped operands render as
    ``AGG(m) op value``; a ``CoalesceEmpty([Measures].[m], d) op value`` operand
    is captured with its default so the HAVING renders ``COALESCE(AGG(m), d) op
    value`` and preserves MDX's empty/null semantics (Bug-5523). Returns ``None``
    (deferring to the fail-loud guard in ``_mdx_to_sql``) whenever the whole
    condition cannot be translated faithfully — unknown measure, a mixed AND/OR
    body, or any measure / coalesce operand the numeric matcher could not consume
    (e.g. a string-valued comparison or a non-numeric coalesce default) — so no
    condition is ever silently dropped or mistranslated.
    """
    cond_text = _filter_condition_text(axis_text)
    if not cond_text:
        return None

    # Bug-5505/Bug-5523: collapse only the neutral *paren*-wrapped operand
    # (`([Measures].[m]) > 5`) to the bare `[Measures].[m] > 5` form. Scalar
    # function-wrapped operands such as `CoalesceEmpty([m], 0) > 5` are NOT
    # collapsed (that would drop the coalesce default and change empty/null
    # semantics) — they are captured separately below with the default preserved.
    cond_text = _normalize_wrapped_filter_operands(cond_text)

    known = {n.lower() for n in measure_names}
    conditions: list[_FilterCond] = []
    # Bug-5523 (Codex round 3): the character spans of every recognised predicate,
    # so the WHOLE-CONDITION CONSUMED-SPAN CHECK below can verify nothing in the
    # Filter body is left unconsumed. Each recognised predicate contributes its
    # `(start, end)` here; the residual (cond_text minus these spans) must collapse
    # to only the supported boolean structure (AND/OR + parens/whitespace), or the
    # whole Filter() fails loud. This replaces the earlier per-shape occurrence
    # count guards, which only counted coalesce/bare comparisons *on a measure* and
    # therefore let a coalesce on a NON-measure attribute, an unhandled scalar
    # function, or any other sub-condition slip through under AND/OR.
    consumed_spans: list[tuple[int, int]] = []

    # 1) CoalesceEmpty operands first, so their inner measure is not also seen by
    #    the bare-predicate scan and so the coalesce default reaches the HAVING.
    for cm in _FILTER_COALESCE_PREDICATE.finditer(cond_text):
        meas = cm.group(1).strip()
        if meas.lower() not in known and meas not in measure_names:
            return None
        conditions.append(
            _FilterCond(meas, cm.group(3), cm.group(4), coalesce_default=cm.group(2))
        )
        consumed_spans.append(cm.span())

    # 2) Bare / paren-collapsed measure-threshold predicates. The bare-predicate
    #    regex requires an operator immediately after `[Measures].[m]`, so the
    #    `[Measures].[m], d` inside a CoalesceEmpty call is never matched here.
    #    Skip any match that overlaps an already-consumed coalesce span (the
    #    coalesce default `, 0) > 5` cannot match the bare shape, but guard anyway
    #    so an inner reference is never double-counted).
    for pm in _FILTER_PREDICATE.finditer(cond_text):
        if _span_overlaps(pm.span(), consumed_spans):
            continue
        meas = pm.group(1).strip()
        if meas.lower() not in known and meas not in measure_names:
            # An unknown measure predicate — let the fail-loud guard reject the
            # whole Filter rather than translate a partial condition set.
            return None
        conditions.append(_FilterCond(meas, pm.group(2), pm.group(3)))
        consumed_spans.append(pm.span())
    if not conditions:
        return None

    # WHOLE-CONDITION CONSUMED-SPAN CHECK (Bug-5523, Codex round 3). Blank out the
    # character spans of every recognised predicate, then strip the only structure
    # this translator can faithfully express around them — boolean joiners
    # (AND/OR), parentheses, and whitespace. If ANY residual character remains the
    # Filter body carries a sub-condition the translator did NOT consume (a
    # coalesce on a non-measure attribute, an unhandled scalar function, an extra
    # comparison, a string-valued measure predicate, a stray member reference),
    # so we return None and let `_mdx_to_sql` raise the existing "Unsupported
    # Filter() usage" SOAP fault instead of running a PARTIAL filter that silently
    # drops the unrecognised clause. This is the general close of the leak class
    # the earlier per-shape count guards missed.
    residual_chars = list(cond_text)
    for start, end in consumed_spans:
        for idx in range(start, end):
            residual_chars[idx] = " "
    # The condition with every recognised predicate blanked out: now only the
    # joining boolean structure (and any UNCONSUMED content) survives. Both the
    # residual check below and the AND/OR joiner detection read from THIS string —
    # never raw `cond_text` — so a measure name that literally contains a
    # standalone `AND`/`OR` word (`[Net AND Gross]`) cannot leak a spurious joiner
    # token: it lives inside a consumed predicate span and is already blanked here.
    blanked = "".join(residual_chars)

    # Remove the supported boolean structure: whole-word AND/OR, parentheses, and
    # whitespace. `\bAND\b`/`\bOR\b` only strips standalone joiner words; any
    # surviving `AND`/`OR`-spelled text would have to sit OUTSIDE a consumed span,
    # which by construction means it is part of an unconsumed sub-condition and
    # should fail loud — but member references only appear inside consumed
    # (blanked) predicates, so no genuine residual is masked.
    residual = re.sub(r'\b(?:AND|OR)\b', " ", blanked, flags=re.IGNORECASE)
    residual = re.sub(r'[()\s]+', "", residual)
    if residual:
        return None

    # The compiler uses a single joiner for the whole filter. Reject a body that
    # mixes AND and OR (operator precedence cannot be expressed by one flat
    # HAVING) so the translation never changes the set's membership semantics.
    # Read from `blanked`, not `cond_text`, so a bracket-embedded AND/OR inside a
    # consumed predicate cannot poison the joiner set (span-consistent detection).
    logic_tokens = {t.upper() for t in re.findall(r'\b(AND|OR)\b', blanked, re.IGNORECASE)}
    if logic_tokens == {"AND", "OR"}:
        return None
    logic = "OR" if logic_tokens == {"OR"} else "AND"
    return _FilterSpec(conditions=conditions, logic=logic)


def _span_overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    """True when ``span`` intersects any ``(start, end)`` in ``spans``."""
    s, e = span
    for os_, oe in spans:
        if s < oe and os_ < e:
            return True
    return False


def _count_mdx_function_calls(axis_text: str, func_names: tuple[str, ...]) -> int:
    """Count occurrences of named MDX set functions (case-insensitive).

    Used to detect TopCount/BottomCount calls that the narrow single-spec
    extractor cannot translate (composite first argument, a second occurrence).
    When the count exceeds what was extracted the caller must fail loud rather
    than silently run an over-complete result set (F-002-05).

    ``Filter()`` no longer uses this counter: counting calls against a count of
    extracted specs is exactly the Bug-8926 hole (one call producing two specs
    tested ``1 > 2`` and passed). Filter consumption is tracked per call SPAN via
    ``_iter_mdx_filter_calls``.
    """
    if not axis_text:
        return 0
    pattern = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in func_names) + r')\s*\(',
        re.IGNORECASE,
    )
    return len(pattern.findall(axis_text))


class _LabelFilterSpec:
    """The four predicate fields of a translatable label filter.

    Position-free: this is the render input for ``_label_filter_to_sql`` and
    nothing else. The occurrence evidence the axis audit needs lives on
    ``_AppliedLabelFilter``, which is the only type the production paths build.
    """

    __slots__ = ("dim_ref", "operation", "value", "negated")

    def __init__(self, dim_ref: str, operation: str, value: str, negated: bool) -> None:
        self.dim_ref = dim_ref
        self.operation = operation
        self.value = value
        self.negated = negated


class _MdxFilterCall:
    """One syntactic ``Filter(...)`` call located in an axis expression.

    ``cond_span`` / ``cond_text`` describe the SECOND top-level argument (the
    condition); ``set_text`` is the FIRST — the set being iterated. All three
    are ``None`` when the call's parentheses do not balance or it carries no
    top-level comma — a shape no translator can consume, which the caller's
    consumption audit therefore rejects (fail closed).

    Finding XMLA-LF-B2 (challenger round 2): ``set_text`` is not decoration. The condition's dimension must be
    the dimension of the set being iterated, and until the set argument was
    carried here nothing checked that.
    """

    __slots__ = ("call_span", "cond_span", "cond_text", "set_text")

    def __init__(
        self,
        call_span: tuple[int, int],
        cond_span: tuple[int, int] | None,
        cond_text: str | None,
        set_text: str | None = None,
    ) -> None:
        self.call_span = call_span
        self.cond_span = cond_span
        self.cond_text = cond_text
        self.set_text = set_text


def _iter_mdx_filter_calls(axis_text: str) -> list[_MdxFilterCall]:
    """Locate EVERY syntactic ``Filter(...)`` call and split its two arguments.

    Bug-8926. The previous accounting compared a COUNT of ``Filter(`` tokens
    against a COUNT of extracted label predicates, so one call containing two
    predicates tested ``1 > 2`` and passed — and passed more easily the more
    predicates a single call carried. Two ``OR``-joined label predicates were
    then rendered as two independent clauses joined by ``AND``, silently
    returning a subset of the requested rows.

    Consumption is therefore tracked per CALL SPAN, which requires knowing where
    each call starts and ends. Every ``\\bFilter\\s*(`` occurrence is reported,
    including one nested inside another call's set argument (each has its own
    condition argument, so nested calls each translate independently) and
    including one that occurs inside a string literal. Reporting the latter is
    deliberate: ``_filter_condition_text`` sees it too, so treating it as a call
    that must be consumed keeps the two views aligned and fails CLOSED.

    Parenthesis depth is tracked outside double-quoted string literals so a
    parenthesis or comma inside a label filter's literal cannot mis-bound the
    condition.
    """
    calls: list[_MdxFilterCall] = []
    for fm in re.finditer(r'\bFilter\s*\(', axis_text, re.IGNORECASE):
        open_idx = fm.end() - 1  # index of the '('
        depth = 0
        in_str = False
        end = -1
        for j in range(open_idx, len(axis_text)):
            ch = axis_text[j]
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            # Unbalanced (or unterminated string): no condition can be proven,
            # so record the call with no condition — it can never be consumed.
            calls.append(_MdxFilterCall((fm.start(), len(axis_text)), None, None))
            continue
        call_span = (fm.start(), end + 1)
        # Split off the set argument at the first TOP-LEVEL comma. BRACE depth
        # counts as well as parenthesis depth: `Filter({a, b}, cond)` would
        # otherwise split at the comma INSIDE the member set literal, leaving a
        # condition no pattern can match — rejecting a legitimate label filter
        # (the Bug-8925 availability class) rather than mistranslating it.
        depth = 0
        in_str = False
        comma = -1
        for k in range(open_idx + 1, end):
            ch = axis_text[k]
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "({":
                depth += 1
            elif ch in ")}":
                depth -= 1
            elif ch == "," and depth == 0:
                comma = k
                break
        if comma == -1:
            calls.append(_MdxFilterCall(call_span, None, None))
            continue
        cond_span = (comma + 1, end)
        calls.append(_MdxFilterCall(
            call_span,
            cond_span,
            axis_text[cond_span[0]:cond_span[1]],
            axis_text[open_idx + 1:comma],
        ))
    return calls


@dataclass(frozen=True)
class _MdxSetGroup:
    """One balanced ``(...)`` or ``{...}`` group in an axis expression.

    ``name`` is the identifier immediately preceding an open parenthesis (the
    MDX function being called), ``"{}"`` for a set literal, and ``""`` for a
    bare grouping parenthesis. ``multi_element`` is True when the group contains
    a top-level comma — i.e. it COMBINES two or more things rather than merely
    grouping one.
    """

    open_idx: int
    close_idx: int
    name: str
    multi_element: bool


def _iter_mdx_set_groups(text: str) -> list[_MdxSetGroup]:
    """Locate every bracket group in *text*, outside double-quoted literals.

    An unterminated group is reported as running to the end of the text and as
    multi-element: a shape this scanner cannot account for must constrain more,
    never less (fail closed).
    """
    groups: list[_MdxSetGroup] = []
    stack: list[list[Any]] = []  # [open_idx, open_char, saw_top_level_comma]
    in_str = False
    for i, ch in enumerate(text):
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "({":
            stack.append([i, ch, False])
        elif ch in ")}":
            if not stack:
                continue
            open_idx, open_ch, saw_comma = stack.pop()
            groups.append(_MdxSetGroup(
                open_idx, i + 1, _mdx_group_name(text, open_idx, open_ch),
                bool(saw_comma),
            ))
        elif ch == "," and stack:
            stack[-1][2] = True
    while stack:
        open_idx, open_ch, _saw_comma = stack.pop()
        groups.append(_MdxSetGroup(
            open_idx, len(text), _mdx_group_name(text, open_idx, open_ch), True,
        ))
    return groups


def _mdx_group_name(text: str, open_idx: int, open_ch: str) -> str:
    if open_ch == "{":
        return "{}"
    m = re.search(r'([A-Za-z_][A-Za-z0-9_]*)\s*$', text[:open_idx])
    return m.group(1) if m else ""


# The only two enclosing constructs under which sibling set expressions compose
# CONJUNCTIVELY, which is what AND-joining their rendered WHERE clauses means:
#   CrossJoin(A, B) — the cartesian product of A and B.
#   Filter(set, cond) — the label call sits in the SET argument of an outer
#       Filter, so the outer condition restricts the already-restricted set.
# Everything else with more than one top-level element is refused, including a
# name this scanner does not recognise. A whitelist fails CLOSED; a blacklist of
# known-OR combiners would fail OPEN on the first combiner nobody listed.
_CONJUNCTIVE_SET_COMBINERS = frozenset({"crossjoin", "filter"})


def _assert_label_filters_not_set_combined(
    axis_text: str,
    applied: Sequence[_AppliedLabelFilter],
) -> None:
    """Refuse a label filter that is COMBINED with other set elements.

    Finding XMLA-LF-B1 (challenger round 2). The Bug-8926 guard proved that each ``Filter()`` call's condition
    was consumed completely — consumption per SYNTACTIC UNIT. It said nothing
    about how the units are combined with one another, so

        Union(Filter([R].[R].Members, Left(...) = "US"),
              Filter([R].[R].Members, Right(...) = "East"))

    translated both calls, consumed both calls, and then appended both rendered
    clauses to a list joined by ``AND``. MDX ``Union`` means OR. The query ran
    and silently returned a SUBSET of the requested rows — the Bug-8926
    wrong-numbers fault expressed across calls instead of inside one.

    One label filter is enough to be wrong: ``Union(Filter(...), {[R].[R].&[E]})``
    OR-combines a rendered LIKE with an enumerated member that the axis audit is
    perfectly happy with, and the two are then AND-joined into a smaller result.

    The generalised lesson, and the reason this is a separate guard rather than
    an extension of the consumption check: consumption must be proven for the
    WHOLE axis expression, not for each syntactic unit in isolation. A guard
    scoped to the unit you happen to be looking at leaves the composition of
    those units unproven.

    Rendering true OR semantics is a larger change (the clause list is flat and
    globally AND-joined). Until it exists, refusing is the only correct answer.
    """
    if not applied:
        return
    groups = _iter_mdx_set_groups(axis_text)
    for lf in applied:
        start, end = lf.filter_call_span
        for group in groups:
            if not group.multi_element:
                continue
            if not (group.open_idx < start and group.close_idx >= end):
                continue
            if group.name.lower() in _CONJUNCTIVE_SET_COMBINERS:
                continue
            combiner = f"{group.name}()" if group.name else "{...}"
            raise ValueError(
                f"Unsupported MDX set combination on an axis: the label filter "
                f"on {lf.dim_ref} is combined with other set elements by "
                f"{combiner}. A set combination such as Union() or a "
                f"multi-element set literal means OR, but the gateway can only "
                f"AND the rendered filter clauses together, which would "
                f"silently return too few rows. Express each label filter over "
                f"its own set instead — nested Filter() calls or CrossJoin() "
                f"compose conjunctively and are supported."
            )


# The member reference a label filter operates on. Named groups so the exact
# source SPAN of the reference can be recorded as translation evidence.
_LABEL_MEMBER_REF = (
    r'(?P<ref>\[(?P<dim>[^\]]+)\](?:\.\[(?P<hier>[^\]]+)\])?'
    r'\.CurrentMember\.Name)'
)

# Bug-8925/Bug-8926: these ANCHOR the whole condition (``\A``/``\Z``). A
# ``re.search`` would recognise a predicate buried inside a composite condition
# and silently drop the rest; an anchored full match means "this Filter() call
# contains exactly this one supported predicate, and nothing else".
_LABEL_LEFT_COND = re.compile(
    r'\A\s*Left\s*\(\s*' + _LABEL_MEMBER_REF +
    r'\s*,\s*(?P<count>\d+)\s*\)\s*(?P<op>=|<>)\s*"(?P<val>[^"]*)"\s*\Z',
    re.IGNORECASE,
)
_LABEL_RIGHT_COND = re.compile(
    r'\A\s*Right\s*\(\s*' + _LABEL_MEMBER_REF +
    r'\s*,\s*(?P<count>\d+)\s*\)\s*(?P<op>=|<>)\s*"(?P<val>[^"]*)"\s*\Z',
    re.IGNORECASE,
)
_LABEL_INSTR_COND = re.compile(
    r'\A\s*InStr\s*\(\s*' + _LABEL_MEMBER_REF +
    r'\s*,\s*"(?P<val>[^"]*)"\s*\)\s*(?P<op>>|=)\s*0\s*\Z',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _AppliedLabelFilter:
    """Occurrence-bound proof that ONE axis label predicate was translated.

    Bug-8925. The enumerated-member audit rejects any ``[Dim].[Hier].<member>``
    reference on a ROWS/COLUMNS axis that produced no filter. A label filter's
    ``[Dim].[Hier].CurrentMember.Name`` reference matches that grammar but is
    restricted through a DIFFERENT channel (a rendered ``LIKE`` clause), so the
    audit faulted the whole Execute and the shipped Excel "Label Filter" feature
    was unavailable.

    The evidence handed to the audit is a SPAN, never a dimension name. The audit
    exempts exactly the bracket reference contained in a proven translated
    context; a second ``.CurrentMember`` occurrence — even on the same dimension
    — remains subject to rejection. A global ``.CurrentMember`` token exemption
    and a dimension-level exemption are both unsafe and deliberately absent:
    ``Filter([R].[R].Members, Left(...) = "US" AND UnsupportedFn(
    [R].[R].CurrentMember.Name))`` would excuse BOTH references when only one was
    translated.

    ``context_text`` pins the string identity of ``context_span``. The span is
    only meaningful against the exact string it was computed from; if extraction
    ran on one axis string and the audit iterates another (normalized, stripped,
    re-concatenated), every span is silently wrong — and a wrong span grants a
    spurious EXEMPTION, the unsafe direction. The audit re-slices and compares,
    and fails closed on a mismatch.

    ``sql_clause`` is rendered BEFORE this record exists: "recognised" never
    bypasses the audit. The same string is what the SQL builder appends, so the
    exemption is a proof — this exact reference already produced this predicate.
    """

    dim_ref: str
    operation: str
    value: str
    negated: bool
    context_span: tuple[int, int]
    context_text: str
    filter_call_span: tuple[int, int]
    sql_clause: str


def _match_label_condition(
    cond_text: str,
) -> tuple[str, str | None, str, str, bool, tuple[int, int]] | None:
    """Full-match one ``Filter()`` condition against the supported label shapes.

    Returns ``(dim, hierarchy, operation, value, negated, ref_span)`` or ``None``
    when the condition is not EXACTLY one supported label predicate. ``ref_span``
    is relative to *cond_text*.

    The Left/Right character count is captured (it used to be a bare ``\\d+``,
    discarded) and must equal the literal's length. ``Left(name, 2) = "USA"`` is
    unsatisfiable in MDX — a 2-character prefix cannot equal a 3-character
    literal — yet rendered as ``LIKE 'usa%'`` and returned rows. A disagreeing
    count is rejected rather than reinterpreted.
    """
    for pattern, operation in (
        (_LABEL_LEFT_COND, "begins_with"),
        (_LABEL_RIGHT_COND, "ends_with"),
    ):
        m = pattern.match(cond_text)
        if m:
            value = m.group("val")
            if int(m.group("count")) != len(value):
                return None
            return (
                m.group("dim"), m.group("hier"), operation, value,
                m.group("op") == "<>", m.span("ref"),
            )
    m = _LABEL_INSTR_COND.match(cond_text)
    if m:
        return (
            m.group("dim"), m.group("hier"), "contains", m.group("val"),
            m.group("op") == "=", m.span("ref"),
        )
    return None


def _translate_label_filter_calls(
    axis_text: str,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
    quote_fn: Callable[[str], str],
) -> list[_AppliedLabelFilter]:
    """Translate every ``Filter()`` call that is EXACTLY one label predicate.

    THE single label-filter translator. The main SQL path, the subtotal/grain
    path and the AVG/COUNT_DISTINCT re-query path all consume this one output, so
    a variant supported by one of them can never be silently ignored by another.

    Contract: one exact supported label predicate per ``Filter()`` call. Multiple
    label filters stay supported when the client expresses them as separate or
    nested calls (they compose conjunctively, which is what ``AND``-joining the
    rendered clauses means). A COMPOSITE condition inside one call — including
    ``Left(...) = "US" OR Right(...) = "East"`` and
    ``Left(...) = "US" AND [Measures].[Sales] > 100`` — is NOT translated here;
    it leaves the call unconsumed and the caller's consumption audit fails the
    Execute closed. Boolean semantics inside one call are not implemented, and
    guessing them is exactly the Bug-8926 wrong-numbers fault.

    Finding XMLA-LF-B2 (challenger round 2): the condition's dimension must ALSO be the dimension of the SET
    being iterated. ``Filter([Product].[Product].Members, Left([Region].[Region]
    .CurrentMember.Name, 2) = "US")`` iterates products while testing the region
    current member, which comes from the surrounding query context — it is not
    the iterated member at all. Rendering it as a row-level ``region LIKE 'us%'``
    is silently incorrect filtering, and dimension extraction can add region to
    the SQL grain on top of that. A set argument that does not provably resolve
    to exactly the condition's dimension is refused, including one that cannot
    be resolved at all: "could not parse it" is never a reason to accept.

    A call is skipped (left unconsumed, therefore rejected upstream) when the
    condition is not an exact label predicate, the character count disagrees with
    the literal, the dimension/hierarchy does not resolve to a known dimension,
    the set argument's dimension does not resolve or does not agree with the
    condition's, or the SQL clause does not render.
    """
    applied: list[_AppliedLabelFilter] = []
    for call in _iter_mdx_filter_calls(axis_text):
        if call.cond_span is None or call.cond_text is None:
            continue
        matched = _match_label_condition(call.cond_text)
        if matched is None:
            continue
        dim_part, hier_part, operation, value, negated, ref_span = matched
        dim_ref = _resolve_label_filter_dim(
            dim_part, hier_part, dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if not dim_ref or dim_ref not in dim_names:
            continue
        set_dim = _resolve_filter_set_dimension(
            call.set_text, dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if set_dim is None or set_dim != dim_ref:
            continue
        # Render BEFORE the evidence exists. "Recognised" must never bypass the
        # audit; only a successfully rendered clause earns the exemption.
        sql_clause = _label_filter_to_sql(
            _LabelFilterSpec(dim_ref, operation, value, negated), quote_fn,
        )
        if not sql_clause:
            continue
        start = call.cond_span[0] + ref_span[0]
        end = call.cond_span[0] + ref_span[1]
        applied.append(_AppliedLabelFilter(
            dim_ref=dim_ref,
            operation=operation,
            value=value,
            negated=negated,
            context_span=(start, end),
            context_text=axis_text[start:end],
            filter_call_span=call.call_span,
            sql_clause=sql_clause,
        ))
    return applied


# A member / level reference inside a ``Filter()`` SET argument. The trailing
# ``(?:\.&?\[...\])*`` swallows the key or caption tail of an enumerated member
# (``[R].[R].&[US-East]``) so its key is never mistaken for a dimension of its
# own. The lookbehind stops a match starting mid-chain.
_SET_ARG_MEMBER_REF = re.compile(
    r'(?<![\]\w.])\[([^\]]+)\]'
    r'(?:\.\[([^\]]+)\])?'
    r'(?:\.\[([^\]]+)\])?'
    r'(?:\.&?\[[^\]]*\])*'
)


def _resolve_filter_set_dimension(
    set_text: str | None,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
) -> str | None:
    """Resolve the ONE dimension a ``Filter()`` set argument iterates.

    Finding XMLA-LF-B2 (challenger round 2). Returns ``None`` — which the caller treats as "refuse" — when the
    set argument is empty, when any member reference in it does not resolve to a
    known dimension, or when it spans MORE than one dimension (a CrossJoin set,
    say: the condition cannot be proven to test the iterated member).

    Every reference in the argument must resolve, not merely one of them. A set
    that is partly unresolvable is exactly the case where "the condition matches
    the bit I could read" would license a filter on a dimension the query never
    iterates.

    Measures are skipped: they are never the iterated dimension and appear in a
    nested condition when the set argument is itself a ``Filter()`` call, whose
    own set references resolve alongside.
    """
    if not set_text or not set_text.strip():
        return None
    # A double-quoted literal is never a member reference. Blanked out (not
    # deleted, so nothing is re-joined into a new reference) because a set
    # argument that is itself a nested `Filter()` carries that call's condition,
    # and `InStr(..., "a[b]c")` would otherwise present `[b]` as an
    # unresolvable dimension and refuse a legitimate query.
    scannable = re.sub(r'"[^"]*"', lambda m: " " * len(m.group(0)), set_text)
    resolved_dims: set[str] = set()
    for m in _SET_ARG_MEMBER_REF.finditer(scannable):
        dim_part = (m.group(1) or "").strip()
        if dim_part.lower() == "measures":
            continue
        hier_part = (m.group(2) or "").strip() or None
        level_part = (m.group(3) or "").strip() or None
        # `[Dim].[Hier].[X]` is ambiguous between a LEVEL and a caption MEMBER.
        # Try the most specific reading first and fall back, so an enumerated
        # member does not look like an unresolvable level.
        candidate: str | None = None
        for hier, level in ((hier_part, level_part), (hier_part, None), (None, None)):
            got = _resolve_hierarchy_dimension_name(
                dim_name=dim_part,
                hierarchy_name=hier,
                level_name=level,
                dim_names=dim_names,
                hierarchy_level_dim_map=hierarchy_level_dim_map,
                hierarchy_default_dim_map=hierarchy_default_dim_map,
            )
            if got and got in dim_names:
                candidate = got
                break
        if candidate is None:
            return None
        resolved_dims.add(candidate)
    if len(resolved_dims) != 1:
        return None
    return next(iter(resolved_dims))


def _resolve_label_filter_dim(
    dim_part: str,
    hier_part: str | None,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
) -> str | None:
    """Resolve an MDX dimension reference from a label filter to a SQL column name."""
    resolved = _resolve_hierarchy_dimension_name(
        dim_name=dim_part.strip(),
        hierarchy_name=hier_part.strip() if hier_part else None,
        level_name=None,
        dim_names=dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    return resolved if resolved else None


def _label_filter_to_sql(spec: _LabelFilterSpec, quote_fn) -> str:
    """Convert a label filter spec to a SQL WHERE clause fragment.

    Bug-6938 / Bug-6635: the string-LITERAL body routes through the sanctioned
    ``_ql()`` (``connector_qualify.quote_literal``) path — no hand-rolled
    ''-doubling. The LIKE-PATTERN metacharacters (``%`` ``_``) and the ESCAPE
    character (``\\``) still have to be escaped explicitly BEFORE the literal is
    formed: ``quote_literal`` handles string-literal quoting only, not LIKE
    wildcard semantics, and there is no shared LIKE-metachar helper. On
    PostgreSQL (standard-conforming strings) ``_ql`` doubles ``'`` and preserves
    ``\\`` verbatim, so the ``\\``-doubled metachars survive into the pattern and
    are consumed by ``ESCAPE '\\'`` — matching a literal ``%`` / ``_`` / ``\\``.
    """
    col = f"LOWER({quote_fn(spec.dim_ref)})"
    like_safe = (
        spec.value.lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    op = "NOT LIKE" if spec.negated else "LIKE"
    if spec.operation == "begins_with":
        pattern = f"{like_safe}%"
    elif spec.operation == "ends_with":
        pattern = f"%{like_safe}"
    else:
        pattern = f"%{like_safe}%"
    quoted = _ql("postgresql", pattern)
    return f"{col} {op} {quoted} ESCAPE '\\'"


_TIME_GRAIN_RANK = {
    "date": 0,
    "day": 0,
    "daily": 0,
    "week": 1,
    "weekly": 1,
    "month": 2,
    "monthly": 2,
    "quarter": 3,
    "quarterly": 3,
    "year": 4,
    "yearly": 4,
}


def _dimension_is_time(dim_meta: dict[str, Any]) -> bool:
    return bool(
        dim_meta.get("is_time_dim")
        or str(dim_meta.get("dimension_kind") or "").strip().lower() == "time"
        or dim_meta.get("time_grain")
    )


def _finest_time_dimension_name(dimensions_meta: list[dict[str, Any]]) -> str | None:
    time_dims = [d for d in dimensions_meta if d.get("name") and _dimension_is_time(d)]
    if not time_dims:
        return None
    return min(
        time_dims,
        key=lambda d: _TIME_GRAIN_RANK.get(
            str(d.get("time_grain") or "").strip().lower(),
            99,
        ),
    ).get("name")


def _lne_measures_for_mdx(
    mdx_measures: list[str],
    measures_meta: list[dict[str, Any]],
    measure_canonical: dict[str, str],
) -> list[str]:
    requested = {measure_canonical.get(m.lower(), m) for m in mdx_measures}
    lne: list[str] = []
    for m in measures_meta:
        name = m.get("name", "")
        if name not in requested:
            continue
        default_agg = str(m.get("default_agg") or "").strip().upper()
        semi = str(m.get("semi_additive_behavior") or "").strip().lower()
        if default_agg == "LAST_NON_EMPTY" or semi == "last_non_empty":
            lne.append(name)
    return lne


def _flat_lne_hidden_time_dim(
    *,
    mdx_dims: list[str],
    lne_measures: list[str],
    dimensions_meta: list[dict[str, Any]],
    subtotal_hierarchies: list | None,
) -> str | None:
    """Return a hidden time grain needed to repair flat LAST_NON_EMPTY cells."""
    if not lne_measures or subtotal_hierarchies:
        return None
    time_dim_names = {
        str(d.get("name") or "")
        for d in dimensions_meta
        if d.get("name") and _dimension_is_time(d)
    }
    if any(d in time_dim_names for d in mdx_dims):
        return None
    return _finest_time_dimension_name(dimensions_meta)


def _unsupported_flat_lne_companion_measures(
    *,
    mdx_measures: list[str],
    lne_measures: list[str],
    measure_canonical: dict[str, str],
    measure_agg: dict[str, str],
) -> list[str]:
    unsupported: list[str] = []
    lne_set = set(lne_measures)
    for meas in mdx_measures:
        canonical = measure_canonical.get(meas.lower(), meas)
        if canonical in lne_set:
            continue
        agg = measure_agg.get(canonical, "SUM")
        if agg in {"AVG", "COUNT_DISTINCT"}:
            unsupported.append(canonical)
    return unsupported


def _collapse_flat_lne_rows(
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    hidden_time_dim: str | None,
    lne_measures: list[str],
    measures_meta: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Collapse hidden time-grain rows back to the requested flat pivot grain."""
    if not hidden_time_dim or hidden_time_dim not in columns:
        return columns, rows
    visible_columns = [c for c in columns if c != hidden_time_dim]
    if not rows or not lne_measures:
        return visible_columns, rows

    from src.dax.subtotal_engine import _last_non_empty_value

    measure_names = {str(m.get("name") or "") for m in measures_meta if m.get("name")}
    group_cols = [c for c in columns if c != hidden_time_dim and c not in measure_names]
    measure_cols = [c for c in columns if c != hidden_time_dim and c in measure_names]
    measure_agg = {
        str(m.get("name") or ""): str(m.get("default_agg") or "sum").strip().upper()
        for m in measures_meta
        if m.get("name")
    }

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row.get(c) for c in group_cols)
        groups.setdefault(key, []).append(row)

    collapsed_rows: list[dict[str, Any]] = []
    for key, group_rows in groups.items():
        out = {col: key[idx] for idx, col in enumerate(group_cols)}
        for meas in measure_cols:
            if meas in lne_measures:
                out[meas] = _last_non_empty_value(group_rows, meas, hidden_time_dim)
                continue
            vals = [
                r.get(meas) for r in group_rows
                if r.get(meas) is not None and str(r.get(meas)).strip() != ""
            ]
            if not vals:
                out[meas] = None
                continue
            agg = measure_agg.get(meas, "SUM")
            try:
                nums = [float(v) for v in vals]
            except (TypeError, ValueError):
                out[meas] = vals[-1]
                continue
            if agg in {"SUM", "COUNT", "COUNT_DISTINCT"}:
                out[meas] = sum(nums)
            elif agg == "MIN":
                out[meas] = min(nums)
            elif agg == "MAX":
                out[meas] = max(nums)
            elif agg == "AVG":
                out[meas] = sum(nums) / len(nums)
            else:
                out[meas] = nums[-1]
        collapsed_rows.append(out)

    return visible_columns, collapsed_rows


def _mdx_to_sql(
    mdx: str,
    measures_meta: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_meta: list[dict[str, Any]] | None = None,
    model_slug: str = "",
    subtotal_hierarchies: list | None = None,
    constant_measure_names: set[str] | None = None,
) -> tuple[str, str]:
    """
    Translate an MDX statement from Excel into SQL for the query-router.

    Returns (sql_string, protocol).
    Protocol is "jdbc" so the query-router uses its SQL parser.
    """
    # Build lookup for measure aggregation types (case-insensitive)
    measure_agg: dict[str, str] = {}
    measure_names: set[str] = set()
    measure_canonical: dict[str, str] = {}
    for m in measures_meta:
        name = m.get("name", "")
        measure_names.add(name)
        measure_canonical[name.lower()] = name
        measure_agg[name] = m.get("default_agg", "sum").upper()

    (
        dim_names,
        hierarchy_level_dim_map,
        hierarchy_default_dim_map,
    ) = _build_hierarchy_dimension_map(dimensions_meta, hierarchy_meta or [])

    # Strip CELL PROPERTIES clause
    cleaned = re.sub(r"\s+CELL\s+PROPERTIES\s+.*$", "", mdx, flags=re.IGNORECASE).strip()

    # Extract subselect filter members (Excel slicer pattern), supports nesting:
    #   FROM (SELECT {members} ON 0 FROM (SELECT ... FROM [cube]))
    subselect_filters = _mdx_extract_subselect_filters(
        cleaned, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    # Strip all subselect layers, peeling innermost first, until only FROM [cube] remains
    while True:
        sub_match = re.search(
            r'\bFROM\s*\(\s*SELECT\s+(.+?)\s+ON\s+(?:COLUMNS|0)\s+FROM\s+\[[^\]]+\]\s*\)',
            cleaned, re.IGNORECASE | re.DOTALL,
        )
        if not sub_match:
            break
        _check_unsupported_mdx_constructs(sub_match.group(1), "subselect", allow_range=True)
        cube_name = re.search(
            r'FROM\s+\[([^\]]+)\]', sub_match.group(0), re.IGNORECASE
        ).group(1)
        cleaned = cleaned[:sub_match.start()] + "FROM [" + cube_name + "]" + cleaned[sub_match.end():]

    # Extract axis expressions and WHERE/slicer
    col_expr = _mdx_axis_expr(cleaned, 0)   # ON COLUMNS / ON 0
    row_expr = _mdx_axis_expr(cleaned, 1)   # ON ROWS / ON 1
    where_expr = _mdx_where_expr(cleaned)

    def _q(name: str) -> str:
        return _qi("postgresql", name)

    # Extract TopCount/BottomCount/Filter before validation so we can
    # translate them to SQL instead of rejecting them.
    #
    # Bug-8925/Bug-8926: ``axis_text`` is built ONCE here and is the only string
    # the label-filter translator and the axis member audit ever see. The
    # translation evidence handed to the audit is a set of character SPANS, and a
    # span is only meaningful against the exact string it was measured on — so
    # the two sides must not independently re-derive "the axis text".
    axis_text = col_expr + " " + row_expr
    topn_spec = _extract_topn_spec(axis_text)
    filter_spec = _extract_filter_spec(axis_text, measure_names)
    applied_label_filters = _translate_label_filter_calls(
        axis_text, dim_names,
        hierarchy_level_dim_map, hierarchy_default_dim_map,
        quote_fn=_q,
    )

    # F-002-05: the single-spec extractors above match only the first, simple
    # TopCount/BottomCount/Filter shape. A composite first argument
    # (TopCount(CrossJoin(...), N, ...)) or a SECOND occurrence is NOT consumed
    # and would otherwise pass validation and run WITHOUT the intended LIMIT /
    # HAVING — an over-complete result that looks like a working filter. Detect
    # every occurrence and fail loud on anything the translator did not consume,
    # honouring this module's fail-loud policy (Bug-490).
    topn_calls = _count_mdx_function_calls(axis_text, ("TopCount", "BottomCount"))
    topn_consumed = 1 if topn_spec is not None else 0
    if topn_calls > topn_consumed:
        raise ValueError(
            "Unsupported TopCount/BottomCount usage on an axis. Only a single "
            "TopCount/BottomCount over a member set with a numeric count and a "
            "[Measures] ordering can be translated to SQL; a composite first "
            "argument (e.g. CrossJoin) or multiple Top-N calls would return "
            "incorrect (unfiltered) results."
        )

    # Bug-8926: whole-`Filter()`-call consumption. Consumption is tracked by the
    # SPAN of each call, not by comparing a count of calls against a count of
    # extracted predicates. The old count comparison passed whenever one call
    # produced two or more specs (`1 > 2` is False) — so two OR-joined label
    # predicates inside one call were accepted and then rendered as two clauses
    # joined by AND, silently returning too few rows. Every located call must now
    # be consumed COMPLETELY by exactly one supported translation family.
    _filter_calls = _iter_mdx_filter_calls(axis_text)
    _label_call_spans = [lf.filter_call_span for lf in applied_label_filters]
    _label_consumed = set(_label_call_spans)
    # "One exact label predicate per Filter() call" is enforced structurally, not
    # only by the anchored condition match: if any call ever yielded two
    # translations they would be AND-joined below regardless of the MDX joiner,
    # which is precisely the Bug-8926 wrong-numbers fault.
    _unconsumed = len(_label_consumed) != len(_label_call_spans)
    _measure_consumed: set[tuple[int, int]] = set()
    if filter_spec is not None and _filter_calls:
        # `_extract_filter_spec` reads the FIRST `Filter(` call's condition
        # (`_filter_condition_text`), so that is the one call it can consume.
        _first_call_span = _filter_calls[0].call_span
        if _first_call_span not in _label_consumed:
            _measure_consumed.add(_first_call_span)
    _unconsumed = _unconsumed or any(
        call.call_span not in _label_consumed
        and call.call_span not in _measure_consumed
        for call in _filter_calls
    )
    if _unconsumed:
        raise ValueError(
            "Unsupported Filter() usage on an axis. Each Filter() call must be "
            "either a single value filter (Filter(set, [Measures].[M] op value)) "
            "or exactly ONE label filter (Left/Right/InStr over "
            "[Dim].[Hier].CurrentMember.Name, with the character count equal to "
            "the compared literal's length, over a set of that same dimension). "
            "A composite condition inside one Filter() call (for example two "
            "label predicates joined by AND/OR), a condition naming a different "
            "dimension than the set being iterated, or an unrecognised Filter() "
            "call, is rejected because translating it would return incorrect "
            "(wrongly filtered) results."
        )

    has_any_filter = bool(filter_spec) or bool(applied_label_filters)
    has_topn = topn_spec is not None
    _check_unsupported_mdx_constructs(
        col_expr, "COLUMNS axis", allow_topn=has_topn, allow_filter=has_any_filter,
    )
    _check_unsupported_mdx_constructs(
        row_expr, "ROWS axis", allow_topn=has_topn, allow_filter=has_any_filter,
    )
    _check_unsupported_mdx_constructs(where_expr, "WHERE clause")

    # Extract measures and dimensions from all parts. The measure set is derived
    # by the SHARED ``_sql_measure_set`` helper (Bug-6887 Info measures,
    # Bug-6888 KPI constants, Bug-8751 WITH-declared calc members and their input
    # measures) so this path, the flat-LNE grain repair and the subtotal GRAIN
    # queries cannot drift apart.
    all_text = col_expr + " " + row_expr + " " + where_expr
    mdx_measures = _sql_measure_set(
        cleaned, all_text, constant_measure_names=constant_measure_names,
    )
    mdx_dims = _mdx_extract_dimensions(
        col_expr + " " + row_expr,
        dim_names=dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    where_filters = _mdx_extract_where_filters(
        where_expr,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    # Merge subselect slicer filters
    for dim, vals in subselect_filters.items():
        existing = where_filters.get(dim, [])
        for v in vals:
            if v not in existing:
                existing.append(v)
        where_filters[dim] = existing

    # Bug-5548: an enumerated member set on ROWS/COLUMNS must restrict the level
    # to exactly those members. The axis dimension is already GROUP-BY'd by
    # _mdx_extract_dimensions; merge the explicit members in as a WHERE filter so
    # the level is no longer fully expanded. A bare .Members/.Children/
    # .AllMembers expansion produces no filter (full level preserved).
    axis_member_filters = _mdx_extract_axis_member_filters(
        col_expr + " " + row_expr,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    for dim, vals in axis_member_filters.items():
        existing = where_filters.get(dim, [])
        for v in vals:
            if v not in existing:
                existing.append(v)
        where_filters[dim] = existing

    # Bug-1060: every WHERE-slicer dimension member must have resolved AND
    # produced a filter. Reject anything that would otherwise run unfiltered.
    _assert_where_members_applied(
        where_expr, where_filters, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )
    # Bug-5548 (Codex review): the AXIS enumerated-set path must fail loud too.
    # An unknown or partially-resolving member set on ROWS/COLUMNS would
    # otherwise be silently dropped (above, in the merge) and the level run
    # unfiltered — the same fail-open seam as Bug-1060. Level expansions
    # (.Members/.Children/.AllMembers) and (All) are exempt.
    #
    # Bug-8925: the audit also receives the occurrence-bound evidence of what the
    # label-filter channel consumed — spans, never dimension names — so a
    # reference that provably produced a rendered LIKE predicate is exempt and
    # every other reference stays subject to rejection. The SAME `axis_text`
    # object the spans were measured on is passed here.
    _assert_axis_member_references_applied(
        axis_text, where_filters, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
        translated_label_filters=applied_label_filters,
    )
    # Finding XMLA-LF-B1: consumption was proven per Filter() CALL; this proves the whole
    # axis EXPRESSION. Two calls that each translate cleanly still return the
    # wrong rows when the axis combines them with Union() (or any other
    # non-conjunctive set construct), because every rendered clause is appended
    # to one globally AND-joined list below. Runs after the member audit so the
    # existing, more specific member diagnostics are still what a user sees when
    # they also apply.
    _assert_label_filters_not_set_combined(axis_text, applied_label_filters)

    # When subtotals are detected, expand dimensions to include all
    # hierarchy levels so the detail query returns the grain columns
    # needed for parent assignment and hierarchical sorting.
    if subtotal_hierarchies:
        dim_set = set(mdx_dims)
        for sh in subtotal_hierarchies:
            for lvl in sh.levels:
                if lvl.dim_name not in dim_set and lvl.dim_name in dim_names:
                    mdx_dims.append(lvl.dim_name)
                    dim_set.add(lvl.dim_name)

    # If Excel asks only for dimension members on an axis, do not invent measures.
    # Falling back to all measures turns a member-picker request into a fact query.
    if not mdx_measures and not mdx_dims:
        mdx_measures = [m.get("name", "") for m in measures_meta if m.get("name")]

    # If still nothing, return original (fallback)
    if not mdx_measures and not mdx_dims:
        return mdx, "dax"

    lne_measures = _lne_measures_for_mdx(mdx_measures, measures_meta, measure_canonical)
    hidden_lne_time_dim = _flat_lne_hidden_time_dim(
        mdx_dims=mdx_dims,
        lne_measures=lne_measures,
        dimensions_meta=dimensions_meta,
        subtotal_hierarchies=subtotal_hierarchies,
    )
    if lne_measures and not subtotal_hierarchies and hidden_lne_time_dim is None:
        time_dim_names = {
            str(d.get("name") or "")
            for d in dimensions_meta
            if d.get("name") and _dimension_is_time(d)
        }
        if not any(d in time_dim_names for d in mdx_dims):
            raise ValueError(
                "LAST_NON_EMPTY XMLA flat pivots require a DATE/TIME grain "
                "dimension in model metadata so the gateway can evaluate the "
                "latest non-empty period instead of returning SUM."
            )
    if hidden_lne_time_dim and hidden_lne_time_dim not in mdx_dims:
        unsupported_companions = _unsupported_flat_lne_companion_measures(
            mdx_measures=mdx_measures,
            lne_measures=lne_measures,
            measure_canonical=measure_canonical,
            measure_agg=measure_agg,
        )
        if unsupported_companions:
            raise ValueError(
                "LAST_NON_EMPTY XMLA flat pivots cannot be combined with "
                "AVG or COUNT_DISTINCT companion measures because hidden "
                "time-grain repair cannot re-aggregate those measures exactly: "
                + ", ".join(unsupported_companions)
            )
        mdx_dims.append(hidden_lne_time_dim)

    select_parts: list[str] = []
    for dim in mdx_dims:
        if dim in dim_names:
            select_parts.append(_q(dim))
    unresolved_measures: list[str] = []
    for meas in mdx_measures:
        canonical = measure_canonical.get(meas.lower())
        if not canonical:
            unresolved_measures.append(meas)
            continue
        qc = _q(canonical)
        agg = measure_agg.get(canonical, "SUM")
        if agg == "COUNT_DISTINCT":
            select_parts.append(f'COUNT(DISTINCT {qc}) AS {qc}')
        elif agg == "COUNT":
            select_parts.append(f'COUNT({qc}) AS {qc}')
        elif agg == "LAST_NON_EMPTY":
            select_parts.append(f'SUM({qc}) AS {qc}')
        else:
            select_parts.append(f'{agg}({qc}) AS {qc}')

    # Bug-1067: a measure the caller's persona excludes is absent from
    # measure_canonical (it was filtered out of the catalogue metadata). The
    # old code silently dropped it and, when nothing else resolved, forwarded
    # the RAW MDX to the router as protocol=dax — which the router cannot parse
    # (empty IR), so it either errored as source SQL or was rejected by the RLS
    # injector with a misleading message. Fail loud with a clear persona fault
    # instead of forwarding unparseable MDX.
    if unresolved_measures:
        raise ValueError(
            "Measure"
            + ("s" if len(unresolved_measures) > 1 else "")
            + " not available to this persona: "
            + ", ".join(unresolved_measures)
        )

    if not select_parts:
        return mdx, "dax"

    from_table = _q(model_slug or "model_table")
    where_sql_clauses = _build_where_sql_clauses(
        where_filters, _q,
        dim_type_map=_dim_type_map_from_meta(dimensions_meta),
    )
    # Bug-8925: the SAME rendered clause that earned the audit exemption is what
    # reaches the SQL. The exemption is therefore a proof, not an assertion:
    # this exact reference already produced this exact predicate.
    for lf in applied_label_filters:
        where_sql_clauses.append(lf.sql_clause)
    sql = f'SELECT {", ".join(select_parts)} FROM {from_table}'
    if where_sql_clauses:
        sql += f" WHERE {' AND '.join(where_sql_clauses)}"
    if mdx_dims:
        group_cols = [_q(d) for d in mdx_dims if d in dim_names]
        if group_cols:
            sql += f' GROUP BY {", ".join(group_cols)}'
    if filter_spec:
        # The MDX `Filter(set, [Measures].[m] op value ...)` tests *aggregated*
        # measures per member, so every predicate belongs in HAVING over the
        # GROUP BY entity AND must wrap its measure in the measure's aggregate.
        # A bare column reference (`HAVING "m" op value`) is rejected by
        # PostgreSQL (column neither grouped nor aggregated) and mis-resolves on
        # other engines, so each clause mirrors the SELECT-list aggregate and
        # the model-service preview's `_build_filter_having`. All conditions and
        # the compiler's single AND/OR joiner are rendered so the gateway result
        # never silently contradicts the deployed named set.
        having_clauses: list[str] = []
        for cond in filter_spec.conditions:
            canon = measure_canonical.get(cond.measure.lower(), cond.measure)
            agg = measure_agg.get(canon, "SUM")
            qc = _q(canon)
            if agg == "COUNT_DISTINCT":
                agg_expr = f"COUNT(DISTINCT {qc})"
            elif agg == "COUNT":
                agg_expr = f"COUNT({qc})"
            elif agg == "LAST_NON_EMPTY":
                agg_expr = f"SUM({qc})"
            else:
                agg_expr = f"{agg}({qc})"
            # Bug-5523: a `CoalesceEmpty([m], d)` operand replaced an empty/null
            # measure cell with `d` in MDX BEFORE the comparison, so a group whose
            # aggregate is NULL/empty is tested as `d op value` and may be
            # INCLUDED. Plain `AGG(m) op value` yields NULL (unknown -> excluded)
            # for those groups, dropping rows MDX keeps. Wrapping the aggregate in
            # `COALESCE(AGG(m), d)` reproduces the MDX semantics exactly for every
            # operator (`= 0`, `<= 0`, `> 5`, ...). COALESCE is ANSI SQL; the
            # router transpiles to the source dialect downstream.
            if cond.coalesce_default is not None:
                agg_expr = f"COALESCE({agg_expr}, {cond.coalesce_default})"
            having_clauses.append(f"{agg_expr} {cond.operator} {cond.value}")
        joiner = f" {filter_spec.logic} "
        sql += f' HAVING {joiner.join(having_clauses)}'
    if topn_spec:
        canon = measure_canonical.get(topn_spec.measure.lower(), topn_spec.measure)
        direction = "DESC" if topn_spec.descending else "ASC"
        sql += f' ORDER BY {_q(canon)} {direction} LIMIT {topn_spec.count}'
    elif mdx_dims and not axis_member_filters:
        # Bug-6655: add a deterministic ORDER BY on grain dimensions so flat
        # pivot members appear in a stable, reproducible order across source
        # engines and refreshes.  SSAS orders by member key/ordinal
        # (Hierarchize); without this the order depends on the source GROUP BY
        # implementation and can change between queries.
        # Codex-R1-F8: skip when explicit enumerated member sets exist --
        # the MDX set order must be preserved (build_real_execute_response
        # uses result-row appearance order for axis construction).
        order_cols = [_q(d) for d in mdx_dims if d in dim_names]
        if order_cols:
            sql += f' ORDER BY {", ".join(order_cols)}'

    return sql, "jdbc"


def _resolve_hierarchy_dimension_name(
    *,
    dim_name: str,
    hierarchy_name: str | None,
    level_name: str | None,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
) -> str | None:
    if dim_name == "Measures":
        return None

    hkey = (hierarchy_name or dim_name).strip().lower()
    level_key = (level_name or "").strip().lower()

    by_level = hierarchy_level_dim_map.get(hkey)
    if by_level and level_key:
        resolved = by_level.get(level_key)
        if resolved:
            return resolved

    # Bug-5514: two-bracket level reference [Hierarchy].[Level].Members.
    # MDSCHEMA emits levels as [Dim].[Dim].[Level], but Excel/Power BI abbreviate
    # to [Dim].[Level] when the dimension and hierarchy share a name. In that form
    # the parser hands us dim_name=<hierarchy> and hierarchy_name=<level> with no
    # explicit level_name. If dim_name is itself a known multi-level hierarchy and
    # the second segment names one of its levels, resolve to THAT level's grain
    # dimension instead of falling through to the hierarchy's leaf default below.
    if not level_key and hierarchy_name:
        dim_as_hier = hierarchy_level_dim_map.get((dim_name or "").strip().lower())
        if dim_as_hier:
            level_as_second = dim_as_hier.get((hierarchy_name or "").strip().lower())
            if level_as_second:
                return level_as_second

    if hkey in hierarchy_default_dim_map:
        return hierarchy_default_dim_map[hkey]

    if dim_name in dim_names:
        return dim_name
    return dim_name


def _extract_axis_hierarchy_dimension_aliases(
    mdx: str,
    *,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
) -> dict[str, str]:
    """
    Map axis hierarchy names used in MDX to semantic dimension names selected in SQL.
    """
    expr = f"{_mdx_axis_expr(mdx, 0)} {_mdx_axis_expr(mdx, 1)}"
    aliases: dict[str, str] = {}

    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\](?:\.\&?\[[^\]]+\])?(?:\.(?:Members|MEMBERS|AllMembers))?',
        expr,
    ):
        hierarchy_alias = m.group(1).strip()
        resolved = _resolve_hierarchy_dimension_name(
            dim_name=hierarchy_alias,
            hierarchy_name=m.group(2).strip(),
            level_name=m.group(3).strip(),
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        if resolved and resolved in dim_names:
            aliases.setdefault(hierarchy_alias, resolved)

    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\](?:\.\[(?:[^\]]+)\])?(?:\.(?:Members|MEMBERS|AllMembers))?',
        expr,
    ):
        hierarchy_alias = m.group(1).strip()
        resolved = _resolve_hierarchy_dimension_name(
            dim_name=hierarchy_alias,
            hierarchy_name=m.group(2).strip(),
            level_name=None,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        if resolved and resolved in dim_names:
            aliases.setdefault(hierarchy_alias, resolved)

    return aliases


def _alias_result_dimensions_for_hierarchy_axes(
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    alias_to_source: dict[str, str],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Rename SQL result dimension columns to hierarchy aliases expected by axis rendering.
    """
    if not alias_to_source:
        return columns, rows, dimensions_meta

    source_to_aliases: dict[str, list[str]] = {}
    for alias, source in alias_to_source.items():
        if source not in columns or alias in columns:
            continue
        source_to_aliases.setdefault(source, []).append(alias)

    if not source_to_aliases:
        return columns, rows, dimensions_meta

    new_columns: list[str] = []
    for col in columns:
        aliases = source_to_aliases.get(col)
        if aliases:
            new_columns.extend(aliases)
        else:
            new_columns.append(col)

    new_rows: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        for alias, source in alias_to_source.items():
            if source in row:
                out[alias] = row.get(source)
        new_rows.append(out)

    out_dimensions = list(dimensions_meta)
    existing_names = {str(d.get("name") or "") for d in out_dimensions}
    for alias in new_columns:
        source = alias_to_source.get(alias)
        if not source or alias in existing_names:
            continue
        out_dimensions.append({"name": alias})
        existing_names.add(alias)

    return new_columns, new_rows, out_dimensions


# ---------------------------------------------------------------------------
# Named-set inlining (Bug-5499)
# ---------------------------------------------------------------------------

# Bug-6073: MDX reserved words and common set/member/statistical function names.
# The named-set inliner replaces a set reference with the set's stored MDX
# expression. The bracket-quoted form ``[Name]`` is an unambiguous identifier
# and is always safe to replace. The BARE (unquoted) form, however, matches any
# whole-word occurrence — so a set whose name collides with an MDX keyword or
# function (e.g. a set literally named ``Order`` or ``Filter``) would rewrite
# the ``Order(...)`` / ``Filter(...)`` function CALLS inside the very same
# query, corrupting the MDX into an invalid or wrong statement. When a set name
# collides with a token in this set we perform ONLY the bracketed replacement
# and skip the bare one: a legitimately keyword-named set must be referenced in
# brackets to be inlined, which is safe; skipping the ambiguous bare rewrite is
# strictly better than corrupting the statement. Compared case-insensitively.
_MDX_RESERVED_LOWER: frozenset[str] = frozenset(
    w.lower()
    for w in (
        # Statement / axis keywords
        "WITH", "SELECT", "FROM", "WHERE", "ON", "COLUMNS", "ROWS", "PAGES",
        "SECTIONS", "CHAPTERS", "AXIS", "NON", "EMPTY", "MEMBER", "SET", "AS",
        "DIMENSION", "PROPERTIES", "CELL", "CALCULATED", "CURRENTCUBE",
        # Bare syntactic FLAG tokens (Bug-6073, Codex R1). These appear as bare
        # words inside function calls — Order(set, expr, BDESC),
        # Descendants(m, lvl, SELF_AND_BEFORE), DrilldownLevel(set, , POST) — so
        # a set named after one of them would clobber the flag if inlined bare.
        "ASC", "DESC", "BASC", "BDESC",
        "SELF", "AFTER", "BEFORE", "BEFORE_AND_AFTER", "SELF_AND_AFTER",
        "SELF_AND_BEFORE", "SELF_BEFORE_AFTER", "LEAVES",
        "INCLUDEEMPTY", "EXCLUDEEMPTY", "RECURSIVE", "INCLUDE_CALC_MEMBERS",
        "PRE", "POST", "ALL",
        # Logical / conditional
        "CASE", "WHEN", "THEN", "ELSE", "END", "IIF", "IS", "NULL", "AND",
        "OR", "NOT", "XOR",
        # Set / member navigation functions
        "MEMBERS", "CHILDREN", "DESCENDANTS", "ANCESTOR", "ANCESTORS",
        "PARENT", "FIRSTCHILD", "LASTCHILD", "PREVMEMBER", "NEXTMEMBER",
        "LEAD", "LAG", "COUSIN", "SIBLINGS", "CURRENTMEMBER", "DEFAULTMEMBER",
        "ALLMEMBERS", "LEVEL", "LEVELS", "HIERARCHY", "ORDINAL",
        # Set operators / builders
        "FILTER", "ORDER", "TOPCOUNT", "BOTTOMCOUNT", "TOPSUM", "BOTTOMSUM",
        "TOPPERCENT", "BOTTOMPERCENT", "HEAD", "TAIL", "SUBSET", "UNION",
        "EXCEPT", "INTERSECT", "CROSSJOIN", "HIERARCHIZE", "DISTINCT",
        "GENERATE", "EXTRACT", "EXISTS", "NONEMPTY", "DRILLDOWNLEVEL",
        "DRILLDOWNMEMBER", "DRILLUPLEVEL", "DRILLUPMEMBER", "TOGGLEDRILLSTATE",
        # Aggregation / statistical
        "SUM", "COUNT", "AVG", "MIN", "MAX", "AGGREGATE", "MEDIAN", "STDEV",
        "STDDEV", "VAR", "VARIANCE", "RANK", "COALESCEEMPTY",
        # Time-series
        "PARALLELPERIOD", "PERIODSTODATE", "YTD", "QTD", "MTD", "WTD",
        "CLOSINGPERIOD", "OPENINGPERIOD", "LASTPERIODS", "CLOSINGPERIOD",
        # String / value / conversion
        "STRTOSET", "STRTOMEMBER", "STRTOVALUE", "SETTOARRAY", "ITEM",
        "NAME", "UNIQUENAME", "VALUE", "MEMBERVALUE", "FORMAT", "TUPLE",
        "PROPERTIES",
    )
)


def _inline_named_sets(
    mdx: str,
    named_sets: list[dict[str, Any]],
) -> str:
    """Replace named-set references in MDX with their compiled expressions.

    When a BI tool (Excel / Power BI) places a saved named set on an axis, it
    emits the set NAME as a bare reference:

      ``SELECT {[Measures].[Revenue]} ON COLUMNS, {[Top Customers]} ON ROWS ...``

    or without braces:

      ``SELECT {[Measures].[Revenue]} ON COLUMNS, [Top Customers] ON ROWS ...``

    The gateway's axis/dimension extractors do not recognise the bare set name
    (it is not a ``[Dim].[Hier]`` bracket or a ``[Measures].[m]`` reference), so
    no dimensions are detected on the axis and the row axis renders empty.

    This function replaces every named-set reference with the set's stored MDX
    expression (e.g. ``TopCount([customer].[customer].Members, 5, [Measures].[Revenue])``).
    The replacement makes the expression visible to the existing axis/dimension
    extractors and the SQL translator.

    Replacement targets (case-insensitive, whole-word):
      - ``[SetName]`` — bracket-quoted bare name
      - ``SetName``   — unquoted bare name (only when it is not a substring of
                        a longer ``[X].[Y]`` hierarchy path)

    Sets whose expression is empty or whitespace-only are skipped.
    Sets with ``list_type == "sql_fixed"`` are unconditionally skipped (Invariant 8).

    Bug-8713: substitution is iterated to a FIXED POINT. One ordered pass
    resolved a set that references another set only when the parent happened to
    be substituted before the child — i.e. correctness depended on the order the
    model-service returned the rows in. In the other order the parent's
    expression introduced a bare ``[Child]`` token after the child's turn had
    already passed; the axis extractors do not recognise a bare set name, so the
    axis rendered EMPTY rather than failing. Iterating removes the order
    dependence, and the depth cap makes a cyclic definition fail LOUD instead of
    spinning or silently emitting an unresolved token.
    """
    if not named_sets or not mdx:
        return mdx

    result = mdx
    for _pass in range(_NAMED_SET_MAX_EXPANSION_DEPTH):
        expanded = _inline_named_sets_once(result, named_sets)
        if expanded == result:
            break
        result = expanded
    else:
        # The last bounded pass may have produced the final acyclic expansion.
        # Probe one equality pass before classifying the chain as cyclic; this
        # keeps the documented depth cap while allowing exactly ten nested
        # definitions to settle (Bug-8713 / L1-R1-007).
        final = _inline_named_sets_once(result, named_sets)
        if final == result:
            return result
        raise ValueError(
            "Named set expansion did not settle after "
            f"{_NAMED_SET_MAX_EXPANSION_DEPTH} passes; a named set most likely "
            "references itself (directly or through another set). Break the "
            "cycle in the set definitions."
        )
    return result


# Nesting deeper than this is a definition cycle in practice, not a real model.
# The cap is what turns a cycle into a loud error instead of an unbounded loop.
_NAMED_SET_MAX_EXPANSION_DEPTH = 10


def _inline_named_sets_once(
    mdx: str,
    named_sets: list[dict[str, Any]],
) -> str:
    """One ordered substitution pass — see ``_inline_named_sets``."""
    result = mdx
    for ns in named_sets:
        # Skip SQL-type named lists — they must never be inlined as MDX.
        # Invariant 8 (architecture_tessallite-named-lists.md): an SQL list
        # must never be pasted into MDX.  A NULL/empty expression would be
        # harmlessly skipped by the empty-expression check below, but a
        # sql_fixed set that somehow carries a non-empty expression would be
        # wrongly inlined as MDX (silent wrong results).  list_type is the
        # authoritative discriminator, not the expression value.
        if ns.get("list_type") == "sql_fixed":
            continue

        name = ns.get("name", "")
        expression = (ns.get("expression") or "").strip()
        if not name or not expression:
            continue

        # (1) Bracket-quoted form: `[SetName]` not preceded by `.` or `&`
        #     (which would indicate a terminal member like `[Dim].[Hier].[SetName]`
        #     or a key member like `[Dim].[Hier].&[SetName]`) and not followed
        #     by `.[` (which would indicate a hierarchy start like `[SetName].[Hier]`).
        #     Also skip `FROM [SetName]` (cube name) via the callback.
        pattern_bracket = re.compile(
            r'(?<![.&])(\[' + re.escape(name) + r'\])(?!\s*\.)',
            re.IGNORECASE,
        )

        # Bug-5696: capture ``result`` and ``expression`` via default
        # arguments so each iteration's closure binds the current values,
        # not the loop variable by reference (classic Python closure-in-a-
        # loop pitfall).
        def _bracket_replace(
            m: re.Match,
            _result: str = result,
            _expression: str = expression,
        ) -> str:
            # Check whether this match is preceded by FROM + whitespace.
            start = m.start()
            prefix = _result[:start].rstrip()
            if prefix.upper().endswith("FROM"):
                return m.group(0)  # keep the cube name intact
            return _expression

        result = pattern_bracket.sub(_bracket_replace, result)

        # (2) Bare unquoted form: `SetName` as a whole word, not inside brackets,
        #     not preceded by `[` or `.`, not followed by `]` or `.`.
        #     This catches `{SetName}` and `SetName ON ROWS`.
        #
        # Bug-6073: skip the bare rewrite when the set name collides with an MDX
        # keyword/function. The bracketed form above has already inlined any
        # explicit `[SetName]` reference; doing the bare rewrite as well would
        # also match the keyword/function CALLS in the query (e.g. a set named
        # `Order` would clobber `Order(...)`), corrupting the MDX. A
        # keyword-named set must be referenced in brackets to be inlined.
        if name.strip().lower() not in _MDX_RESERVED_LOWER:
            pattern_bare = re.compile(
                r'(?<![.\[\w])' + re.escape(name) + r'(?![.\]\w])',
                re.IGNORECASE,
            )
            # Bug-7252 (CF-018-Fable-F01801): the expression must be treated
            # as a LITERAL replacement, not a regex template.  The old call
            # ``pattern_bare.sub(expression, result)`` interprets backslashes
            # (``\C``, ``\1``, ``\g<...>``) in the expression as group
            # references, raising ``re.error`` for any set whose MDX
            # expression contains a backslash (e.g. fixed-member keys like
            # ``EMEA\Central``).  Using a callable mirrors the bracket path
            # above (line ~4875) and avoids template interpretation.
            result = pattern_bare.sub(lambda m, _e=expression: _e, result)

    return result


_SELECT_KEYWORD_RE = re.compile(r'SELECT\b', re.IGNORECASE)
_WITH_DECL_KEYWORD_RE = re.compile(r'(?:MEMBER|SET)\b', re.IGNORECASE)


def _mdx_visible_positions(text: str):
    """Yield ``(index, paren_depth)`` for each character of *text* that is real
    MDX SYNTAX — never a character inside a string literal, a ``[bracket]`` body
    (``]]``-escape aware), or a comment.

    Block comments are counted with NESTING depth. SSAS MDX block comments nest
    (Bug-6612, ``mdx_execute._mdx_strip_literals_and_comments``); a scanner that
    closes at the first ``*/`` leaks the comment tail back into the syntax
    stream, and a keyword found in that tail is treated as real — fail OPEN.

    One walker so every "find the top-level X" question in this module answers
    from the same lexing rules instead of growing another private scanner.
    """
    n = len(text)
    i = 0
    depth = 0
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "[":
            i += 1
            while i < n:
                if text[i] == "]":
                    if i + 1 < n and text[i + 1] == "]":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if text.startswith("/*", i):
            level = 1
            i += 2
            while i < n and level:
                if text.startswith("/*", i):
                    level += 1
                    i += 2
                elif text.startswith("*/", i):
                    level -= 1
                    i += 2
                else:
                    i += 1
            continue
        if text.startswith("//", i) or text.startswith("--", i):
            nl = text.find("\n", i + 2)
            i = n if nl < 0 else nl + 1
            continue
        if ch in "({":
            depth += 1
            yield i, depth - 1
            i += 1
            continue
        if ch in ")}":
            depth = max(0, depth - 1)
            yield i, depth
            i += 1
            continue
        yield i, depth
        i += 1


def _mdx_top_level_keyword_positions(text: str, pattern: re.Pattern) -> list[int]:
    """Start offsets where *pattern* matches a whole word at paren-depth 0 and
    outside literals/brackets/comments."""
    out: list[int] = []
    for i, depth in _mdx_visible_positions(text):
        if depth != 0:
            continue
        if i and (text[i - 1].isalnum() or text[i - 1] == "_"):
            continue
        if pattern.match(text, i):
            out.append(i)
    return out


def _mdx_scan_statement_body_offset(mdx: str) -> int:
    """Uncached scan for the first top-level ``SELECT`` offset (0 if none)."""
    found = _mdx_top_level_keyword_positions(mdx, _SELECT_KEYWORD_RE)
    return found[0] if found else 0


# Above this length a statement is NOT memoised. The memo holds the only strong
# reference to its key for the process lifetime, so a count-bounded cache is
# byte-UNBOUNDED against a 10 MB request ceiling (deep-review R3 finding 4);
# real Excel statements are ~100 KB, which is what the memo is for.
_MDX_BODY_CACHE_MAX_CHARS = 1_000_000


@functools.lru_cache(maxsize=32)
def _mdx_statement_body_offset_cached(mdx: str) -> int:
    return _mdx_scan_statement_body_offset(mdx)


def _mdx_statement_body_offset(mdx: str) -> int:
    """Offset of the statement's first top-level ``SELECT``, or 0.

    Memoised (deep-review R2 finding 4). ``_mdx_visible_positions`` is a
    per-character Python lexer, and a single ``_handle_execute`` asks the same
    question ~20 times (three axis/where extractors, called from the handler, the
    translator, the subtotal block and the re-query block). Measured on a real
    119 KB Excel keep-only statement: 2.38 M characters re-lexed and ~278 ms of
    pure-Python work per Execute. Statements are immutable strings, so one small
    cache collapses that to a single pass.

    Oversized statements bypass the cache entirely (R3 finding 4): with a 10 MB
    request ceiling, pinning 32 of them would retain hundreds of MB of query text
    — including member values — for the process lifetime.
    """
    if len(mdx) > _MDX_BODY_CACHE_MAX_CHARS:
        return _mdx_scan_statement_body_offset(mdx)
    return _mdx_statement_body_offset_cached(mdx)


def _mdx_statement_body(mdx: str) -> str:
    """Return the statement from its first TOP-LEVEL ``SELECT``, dropping any
    ``WITH`` prelude.

    Bug-8750. Every axis extractor below falls back to
    ``(?:SELECT|,)\\s+(.*?)\\s+ON\\s+(?:COLUMNS|0)`` and ``re.search`` returns the
    LEFTMOST match — so a comma anywhere in a ``WITH MEMBER ... AS ...`` prelude
    (which is where Excel puts every "Show Values As" / custom-group definition)
    anchors the axis-0 capture in the middle of the WITH clause. The captured
    fragment then carries the tail of a calc-member EXPRESSION, which downstream
    reads as axis content: an enumerated member in it becomes a keep-only WHERE
    filter on the main detail SQL (silently one product instead of all), a
    ROWS-axis dimension is attributed to the COLUMNS axis (wrong % of Row/Column
    Total split), and an unfilterable reference trips the Bug-1060 fail-loud audit
    and refuses the whole Execute.

    Fixing it at each regex would leave the next extractor exposed, so the
    statement body is normalised ONCE here and every axis extractor starts from
    it. A subselect's inner ``SELECT`` (always inside ``FROM ( ... )``) and a
    ``SELECT`` inside a member caption, string literal or comment are never
    mistaken for the statement's own. Returns ``mdx`` unchanged when no top-level
    ``SELECT`` exists (a DAX statement, or a fragment already extracted) — the
    pre-fix behaviour, so an unparseable statement degrades safely.
    """
    if not mdx:
        return mdx
    cut = _mdx_statement_body_offset(mdx)
    return mdx[cut:] if cut else mdx


def _mdx_with_prelude(mdx: str) -> str:
    """Everything BEFORE the statement's top-level ``SELECT`` — i.e. the ``WITH``
    formula list, or ``""`` when the statement has none."""
    if not mdx:
        return ""
    cut = _mdx_statement_body_offset(mdx)
    return mdx[:cut] if cut else ""


# ``MEMBER [Measures].[Name]`` and the unbracketed ``MEMBER [Measures].Name``
# form (both are legal MDX and both are emitted in the wild).
_WITH_MEMBER_MEASURE_DECL_RE = re.compile(
    r'MEMBER\s+\[Measures\]\s*\.\s*(?:\[((?:[^\]]|\]\])+)\]|([A-Za-z_]\w*))',
    re.IGNORECASE,
)


def _mdx_declared_calc_measures(mdx: str) -> dict[str, str]:
    """Map ``WITH MEMBER [Measures].[X]`` name -> its expression text (Bug-8751).

    Declaration boundaries are found with the shared top-level walker, so the
    word "Member" inside a bracketed caption (``[Total Member Revenue]``, the
    Bug-6066 R2 shape) or inside a comment does not split a member's expression.
    """
    prelude = _mdx_with_prelude(mdx)
    if not prelude:
        return {}
    starts = _mdx_top_level_keyword_positions(prelude, _WITH_DECL_KEYWORD_RE)
    out: dict[str, str] = {}
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(prelude)
        m = _WITH_MEMBER_MEASURE_DECL_RE.match(prelude, start)
        if not m:
            continue
        raw = m.group(1) or m.group(2) or ""
        if raw:
            out[raw.replace("]]", "]")] = prelude[m.end():end]
    return out


# A measure reference in EITHER legal spelling: ``[Measures].[Name]`` and the
# unbracketed ``[Measures].Name``. Mirrors ``mdx_execute._MEASURE_REF``.
_MEASURE_REF_ANY_FORM_RE = re.compile(
    r'\[Measures\]\s*\.\s*(?:\[((?:[^\]]|\]\])+)\]|([A-Za-z_]\w*))',
    re.IGNORECASE,
)

# MDX member/set FUNCTIONS and PROPERTIES that legally follow ``[Measures].``.
# Deep-review R4 finding 1: the bare alternative above has no positional anchor,
# so it matches any identifier in that position — and in real MDX an identifier
# after ``[Measures].`` is more often a function/property than a member name.
# ``_WITH_MEMBER_MEASURE_DECL_RE`` is safe from this only because it is anchored
# after the ``MEMBER`` keyword, where an identifier IS a name by grammar; the
# reference scan has to earn that guarantee explicitly. Collecting one of these
# as a measure makes ``_mdx_to_sql`` refuse the whole statement with a FALSE
# "Measure not available to this persona: MEMBERS" — a legal pivot denied, with
# a message that misdirects an admin to the persona configuration.
#
# The denylist applies ONLY to the bare alternative. A real measure may
# legitimately be named "Count"; referenced as ``[Measures].[Count]`` it is
# still collected, and still fails loud under Bug-1067 when the persona hides it.
_MDX_MEASURES_NAMESPACE_FUNCTIONS = frozenset({
    "addcalculatedmembers", "allmembers", "caption", "children", "count",
    "currentmember", "defaultmember", "dimension", "firstchild", "firstsibling",
    "hierarchy", "item", "lag", "lastchild", "lastsibling", "lead", "level",
    "levels", "members", "name", "nextmember", "ordinal", "parent",
    "prevmember", "properties", "siblings", "uniquename", "value",
})


def _mdx_measure_refs_any_form(text: str) -> list[str]:
    """Measure names referenced in *text*, in EITHER bracket spelling.

    ``_mdx_extract_measures`` is bracket-only. ``_WITH_MEMBER_MEASURE_DECL_RE``
    deliberately accepts both forms, and that asymmetry inside
    :func:`_sql_measure_set` was a silent-blank defect (deep-review R3 finding
    1): a member DECLARED with brackets whose input is referenced BARE was
    correctly dropped from SQL resolution, and its input was then never added
    back — so the detail SQL projected no measure at all and the pivot rendered
    every cell blank with a 200 and no fault. Producer and consumer must
    recognise the same syntax.

    A BARE token is rejected when it is a known Measures-namespace function or
    property, or when it is immediately applied as a call (``name(``) — see
    ``_MDX_MEASURES_NAMESPACE_FUNCTIONS`` (R4 finding 1).

    Shared-primitive note (CLAUDE.md): the only other place this two-spelling
    syntax is recognised is ``mdx_execute._MEASURE_REF``. Its consumers
    (``_strip_func_wrapped_comparison_measures``, ``_ANCHORED_MEASURE_RE``,
    ``_BARE_MEASURE_RE``) are blanking/stripping passes that tolerate a spurious
    hit; none of them fails closed, so they are NOT exposed to this
    amplification. This function is the only one whose output drives a
    fail-closed SQL projection.
    """
    out: list[str] = []
    seen: set[str] = set()
    body = text or ""
    for m in _MEASURE_REF_ANY_FORM_RE.finditer(body):
        bracketed, bare = m.group(1), m.group(2)
        if bare is not None:
            if bare.lower() in _MDX_MEASURES_NAMESPACE_FUNCTIONS:
                continue
            # Bounded lookahead (never scan the whole remaining statement): an
            # identifier immediately applied as ``name(...)`` is a call, not a
            # member. Whitespace before the paren is legal but never long.
            if body[m.end():m.end() + 8].lstrip()[:1] == "(":
                continue
        raw = bracketed if bracketed is not None else (bare or "")
        name = raw.strip().replace("]]", "]")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _sql_measure_set(
    statement: str,
    axis_and_where_text: str,
    *,
    constant_measure_names: set[str] | None = None,
) -> list[str]:
    """THE measure set the SQL projection must resolve for *statement*.

    Bug-8751 review finding 1. Three places derive "which measures does this
    statement need from the source": ``_mdx_to_sql`` (the detail SQL), the flat
    LAST_NON_EMPTY grain repair, and the subtotal GRAIN queries. They must agree
    — the LNE repair in particular decides whether to collapse a HIDDEN time
    grain that ``_mdx_to_sql`` added, and if it derives a narrower measure set it
    leaves the phantom grain in the result and the pivot renders NO cells at all
    (HTTP 200, no fault). One helper, three callers.

    The set is: measures referenced on the axes / in the slicer, MINUS the three
    kinds of member that are not SQL columns (Info/trust measures — Bug-6887;
    KPI goal/status constants — Bug-6888; and the statement's own WITH-declared
    calculated members — Bug-8751), PLUS the real measures those calculated
    members need as INPUTS.

    Input measures are collected only from calculated members actually placed on
    an axis or in the slicer, followed transitively through chained members. A
    member the client declared but never used contributes nothing — projecting
    its inputs would cost a needless source aggregate and could fault the pivot
    on an unrelated LNE-companion rule.

    A referenced input the persona cannot see is deliberately LEFT in the set so
    it fails loud under its OWN name (Bug-1067), rather than being silently
    dropped.
    """
    const_lower = {c.lower() for c in (constant_measure_names or set())}

    def _is_non_sql(name: str) -> bool:
        low = name.lower()
        return (
            low in _INFO_MEASURES
            or low in _INFO_DISPLAY_TO_INTERNAL
            or low in const_lower
        )

    measures = [
        m for m in _mdx_measure_refs_any_form(axis_and_where_text)
        if not _is_non_sql(m)
    ]
    declared = _mdx_declared_calc_measures(statement)
    if not declared:
        return measures

    declared_lower = {d.lower(): d for d in declared}
    used = [declared_lower[m.lower()] for m in measures if m.lower() in declared_lower]
    measures = [m for m in measures if m.lower() not in declared_lower]
    have = {m.lower() for m in measures}

    seen_decl = {d.lower() for d in used}
    frontier = list(used)
    while frontier:
        for ref in _mdx_measure_refs_any_form(declared.get(frontier.pop(), "")):
            low = ref.lower()
            if low in declared_lower:
                if low not in seen_decl:
                    seen_decl.add(low)
                    frontier.append(declared_lower[low])
                continue
            if low in have or _is_non_sql(ref):
                continue
            measures.append(ref)
            have.add(low)
    return measures


def _mdx_axis_expr(mdx: str, axis_num: int) -> str:
    """Extract the set expression for a given MDX axis.

    Bug-8750: the WITH prelude is stripped first (see ``_mdx_statement_body``) so
    a comma inside a calc-member expression cannot anchor the axis-0 fallback.
    """
    mdx = _mdx_statement_body(mdx)

    def _clean(expr: str) -> str:
        out = (expr or "").strip()
        # Remove leading NON EMPTY
        out = re.sub(r'^NON\s+EMPTY\s+', '', out, flags=re.IGNORECASE).strip()
        # Remove the DIMENSION PROPERTIES clause in full (Bug-6698). Real Excel
        # (MSOLAP) decorates every axis set with a comma-separated property list,
        # e.g. `DIMENSION PROPERTIES MEMBER_KEY, MEMBER_VALUE, MEMBER_UNIQUE_NAME`
        # and often LEVEL-QUALIFIED bracketed refs such as
        # `[Dim].[Hier].[Level].[MEMBER_KEY]` — which are lexically identical to a
        # member selection `[Dim].[Hier].[Level].[Member]`. The clause is axis
        # metadata (which properties to RETURN), never a member selection. The
        # captured axis fragment is already terminated at its `ON <axis>` keyword,
        # so the property list runs to the end of the fragment: strip all of it.
        # A previous `[^,}]+` form stopped at the FIRST comma, leaving the rest of
        # the list (including the bracketed level-qualified refs) to be mis-parsed
        # as member values, producing WHERE col IN ('MEMBER_KEY', ...) -> 0 rows.
        out = re.sub(
            r'\s+DIMENSION\s+PROPERTIES\s+.*$', '', out,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        return out

    # Axis 1 is special: in standard MDX it appears after axis 0
    # (e.g. SELECT <axis0> ON COLUMNS, <axis1> ON ROWS ...).
    # Avoid matching from SELECT up to ON ROWS, which would include axis 0.
    if axis_num == 1:
        match = re.search(
            r'\bON\s+(?:COLUMNS|0)\b\s*,\s*(.*?)\s+ON\s+(?:ROWS|1)\b',
            mdx,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _clean(match.group(1))

        # Fallback for one-axis queries that only specify ROWS.
        match = re.search(
            r'\bSELECT\s+(.*?)\s+ON\s+(?:ROWS|1)\b',
            mdx,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _clean(match.group(1))

    # Bug-8281: for axis 0 (COLUMNS), try the ROWS-first variant BEFORE the
    # SELECT-anchored fallback. When the MDX lists ROWS before COLUMNS
    # (``SELECT {rows} ON ROWS, {cols} ON COLUMNS``) the generic
    # ``(?:SELECT|,)...ON COLUMNS`` pattern below anchors on SELECT and captures
    # the ROWS fragment into the axis-0 expression — so the COLUMNS axis (and
    # every downstream extractor: measures, dims, member filters, the Bug-8272
    # re-query merge) sees the wrong set. This mirrors the ROWS-first probe
    # ``_mdx_axis_has_non_empty`` already uses (its Opus R1 F3 fix) so both
    # helpers isolate the COLUMNS fragment identically for a ROWS-first query.
    if axis_num == 0:
        match = re.search(
            r'\bON\s+(?:ROWS|1)\b\s*,\s*(.*?)\s+ON\s+(?:COLUMNS|0)\b',
            mdx,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _clean(match.group(1))

    axis_names = {0: "COLUMNS", 1: "ROWS"}
    name = axis_names.get(axis_num, str(axis_num))
    pattern = rf'(?:SELECT|,)\s+(.*?)\s+ON\s+(?:{name}|{axis_num})\b'
    match = re.search(pattern, mdx, re.IGNORECASE | re.DOTALL)
    if match:
        return _clean(match.group(1))
    return ""


def _mdx_axis_has_non_empty(mdx: str, axis_num: int) -> bool:
    """Return True when the axis set carries a leading ``NON EMPTY`` (Bug-6658).

    ``_mdx_axis_expr`` strips ``NON EMPTY`` before returning, so it cannot tell an
    explicit NON EMPTY axis from a plain one. This detects the keyword on the RAW
    axis fragment so the Execute path can restore zero-fact members ("Show items
    with no data") when NON EMPTY is ABSENT, and prune them when it is present.

    Bug-8750: this helper carries the SAME ``(?:SELECT|,)`` axis-0 fallback as
    ``_mdx_axis_expr`` and therefore the same WITH-prelude anchoring hole — a
    calc-member pivot could be judged NON EMPTY (or not) from the wrong fragment,
    silently pruning or restoring zero-fact members. Normalise identically.
    """
    mdx = _mdx_statement_body(mdx)

    def _leads_non_empty(fragment: str) -> bool:
        return bool(re.match(r'\s*NON\s+EMPTY\b', fragment or "", re.IGNORECASE))

    if axis_num == 1:
        match = re.search(
            r'\bON\s+(?:COLUMNS|0)\b\s*,\s*(.*?)\s+ON\s+(?:ROWS|1)\b',
            mdx, re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _leads_non_empty(match.group(1))
        match = re.search(
            r'\bSELECT\s+(.*?)\s+ON\s+(?:ROWS|1)\b',
            mdx, re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _leads_non_empty(match.group(1))
        return False

    # Opus R1 F3: for axis 0 (COLUMNS), try the ROWS-first variant first so we
    # correctly isolate the COLUMNS fragment when the MDX lists ROWS before
    # COLUMNS. Without this, ``SELECT NON EMPTY {rows} ON ROWS, {cols} ON COLUMNS``
    # falsely reports axis 0 as NON EMPTY from the ROWS axis's keyword.
    if axis_num == 0:
        match = re.search(
            r'\bON\s+(?:ROWS|1)\b\s*,\s*(.*?)\s+ON\s+(?:COLUMNS|0)\b',
            mdx, re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _leads_non_empty(match.group(1))

    axis_names = {0: "COLUMNS", 1: "ROWS"}
    name = axis_names.get(axis_num, str(axis_num))
    pattern = rf'(?:SELECT|,)\s+(.*?)\s+ON\s+(?:{name}|{axis_num})\b'
    match = re.search(pattern, mdx, re.IGNORECASE | re.DOTALL)
    if match:
        return _leads_non_empty(match.group(1))
    return False


def _mdx_where_expr(mdx: str) -> str:
    """Extract the WHERE/slicer clause expression (measures references for context).

    Handles nested braces and parentheses for multi-select patterns like:
    ``WHERE ({[Dim].[Hier].[M1], [Dim].[Hier].[M2]}, [Measures].[X])``

    Bug-8750 (sibling hardening): the WITH prelude is dropped first, so a
    ``WHERE`` token appearing inside a calc-member expression or a member caption
    ahead of ``SELECT`` cannot be mistaken for the statement's slicer — the
    slicer, by grammar, always follows ``SELECT``.
    """
    mdx = _mdx_statement_body(mdx)
    # Find WHERE keyword position, then capture everything up to CELL PROPERTIES or end
    where_match = re.search(r'\bWHERE\s+', mdx, re.IGNORECASE)
    if not where_match:
        return ""
    after_where = mdx[where_match.end():].strip()
    # Strip trailing CELL PROPERTIES clause
    after_where = re.sub(r'\s+CELL\s+PROPERTIES\s+.*$', '', after_where, flags=re.IGNORECASE).strip()
    if after_where.startswith("("):
        depth = 0
        end = 0
        for i, ch in enumerate(after_where):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > 0:
            return after_where[1:end].strip()
    # WHERE [Measures].[name] — bare member reference
    # Bug-6717: accept ]] inside bracket bodies.
    bare = re.match(r'(\[Measures\]\.\[(?:[^\]]|\]\])+\])', after_where)
    if bare:
        return bare.group(1).strip()
    # Bug-8383 / L1-R1-004: SSAS clients may omit the tuple parentheses for a
    # single dimension member (``WHERE [Region].[Region].[EMEA]``).  Returning
    # an empty expression here discarded the slicer before the governed KPI
    # batch translator saw it, allowing the unsliced single-evaluate path to
    # run.  Admit only a complete member unique-name shape; arbitrary bare
    # expressions still fail closed through the normal WHERE audit.
    if re.fullmatch(
        r'(?:\[(?:[^\]]|\]\])+\]\.){2,3}'
        r'(?:&?\[(?:[^\]]|\]\])+\](?:&\[(?:[^\]]|\]\])+\])*)',
        after_where,
    ):
        return after_where
    return ""


def _mdx_extract_measures(expr: str) -> list[str]:
    """Extract measure names from [Measures].[name] references.

    Bug-6717: the bracket pattern accepts ``]]`` (escaped ``]``) inside
    bracketed names and unescapes the captured content so the returned names
    are the raw technical names suitable for model-metadata lookup.
    """
    measures: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'\[Measures\]\.\[((?:[^\]]|\]\])+)\]', expr):
        # Bug-6717: unescape ]] -> ] for model-metadata lookup
        name = m.group(1).strip().replace("]]", "]")
        if name not in seen:
            seen.add(name)
            measures.append(name)
    return measures


def _mdx_extract_dimensions(
    expr: str,
    *,
    dim_names: set[str] | None = None,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
) -> list[str]:
    """Extract dimension names from MDX axis expressions."""
    dim_names = dim_names or set()
    hierarchy_level_dim_map = hierarchy_level_dim_map or {}
    hierarchy_default_dim_map = hierarchy_default_dim_map or {}

    dims: list[str] = []
    seen: set[str] = set()

    def _add(
        dim_name: str,
        hierarchy_name: str | None = None,
        level_name: str | None = None,
    ) -> None:
        resolved = _resolve_hierarchy_dimension_name(
            dim_name=dim_name.strip(),
            hierarchy_name=hierarchy_name,
            level_name=level_name,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        if resolved and resolved not in seen:
            seen.add(resolved)
            dims.append(resolved)

    # Explicit level references:
    # [Dim].[Hierarchy].[Level].Members or [Dim].[Hierarchy].[Level].&[Member]
    # (key references may be path-qualified: .&[2025]&[4] — B8 round 2,
    # consume the full key path so no trailing segment leaks into the
    # later, looser patterns).
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\](?:\.\&?\[[^\]]+\](?:\&\[[^\]]+\])*)?(?:\.(?:Members|MEMBERS|AllMembers))?',
        expr,
    ):
        _add(m.group(1), m.group(2), m.group(3))

    # Hierarchy key-member references without explicit level:
    # [Dim].[Hierarchy].&[Member] (possibly path-qualified)
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\&\[[^\]]+\](?:\&\[[^\]]+\])*(?:\.(?:Members|MEMBERS|AllMembers))?',
        expr,
    ):
        _add(m.group(1), m.group(2), None)

    # Hierarchy references without explicit level:
    # [Dim].[Hierarchy].[(All)] / [Dim].[Hierarchy].[(All)].Members
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[(?:\(All\)|All)\](?:\.(?:Members|MEMBERS|AllMembers))?',
        expr,
    ):
        _add(m.group(1), m.group(2), None)

    # [Dim].[Hierarchy] / [Dim].[Hierarchy].Members
    #
    # IMPORTANT:
    # Do not match prefixes of explicit-level references such as
    # [Dim].[Hierarchy].[Level].Members.
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\](?!\s*\.\s*(?:\[|&\[))(?:\.(?:Members|MEMBERS|AllMembers))?',
        expr,
    ):
        _add(m.group(1), m.group(2), None)

    # Simpler [DimName].Members
    #
    # IMPORTANT:
    # Do not match the tail of longer hierarchy paths like
    # [Dim].[Hierarchy].[Level].Members -> "[Level].Members".
    for m in re.finditer(r'(?<!\.)\[([^\]]+)\]\.(?:Members|MEMBERS|AllMembers)', expr):
        _add(m.group(1), None, None)

    return dims


def _mdx_extract_subselect_filters(
    mdx: str,
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Extract dimension member filters from all subselect nesting levels.

    Walks nested ``FROM (SELECT {members} ON 0 FROM (...))`` patterns and
    merges filters from every level.  Returns ``dim_name -> [member_caption, ...]``.
    """
    merged: dict[str, list[str]] = {}
    remaining = mdx
    while True:
        sub_match = re.search(
            r'\bFROM\s*\(\s*SELECT\s+(.+?)\s+ON\s+(?:COLUMNS|0)\s+FROM\s+',
            remaining, re.IGNORECASE | re.DOTALL,
        )
        if not sub_match:
            break
        sub_expr = sub_match.group(1)
        # Bug-6698 (Codex R2 finding 1): Excel can decorate a SUBSELECT axis set
        # with a DIMENSION PROPERTIES clause too. The captured fragment is
        # terminated at its `ON COLUMNS|0` keyword, so the property list runs to
        # fragment end — strip ALL of it BEFORE member extraction, exactly like
        # `_mdx_axis_expr._clean` does for the main axes. Otherwise
        # level-qualified property refs ([D].[H].[L].[MEMBER_KEY]) parse as
        # member selections and poison the slicer filter set with property
        # tokens (empty pivots / loud date-cast errors).
        sub_expr = re.sub(
            r'\s+DIMENSION\s+PROPERTIES\s+.*$', '', sub_expr,
            flags=re.IGNORECASE | re.DOTALL,
        )
        sub_where_text = re.sub(r'^\{|\}$', '', sub_expr.strip()).strip()
        level_filters = _mdx_extract_where_filters(
            sub_where_text, dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        for dim, vals in level_filters.items():
            existing = merged.get(dim, [])
            for v in vals:
                if v not in existing:
                    existing.append(v)
            merged[dim] = existing
        # Preserve the nested FROM token consumed by the outer match so the
        # next iteration can parse the immediately nested SELECT.
        remaining = "FROM " + remaining[sub_match.end():]
    return merged


def _mdx_extract_where_filters(
    where_expr: str,
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
    exclude_level_expansions: bool = False,
) -> dict[str, list[str]]:
    """
    Extract dimension member filters from WHERE clause.

    Supports single-member and multi-select patterns:
    - ``[Dim].[Hier].[Member]``
    - ``{[Dim].[Hier].[M1], [Dim].[Hier].[M2]}`` (OR semantics)

    Bug-5548: ``exclude_level_expansions`` reuses this same member grammar to
    pull an *enumerated member set* off a ROWS/COLUMNS axis (e.g.
    ``{[cat].[cat].&[Shoes], [cat].[cat].&[Music]}``) and turn it into a
    dimension filter, while NOT mistaking a full-level/children expansion
    (``[cat].[cat].[Level].Members``, ``.Children``, ``.AllMembers``) for a
    specific member. A bare ``.Members`` therefore still produces no filter and
    keeps returning the whole level. Off by default so WHERE-slicer extraction
    is byte-for-byte unchanged.

    B8 round-2 fix (deep-review Finding 4): member key references are
    parsed with the same grammar that ``mdx_execute`` uses to emit them
    (``src.dax.member_uname``). A path-qualified uname such as
    ``[Cal].[Cal].[Month].&[2025]&[4]`` filters the named level on its
    own key (month = 4) AND each ancestor level on its key (year = 2025).
    Previously only the FIRST ``&[key]`` was captured, so the year value
    was applied as a month filter.

    Returns mapping ``dim_name -> [member_caption, ...]``.
    """
    filters: dict[str, list[str]] = {}
    if not where_expr:
        return filters
    hierarchy_level_dim_map = hierarchy_level_dim_map or {}
    hierarchy_default_dim_map = hierarchy_default_dim_map or {}

    # Bug-5548: when reading an axis member set, a reference that is immediately
    # followed by a level/children expansion keyword is NOT an enumerated member
    # (it expands the whole level) and must not become a filter. The guard is
    # empty for the WHERE path so its behaviour is unchanged.
    _expansion_guard = (
        # MDX method names are case-insensitive (Codex review): match
        # Members/Children/AllMembers in any case so a lowercase `.members`
        # is not mis-read as an enumerated member and turned into a bogus filter.
        r'(?!\s*\.\s*(?i:Members|AllMembers|Children))'
        if exclude_level_expansions else ''
    )

    def _resolve_target(dim: str, hierarchy: str | None, level: str | None) -> str | None:
        resolved = _resolve_hierarchy_dimension_name(
            dim_name=dim,
            hierarchy_name=hierarchy,
            level_name=level,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        if not resolved or resolved == "Measures":
            return None
        return resolved if resolved in dim_names else None

    def _add(target: str, member: str) -> None:
        if member.lower() in {"all", "(all)"}:
            return
        filters.setdefault(target, [])
        if member not in filters[target]:
            filters[target].append(member)

    def _level_dims_ordered(dim: str, hierarchy: str | None) -> list[str] | None:
        """Ordered (ancestor-first) level dims for the referenced hierarchy."""
        for hk in [(hierarchy or "").lower(), (dim or "").lower()]:
            if not hk:
                continue
            by_level = hierarchy_level_dim_map.get(hk)
            if by_level:
                # resolve_hierarchy_dimension_map inserts levels sorted by
                # ordinal, so dict order is ancestor-first.
                return list(by_level.values())
        return None

    def _add_ancestors(
        dim: str,
        hierarchy: str | None,
        target: str,
        keys: list[str],
        range_targets: set[str],
    ) -> None:
        """Apply the ancestor keys of a composite path to their level dims."""
        if len(keys) < 2:
            return
        dims_ordered = _level_dims_ordered(dim, hierarchy)
        if not dims_ordered or target not in dims_ordered:
            logger.warning(
                "Composite member key path %s on '%s' could not be aligned "
                "to hierarchy levels; ancestor keys ignored.", keys, target,
            )
            return
        level_idx = dims_ordered.index(target)
        n_ancestors = len(keys) - 1
        if n_ancestors > level_idx:
            logger.warning(
                "Composite member key path %s is deeper than the levels above "
                "'%s'; ancestor keys ignored.", keys, target,
            )
            return
        ancestor_dims = dims_ordered[level_idx - n_ancestors:level_idx]
        for a_dim, a_key in zip(ancestor_dims, keys[:-1]):
            if a_dim in dim_names and a_dim not in range_targets:
                _add(a_dim, a_key.strip().strip("()"))

    # Range expressions (Timeline slicers), single or path-qualified keys:
    # [Dim].[Hier].[Level].&[Start]:[Dim].[Hier].[Level].&[End]
    # [Dim].[Hier].[Level].&[2025]&[4]:[Dim].[Hier].[Level].&[2025]&[6]
    range_seen: set[str] = set()
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.' + KEYS_OR_CAPTION +
        r'\s*:\s*'
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.' + KEYS_OR_CAPTION,
        where_expr,
    ):
        start_keys = parse_member_keys(m.group(4))
        end_keys = parse_member_keys(m.group(8))
        if not start_keys or not end_keys:
            continue
        start_key = start_keys[-1].strip().strip("()")
        end_key = end_keys[-1].strip().strip("()")
        target = _resolve_target(m.group(1).strip(), m.group(2).strip(), m.group(3).strip())
        if target:
            filters.setdefault(target, [])
            sentinel = f"{_RANGE_PREFIX}{start_key}{_RANGE_SEP}{end_key}"
            if sentinel not in filters[target]:
                filters[target].append(sentinel)
            range_seen.add(target)
            if start_keys[:-1] == end_keys[:-1]:
                _add_ancestors(
                    m.group(1).strip(), m.group(2).strip(),
                    target, start_keys, range_seen,
                )
            elif len(start_keys) > 1:
                logger.warning(
                    "Range endpoints %s : %s have different ancestor paths; "
                    "only the deepest keys are applied as a range filter.",
                    start_keys, end_keys,
                )

    # [Dim].[Hierarchy].[Level].&[Member] / .&[k0]&[k1]... / .[Member]
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.' + KEYS_OR_CAPTION
        + _expansion_guard,
        where_expr,
    ):
        keys = parse_member_keys(m.group(4))
        if not keys:
            continue
        member = keys[-1].strip().strip("()")
        target = _resolve_target(m.group(1).strip(), m.group(2).strip(), m.group(3).strip())
        if target and target not in range_seen:
            _add(target, member)
            _add_ancestors(
                m.group(1).strip(), m.group(2).strip(), target, keys, range_seen,
            )

    # [Dim].[Hierarchy].&[Member] / .&[k0]&[k1]...
    # (shared KEY_PATH fragment — an inline single-bracket copy here
    # truncated keys containing the SSAS ``]]`` escape, Bug-1052.)
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.(' + KEY_PATH + r')' + _expansion_guard,
        where_expr,
    ):
        keys = parse_member_keys(m.group(3))
        if not keys:
            continue
        member = keys[-1].strip().strip("()")
        dim_part = m.group(1).strip()
        hier_part = m.group(2).strip()
        if len(keys) > 1:
            # No explicit level: the path runs from the root level, so the
            # named member sits at level index len(keys) - 1.
            dims_ordered = _level_dims_ordered(dim_part, hier_part)
            if dims_ordered and len(keys) <= len(dims_ordered):
                target = dims_ordered[len(keys) - 1]
                if target in dim_names and target not in range_seen:
                    _add(target, member)
                    for a_dim, a_key in zip(dims_ordered[: len(keys) - 1], keys[:-1]):
                        if a_dim in dim_names and a_dim not in range_seen:
                            _add(a_dim, a_key.strip().strip("()"))
                continue
        target = _resolve_target(dim_part, hier_part, None)
        if target and target not in range_seen:
            _add(target, member)

    # [Dim].[Hierarchy].[Member]
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\](?!\s*\.\s*(?:\[|&\[))'
        + _expansion_guard,
        where_expr,
    ):
        member = m.group(3).strip().strip('()')
        target = _resolve_target(m.group(1).strip(), m.group(2).strip(), None)
        if target:
            _add(target, member)

    return filters


def _mdx_extract_axis_member_filters(
    axis_expr: str,
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Extract dimension filters from an *enumerated member set* on ROWS/COLUMNS.

    Bug-5548: an explicit set of members on an axis
    (``{[cat].[cat].&[Shoes], [cat].[cat].&[Music]}`` — key form — or
    ``{[cat].[cat].[Shoes], ...}`` — caption form) must restrict the result to
    exactly those members. The axis dimension is already added to GROUP BY by
    ``_mdx_extract_dimensions``; this turns the enumerated members into a
    ``WHERE col IN (...)`` filter so the level is restricted rather than fully
    expanded. Server-defined named sets inlined by ``_inline_named_sets``
    (Bug-5499) land here too, so named member lists finally filter.

    A bare ``.Members`` / ``.Children`` / ``.AllMembers`` level expansion is
    deliberately ignored (``exclude_level_expansions=True``), so the existing
    full-level path is preserved with no regression.

    Returns ``dim_name -> [member, ...]`` (same shape as the WHERE extractor),
    ready to merge into ``where_filters`` and feed ``_build_where_sql_clauses``.
    """
    return _mdx_extract_where_filters(
        axis_expr,
        dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
        exclude_level_expansions=True,
    )


def _is_all_member_ref(text: str) -> bool:
    """True for an ``[All]`` / ``[(All)]`` member reference.

    An All member imposes no restriction — the extractors deliberately produce
    no filter for it, so the audits must not flag it.
    """
    return bool(
        re.search(r'\[\s*\(?\s*all\s*\)?\s*\]\s*$', text.strip(), re.IGNORECASE)
    )


def _iter_member_references(
    expr: str,
    *,
    exclude_level_expansions: bool = False,
):
    """Yield every ENUMERATED dimension-member reference in *expr*.

    Shared, side-effect-free enumeration used by BOTH member audits. It yields
    ``(span, dim, hierarchy, level, ref_text)`` in the original four-pattern
    order, with the original span-consumption and All-member rules, and takes NO
    exemption evidence of any kind. Keeping the enumeration in one place is what
    stops the WHERE audit and the axis audit from drifting apart in what counts
    as a member reference; keeping exemption evidence OUT of it is what stops the
    axis path from ever weakening the WHERE path (Bug-1060). ``span`` is measured
    against *expr* exactly as passed.

    ``exclude_level_expansions`` marks an axis expression, which legitimately
    carries full-level expansions (``[D].[H].[L].Members`` / ``.Children`` /
    ``.AllMembers``) that produce no filter.
    """
    consumed: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        return any(s <= span[0] < e for s, e in consumed)

    def _expansion_follows(end: int) -> bool:
        # Axis path: a member reference immediately followed by a level-expansion
        # method (.Members/.Children/.AllMembers) is a full-level expansion, not
        # an enumerated member — exempt it.
        if not exclude_level_expansions:
            return False
        return bool(re.match(r'\s*\.\s*(?:Members|AllMembers|Children)\b',
                             expr[end:], re.IGNORECASE))

    # Member references, longest form first; spans consumed to avoid
    # re-flagging the same text under a shorter pattern.
    # [Dim].[Hier].[Level].&[k] / .[Member]  — also matches .[Level].[All]
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.(?:&?\[[^\]]*\]|[A-Za-z_])',
        expr,
    ):
        consumed.append(m.span())
        if exclude_level_expansions and not m.group(0).rstrip().endswith("]"):
            # Axis path: `[D].[H].[L].Members/.Children/.AllMembers` is a
            # full-level expansion (the regex consumes the leading method letter,
            # so the match ends in a letter, not `]`), not an enumerated member.
            continue
        if _is_all_member_ref(m.group(0)):
            continue
        yield (m.span(), m.group(1), m.group(2), m.group(3),
               f"[{m.group(1)}].[{m.group(2)}].[{m.group(3)}]")
    # [Dim].[Hier].&[k]
    for m in re.finditer(r'\[([^\]]+)\]\.\[([^\]]+)\]\.&\[[^\]]*\]', expr):
        if _overlaps(m.span()):
            continue
        consumed.append(m.span())
        if _expansion_follows(m.end()):
            continue
        if _is_all_member_ref(m.group(0)):
            continue
        yield (m.span(), m.group(1), m.group(2), None,
               f"[{m.group(1)}].[{m.group(2)}]")
    # [Dim].[Hier].[Member]  (caption form, not followed by a deeper ref)
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\](?!\s*\.\s*(?:\[|&\[))',
        expr,
    ):
        if _overlaps(m.span()):
            continue
        consumed.append(m.span())
        if _expansion_follows(m.end()):
            continue
        if _is_all_member_ref(m.group(0)):
            continue
        yield (m.span(), m.group(1), m.group(2), None,
               f"[{m.group(1)}].[{m.group(2)}].[{m.group(3)}]")
    # Single-bracket attribute form: [Dim].&[k] / [Dim].[Member]
    for m in re.finditer(r'(?<![\].])\[([^\]]+)\]\.(?:&?\[[^\]]*\])', expr):
        if _overlaps(m.span()):
            continue
        if _expansion_follows(m.end()):
            continue
        if _is_all_member_ref(m.group(0)):
            continue
        yield m.span(), m.group(1), None, None, f"[{m.group(1)}]"


def _resolve_audited_member_dimension(
    dim: str,
    hierarchy: str | None,
    level: str | None,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
) -> str | None:
    """Resolve one audited member reference to a known dimension name.

    ``None`` means the reference does not resolve — both audits treat that as a
    hard failure. Measure references are filtered out by the callers before this
    point.
    """
    return _resolve_hierarchy_dimension_name(
        dim_name=dim.strip(),
        hierarchy_name=hierarchy.strip() if hierarchy else None,
        level_name=level.strip() if level else None,
        dim_names=dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )


def _assert_where_members_applied(
    where_expr: str,
    where_filters: dict[str, list[str]],
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
) -> None:
    """Fail loud when a WHERE-slicer member was not applied as a filter.

    Bug-1060 (fail-open security seam): an MDX Execute WHERE slicer was
    silently dropped — and the query ran UNFILTERED — in two cases: a member
    referencing a dimension/hierarchy the gateway cannot resolve, and a
    single-bracket attribute form (``[business_date_month].&[4]``) the
    extractor does not capture. Either way the dropped filter never reaches the
    router, so the router-side filter-presence audit structurally cannot catch
    it. This check closes the fail-open at the gateway: every dimension member
    reference found in the WHERE clause (measures excluded) MUST resolve to a
    known dimension AND have produced a captured filter; otherwise a ValueError
    is raised, which the Execute handler turns into a clean SOAP Client fault.
    A widened (unfiltered) result is never returned silently.

    Bug-8925: this function serves the WHERE slicer and NOTHING else. The
    ROWS/COLUMNS axis has its own entry point,
    ``_assert_axis_member_references_applied``. The split is deliberate: the axis
    path carries label-filter exemption evidence, and there must be no parameter
    through which that evidence — or any other exemption — can reach the WHERE
    slicer audit and weaken it as collateral damage.
    """
    if not where_expr:
        return
    hierarchy_level_dim_map = hierarchy_level_dim_map or {}
    hierarchy_default_dim_map = hierarchy_default_dim_map or {}
    captured = {k for k, v in (where_filters or {}).items() if v}

    for _span, dim, hierarchy, level, ref in _iter_member_references(
        where_expr, exclude_level_expansions=False,
    ):
        if dim.strip().lower() == "measures":
            continue
        resolved = _resolve_audited_member_dimension(
            dim, hierarchy, level, dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if not resolved or resolved not in dim_names:
            raise ValueError(
                f"WHERE slicer references an unknown dimension or hierarchy: {ref}"
            )
        if resolved not in captured:
            raise ValueError(
                f"WHERE slicer member {ref} could not be applied as a filter; "
                f"refusing to run the query unfiltered."
            )


def _assert_axis_member_references_applied(
    axis_text: str,
    where_filters: dict[str, list[str]],
    dim_names: set[str],
    *,
    hierarchy_level_dim_map: dict[str, dict[str, str]] | None = None,
    hierarchy_default_dim_map: dict[str, str] | None = None,
    translated_label_filters: Sequence[_AppliedLabelFilter] = (),
) -> None:
    """Fail loud when a ROWS/COLUMNS axis member reference produced no filter.

    Bug-5548: an enumerated member set on an axis must restrict the level to
    exactly those members. An unknown or partially-resolving set would otherwise
    be silently dropped and the level run unfiltered — the same fail-open seam as
    Bug-1060. Full-level expansions (``.Members`` / ``.Children`` /
    ``.AllMembers``) and ``[All]`` are exempt because they impose no restriction.

    Bug-8925: a label filter restricts its level through a different channel — a
    rendered ``LIKE`` predicate — so its ``[Dim].[Hier].CurrentMember.Name``
    reference produces no entry in ``where_filters`` and was rejected, faulting
    the whole Execute. ``translated_label_filters`` supplies the missing
    producer/consumer contract as occurrence-bound evidence.

    Exemption rule — CONTAINMENT, not overlap. An enumerated-member match is
    exempt only when it lies ENTIRELY inside a proven translated context span.
    Containment is the safe direction: a match that merely overlaps a translated
    context extends into text that was never proven translated, and exempting it
    would excuse an untranslated reference. Containment is strictly stricter than
    overlap, and it is sufficient here because the audit's member grammar
    (``[Dim].[Hier]``) is a prefix of the translated context
    (``[Dim].[Hier].CurrentMember.Name``).

    Span-identity guard: ``context_span`` is only meaningful against the exact
    string it was measured on. Before any exemption is granted, each span is
    re-sliced out of ``axis_text`` and compared with the ``context_text`` the
    translator recorded. A normalized, stripped or re-concatenated axis string
    would shift every span, and a shifted span grants a SPURIOUS exemption — the
    unsafe direction — so a mismatch raises instead of exempting. That is this
    coverage mechanism failing CLOSED on a shape it cannot account for.
    """
    if not axis_text:
        return
    hierarchy_level_dim_map = hierarchy_level_dim_map or {}
    hierarchy_default_dim_map = hierarchy_default_dim_map or {}
    captured = {k for k, v in (where_filters or {}).items() if v}

    exempt_spans: list[tuple[int, int]] = []
    for lf in translated_label_filters:
        start, end = lf.context_span
        if (
            start < 0
            or end > len(axis_text)
            or start >= end
            or axis_text[start:end] != lf.context_text
        ):
            raise ValueError(
                "Internal error auditing an axis label filter: the translated "
                "context span does not match the audited axis text; refusing to "
                "run the query rather than exempt an unverified member reference."
            )
        exempt_spans.append((start, end))

    def _within_translated_context(span: tuple[int, int]) -> bool:
        start, end = span
        return any(s <= start and end <= e for s, e in exempt_spans)

    for span, dim, hierarchy, level, ref in _iter_member_references(
        axis_text, exclude_level_expansions=True,
    ):
        if dim.strip().lower() == "measures":
            continue
        if _within_translated_context(span):
            continue
        resolved = _resolve_audited_member_dimension(
            dim, hierarchy, level, dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if not resolved or resolved not in dim_names:
            raise ValueError(
                f"Axis member set references an unknown dimension or "
                f"hierarchy: {ref}"
            )
        if resolved not in captured:
            raise ValueError(
                f"Axis member {ref} could not be applied as a filter; "
                f"refusing to run the query unfiltered."
            )


def _find_method(root: ET.Element) -> Optional[ET.Element]:
    """Find the first DISCOVER or EXECUTE element in the SOAP body."""
    # Try SOAP body path
    for ns in (_SOAP_NS, ""):
        body_tag = f"{{{ns}}}Body" if ns else "Body"
        body = root.find(f".//{body_tag}")
        if body is not None:
            for child in body:
                ln = _local_name(child.tag)
                if ln in ("Discover", "Execute"):
                    return child

    # Fallback: scan all descendants
    for el in root.iter():
        if _local_name(el.tag) in ("Discover", "Execute"):
            return el
    return None


def _find_text(parent: ET.Element, local: str) -> Optional[str]:
    """Find first child element with the given local name and return its text."""
    for el in parent.iter():
        if _tag_matches(el.tag, local):
            return (el.text or "").strip() or None
    return None


def _parse_properties(method_el: ET.Element) -> dict[str, str]:
    """Extract Properties/PropertyList children into a dict."""
    props: dict[str, str] = {}
    for el in method_el.iter():
        if _tag_matches(el.tag, "PropertyList"):
            for child in el:
                props[_local_name(child.tag)] = (child.text or "").strip()
    return props


def _parse_xmla_parameters(method_el: ET.Element) -> dict[str, str]:
    """Parse an XMLA Execute ``<Parameters>`` block into ``{canonical_name: value}``.

    Wave C #11: the common scalar XMLA parameter form is
    ``<Parameter><Name>Region</Name><Value>EMEA</Value></Parameter>``. The name is
    canonicalised to ``lower(ltrim('@'))`` so ``Region`` and ``@Region`` map to the
    same declared model parameter. The VALUE is taken as scalar text (which also
    carries the existing JSON multi-value and date-range encodings — the
    query-router resolver decodes those by declared ``param_type``).

    Raises ``ValueError`` (→ SOAP client fault) on a malformed / duplicate /
    table-valued / expression-valued parameter. It does NOT validate that the
    parameter is DECLARED — that check needs the model and is done by the caller.
    Returns ``{}`` when no ``<Parameters>`` block is present.
    """
    container = None
    for el in method_el.iter():
        if _tag_matches(el.tag, "Parameters"):
            container = el
            break
    if container is None:
        return {}

    result: dict[str, str] = {}
    for child in container:
        if not _tag_matches(child.tag, "Parameter"):
            continue
        name: str | None = None
        value_els: list[ET.Element] = []
        for sub in child:
            if _tag_matches(sub.tag, "Name"):
                name = (sub.text or "").strip()
            elif _tag_matches(sub.tag, "Value"):
                value_els.append(sub)
        if not name:
            raise ValueError("An XMLA <Parameter> is missing its <Name>.")
        if len(value_els) != 1:
            raise ValueError(
                f"XMLA parameter '{name}' must have exactly one <Value> "
                "(scalar). Multi-valued/table-valued parameters are not supported."
            )
        value_el = value_els[0]
        # A scalar <Value> carries only text. Element children mean a
        # table-valued (rowset) or expression-valued parameter — not supported.
        if len(list(value_el)) > 0:
            raise ValueError(
                f"XMLA parameter '{name}' is not a scalar value; table-valued and "
                "expression-valued parameters are not supported."
            )
        canonical = name.lstrip("@").strip().lower()
        if not canonical:
            raise ValueError(f"XMLA parameter name '{name}' is not valid.")
        if canonical in result:
            raise ValueError(
                f"XMLA parameter '{name}' is specified more than once."
            )
        result[canonical] = value_el.text or ""
    return result


def _find_command_statement(method_el: ET.Element) -> Optional[str]:
    """Extract the DAX statement from Command/Statement."""
    for el in method_el.iter():
        if _tag_matches(el.tag, "Statement"):
            return (el.text or "").strip() or None
    return None


def _is_cancel_command(method_el: ET.Element) -> bool:
    """True when the Execute Command is an XMLA <Cancel> (Bug-5436b).

    The Cancel command lives under ``Command`` and carries ConnectionID /
    SessionID / SPID children. We only need to recognise the element name
    here; the caller (``_handle_execute``) does the actual cancellation by
    looking up the target SessionId in ``_xmla_inflight_tasks`` (Bug-5888).
    A session with no registered in-flight task (unknown session, or a
    subtotal/DMV sub-query phase not covered by the registry -- see the
    registry docstring above) still acknowledges success, since there is
    genuinely nothing left to cancel in that case.
    """
    for el in method_el.iter():
        if _tag_matches(el.tag, "Cancel"):
            return True
    return False


# Bug-5430: matches a DMV ``SELECT ... FROM $SYSTEM.TMSCHEMA_<table>`` and
# captures the table name. ``$SYSTEM`` may be bracket-quoted; whitespace is
# flexible. Case-insensitive.
_TMSCHEMA_DMV_RE = re.compile(
    r"\bfrom\s+\[?\$system\]?\s*\.\s*\[?\s*(tmschema_[a-z_]+)\s*\]?",
    re.IGNORECASE,
)


def _is_tmschema_dmv(statement: str | None) -> bool:
    """True when the Execute statement is a ``$SYSTEM.TMSCHEMA_*`` DMV query."""
    if not statement:
        return False
    return bool(_TMSCHEMA_DMV_RE.search(statement))


def _tmschema_table_name(statement: str) -> str:
    """Extract the TMSCHEMA table name from a DMV statement (uppercased)."""
    m = _TMSCHEMA_DMV_RE.search(statement)
    return m.group(1).upper() if m else ""


def _local_name(tag: str) -> str:
    """Strip XML namespace from a tag string."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _tag_matches(el_tag: str, expected: str) -> bool:
    """Case-insensitive tag name comparison for XML parsing."""
    return _local_name(el_tag).upper() == expected.upper()


def _soap_response(inner_xml: str, session_id: str = "") -> Response:
    """
    Render a SOAP response matching OlaPy/Spyne format exactly.
    OlaPy uses:
    - soap11env: prefix for SOAP namespace
    - tns: prefix for XMLA namespace
    - xs: prefix for XSD namespace
    - Session header with tns: prefix
    """
    # Bug-6069: the SessionId may be client-supplied (echoed from a <Session>
    # header). Emit it into the response envelope as a properly XML-escaped
    # attribute value so a crafted SessionId cannot inject markup / break out of
    # the attribute and forge SOAP structure. _escape_xml covers & < > and the
    # double-quote delimiter.
    header = (
        f'<soap11env:Header>'
        f'<tns:Session SessionId="{_escape_xml(session_id)}"/>'
        f'</soap11env:Header>'
        if session_id else ""
    )
    envelope = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<soap11env:Envelope'
        ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:soap11env="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:tns="urn:schemas-microsoft-com:xml-analysis">'
        + header
        + "<soap11env:Body>"
        + inner_xml
        + "</soap11env:Body>"
        + "</soap11env:Envelope>"
    )

    body_bytes = envelope.encode("utf-8")
    headers = {
        "Content-Type": _CONTENT_TYPE,
        "Connection": "keep-alive",
    }

    # Bug-5436b: gzip/deflate the response when the client advertised support
    # via Accept-Encoding and the payload is large enough to benefit. MSOLAP and
    # Power BI both send ``Accept-Encoding: gzip, deflate`` and transparently
    # decode the body. Identity (no/unknown encoding) is left untouched so the
    # plain-XML contract every existing client relies on is preserved.
    encoding = _pick_content_encoding(_accept_encoding.get())
    if encoding and len(body_bytes) >= _COMPRESS_MIN_BYTES:
        if encoding == "gzip":
            body_bytes = gzip.compress(body_bytes)
        else:  # deflate
            body_bytes = zlib.compress(body_bytes)
        headers["Content-Encoding"] = encoding
        # Caches/proxies must not serve a gzip body to an identity-only client.
        headers["Vary"] = "Accept-Encoding"

    headers["Content-Length"] = str(len(body_bytes))
    return Response(
        content=body_bytes,
        status_code=200,
        headers=headers,
    )


def _extract_session_action(root: ET.Element) -> str:
    """
    Return the session ID to echo in the SOAP response header.
    - BeginSession → generate and return a new UUID.
    - Session (existing session) → echo back the same SessionId.
    - Neither → return empty string (no Session header in response).
    MSOLAP requires the SessionId to be echoed in every response once a
    session has been established.
    """
    for el in root.iter():
        ln = _local_name(el.tag)
        if ln == "BeginSession":
            return str(uuid.uuid4())
        if ln == "Session":
            sid = el.get("SessionId", "")
            if sid:
                return sid
    return ""


def _parse_restrictions(method_el: ET.Element) -> dict[str, list[str]]:
    """Parse Restrictions/RestrictionList from a DISCOVER request into a dict."""
    result: dict[str, list[str]] = {}
    for el in method_el.iter():
        if _tag_matches(el.tag, "RestrictionList"):
            for child in el:
                col_name = _local_name(child.tag)
                value_els = [c for c in child if _tag_matches(c.tag, "Value")]
                if value_els:
                    result[col_name] = [(v.text or "").strip() for v in value_els]
                else:
                    text = (child.text or "").strip()
                    if text:
                        result[col_name] = [text]
            break
    return result


def _soap_fault(message: str, fault_code: str = "Server", status_code: int = 200) -> Response:
    envelope = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<soap11env:Envelope xmlns:soap11env="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap11env:Body>"
        "<soap11env:Fault>"
        f"<faultcode>soap11env:{fault_code}</faultcode>"
        f"<faultstring>{_escape_xml(message)}</faultstring>"
        "</soap11env:Fault>"
        "</soap11env:Body>"
        "</soap11env:Envelope>"
    )
    return Response(content=envelope, media_type=_CONTENT_TYPE, status_code=status_code)


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
