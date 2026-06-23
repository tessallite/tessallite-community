"""
HTTP clients for query-router and model-service.

query-router  -- POST /api/v1/query/execute
model-service -- GET  /api/v1/projects/{pid}/models
                GET  /api/v1/projects/{pid}/models/{mid}
                GET  /api/v1/projects/{pid}/models/{mid}/measures
                GET  /api/v1/projects/{pid}/models/{mid}/dimensions
                GET  /api/v1/projects/{pid}/models/{mid}/hierarchies
                GET  /api/v1/projects/{pid}/models/{mid}/hierarchies/{hid}
                GET  /api/v1/projects/{pid}/models/{mid}/hierarchies/{hid}/preview
                POST /api/v1/auth/login   (username/password -> JWT)
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.middleware.internal_bypass import internal_request_headers

logger = logging.getLogger(__name__)
settings = get_settings()


# Schema of the ``<model>$KPIs`` scorecard virtual table. These columns MUST
# match the rows returned by the query-router's ``_handle_kpi_table_query``
# (one row per KPI read from ``kpi_latest``) so metadata discovery and data
# execution agree. Order is the advertised ordinal position.
KPI_VIRTUAL_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("kpi_name", "text"),
    ("value", "float8"),
    ("target", "float8"),
    ("status", "int4"),
    ("status_label", "text"),
    ("trend_pct", "float8"),
    ("formatted_value", "text"),
    ("evaluated_at", "timestamp"),
]


def build_kpi_virtual_table_columns() -> list[dict]:
    """Build the column dicts for a ``$KPIs`` virtual table (long/scorecard)."""
    return [
        {
            "name": col_name,
            "display_name": col_name,
            "description": "",
            "display_folder": "",
            "data_type": col_type,
            "ordinal_position": ordinal_pos,
            "kind": "dimension" if col_type in ("text", "timestamp") else "measure",
            "is_hidden": False,
            "is_nullable": col_name != "kpi_name",
            "is_primary_key": col_name == "kpi_name",
        }
        for ordinal_pos, (col_name, col_type) in enumerate(
            KPI_VIRTUAL_TABLE_COLUMNS, start=1
        )
    ]


def _t_default() -> float:
    return float(system_snapshot_get("gateway.router_client_timeout_default"))


def _t_medium() -> float:
    return float(system_snapshot_get("gateway.router_client_timeout_medium"))


def _t_long() -> float:
    return float(system_snapshot_get("gateway.router_client_timeout_long"))


def _t_xlong() -> float:
    return float(system_snapshot_get("gateway.router_client_timeout_xlong"))


# ---------------------------------------------------------------------------
# Query Router client
# ---------------------------------------------------------------------------

class QueryRouterError(Exception):
    """Router returned a non-2xx response. `str(exc)` carries the server's
    `detail` message (e.g. "Parse failed: column …") so the JDBC / XMLA
    gateway can surface it verbatim to the client instead of a generic
    "Client error '400 Bad Request'" string from httpx."""

    def __init__(self, detail: str, status_code: int, sqlstate: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.sqlstate = sqlstate


async def execute_drill_options(
    measure_id: str,
    grouping_levels: list[dict[str, Any]],
    jwt_token: str,
) -> dict[str, Any]:
    """POST /api/v1/measures/{measure_id}/drill-options to the query-router."""
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/measures/{measure_id}/drill-options"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    body: dict[str, Any] = {"grouping_levels": grouping_levels}
    async with httpx.AsyncClient(timeout=_t_medium()) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise QueryRouterError(detail, resp.status_code)
        return resp.json()


async def execute_drill_through(
    measure_id: str,
    grouping_levels: list[dict[str, Any]],
    jwt_token: str,
    *,
    hierarchy_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    persona_id: str | None = None,
) -> dict[str, Any]:
    """POST /api/v1/measures/{measure_id}/drill-through to the query-router."""
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/measures/{measure_id}/drill-through"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    body: dict[str, Any] = {"grouping_levels": grouping_levels}
    if hierarchy_id:
        body["hierarchy_id"] = hierarchy_id
    if limit is not None:
        body["limit"] = limit
    if cursor:
        body["cursor"] = cursor
    if persona_id:
        body["persona_id"] = persona_id
    async with httpx.AsyncClient(timeout=_t_long()) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise QueryRouterError(detail, resp.status_code)
        return resp.json()


def _extract_error_detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            return detail.get("detail", "") or str(detail)
        if isinstance(detail, str) and detail:
            return detail
    except Exception:
        pass
    return resp.text or f"Query router returned HTTP {resp.status_code}"


async def execute_query(
    model_id: str,
    sql: str,
    tenant_slug: str,
    jwt_token: str,
    protocol: str = "jdbc",
    include_hidden: bool = False,
    persona_id: str | None = None,
    dialect: str | None = "postgres",
    session_vars: dict[str, str] | None = None,
    client_kind: str | None = None,
) -> dict[str, Any]:
    """
    POST /api/v1/execute to the query-router.

    Returns the full JSON response body.
    Raises QueryRouterError on 4xx/5xx with the server's `detail` as the
    message.

    ``include_hidden`` forwards the visibility cascade skip so persona
    catalogs that set ``includes_hidden_columns`` expose every column.
    ``persona_id`` forwards the resolved persona so the router's gate
    enforces that persona's allow list and default filters (Phase 8
    persona-as-catalog).
    """
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    body: dict[str, Any] = {
        "model_id": str(model_id),
        "raw_query": sql,
        "protocol": protocol,
        "include_hidden": bool(include_hidden),
    }
    if dialect:
        body["dialect"] = str(dialect)
    if persona_id:
        body["persona_id"] = str(persona_id)
    if session_vars:
        body["session_vars"] = session_vars
    if client_kind:
        body["client_kind"] = client_kind
    async with httpx.AsyncClient(timeout=_t_xlong()) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            detail: str
            sqlstate: str | None = None
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if isinstance(detail, dict):
                    sqlstate_value = detail.get("sqlstate")
                    if isinstance(sqlstate_value, str):
                        sqlstate = sqlstate_value
                    detail = detail.get("message") or detail.get("detail")
                if not isinstance(detail, str) or not detail:
                    detail = resp.text or f"Query router returned HTTP {resp.status_code}"
            except Exception:
                detail = resp.text or f"Query router returned HTTP {resp.status_code}"
            raise QueryRouterError(detail, resp.status_code, sqlstate=sqlstate)
        return resp.json()


# ---------------------------------------------------------------------------
# Model Service client (project-scoped URLs)
# ---------------------------------------------------------------------------

def _headers(jwt_token: str, tenant_slug: str) -> dict[str, str]:
    # tenant_slug kept in the signature for call-site compatibility but
    # no longer emitted — see CR-002 Finding 8 note above.
    # internal_request_headers: gateway -> model-service traffic is part of
    # the BI query pipeline and must not be throttled by the model-service
    # per-tenant rate limiter (F-021-01) — limits scope to user ingress.
    del tenant_slug
    return {
        "Authorization": f"Bearer {jwt_token}",
        **internal_request_headers(),
    }


async def list_projects(
    tenant_slug: str,
    jwt_token: str,
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects -- list all projects for the authenticated tenant.
    Returns list of project dicts with id, slug, display_name, etc.
    """
    url = f"{settings.MODEL_SERVICE_URL}/api/v1/projects"
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        return resp.json()


async def list_models_for_project(
    project_id: str,
    tenant_slug: str,
    jwt_token: str,
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models
    Returns list of model dicts.
    """
    url = f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}/models"
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        return resp.json()


async def list_all_models_for_tenant(
    tenant_slug: str,
    jwt_token: str,
) -> list[dict[str, Any]]:
    """
    List all models across all projects for the tenant.
    Returns a flat list of model dicts, each augmented with 'project_id'.
    """
    projects = await list_projects(tenant_slug, jwt_token)

    project_slug_map = {str(p["id"]): str(p.get("slug") or p.get("id")) for p in projects}

    async def _fetch_project_models(pid: str) -> list[dict[str, Any]]:
        try:
            models = await list_models_for_project(pid, tenant_slug, jwt_token)
            out: list[dict[str, Any]] = []
            for m in models:
                if str(m.get("status", "")).lower() == "disabled":
                    continue
                if not m.get("deployed_version_id"):
                    continue
                m["project_id"] = pid
                m["project_slug"] = project_slug_map.get(pid, pid)
                out.append(m)
            return out
        except Exception as exc:
            # F-013-12: a per-project failure here drops that project's entire
            # catalog from BI-tool discovery. Degrading gracefully (returning
            # the projects that DID resolve) is the right call — one flaky
            # project must not blank every catalog — but the drop must be loud,
            # not a stray warning, because the user-visible symptom is "my
            # models vanished" with no other signal. Log at error level and say
            # exactly what the consequence is.
            logger.error(
                "Catalog discovery: project %s failed to list models (%s); "
                "its models are HIDDEN from BI tools for this discovery call. "
                "Other projects are unaffected.",
                pid, exc,
            )
            return []

    results = await asyncio.gather(
        *[_fetch_project_models(p["id"]) for p in projects]
    )
    return [m for batch in results for m in batch]


# Backward-compatible alias
async def list_models_for_tenant(
    tenant_slug: str,
    jwt_token: str,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper -- delegates to list_all_models_for_tenant."""
    return await list_all_models_for_tenant(tenant_slug, jwt_token)


async def get_model_measures(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/measures
    Returns list of measure dicts.
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/measures"
    )
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        return resp.json()


async def get_model_named_sets(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/named-sets"
    )
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        rows = resp.json()
    # F-018-13: deprecated sets are removed from the governance workflow, so the
    # BI catalogue (XMLA MDSCHEMA_SETS) must not list them — otherwise Excel
    # users keep seeing and using a set an admin retired. Mirrors the KPI
    # deployed-filter (F-017-05): the gateway is the BI boundary that applies
    # the visibility rule. Non-deprecated sets (draft/certified/shared) remain
    # available; their certification state is surfaced as a caption marker in
    # `_rows_sets`.
    if isinstance(rows, list):
        return [
            ns for ns in rows
            if isinstance(ns, dict)
            and ns.get("certification_status") != "deprecated"
        ]
    return rows


async def get_model_kpis(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/kpis"
    )
    # F-017-05: BI catalogue surfaces (XMLA MDSCHEMA_KPIS, JDBC $KPIs, inline
    # KPI columns) must only expose deployed KPIs. Undeployed KPIs stay visible
    # to modellers in the builder (which omits this flag) but never reach BI
    # clients through the gateway.
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(
            url,
            headers=_headers(jwt_token, tenant_slug),
            params={"deployed_only": "true"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_model_dimensions(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/dimensions
    Returns list of dimension dicts.
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/dimensions"
    )
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        return resp.json()


async def get_model_hierarchies(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
    include_details: bool = True,
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/hierarchies
    Optionally enrich each hierarchy with detail payload (levels, links).
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    base_url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/hierarchies"
    )
    async with httpx.AsyncClient(timeout=_t_medium()) as client:
        resp = await client.get(base_url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        items = resp.json()
        if not include_details:
            return items

        detailed: list[dict[str, Any]] = []
        for item in items:
            hid = item.get("id")
            if not hid:
                detailed.append(item)
                continue
            try:
                det = await client.get(
                    f"{base_url}/{hid}",
                    headers=_headers(jwt_token, tenant_slug),
                )
                det.raise_for_status()
                detailed.append(det.json())
            except Exception as exc:
                logger.warning("Failed to fetch hierarchy detail %s for model %s: %s", hid, model_id, exc)
                detailed.append(item)
        return detailed


async def get_model_personas(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/personas?for_audience=true

    Returns every persona attached to the model. Each row carries at
    least: id, slug, name, description, included_measure_ids,
    included_dimension_ids, included_hierarchy_ids, audience_roles,
    includes_hidden_columns. The gateway uses this to enumerate one
    catalog per persona as ``<model.slug>_<persona.slug>``.

    ``for_audience=true`` lets the model-service filter the list by the
    caller's roles: admins and modellers see every persona (Phase 8
    persona impersonation — Q5 in the persona-rename open-questions),
    while regular viewers only see personas whose ``audience_roles``
    intersect theirs or have no audience_roles configured.
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    if not project_id:
        return []
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/personas?for_audience=true"
    )
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        try:
            resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Failed to list personas for model %s: %s", model_id, exc)
            return []


async def get_model_snapshot(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> dict[str, Any]:
    """Fetch exported model metadata used for table-grained JDBC relations."""
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    if not project_id:
        return {}
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/snapshot-export"
    )
    async with httpx.AsyncClient(timeout=_t_medium()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        payload = resp.json()
        return ((payload.get("bundle") or {}).get("snapshot") or {})


async def get_model_version_snapshot(
    model_id: str,
    version_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> dict[str, Any]:
    """Fetch a specific model version's immutable ``snapshot_json``.

    Used by the BI-serving catalog path to pin the published measure /
    dimension / column field list to the DEPLOYED version, mirroring the
    query-router binder (B15 / F-013-01). Returns the snapshot dict, or
    ``{}`` if the version cannot be resolved (caller falls back to the live
    field list so the catalog is never silently emptied).
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    if not project_id:
        return {}
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/versions/{version_id}"
    )
    async with httpx.AsyncClient(timeout=_t_medium()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        payload = resp.json()
        snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
        return snapshot if isinstance(snapshot, dict) else {}


async def get_hierarchy_preview(
    model_id: str,
    hierarchy_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
    *,
    sample_size: int = 1000,
    expand_level: int | None = None,
    parent_key: str | None = None,
    persona_id: str | None = None,
    include_key_path: bool = False,
) -> dict[str, Any]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/hierarchies/{hierarchy_id}/preview

    Bug-5424: ``persona_id`` scopes the preview to the resolved persona so
    a restricted persona cannot see hierarchy members it should not —
    mirrors the Bug-5189 pattern for dimension members.

    Bug-3617 (Phase 0.5b): ``include_key_path`` asks the model-service to return
    each member's full ancestor key path when it can (parent-less whole-level
    enumeration of a single-table hierarchy), so the XMLA layer can build the
    canonical composite member unique name without per-member drill queries.
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}/models/{model_id}"
        f"/hierarchies/{hierarchy_id}/preview"
    )
    params: dict[str, Any] = {"sample_size": sample_size}
    if expand_level is not None:
        params["expand_level"] = expand_level
    if parent_key is not None:
        params["parent_key"] = parent_key
    if persona_id is not None:
        params["persona_id"] = persona_id
    if include_key_path:
        params["include_key_path"] = True
    async with httpx.AsyncClient(timeout=_t_long()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug), params=params)
        resp.raise_for_status()
        return resp.json()


async def _resolve_project_id(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
) -> str:
    """
    Find which project a model belongs to by listing all tenant models.
    Returns the project_id string, or empty string if not found.
    """
    try:
        all_models = await list_all_models_for_tenant(tenant_slug, jwt_token)
        for m in all_models:
            if str(m.get("id")) == str(model_id):
                return str(m.get("project_id", ""))
    except Exception as exc:
        logger.warning("Failed to resolve project_id for model %s: %s", model_id, exc)
    return ""


async def get_dimension_members(
    model_id: str,
    dimension_name: str,
    tenant_slug: str,
    jwt_token: str,
    *,
    persona_id: str | None = None,
) -> dict[str, Any]:
    """
    POST /api/v1/discover/members to the query-router.
    Fetches the actual database values for a dimension to populate
    XMLA Discovery responses (MDSCHEMA_MEMBERS, levels, etc.)

    Returns a dict with 'members' list and 'levels' list.

    Bug-5189: ``persona_id`` scopes the query to the resolved persona so
    a restricted persona cannot enumerate dimension members it should
    not see.
    """
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/discover/members"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    body: dict[str, Any] = {
        "model_id": str(model_id),
        "dimension_name": dimension_name,
    }
    if persona_id:
        body["persona_id"] = persona_id
    async with httpx.AsyncClient(timeout=_t_long()) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch members for %s.%s: %s", model_id, dimension_name, exc)
            return {"members": [], "levels": []}

# ---------------------------------------------------------------------------
# Metadata helpers -- build INFORMATION_SCHEMA inputs from model-service data
# ---------------------------------------------------------------------------

async def fetch_model_metadata(
    model_id: str | None,
    tenant_slug: str,
    jwt_token: str,
) -> tuple[
    list[str],
    dict[str, list[dict]],
    dict[str, str],
    dict[str, str],
    dict[str, dict],
    dict[str, str | None],
    dict[str, bool],
    dict[str, str],
    dict[str, list[dict[str, str]]],
    dict[str, int | float | None],
    set[str],
    dict[str, str],
]:
    """
    Fetch measures and dimensions and return:
      - model_names:          [table_name, ...]
      - table_columns:        {table_name: [col_dict, ...]}
      - table_model_id:       {table_name: model_id}
      - table_descriptions:   {table_name: description}
      - table_trust_meta:     {table_name: {last_refreshed_at, source_system, owner}}
      - table_persona_id:     {table_name: persona_id | None}  (None = business base)
      - table_include_hidden: {table_name: bool}  persona.includes_hidden_columns
      - table_query_name:     {table_name: canonical model slug for execution}
      - table_foreign_keys:   {table_name: declared key-backed join metadata}
      - table_row_estimates:  {table_name: known semantic table row estimate}
      - looker_relations:      known generated adapter names for exposure or disabled detection
      - table_project_slug:   {table_name: project slug for schema grouping}

    Phase 8 persona-as-catalog: each model is exposed as a business
    base table ``<slug>`` plus one ``<slug>_<persona.slug>`` sibling
    per persona. Persona allow lists trim the column set; a persona
    with ``includes_hidden_columns`` keeps the hidden columns visible.

    If model_id is given, only that model is fetched (single table).
    If model_id is None, ALL enabled models for the tenant are fetched.
    """
    try:
        all_models = await list_all_models_for_tenant(tenant_slug, jwt_token)
    except Exception as exc:
        logger.warning("Failed to list models for tenant %s: %s", tenant_slug, exc)
        return [], {}, {}, {}, {}, {}, {}, {}, {}, {}, set(), {}

    if model_id:
        target_models = [m for m in all_models if str(m.get("id")) == str(model_id)]
    else:
        target_models = all_models

    model_names: list[str] = []
    table_columns: dict[str, list[dict]] = {}
    table_model_id: dict[str, str] = {}
    table_descriptions: dict[str, str] = {}
    table_trust_meta: dict[str, dict] = {}
    table_persona_id: dict[str, str | None] = {}
    table_include_hidden: dict[str, bool] = {}
    table_query_name: dict[str, str] = {}
    table_foreign_keys: dict[str, list[dict[str, str]]] = {}
    table_row_estimates: dict[str, int | float | None] = {}
    looker_relations: set[str] = set()
    table_project_slug: dict[str, str] = {}
    # F-008-15: relation_name -> owner key ("<mid>:<persona_id|base>") so a
    # persona-variant name (e.g. `sales_eu`) that collides with another
    # model's base name (`sales_eu`) is detected. Without this the second
    # writer silently overwrote the first in table_model_id/table_persona_id,
    # so a JDBC client could be served the wrong model's catalogue (and the
    # XMLA path, which prefers the exact base match, disagreed). Colliding
    # relations are project-prefixed (same treatment as base-slug collisions).
    _relation_owner: dict[str, str] = {}

    def _register_relation(
        name: str, owner_key: str, project_slug: str,
    ) -> str:
        """Return a collision-free relation name, prefixing on a true
        cross-owner collision (fail closed: never silently overwrite)."""
        prior = _relation_owner.get(name)
        if prior is None or prior == owner_key:
            _relation_owner[name] = owner_key
            return name
        prefixed = f"{project_slug}__{name}"
        # Guard against a second-order collision on the prefixed name.
        suffix = 2
        candidate = prefixed
        while (
            _relation_owner.get(candidate) is not None
            and _relation_owner.get(candidate) != owner_key
        ):
            candidate = f"{prefixed}_{suffix}"
            suffix += 1
        logger.warning(
            "Relation name collision: %r already owned by %s; "
            "exposing this one as %r",
            name, prior, candidate,
        )
        _relation_owner[candidate] = owner_key
        return candidate

    # Pre-scan: detect slug collisions across projects.  When two
    # projects expose the same model slug (e.g. "sales"), prefix the
    # colliding names with the project slug to produce unique relation
    # names (e.g. "alpha__sales", "beta__sales").
    _slug_count: dict[str, int] = {}
    for _m in target_models:
        _s = _m.get("slug") or _m.get("display_name") or str(_m.get("id", ""))
        _slug_count[_s] = _slug_count.get(_s, 0) + 1
    _colliding_slugs = {s for s, c in _slug_count.items() if c > 1}

    for m in target_models:
        mid = str(m.get("id", ""))
        pid = str(m.get("project_id", ""))
        raw_slug = m.get("slug") or m.get("display_name") or mid
        project_slug = m.get("project_slug", "public")
        if raw_slug in _colliding_slugs:
            base_name = f"{project_slug}__{raw_slug}"
        else:
            base_name = raw_slug

        deployed_version_id = m.get("deployed_version_id")
        try:
            gather_targets = [
                get_model_dimensions(mid, tenant_slug, jwt_token, project_id=pid),
                get_model_measures(mid, tenant_slug, jwt_token, project_id=pid),
                get_model_personas(mid, tenant_slug, jwt_token, project_id=pid),
                get_model_snapshot(mid, tenant_slug, jwt_token, project_id=pid),
                get_model_kpis(mid, tenant_slug, jwt_token, project_id=pid),
            ]
            # B15 / F-013-01: when the model is deployed, the BI-serving catalog
            # field list MUST come from the DEPLOYED version snapshot, not the
            # live draft tables — so a draft measure/dimension add/rename/hide on
            # a deployed model does not leak into the Excel / Power BI field list
            # until the next Save+Deploy. Mirrors the query-router binder pin.
            if deployed_version_id:
                gather_targets.append(
                    get_model_version_snapshot(
                        mid, str(deployed_version_id), tenant_slug, jwt_token, project_id=pid
                    )
                )
            gathered = await asyncio.gather(*gather_targets, return_exceptions=True)
            dim_result, meas_result, persona_result, snapshot_result, kpi_result = gathered[:5]
            deployed_snapshot_result = gathered[5] if deployed_version_id else None
            if isinstance(dim_result, BaseException) or isinstance(meas_result, BaseException):
                exc = dim_result if isinstance(dim_result, BaseException) else meas_result
                logger.warning("Failed to fetch metadata for model %s: %s", mid, exc)
                continue
            dimensions = dim_result
            measures = meas_result
            personas: list = persona_result if not isinstance(persona_result, BaseException) else []
            snapshot: dict[str, Any] = (
                snapshot_result if not isinstance(snapshot_result, BaseException) else {}
            )
            kpis: list = kpi_result if not isinstance(kpi_result, BaseException) else []
            if isinstance(persona_result, BaseException):
                logger.warning("Failed to list personas for model %s: %s", mid, persona_result)
            if isinstance(snapshot_result, BaseException):
                logger.warning("Failed to fetch snapshot metadata for model %s: %s", mid, snapshot_result)
            if isinstance(kpi_result, BaseException):
                logger.warning("Failed to fetch KPIs for model %s: %s", mid, kpi_result)

            # Pin the published catalog field list to the deployed snapshot.
            # Only the measure/dimension/column field list is pinned here; the
            # query-router binder independently pins query RESULTS. When the
            # deployed snapshot is unavailable or empty (e.g. seed v1), fall back
            # to the live lists so the catalog is never silently emptied — this
            # matches resolve_deployed_shape's None fallback in the binder.
            if deployed_version_id:
                if isinstance(deployed_snapshot_result, BaseException):
                    logger.warning(
                        "Failed to fetch deployed snapshot for model %s: %s",
                        mid, deployed_snapshot_result,
                    )
                else:
                    deployed_snapshot = deployed_snapshot_result or {}
                    deployed_dims = deployed_snapshot.get("dimensions") or []
                    deployed_meas = deployed_snapshot.get("measures") or []
                    deployed_cols = deployed_snapshot.get("columns") or []
                    if deployed_dims or deployed_meas or deployed_cols:
                        dimensions = deployed_dims
                        measures = deployed_meas
                        # Use the deployed snapshot's full shape (tables, columns,
                        # joins, UDAs) so column metadata, technical persona, and
                        # FK relations also reflect the deployed contract.
                        snapshot = deployed_snapshot
        except Exception as exc:
            logger.warning("Failed to fetch metadata for model %s: %s", mid, exc)
            continue

        semantic_tables = snapshot.get("tables") or []
        fact_estimates = [
            table.get("row_count_estimate")
            for table in semantic_tables
            if str(table.get("table_type") or "").lower() in {"fact", "center"}
            and table.get("row_count_estimate") is not None
        ]
        base_row_estimate = max(fact_estimates) if fact_estimates else None
        column_rows = {
            str(column.get("id")): column
            for column in snapshot.get("columns") or []
        }

        variants: list[tuple[str, bool, dict]] = [("", False, {})]
        for persona in personas:
            slug = persona.get("slug")
            if not slug:
                continue
            variants.append((
                f"_{slug}",
                bool(persona.get("includes_hidden_columns")),
                persona,
            ))

        for variant_suffix, include_hidden, persona in variants:
            table_name = f"{base_name}{variant_suffix}"
            allow_measure_ids = {
                str(x) for x in (persona.get("included_measure_ids") or [])
            }
            allow_dimension_ids = {
                str(x) for x in (persona.get("included_dimension_ids") or [])
            }
            # F-008-05 residual: tag-restricted columns must not be
            # advertised in the persona's catalogue metadata. Values are
            # enforced by the query-router on every path; this hides the
            # restricted column *names* from BI field lists as well.
            restricted_column_ids = {
                str(x) for x in (persona.get("restricted_column_ids") or [])
            }
            cols: list[dict] = []
            ordinal = 1
            for dim in dimensions:
                if dim.get("is_hidden") and not include_hidden:
                    continue
                if allow_dimension_ids and str(dim.get("id", "")) not in allow_dimension_ids:
                    continue
                if str(dim.get("source_column_id")) in restricted_column_ids:
                    continue
                # Phase 4: prefer the curated glossary text (effective_description)
                # over the bare dimension.description set in Phase 0.
                description = (
                    dim.get("effective_description")
                    or dim.get("description")
                    or ""
                )
                cols.append({
                    "name": dim.get("name", ""),
                    "display_name": dim.get("display_name") or dim.get("name", ""),
                    "description": description,
                    "display_folder": dim.get("display_folder") or "",
                    "data_type": dim.get("data_type", "text"),
                    "ordinal_position": ordinal,
                    "kind": "dimension",
                    "is_hidden": bool(dim.get("is_hidden")),
                    "is_nullable": bool(
                        column_rows.get(str(dim.get("source_column_id")), {}).get("is_nullable", True)
                    ),
                    "is_primary_key": bool(
                        column_rows.get(str(dim.get("source_column_id")), {}).get("is_primary_key")
                    ),
                })
                ordinal += 1
            for measure in measures:
                if measure.get("is_hidden") and not include_hidden:
                    continue
                if allow_measure_ids and str(measure.get("id", "")) not in allow_measure_ids:
                    continue
                if str(measure.get("source_column_id")) in restricted_column_ids:
                    continue
                description = (
                    measure.get("effective_description")
                    or measure.get("description")
                    or ""
                )
                cols.append({
                    "name": measure.get("name", ""),
                    "display_name": measure.get("display_name") or measure.get("name", ""),
                    "description": description,
                    "display_folder": measure.get("display_folder") or "",
                    "data_type": _measure_sql_type(measure.get("default_agg", "sum")),
                    "ordinal_position": ordinal,
                    "kind": "measure",
                    "is_hidden": bool(measure.get("is_hidden")),
                    "is_nullable": bool(
                        column_rows.get(str(measure.get("source_column_id")), {}).get("is_nullable", True)
                    ),
                    "is_primary_key": False,
                })
                ordinal += 1

            # Inline KPI columns: when expose_kpis_inline is true on the
            # model, inject KPI values as read-only computed measure columns
            # directly in the base table (prefixed with "[KPI] ").
            if m.get("expose_kpis_inline") and kpis:
                for kpi in kpis:
                    kpi_name = kpi.get("name", "")
                    if not kpi_name:
                        continue
                    kpi_display = kpi.get("display_name") or kpi_name
                    cols.append({
                        "name": f"[KPI] {kpi_name}",
                        "display_name": f"[KPI] {kpi_display}",
                        "description": kpi.get("description") or "",
                        "display_folder": kpi.get("display_folder") or "KPIs",
                        "data_type": "float8",
                        "ordinal_position": ordinal,
                        "kind": "measure",
                        "is_hidden": False,
                        "is_nullable": True,
                        "is_primary_key": False,
                    })
                    ordinal += 1

            # F-008-15: resolve any cross-model collision (persona variant
            # name vs another model's base/variant) before registering.
            _owner_key = (
                f"{mid}:{persona['id']}"
                if persona and persona.get("id")
                else f"{mid}:base"
            )
            table_name = _register_relation(
                table_name, _owner_key, m.get("project_slug", "public"),
            )
            model_names.append(table_name)
            table_columns[table_name] = cols
            table_model_id[table_name] = mid
            if persona and persona.get("id"):
                table_persona_id[table_name] = str(persona["id"])
            else:
                table_persona_id[table_name] = None
            table_include_hidden[table_name] = bool(include_hidden)
            table_query_name[table_name] = str(raw_slug)
            table_row_estimates[table_name] = base_row_estimate
            table_project_slug[table_name] = m.get("project_slug", "public")
            description = m.get("description") or ""
            if variant_suffix and persona:
                persona_label = persona.get("description") or persona.get("name") or ""
                if persona_label:
                    description = f"{description} ({persona_label})".strip()
            table_descriptions[table_name] = description
            trust = m.get("trust_meta") or {}
            table_trust_meta[table_name] = {
                "last_refreshed_at": trust.get("last_refreshed_at"),
                "source_system": trust.get("source_system"),
                "owner": trust.get("owner") or "",
            }

        # KPI virtual table: register a $KPIs scorecard virtual table for the
        # model when KPIs exist. The query-router intercepts SELECTs against
        # this table (_handle_kpi_table_query) and returns one row per KPI read
        # from kpi_latest. The advertised columns must therefore match that
        # long/scorecard shape exactly — one fixed schema, not one column per
        # KPI — so metadata discovery and data execution agree.
        if kpis:
            kpi_table_name = f"{base_name}$KPIs"
            kpi_cols = build_kpi_virtual_table_columns()

            model_names.append(kpi_table_name)
            table_columns[kpi_table_name] = kpi_cols
            table_model_id[kpi_table_name] = mid
            table_persona_id[kpi_table_name] = None
            table_include_hidden[kpi_table_name] = False
            # The query_name MUST be the KPI table's own name, not the base
            # model slug. The query-router detects the "$KPIs" suffix to route
            # to the scorecard handler; aliasing it to the base slug would let
            # _rewrite_exposed_relations strip "$KPIs", so the query falls
            # through to the base model and returns full fact-table data.
            table_query_name[kpi_table_name] = kpi_table_name
            table_row_estimates[kpi_table_name] = None
            table_project_slug[kpi_table_name] = m.get("project_slug", "public")
            table_descriptions[kpi_table_name] = "KPI scorecard virtual table"
            trust = m.get("trust_meta") or {}
            table_trust_meta[kpi_table_name] = {
                "last_refreshed_at": trust.get("last_refreshed_at"),
                "source_system": trust.get("source_system"),
                "owner": trust.get("owner") or "",
            }

        columns_by_table = {
            column_id: str(column.get("model_table_id"))
            for column_id, column in column_rows.items()
        }
        uda_by_table = {
            str(uda.get("id")): str(uda.get("table_id"))
            for uda in snapshot.get("user_defined_attributes") or []
        }
        dimension_name_by_column = {
            str(dimension.get("source_column_id")): str(dimension.get("name", ""))
            for dimension in dimensions
            if dimension.get("source_column_id") is not None and dimension.get("name")
        }

        def _in_table(item: dict, table_id: str) -> bool:
            source_id = item.get("source_column_id")
            uda_id = item.get("user_defined_attribute_id")
            return (
                (source_id is not None and columns_by_table.get(str(source_id)) == table_id)
                or (uda_id is not None and uda_by_table.get(str(uda_id)) == table_id)
            )

        # Generated LookML uses table-scoped adapter relations. These are a
        # technical surface: hidden fields remain queryable because Looker
        # may need a hidden declared key for symmetric aggregate SQL.
        def _relation_identifier(value: str) -> str:
            normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower())
            normalized = re.sub(r"_+", "_", normalized).strip("_")
            if normalized and normalized[0].isdigit():
                normalized = f"field_{normalized}"
            return normalized

        relation_by_table: dict[str, str] = {}
        for semantic_table in semantic_tables:
            table_id = str(semantic_table.get("id"))
            alias = _relation_identifier(str(semantic_table.get("alias") or ""))
            if not alias:
                continue
            relation_name = f"{_relation_identifier(str(base_name))}__{alias}"
            looker_relations.add(relation_name)
            if not settings.LOOKER_GATEWAY_ENABLED:
                continue
            relation_by_table[table_id] = relation_name
            scoped_dimensions = [dim for dim in dimensions if _in_table(dim, table_id)]
            scoped_measures = [measure for measure in measures if _in_table(measure, table_id)]
            cols = []
            for ordinal, dim in enumerate(scoped_dimensions, start=1):
                cols.append({
                    "name": dim.get("name", ""),
                    "display_name": dim.get("display_name") or dim.get("name", ""),
                    "description": dim.get("effective_description") or dim.get("description") or "",
                    "display_folder": dim.get("display_folder") or "",
                    "data_type": dim.get("data_type", "text"),
                    "ordinal_position": ordinal,
                    "kind": "dimension",
                    "is_hidden": bool(dim.get("is_hidden")),
                    "is_nullable": bool(
                        column_rows.get(str(dim.get("source_column_id")), {}).get("is_nullable", True)
                    ),
                    "is_primary_key": bool(
                        column_rows.get(str(dim.get("source_column_id")), {}).get("is_primary_key")
                    ),
                })
            for measure in scoped_measures:
                cols.append({
                    "name": measure.get("name", ""),
                    "display_name": measure.get("display_name") or measure.get("name", ""),
                    "description": measure.get("effective_description") or measure.get("description") or "",
                    "display_folder": measure.get("display_folder") or "",
                    "data_type": _measure_sql_type(measure.get("default_agg", "sum")),
                    "ordinal_position": len(cols) + 1,
                    "kind": "measure",
                    "is_hidden": bool(measure.get("is_hidden")),
                    "is_nullable": bool(
                        column_rows.get(str(measure.get("source_column_id")), {}).get("is_nullable", True)
                    ),
                    "is_primary_key": False,
                })
            model_names.append(relation_name)
            table_columns[relation_name] = cols
            table_model_id[relation_name] = mid
            table_persona_id[relation_name] = None
            table_include_hidden[relation_name] = True
            table_query_name[relation_name] = str(raw_slug)
            table_row_estimates[relation_name] = semantic_table.get("row_count_estimate")
            table_descriptions[relation_name] = semantic_table.get("description") or ""
            table_trust_meta[relation_name] = table_trust_meta.get(str(raw_slug), {})
            table_foreign_keys[relation_name] = []
            table_project_slug[relation_name] = m.get("project_slug", "public")

        for join in snapshot.get("joins") or []:
            left_column = column_rows.get(str(join.get("left_column_id")), {})
            right_column = column_rows.get(str(join.get("right_column_id")), {})
            left_is_key = bool(left_column.get("is_primary_key"))
            right_is_key = bool(right_column.get("is_primary_key"))
            if left_is_key == right_is_key:
                continue
            if left_is_key:
                foreign_column, primary_column = right_column, left_column
            else:
                foreign_column, primary_column = left_column, right_column
            foreign_relation = relation_by_table.get(str(foreign_column.get("model_table_id")))
            primary_relation = relation_by_table.get(str(primary_column.get("model_table_id")))
            foreign_name = dimension_name_by_column.get(str(foreign_column.get("id")))
            primary_name = dimension_name_by_column.get(str(primary_column.get("id")))
            if not foreign_relation or not primary_relation or not foreign_name or not primary_name:
                continue
            table_foreign_keys[foreign_relation].append({
                "column_name": foreign_name,
                "foreign_table_name": primary_relation,
                "foreign_column_name": primary_name,
            })

        # -----------------------------------------------------------
        # Auto-inject _technical persona for every model.
        # Shows physical column names (not semantic names), includes
        # hidden columns.  Constrained to the fact/center table that
        # the execution path actually resolves, so the advertised
        # metadata matches the queryable projection.
        # -----------------------------------------------------------
        has_technical = any(
            s == "_technical" for s, _, _ in variants if s
        )
        if not has_technical and semantic_tables:
            tech_table = f"{base_name}_technical"
            cols_by_table: dict[str, list[dict]] = {}
            for col in snapshot.get("columns") or []:
                tid = str(col.get("model_table_id", ""))
                cols_by_table.setdefault(tid, []).append(col)

            # Only include columns from the single base table the execution
            # path resolves to (fact preferred, else first table).  This
            # keeps the advertised technical metadata consistent with what
            # a SELECT * against the technical relation actually returns.
            exec_table = None
            for st in semantic_tables:
                tt = str(st.get("table_type") or "").lower()
                if tt == "fact":
                    exec_table = st
                    break
            if exec_table is None and semantic_tables:
                exec_table = semantic_tables[0]
            exec_table_id = str(exec_table.get("id", "")) if exec_table else None

            tech_cols: list[dict] = []
            ordinal = 1
            seen_names: set[str] = set()
            for st in semantic_tables:
                st_id = str(st.get("id", ""))
                if st_id != exec_table_id:
                    continue
                phys_name = st.get("physical_name") or st.get("alias") or ""
                for col in cols_by_table.get(st_id, []):
                    raw = col.get("column_name", "")
                    # Prefix with table name to avoid duplicates
                    # across source tables (e.g. description, sort_order).
                    col_name = f"{phys_name}__{raw}" if raw in seen_names else raw
                    seen_names.add(raw)
                    tech_cols.append({
                        "name": col_name,
                        "display_name": f"{phys_name}.{raw}",
                        "description": col.get("description") or "",
                        "display_folder": phys_name,
                        "data_type": col.get("data_type", "text"),
                        "ordinal_position": ordinal,
                        "kind": "dimension",
                        "is_hidden": False,
                        "is_nullable": bool(col.get("is_nullable", True)),
                        "is_primary_key": bool(col.get("is_primary_key")),
                    })
                    ordinal += 1

            if tech_cols:
                model_names.append(tech_table)
                table_columns[tech_table] = tech_cols
                table_model_id[tech_table] = mid
                table_persona_id[tech_table] = None
                table_include_hidden[tech_table] = True
                table_query_name[tech_table] = str(raw_slug)
                table_row_estimates[tech_table] = base_row_estimate
                table_project_slug[tech_table] = m.get("project_slug", "public")
                table_descriptions[tech_table] = (
                    f"{m.get('description') or base_name} "
                    "(technical — physical column names)"
                )
                trust = m.get("trust_meta") or {}
                table_trust_meta[tech_table] = {
                    "last_refreshed_at": trust.get("last_refreshed_at"),
                    "source_system": trust.get("source_system"),
                    "owner": trust.get("owner") or "",
                }
                table_foreign_keys[tech_table] = []

    return (
        model_names,
        table_columns,
        table_model_id,
        table_descriptions,
        table_trust_meta,
        table_persona_id,
        table_include_hidden,
        table_query_name,
        table_foreign_keys,
        table_row_estimates,
        looker_relations,
        table_project_slug,
    )


def _extract_token_from_response(resp: httpx.Response) -> str:
    """Read the JWT from the httpOnly access_token cookie on the response."""
    token = resp.cookies.get("access_token")
    if token:
        return token
    raise ValueError("model-service login response missing access_token cookie")


async def login_for_token(tenant_slug: str, email: str, password: str) -> str:
    """
    Exchange email + plain password for a JWT from model-service.

    Called by both the XMLA and JDBC auth paths when the client provides
    username/password credentials instead of a pre-obtained JWT.

    Raises:
        httpx.HTTPStatusError  -- if model-service returns 401 (bad creds) or 5xx.
        ValueError             -- if the response is missing the access_token cookie.
    """
    url = f"{settings.MODEL_SERVICE_URL}/api/v1/auth/login"
    body = {"tenant_id": tenant_slug, "email": email, "password": password}
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        # BI clients open many short-lived connections; the gateway's login
        # relay must not be throttled by the model-service login limiter.
        resp = await client.post(url, json=body, headers=internal_request_headers())
        resp.raise_for_status()
    return _extract_token_from_response(resp)


async def login_discover(email: str, password: str) -> str:
    """
    Cross-tenant login: find which tenant the email belongs to.

    Calls model-service POST /auth/login/discover which searches all
    active tenants for the email/password combination.

    Returns:
        JWT access token string.
    Raises:
        httpx.HTTPStatusError on 401 (no matching user) or 5xx.
        ValueError if access_token cookie missing from response.
    """
    url = f"{settings.MODEL_SERVICE_URL}/api/v1/auth/login/discover"
    body = {"tenant_id": "_discover", "email": email, "password": password}
    async with httpx.AsyncClient(timeout=_t_medium()) as client:
        resp = await client.post(url, json=body, headers=internal_request_headers())
        resp.raise_for_status()
    return _extract_token_from_response(resp)


def _measure_sql_type(default_agg: str) -> str:
    """Map a measure aggregation type to a SQL data type string."""
    if default_agg in ("count", "count_distinct"):
        return "bigint"
    if default_agg in ("sum", "avg", "min", "max"):
        return "numeric"
    return "numeric"
