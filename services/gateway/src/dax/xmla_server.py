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

import logging
import os
import re
import uuid
from typing import Any, Callable, Optional
from defusedxml import DefusedXmlException, ElementTree as ET

from fastapi import APIRouter, Request, Response

from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings as _get_settings
from shared.connector_qualify import quote_identifier as _qi
from shared.semantic.hierarchy_resolver import resolve_hierarchy_dimension_map as _build_hierarchy_dimension_map
from src.auth.base import verify_jwt_token
from src.dax import session_store
from src.dax.adapter import XmlaAdapter
from src.dax.constants import SERVER_NAME
from src.dax.dax_parser import translate_dax, find_kpi_member_functions
from src.dax.drillthrough_handler import handle_drillthrough
from src.dax.mdx_validators import (
    check_unsupported_mdx_constructs as _check_unsupported_mdx_constructs,
)
from src.dax.mdschema import build_discover_response
from src.dax.member_uname import (
    KEY_PATH,
    KEYS_OR_CAPTION,
    parse_member_keys,
    parse_member_uname,
)
from src.dax.mdx_execute import (
    build_real_execute_response,
    resolve_kpi_property_expr,
)
from src.dax.ts_mdx_parser import parse_mdx as parse_mdx_statement
from src.router_client import (
    execute_query,
    get_model_dimensions,
    get_model_hierarchies,
    get_hierarchy_preview,
    get_model_kpis,
    get_model_measures,
    get_model_named_sets,
    get_model_personas,
    get_dimension_members,
    list_all_models_for_tenant,
    list_models_for_tenant,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_XMLA_NS = "urn:schemas-microsoft-com:xml-analysis"
_CONTENT_TYPE = "text/xml; charset=utf-8"


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
async def xmla_server_endpoint(request: Request) -> Response:
    """
    XMLA-over-HTTP server endpoint (SSAS-style for Excel).
    Excel connects to single server URL, tenant resolved from Catalog property.
    """
    if request.method == "GET":
        # GET probe required by MSOLAP. Middleware already verified auth.
        return Response(status_code=200, media_type="text/plain")

    # Access authenticated user and JWT token set by BasicAuthMiddleware
    username = getattr(request.state, "username", "")
    jwt_token = getattr(request.state, "jwt_token", "")

    body_bytes = await request.body()
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

@router.api_route("/xmla/{tenant_slug}", methods=["GET", "POST"])
async def xmla_tenant_endpoint(tenant_slug: str, request: Request) -> Response:
    """
    XMLA-over-HTTP endpoint with tenant in path (Power BI / API style).
    Kept for backwards compatibility with Power BI and direct API users.
    """
    username = getattr(request.state, "username", "")
    jwt_token = getattr(request.state, "jwt_token", "")
    body_bytes = await request.body()
    body_bytes = XmlaAdapter.normalize_inbound(body_bytes)

    logger.debug("xmla tenant resolved: tenant=%r user=%r", tenant_slug, username)
    return await _handle_xmla_request(tenant_slug, username, jwt_token, body_bytes, request)


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
        # Propagation of 401 from downstream services
        if "401" in str(exc):
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
    by_name: dict[str, dict[str, Any]] = {}
    ordered_names: list[str] = []

    for item in raw_dimensions:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        enriched = dict(item)
        enriched.setdefault("source", "dimension")
        by_name[name] = enriched
        ordered_names.append(name)

    for hierarchy in hierarchy_defs:
        name = str(hierarchy.get("name", "")).strip()
        if not name:
            continue
        levels = hierarchy.get("levels") or []
        enriched = {
            "name": name,
            "source": "hierarchy",
            "hierarchy_id": str(hierarchy.get("id", "")),
            "levels": levels,
        }
        if name in by_name:
            by_name[name] = enriched
        else:
            by_name[name] = enriched
            ordered_names.append(name)

    return [by_name[name] for name in ordered_names if name in by_name]


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
            sample_size=_get_settings().MEMBER_DISCOVERY_LIMIT,
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
    _limit = _get_settings().MEMBER_DISCOVERY_LIMIT
    if len(preview_members) >= _limit:
        logger.warning(
            "Member discovery hit the cap (%d) for model=%s hierarchy=%s level=%s — "
            "result may be truncated; raise MEMBER_DISCOVERY_LIMIT if this dimension "
            "is legitimately larger.",
            _limit, model_id, hierarchy_id, expand_level,
        )
    members_by_level[str(expand_level)] = [
        _to_preview_member_row(member, idx)
        for idx, member in enumerate(preview_members)
    ]
    if sibling_mode:
        # Bug-5431: siblings sit at the filter member's level sharing its parent;
        # stamp the canonical ancestor path (parent path + own key) so the matcher
        # resolves each sibling's identity correctly.
        _, _, _, _sfp = parse_member_uname(member_filter)
        if len(_sfp) >= 1:
            for _row in members_by_level.get(str(expand_level), []):
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
    dims_to_fetch = dimensions
    if dim_filter:
        dname = dim_filter.strip("[]")
        dims_to_fetch = [d for d in dimensions if d.get("name") == dname]
    elif hier_filter:
        dname = hier_filter.split(".")[0].strip("[]")
        dims_to_fetch = [d for d in dimensions if d.get("name") == dname]

    tasks = []
    dim_names = []
    for d in dims_to_fetch:
        dname = d.get("name", "")
        if not dname:
            continue
        dim_names.append(dname)
        if d.get("source") == "hierarchy":
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
        return {}

    member_data: dict[str, dict[str, Any]] = {}
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for dname, result in zip(dim_names, results):
        if isinstance(result, dict):
            member_data[dname] = result
    return member_data


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
    model_id, project_id, persona = await _resolve_model_id(
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

    measures: list[dict[str, Any]] = []
    raw_dimensions: list[dict[str, Any]] = []
    hierarchy_defs: list[dict[str, Any]] = []
    discover_dimensions: list[dict[str, Any]] = []
    if model_id:
        try:
            measures = await get_model_measures(
                model_id, tenant_slug, jwt_token, project_id=project_id,
            )
            raw_dimensions = await get_model_dimensions(
                model_id, tenant_slug, jwt_token, project_id=project_id,
            )
            hierarchy_defs = await get_model_hierarchies(
                model_id, tenant_slug, jwt_token, project_id=project_id, include_details=True,
            )
            discover_dimensions = _build_discover_dimensions(raw_dimensions, hierarchy_defs)
        except Exception as exc:
            logger.warning("Failed to fetch model metadata: %s", exc)
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
        measures, raw_dimensions, _ = _apply_persona_allow_lists(
            persona,
            measures=measures,
            dimensions=raw_dimensions,
        )
        _, discover_dimensions, _ = _apply_persona_allow_lists(
            persona,
            measures=[],
            dimensions=discover_dimensions,
        )

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
    # Bug-5189: extract persona_id so member enumeration is scoped to the
    # resolved persona. A restricted persona must not be able to enumerate
    # dimension members it should not see.
    persona_id = str(persona["id"]) if persona and persona.get("id") else None
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
    if model_id and request_type.upper() in ("MDSCHEMA_SETS", "MDSCHEMA_KPIS"):
        try:
            if request_type.upper() == "MDSCHEMA_SETS":
                named_sets = await get_model_named_sets(
                    model_id, tenant_slug, jwt_token, project_id=project_id,
                )
            else:
                kpis_list = await get_model_kpis(
                    model_id, tenant_slug, jwt_token, project_id=project_id,
                )
        except Exception as exc:
            logger.warning("Failed to load named_sets/kpis: %s", exc)

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

async def _handle_execute(
    method_el: ET.Element,
    tenant_slug: str,
    jwt_token: str,
    session_id: str = "",
) -> Response:
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
    model_id, _project_id, persona = await _resolve_model_id(
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

    # Fetch model metadata for classifying result columns
    measures_meta: list[dict[str, Any]] = []
    dimensions_meta: list[dict[str, Any]] = []
    hierarchy_defs: list[dict[str, Any]] = []
    try:
        measures_meta = await get_model_measures(model_id, tenant_slug, jwt_token, project_id=_project_id)
        dimensions_meta = await get_model_dimensions(model_id, tenant_slug, jwt_token, project_id=_project_id)
        hierarchy_defs = await get_model_hierarchies(
            model_id=model_id,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            project_id=_project_id,
            include_details=True,
        )
    except Exception as exc:
        logger.warning("Failed to fetch model metadata for Execute: %s", exc)

    (
        dim_names,
        hierarchy_level_dim_map,
        hierarchy_default_dim_map,
    ) = _build_hierarchy_dimension_map(dimensions_meta, hierarchy_defs)

    # MDX DRILLTHROUGH — route through the semantic drill-through pipeline
    # instead of the normal MDX→SQL translation. Excel sends DRILLTHROUGH
    # on double-click; the response is a flat Rowset, not MDDataSet.
    parsed_mdx = parse_mdx_statement(dax_statement)
    if parsed_mdx.is_drillthrough:
        try:
            xml_body, dt_warnings = await handle_drillthrough(
                parsed=parsed_mdx,
                tenant_slug=tenant_slug,
                jwt_token=jwt_token,
                measures_meta=measures_meta,
                dimensions_meta=dimensions_meta,
                hierarchy_defs=hierarchy_defs,
                persona_id=persona_id,
            )
        except ValueError as exc:
            logger.warning("DRILLTHROUGH failed: %s", exc)
            return _soap_fault(str(exc), "Client")
        except Exception as exc:
            logger.error("DRILLTHROUGH error: %s", exc)
            return _soap_fault(str(exc), "Server")

        messages_xml = ""
        if dt_warnings:
            msgs = "".join(
                f'<Warning><Description>{_escape_xml(w)}</Description></Warning>'
                for w in dt_warnings
            )
            messages_xml = f"<Messages>{msgs}</Messages>"

        return _soap_response(
            f'<tns:ExecuteResponse>{xml_body}{messages_xml}</tns:ExecuteResponse>',
            session_id=session_id,
        )

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

    # Translate Execute statement (MDX or DAX) -> SQL for query-router execution.
    try:
        sql, protocol = _statement_to_sql(
            dax_statement,
            measures_meta,
            dimensions_meta,
            hierarchy_meta=hierarchy_defs,
            model_slug=catalog or "",
            subtotal_hierarchies=subtotal_hierarchies,
        )
    except ValueError as exc:
        logger.warning("Execute translation failed: %s", exc)
        return _soap_fault(str(exc), "Client")
    logger.info("[XMLA-EXEC] stmt=%r -> SQL=%r protocol=%s", dax_statement[:200], sql[:200], protocol)

    try:
        result = await execute_query(
            model_id=model_id,
            sql=sql,
            tenant_slug=tenant_slug,
            jwt_token=jwt_token,
            protocol=protocol,
            include_hidden=is_technical_view,
            persona_id=persona_id,
        )
    except Exception as exc:
        logger.error("Query execution failed: %s", exc)
        return _soap_fault(str(exc), "Server")

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
    flat_mdx_measures = _mdx_extract_measures(
        col_expr + " " + row_expr + " " + _mdx_where_expr(dax_statement)
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

    subtotal_info = None
    if subtotal_hierarchies and rows:
        subtotal_info = subtotal_hierarchies[0] if len(subtotal_hierarchies) == 1 else None

        mdx_dims = _mdx_extract_dimensions(
            col_expr + " " + row_expr,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        all_text = col_expr + " " + row_expr + " " + _mdx_where_expr(dax_statement)
        mdx_measures = _mdx_extract_measures(all_text)
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
        measure_canonical: dict[str, str] = {}
        for m in measures_meta:
            mname = m.get("name", "")
            if mname:
                measure_canonical[mname.lower()] = mname

        where_sql = _build_where_sql_clauses(
            where_filters, lambda n: f'"{n}"',
        )
        # F-002-02: the subtotal / grand-total GRAIN queries must honour the
        # same label filters (Begins/Ends-With, Contains) the detail SQL path
        # applies — otherwise an expanded pivot with a label filter shows
        # filtered detail rows beneath an unfiltered subtotal/grand total.
        # Subselect slicers are already merged above (Bug-1050); label filters
        # live in the axis expressions, not the WHERE clause, so extract them
        # from col_expr + row_expr here.
        _subtotal_label_specs = _extract_label_filter_specs(
            col_expr + " " + row_expr,
            dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
        )
        for _lf in _subtotal_label_specs:
            where_sql.append(_label_filter_to_sql(_lf, lambda n: f'"{n}"'))

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
                    sr = await execute_query(
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
                except Exception as exc:
                    logger.warning("Subtotal query failed (grain=%s): %s", sq.level_name, exc)

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
                    sr = await execute_query(
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
                except Exception as exc:
                    logger.warning(
                        "Multi-subtotal query failed (grain=%s): %s",
                        sq.level_name, exc,
                    )
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

    catalog_name = catalog or tenant_slug

    # Pre-compute re-query results for non-composable aggregations (Bug-575)
    requery_results: dict[tuple, Any] | None = None
    try:
        # F-002-08: reuse the parse computed at the top of _handle_execute
        # rather than parsing the same statement a second time.
        if parsed_mdx.with_members:
            from src.dax.mdx_calc_members import (
                parse_calc_members as _parse_cm,
                plan_aggregate_requeried as _plan_rq,
                build_requery_sql as _build_rq_sql,
            )
            dim_names_set = {(d.get("name") or "") for d in (dimensions_meta or [])}
            _dim_cols = [c for c in columns if c in dim_names_set]
            _cms = _parse_cm(parsed_mdx.with_members)
            _rq_axis_text = (
                _mdx_axis_expr(dax_statement, 0) + " "
                + _mdx_axis_expr(dax_statement, 1) + " "
                + _mdx_where_expr(dax_statement)
            )
            _rq_queried = set(_mdx_extract_measures(_rq_axis_text))
            specs = _plan_rq(_cms, measures_meta, catalog or "", _dim_cols, rows, queried_measures=_rq_queried or None)
            if specs:

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
                _rq_where_sql = _build_where_sql_clauses(
                    _rq_where_filters, lambda n: f'"{n}"',
                ) if _rq_where_filters else []

                # Bug-5191: propagate label filters (Begins/Ends-With,
                # Contains) into the AVG/COUNT_DISTINCT re-query so the
                # re-query is filtered identically to the main SQL path.
                # Previously label_filter_specs were extracted (~2895)
                # but never forwarded here, causing re-queries to ignore
                # the active label filter and return unfiltered
                # aggregates.
                _rq_label_specs = _extract_label_filter_specs(
                    _mdx_axis_expr(dax_statement, 0) + " "
                    + _mdx_axis_expr(dax_statement, 1),
                    dim_names,
                    hierarchy_level_dim_map=hierarchy_level_dim_map,
                    hierarchy_default_dim_map=hierarchy_default_dim_map,
                )
                for _lf in _rq_label_specs:
                    _rq_where_sql.append(
                        _label_filter_to_sql(_lf, lambda n: f'"{n}"')
                    )

                for sp in specs:
                    sp.extra_where = _rq_where_sql

                requery_results = {}

                async def _exec_requery(sp):
                    rq_sql = _build_rq_sql(sp)
                    try:
                        rq_result = await execute_query(
                            sql=rq_sql, model_id=model_id or "",
                            tenant_slug=tenant_slug, jwt_token=jwt_token,
                            protocol="jdbc",
                            include_hidden=is_technical_view,
                            persona_id=persona_id,
                        )
                        rq_rows = rq_result.get("rows", [])
                        if rq_rows:
                            return (sp.calc_name, sp.measure_name, sp.partition_key), rq_rows[0].get(sp.measure_name)
                    except Exception as exc:
                        logger.warning("Re-query for %s/%s failed: %s", sp.calc_name, sp.measure_name, exc)
                    return (sp.calc_name, sp.measure_name, sp.partition_key), None

                rq_results_list = await _gather_bounded(
                    [lambda sp=sp: _exec_requery(sp) for sp in specs],
                    _subtotal_grain_concurrency(),
                )
                for key, val in rq_results_list:
                    if val is not None:
                        requery_results[key] = val
                if not requery_results:
                    requery_results = None
    except Exception as exc:
        logger.warning("Re-query pre-computation failed: %s", exc)

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

    return _soap_response(
        f'<tns:ExecuteResponse>{xml_body}</tns:ExecuteResponse>',
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_model_id(
    catalog: str,
    tenant_slug: str,
    jwt_token: str,
) -> tuple[Optional[str], str, Optional[dict[str, Any]]]:
    """
    Resolve a catalog name to a ``(model_id, project_id, persona)`` tuple.

    The catalog name is one of:
    - ``<uuid>`` — direct model id (persona is always None).
    - ``<slug>`` — business base view; persona is None.
    - ``<slug>_<persona.slug>`` — persona-bound catalog; persona is the
      full dict fetched from model-service (id, slug, name,
      included_*_ids, includes_hidden_columns, ...).

    Returns ``(None, "", None)`` when the catalog cannot be matched.
    """
    if not catalog:
        return None, "", None

    is_uuid = bool(re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        catalog,
        re.IGNORECASE,
    ))

    try:
        models = await list_all_models_for_tenant(tenant_slug, jwt_token)
    except Exception as exc:
        logger.warning("Model lookup failed for catalog '%s': %s", catalog, exc)
        return None, "", None

    if is_uuid:
        # Validate the UUID matches a *deployed* model (list_all_models_for_tenant
        # filters undeployed). If it doesn't appear in the deployed list, refuse.
        for model in models:
            if str(model["id"]).lower() == catalog.lower():
                return catalog, str(model.get("project_id", "")), None
        return None, "", None

    lc = catalog.lower()

    # Prefer an exact slug/display-name match — this is the business
    # base catalog for that model.
    for model in models:
        slug = (model.get("slug") or "").lower()
        name = (model.get("display_name") or "").lower()
        if slug == lc or name == lc:
            return str(model["id"]), str(model.get("project_id", "")), None

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
                return mid, pid, persona

    return None, "", None


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
        models = await list_all_models_for_tenant(tenant_slug, jwt_token)
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

    kpis = await get_model_kpis(
        model_id, tenant_slug, jwt_token, project_id=project_id,
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
    where_expr = _mdx_where_expr(statement)
    where_filters = _mdx_extract_where_filters(
        where_expr, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    ) if where_expr else {}

    def _q(name: str) -> str:
        return _qi("postgresql", name)

    where_sql = _build_where_sql_clauses(where_filters, _q)

    measure_agg: dict[str, str] = {}
    for m in measures_meta:
        nm = m.get("name", "")
        if nm:
            measure_agg[nm] = (m.get("default_agg") or "sum").upper()

    async def _measure_cell(measure_name: str) -> float | None:
        agg = measure_agg.get(measure_name, "SUM")
        qc = _q(measure_name)
        if agg == "COUNT_DISTINCT":
            sel = f"COUNT(DISTINCT {qc}) AS {qc}"
        elif agg == "COUNT":
            sel = f"COUNT({qc}) AS {qc}"
        elif agg == "LAST_NON_EMPTY":
            sel = f"SUM({qc}) AS {qc}"
        else:
            sel = f"{agg}({qc}) AS {qc}"
        sql = f"SELECT {sel} FROM {_q(model_slug or 'model_table')}"
        if where_sql:
            sql += f" WHERE {' AND '.join(where_sql)}"
        result = await execute_query(
            model_id=model_id, sql=sql, tenant_slug=tenant_slug,
            jwt_token=jwt_token, protocol="jdbc",
            include_hidden=is_technical_view, persona_id=persona_id,
        )
        rows = result.get("rows", [])
        if not rows:
            return None
        raw = rows[0].get(measure_name)
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    columns: list[str] = []
    row: dict[str, Any] = {}
    measure_map_by_id = {str(m.get("id", "")): m for m in measures_meta}

    for matched_text, fn, caption in found:
        kpi = kpi_by_caption.get((caption or "").strip().lower())
        col_name = f"{caption} ({fn[3:]})"
        if kpi is None:
            raise ValueError(
                f"KPI '{caption}' is not a deployed KPI in this model."
            )

        value_m = measure_map_by_id.get(str(kpi.get("value_measure_id", "")), {})
        value_measure_name = value_m.get("name", "") if value_m else ""

        async def _goal_scalar() -> float | None:
            goal_expr = resolve_kpi_property_expr(kpi, "KPIGoal", measures_meta)
            if goal_expr is None:
                return None
            text = str(goal_expr).strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
                return float(text)
            gref = re.match(r"\[Measures\]\.\[(.+)\]$", text)
            if gref:
                return await _measure_cell(gref.group(1))
            # F-P4b1-04: an expression-typed goal (target_type="expression")
            # resolves to a DAX-ish expression the scalarizer cannot evaluate
            # (only literals and bare measures are supported). Returning blank
            # here silently hid the limitation and also blanked KPIStatus.
            # Fail loud instead so the caller sees an explicit, named error.
            raise ValueError(
                f"KPI '{caption}' has an expression-typed goal "
                f"({text!r}) that cannot be evaluated through the XMLA live "
                "path. Only static literal goals and bare-measure goals are "
                "supported; expression-typed KPI goals are not yet supported "
                "(logged as a future enhancement)."
            )

        if fn == "KPIValue":
            expr = resolve_kpi_property_expr(kpi, fn, measures_meta)
            mref = re.match(r"\[Measures\]\.\[(.+)\]$", expr or "")
            if not mref:
                raise ValueError(
                    f"KPI '{caption}' value is not a queryable measure."
                )
            row[col_name] = await _measure_cell(mref.group(1))
        elif fn == "KPIGoal":
            row[col_name] = await _goal_scalar()
        elif fn == "KPIStatus":
            row[col_name] = await _compute_kpi_status(
                kpi, value_measure_name, _measure_cell, await _goal_scalar(),
            )
        elif fn == "KPITrend":
            # F-P4b1-05: KPITrend surfaces the published literal trend_expression
            # when present, else 0. Data-driven trend (period-over-period delta
            # using trend_period / trend_threshold) is NOT computed here — this
            # literal/0 behaviour is intentional, not a bug; data-driven trend is
            # logged as a future enhancement (execution_future-features.md).
            trend = kpi.get("trend_expression") or None
            row[col_name] = trend if trend else 0
        else:
            raise ValueError(f"Unsupported KPI member function: {fn}")
        columns.append(col_name)

    return columns, [row]


async def _compute_kpi_status(
    kpi: dict[str, Any],
    value_measure_name: str,
    measure_cell,
    goal: float | None,
) -> int | None:
    """Compute a KPI status (-1/0/1) from the value cell, goal, and direction.

    F-P4b1-02: honour a published ``status_expression`` with the SAME precedence
    as ``resolve_kpi_property_expr`` (which returns it first). The live path
    previously ignored it and always re-derived the default ±10% band, so a KPI
    with a custom status expression returned the wrong status to Excel.

    A published ``status_expression`` that is a bare numeric literal (e.g. a
    pinned status) is used directly. An arbitrary CASE/MDX status expression
    cannot be safely evaluated by this numeric live path (it requires the
    MDX→SQL pipeline), so it FAILS LOUD rather than silently falling back to the
    default-band status — matching the resolver's preference while never
    returning a silently-wrong value.
    """
    status_expr = (kpi.get("status_expression") or "").strip()
    if status_expr:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", status_expr):
            return int(float(status_expr))
        raise ValueError(
            f"KPI '{kpi.get('name', '')}' publishes a status_expression "
            f"({status_expr!r}) that is not a numeric literal. Evaluating an "
            "expression-based KPI status through the XMLA live path is not "
            "supported; define a static status band or remove the "
            "status_expression."
        )

    if not value_measure_name or goal is None:
        return None
    value = await measure_cell(value_measure_name)
    if value is None:
        return None

    direction = kpi.get("direction") or "higher_is_better"
    if direction == "lower_is_better":
        if value <= goal:
            return 1
        if value <= goal * 1.1:
            return 0
        return -1
    if value >= goal:
        return 1
    if value >= goal * 0.9:
        return 0
    return -1


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

def _statement_to_sql(
    statement: str,
    measures_meta: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_meta: list[dict[str, Any]] | None = None,
    model_slug: str = "",
    subtotal_hierarchies: list | None = None,
) -> tuple[str, str]:
    text = (statement or "").strip()
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
    if is_string:
        return "'" + text.replace("'", "''") + "'"
    if is_string is False:
        # Parser saw an unquoted (numeric/boolean) literal — emit it verbatim
        # when it is a clean numeric, else quote defensively.
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            return text
        lowered = text.lower()
        if lowered in {"true", "false"}:
            return lowered.upper()
        return "'" + text.replace("'", "''") + "'"
    # is_string is None — unknown provenance (regex fallback parser): infer
    # from the text shape (legacy behaviour).
    if re.fullmatch(r"-?\d+", text):
        return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        return text
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered.upper()
    return "'" + text.replace("'", "''") + "'"


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
                logger.warning(
                    "DAX time-variant %s(%s) has no matching variant measure "
                    "in the model; using base measure instead.",
                    variant_kind, base_name,
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


def _build_where_sql_clauses(
    where_filters: dict[str, list[str]],
    quote_fn: Callable[[str], str],
) -> list[str]:
    """Convert MDX-extracted where_filters into SQL WHERE clause parts."""
    clauses: list[str] = []
    for dim, vals in where_filters.items():
        if not dim:
            continue
        qd = quote_fn(dim)
        between_vals = [v for v in vals if v.startswith("__BETWEEN__")]
        normal_vals = [v for v in vals if not v.startswith("__BETWEEN__")]
        for bv in between_vals:
            parts = bv.split("__")
            start_lit = "'" + parts[2].replace("'", "''") + "'"
            end_lit = "'" + parts[3].replace("'", "''") + "'"
            clauses.append(f"{qd} BETWEEN {start_lit} AND {end_lit}")
        if normal_vals:
            if len(normal_vals) == 1:
                lit = "'" + normal_vals[0].replace("'", "''") + "'"
                clauses.append(f"{qd} = {lit}")
            else:
                in_list = ", ".join("'" + v.replace("'", "''") + "'" for v in normal_vals)
                clauses.append(f"{qd} IN ({in_list})")
    return clauses


class _TopNSpec:
    __slots__ = ("count", "measure", "descending")

    def __init__(self, count: int, measure: str, descending: bool) -> None:
        self.count = count
        self.measure = measure
        self.descending = descending


class _FilterSpec:
    __slots__ = ("measure", "operator", "value")

    def __init__(self, measure: str, operator: str, value: str) -> None:
        self.measure = measure
        self.operator = operator
        self.value = value


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


def _extract_filter_spec(
    axis_text: str, measure_names: set[str],
) -> _FilterSpec | None:
    """Extract Filter(set, [Measures].[M] op value) from MDX axis text."""
    m = re.search(
        r'\bFilter\s*\([^,]+,\s*\[Measures\]\.\[([^\]]+)\]\s*(>=|<=|<>|>|<|=)\s*([0-9.]+)',
        axis_text, re.IGNORECASE,
    )
    if m:
        meas = m.group(1).strip()
        if meas in measure_names or meas.lower() in {n.lower() for n in measure_names}:
            return _FilterSpec(
                measure=meas, operator=m.group(2), value=m.group(3),
            )
    return None


def _count_mdx_function_calls(axis_text: str, func_names: tuple[str, ...]) -> int:
    """Count occurrences of named MDX set functions (case-insensitive).

    Used to detect TopCount/BottomCount/Filter calls that the narrow single-spec
    extractors above cannot translate (composite first argument, a second
    occurrence). When the count exceeds what was extracted the caller must
    fail loud rather than silently run an over-complete result set (F-002-05).
    """
    if not axis_text:
        return 0
    pattern = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in func_names) + r')\s*\(',
        re.IGNORECASE,
    )
    return len(pattern.findall(axis_text))


class _LabelFilterSpec:
    __slots__ = ("dim_ref", "operation", "value", "negated")

    def __init__(self, dim_ref: str, operation: str, value: str, negated: bool) -> None:
        self.dim_ref = dim_ref
        self.operation = operation
        self.value = value
        self.negated = negated


def _extract_label_filter_specs(
    axis_text: str,
    dim_names: set[str],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    hierarchy_default_dim_map: dict[str, str],
) -> list[_LabelFilterSpec]:
    """Extract label filter patterns from MDX axis text.

    Recognised patterns:
      - Left([Dim].[Hier].CurrentMember.Name, N) = "prefix"  -> Begins With
      - Left(...) <> "prefix"                                 -> Does Not Begin With
      - InStr([Dim].[Hier].CurrentMember.Name, "text") > 0   -> Contains
      - InStr(...) = 0                                        -> Does Not Contain
      - Right([Dim].[Hier].CurrentMember.Name, N) = "suffix"  -> Ends With
      - Right(...) <> "suffix"                                -> Does Not End With
    """
    specs: list[_LabelFilterSpec] = []

    # Left(...) = "prefix" or Left(...) <> "prefix"
    for m in re.finditer(
        r'\bLeft\s*\(\s*\[([^\]]+)\](?:\.\[([^\]]+)\])?\.CurrentMember\.Name'
        r'\s*,\s*\d+\s*\)\s*(=|<>)\s*"([^"]*)"',
        axis_text, re.IGNORECASE,
    ):
        dim_ref = _resolve_label_filter_dim(
            m.group(1), m.group(2), dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if dim_ref:
            specs.append(_LabelFilterSpec(
                dim_ref=dim_ref, operation="begins_with",
                value=m.group(4), negated=(m.group(3) == "<>"),
            ))

    # InStr(...) > 0 or InStr(...) = 0
    for m in re.finditer(
        r'\bInStr\s*\(\s*\[([^\]]+)\](?:\.\[([^\]]+)\])?\.CurrentMember\.Name'
        r'\s*,\s*"([^"]*)"\s*\)\s*(>|=)\s*0',
        axis_text, re.IGNORECASE,
    ):
        dim_ref = _resolve_label_filter_dim(
            m.group(1), m.group(2), dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if dim_ref:
            specs.append(_LabelFilterSpec(
                dim_ref=dim_ref, operation="contains",
                value=m.group(3), negated=(m.group(4) == "="),
            ))

    # Right(...) = "suffix" or Right(...) <> "suffix"
    for m in re.finditer(
        r'\bRight\s*\(\s*\[([^\]]+)\](?:\.\[([^\]]+)\])?\.CurrentMember\.Name'
        r'\s*,\s*\d+\s*\)\s*(=|<>)\s*"([^"]*)"',
        axis_text, re.IGNORECASE,
    ):
        dim_ref = _resolve_label_filter_dim(
            m.group(1), m.group(2), dim_names,
            hierarchy_level_dim_map, hierarchy_default_dim_map,
        )
        if dim_ref:
            specs.append(_LabelFilterSpec(
                dim_ref=dim_ref, operation="ends_with",
                value=m.group(4), negated=(m.group(3) == "<>"),
            ))

    return specs


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
    """Convert a label filter spec to a SQL WHERE clause fragment."""
    col = f"LOWER({quote_fn(spec.dim_ref)})"
    escaped = (
        spec.value.lower()
        .replace("'", "''")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    op = "NOT LIKE" if spec.negated else "LIKE"
    if spec.operation == "begins_with":
        return f"{col} {op} '{escaped}%' ESCAPE '\\'"
    elif spec.operation == "ends_with":
        return f"{col} {op} '%{escaped}' ESCAPE '\\'"
    else:
        return f"{col} {op} '%{escaped}%' ESCAPE '\\'"


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

    # Extract TopCount/BottomCount/Filter before validation so we can
    # translate them to SQL instead of rejecting them.
    axis_text = col_expr + " " + row_expr
    topn_spec = _extract_topn_spec(axis_text)
    filter_spec = _extract_filter_spec(axis_text, measure_names)
    label_filter_specs = _extract_label_filter_specs(
        axis_text, dim_names,
        hierarchy_level_dim_map, hierarchy_default_dim_map,
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

    filter_calls = _count_mdx_function_calls(axis_text, ("Filter",))
    filter_consumed = 1 if filter_spec is not None else 0
    # Each label filter is also written as a Filter(...) call in the MDX axis;
    # those are consumed by _extract_label_filter_specs and must be discounted.
    filter_consumed += len(label_filter_specs)
    if filter_calls > filter_consumed:
        raise ValueError(
            "Unsupported Filter() usage on an axis. Only a single value filter "
            "(Filter(set, [Measures].[M] op value)) or a recognised label "
            "filter can be translated to SQL; additional or unrecognised "
            "Filter() calls would return incorrect (unfiltered) results."
        )

    has_any_filter = bool(filter_spec) or bool(label_filter_specs)
    has_topn = topn_spec is not None
    _check_unsupported_mdx_constructs(
        col_expr, "COLUMNS axis", allow_topn=has_topn, allow_filter=has_any_filter,
    )
    _check_unsupported_mdx_constructs(
        row_expr, "ROWS axis", allow_topn=has_topn, allow_filter=has_any_filter,
    )
    _check_unsupported_mdx_constructs(where_expr, "WHERE clause")

    # Extract measures and dimensions from all parts
    all_text = col_expr + " " + row_expr + " " + where_expr
    mdx_measures = _mdx_extract_measures(all_text)
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

    # Bug-1060: every WHERE-slicer dimension member must have resolved AND
    # produced a filter. Reject anything that would otherwise run unfiltered.
    _assert_where_members_applied(
        where_expr, where_filters, dim_names,
        hierarchy_level_dim_map=hierarchy_level_dim_map,
        hierarchy_default_dim_map=hierarchy_default_dim_map,
    )

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

    def _q(name: str) -> str:
        return _qi("postgresql", name)

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
    where_sql_clauses = _build_where_sql_clauses(where_filters, _q)
    for lf in label_filter_specs:
        where_sql_clauses.append(_label_filter_to_sql(lf, _q))
    sql = f'SELECT {", ".join(select_parts)} FROM {from_table}'
    if where_sql_clauses:
        sql += f" WHERE {' AND '.join(where_sql_clauses)}"
    if mdx_dims:
        group_cols = [_q(d) for d in mdx_dims if d in dim_names]
        if group_cols:
            sql += f' GROUP BY {", ".join(group_cols)}'
    if filter_spec:
        canon = measure_canonical.get(filter_spec.measure.lower(), filter_spec.measure)
        sql += f' HAVING {_q(canon)} {filter_spec.operator} {filter_spec.value}'
    if topn_spec:
        canon = measure_canonical.get(topn_spec.measure.lower(), topn_spec.measure)
        direction = "DESC" if topn_spec.descending else "ASC"
        sql += f' ORDER BY {_q(canon)} {direction} LIMIT {topn_spec.count}'

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


def _mdx_axis_expr(mdx: str, axis_num: int) -> str:
    """Extract the set expression for a given MDX axis."""

    def _clean(expr: str) -> str:
        out = (expr or "").strip()
        # Remove leading NON EMPTY
        out = re.sub(r'^NON\s+EMPTY\s+', '', out, flags=re.IGNORECASE).strip()
        # Remove DIMENSION PROPERTIES clause
        out = re.sub(r'\s+DIMENSION\s+PROPERTIES\s+[^,}]+', '', out, flags=re.IGNORECASE).strip()
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

    axis_names = {0: "COLUMNS", 1: "ROWS"}
    name = axis_names.get(axis_num, str(axis_num))
    pattern = rf'(?:SELECT|,)\s+(.*?)\s+ON\s+(?:{name}|{axis_num})\b'
    match = re.search(pattern, mdx, re.IGNORECASE | re.DOTALL)
    if match:
        return _clean(match.group(1))
    return ""


def _mdx_where_expr(mdx: str) -> str:
    """Extract the WHERE/slicer clause expression (measures references for context).

    Handles nested braces and parentheses for multi-select patterns like:
    ``WHERE ({[Dim].[Hier].[M1], [Dim].[Hier].[M2]}, [Measures].[X])``
    """
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
    bare = re.match(r'(\[Measures\]\.\[[^\]]+\])', after_where)
    if bare:
        return bare.group(1).strip()
    return ""


def _mdx_extract_measures(expr: str) -> list[str]:
    """Extract measure names from [Measures].[name] references."""
    measures: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'\[Measures\]\.\[([^\]]+)\]', expr):
        name = m.group(1).strip()
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
) -> dict[str, list[str]]:
    """
    Extract dimension member filters from WHERE clause.

    Supports single-member and multi-select patterns:
    - ``[Dim].[Hier].[Member]``
    - ``{[Dim].[Hier].[M1], [Dim].[Hier].[M2]}`` (OR semantics)

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
            sentinel = f"__BETWEEN__{start_key}__{end_key}"
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
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.' + KEYS_OR_CAPTION,
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
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.(' + KEY_PATH + r')',
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
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\](?!\s*\.\s*(?:\[|&\[))',
        where_expr,
    ):
        member = m.group(3).strip().strip('()')
        target = _resolve_target(m.group(1).strip(), m.group(2).strip(), None)
        if target:
            _add(target, member)

    return filters


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
    """
    if not where_expr:
        return
    hierarchy_level_dim_map = hierarchy_level_dim_map or {}
    hierarchy_default_dim_map = hierarchy_default_dim_map or {}
    captured = {k for k, v in (where_filters or {}).items() if v}

    def _is_all_ref(text: str) -> bool:
        # An [All]/(All) member imposes no restriction — the extractor
        # deliberately produces no filter for it, so it must not be flagged.
        return bool(re.search(r'\[\s*\(?\s*all\s*\)?\s*\]\s*$', text.strip(), re.IGNORECASE))

    def _check(dim: str, hierarchy: str | None, level: str | None, ref: str) -> None:
        if dim.strip().lower() == "measures":
            return
        resolved = _resolve_hierarchy_dimension_name(
            dim_name=dim.strip(),
            hierarchy_name=hierarchy.strip() if hierarchy else None,
            level_name=level.strip() if level else None,
            dim_names=dim_names,
            hierarchy_level_dim_map=hierarchy_level_dim_map,
            hierarchy_default_dim_map=hierarchy_default_dim_map,
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

    # Member references, longest form first; spans consumed to avoid
    # re-flagging the same text under a shorter pattern.
    consumed: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        return any(s <= span[0] < e for s, e in consumed)

    # [Dim].[Hier].[Level].&[k] / .[Member]  — also matches .[Level].[All]
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\.(?:&?\[[^\]]*\]|[A-Za-z_])',
        where_expr,
    ):
        consumed.append(m.span())
        if _is_all_ref(m.group(0)):
            continue
        _check(m.group(1), m.group(2), m.group(3),
               f"[{m.group(1)}].[{m.group(2)}].[{m.group(3)}]")
    # [Dim].[Hier].&[k]
    for m in re.finditer(r'\[([^\]]+)\]\.\[([^\]]+)\]\.&\[[^\]]*\]', where_expr):
        if _overlaps(m.span()):
            continue
        consumed.append(m.span())
        if _is_all_ref(m.group(0)):
            continue
        _check(m.group(1), m.group(2), None, f"[{m.group(1)}].[{m.group(2)}]")
    # [Dim].[Hier].[Member]  (caption form, not followed by a deeper ref)
    for m in re.finditer(
        r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\](?!\s*\.\s*(?:\[|&\[))',
        where_expr,
    ):
        if _overlaps(m.span()):
            continue
        consumed.append(m.span())
        if _is_all_ref(m.group(0)):
            continue
        _check(m.group(1), m.group(2), None,
               f"[{m.group(1)}].[{m.group(2)}].[{m.group(3)}]")
    # Single-bracket attribute form: [Dim].&[k] / [Dim].[Member]
    for m in re.finditer(r'(?<![\].])\[([^\]]+)\]\.(?:&?\[[^\]]*\])', where_expr):
        if _overlaps(m.span()):
            continue
        if _is_all_ref(m.group(0)):
            continue
        _check(m.group(1), None, None, f"[{m.group(1)}]")


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


def _find_command_statement(method_el: ET.Element) -> Optional[str]:
    """Extract the DAX statement from Command/Statement."""
    for el in method_el.iter():
        if _tag_matches(el.tag, "Statement"):
            return (el.text or "").strip() or None
    return None


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
    header = (
        f'<soap11env:Header>'
        f'<tns:Session SessionId="{session_id}"/>'
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
    return Response(
        content=body_bytes,
        status_code=200,
        headers={
            "Content-Type": _CONTENT_TYPE,
            "Content-Length": str(len(body_bytes)),
            "Connection": "keep-alive",
        }
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
