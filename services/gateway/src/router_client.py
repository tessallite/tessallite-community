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
import math
import re
import time
from typing import Any

import httpx

from shared.config.bootstrap import (
    resolve_system_env_default_int,
    system_snapshot_get,
)
from shared.config.settings import get_settings
from shared.middleware.internal_bypass import internal_request_headers
from shared.semantic.graph_order import is_fact_table
from src.catalogue_cls import (
    build_closure_context as build_cls_closure_context,
    object_hidden_by_cls,
)
from src.dax.kpi_persona_filter import filter_kpis_for_persona

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Bug-7745: per-query cumulative byte ceiling + per-tenant query rate limit
# ---------------------------------------------------------------------------

class QueryByteCeilingExceeded(Exception):
    """Raised when the query-router response payload exceeds the configured
    byte ceiling (GATEWAY_QUERY_BYTE_CEILING).  The gateway surfaces this as
    a typed error so the JDBC / XMLA client gets a clear rejection message
    rather than a truncated or corrupted result set."""

    def __init__(self, response_bytes: int, ceiling: int):
        self.response_bytes = response_bytes
        self.ceiling = ceiling
        super().__init__(
            f"Query response size ({response_bytes:,} bytes) exceeds the "
            f"configured byte ceiling ({ceiling:,} bytes). Narrow the query "
            f"(add filters, reduce columns, or add a LIMIT clause) to reduce "
            f"the result size."
        )


class GatewayQueryRateLimitExceeded(Exception):
    """Raised when a tenant exceeds the per-minute query rate limit on the
    public gateway path (GATEWAY_QUERY_RATE_LIMIT_PER_MINUTE).  The gateway
    surfaces this as a typed error with the retry-after interval."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Query rate limit exceeded. Try again in "
            f"{retry_after_seconds} seconds."
        )


_DEFAULT_BYTE_CEILING = 50 * 1024 * 1024   # 50 MiB
_DEFAULT_RATE_LIMIT_PER_MINUTE = 120


def _query_byte_ceiling() -> int:
    """Return the configured byte ceiling with three-tier resolution:
    an explicitly-stored hot-reloadable system value, then the
    ``GATEWAY_QUERY_BYTE_CEILING`` env/Settings field, then the registry
    default (50 MiB).

    Bug-8108 R2: the previous implementation read ``system_snapshot_get``,
    which folds the registry default in for an ABSENT stored value — so the
    advertised env override was unreachable whenever no system row existed
    (the common case). ``resolve_system_env_default_int`` consults the
    EXPLICIT stored value and rejects a negative env value.
    """
    return resolve_system_env_default_int(
        "gateway.query_byte_ceiling",
        settings.GATEWAY_QUERY_BYTE_CEILING,
        _DEFAULT_BYTE_CEILING,
    )


def _query_rate_limit_per_minute() -> int:
    """Return the configured per-tenant queries/minute with the same
    three-tier resolution as ``_query_byte_ceiling``: explicitly-stored
    system value, then ``GATEWAY_QUERY_RATE_LIMIT_PER_MINUTE`` env/Settings,
    then the registry default (120).

    Bug-8108 R2: shares the previously-dead env fallback fixed at the shared
    resolution level (``resolve_system_env_default_int``).
    """
    return resolve_system_env_default_int(
        "gateway.query_rate_limit_per_minute",
        settings.GATEWAY_QUERY_RATE_LIMIT_PER_MINUTE,
        _DEFAULT_RATE_LIMIT_PER_MINUTE,
    )


def _enforce_byte_ceiling(resp: httpx.Response) -> None:
    """Check that the response payload does not exceed the byte ceiling.

    Called immediately after receiving the query-router response, BEFORE
    parsing the JSON body, so an oversized result is rejected fail-closed
    without the gateway ever deserialising it into memory.

    Raises ``QueryByteCeilingExceeded`` when the ceiling is breached.
    """
    ceiling = _query_byte_ceiling()
    if ceiling <= 0:
        return
    payload_bytes = len(resp.content)
    if payload_bytes > ceiling:
        logger.warning(
            "Bug-7745: query response byte ceiling exceeded "
            "(%d bytes > %d ceiling); rejecting fail-closed",
            payload_bytes, ceiling,
        )
        raise QueryByteCeilingExceeded(payload_bytes, ceiling)


# -- Per-tenant token-bucket query rate limiter ----------------------------
# Semantics: burst up to ``capacity`` queries, sustained ``capacity`` per
# minute. Same pattern as the existing HEADLESS_RATE_LIMIT in
# ``query-router/src/api/rate_limit.py``.

class _GatewayTenantBucket:
    """Token bucket for per-tenant gateway query rate limiting."""
    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: float):
        self.tokens = capacity
        self.last_refill = time.monotonic()


_gw_rate_buckets: dict[str, _GatewayTenantBucket] = {}
_gw_rate_lock = asyncio.Lock()


async def check_gateway_query_rate(tenant_slug: str) -> None:
    """Consume one query token for *tenant_slug*.

    Raises ``GatewayQueryRateLimitExceeded`` with a retry-after interval
    when the bucket is empty.  A capacity of 0 disables the limiter.

    This is a per-process in-memory bucket (same degradation model as the
    headless rate limiter: effective limit is N x configured under
    multi-replica scale-out).
    """
    capacity = _query_rate_limit_per_minute()
    if capacity <= 0:
        return

    refill_per_second = capacity / 60.0
    now = time.monotonic()

    async with _gw_rate_lock:
        bucket = _gw_rate_buckets.get(tenant_slug)
        if bucket is None:
            bucket = _GatewayTenantBucket(float(capacity))
            _gw_rate_buckets[tenant_slug] = bucket

        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(float(capacity), bucket.tokens + elapsed * refill_per_second)
        bucket.last_refill = now

        if bucket.tokens < 1:
            deficit = 1.0 - bucket.tokens
            retry_after = (
                max(1, math.ceil(deficit / refill_per_second))
                if refill_per_second > 0
                else 60
            )
            raise GatewayQueryRateLimitExceeded(retry_after)
        bucket.tokens -= 1


# The structural marker that makes a relation a scorecard table. Consumers test
# for it with ``endswith`` (``jdbc/server._is_kpi_table_query``, the router's
# ``$KPIs`` interception), so any name we generate must preserve it as the LAST
# segment — see ``_suffix_preserving_counter`` (Bug-6773).
KPI_TABLE_SUFFIX = "$KPIs"


def _suffix_preserving_counter(name: str, counter: int) -> str:
    """Append a disambiguating counter WITHOUT breaking a structural suffix.

    Bug-6773: ``alpha__sales$KPIs`` must disambiguate to
    ``alpha__sales_2$KPIs``, never ``alpha__sales$KPIs_2`` — the latter no
    longer ends with ``$KPIs``, so every consumer that recognises a scorecard
    relation by that suffix stops recognising it, and the advertised relation
    becomes permanently unqueryable.
    """
    if name.lower().endswith(KPI_TABLE_SUFFIX.lower()):
        base = name[: -len(KPI_TABLE_SUFFIX)]
        marker = name[-len(KPI_TABLE_SUFFIX):]
        return f"{base}_{counter}{marker}"
    return f"{name}_{counter}"


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


# Named Query output-type domain (``shared/schemas/domains/governance_advanced.py``)
# -> the JDBC catalogue data_type strings the gateway already emits elsewhere.
_NQ_OUTPUT_TYPE_TO_CATALOGUE: dict[str, str] = {
    "string": "text",
    "number": "float8",
    "boolean": "bool",
    "date": "date",
    "timestamp": "timestamp",
}


def build_named_query_relation_columns(nq: dict) -> list[dict]:
    """Build the column dicts for a ``@name`` Named Query relation.

    The advertised columns come from the deployed snapshot's derived
    ``output_columns`` (spec §4.1). Current deployments expand a star definition
    to its semantic field list while producing that snapshot (Bug-9180), so the
    catalogue matches the served result. The authoritative physical column types
    still live in the artifact row manifest. A ``*`` entry is tolerated only for
    compatibility with a snapshot deployed before Bug-9180.
    """
    cols: list[dict] = []
    for ordinal_pos, col in enumerate(nq.get("output_columns") or [], start=1):
        if not isinstance(col, dict) or not col.get("name"):
            continue
        col_name = str(col["name"])
        cols.append({
            "name": col_name,
            "display_name": col_name,
            "description": "",
            "display_folder": nq.get("display_folder") or "",
            "data_type": _NQ_OUTPUT_TYPE_TO_CATALOGUE.get(
                str(col.get("type") or "string"), "text",
            ),
            "ordinal_position": ordinal_pos,
            "kind": "dimension" if col.get("type") != "number" else "measure",
            "is_hidden": False,
            "is_nullable": True,
            "is_primary_key": False,
        })
    return cols


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
    *,
    session_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST /api/v1/measures/{measure_id}/drill-options to the query-router."""
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/measures/{measure_id}/drill-options"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    body: dict[str, Any] = {"grouping_levels": grouping_levels}
    # Wave C #11 / B1: an XMLA Execute's <Parameters> map to app.<name>
    # session_vars that must scope EVERY result-bearing query of that Execute,
    # the DRILLTHROUGH path included. Mirror execute_query so the drill query
    # resolves parameterised row-security / default filters identically to the
    # main query path.
    if session_vars:
        body["session_vars"] = session_vars
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
    session_vars: dict[str, str] | None = None,
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
    # Wave C #11 / B1: carry the XMLA <Parameters> → app.<name> session_vars so a
    # parameterised DRILLTHROUGH scopes its detail rows the same way the main
    # query path does (mirror execute_query). Absent params, unchanged.
    if session_vars:
        body["session_vars"] = session_vars
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
    force_route: str | None = None,
    caption_dimensions: list[str] | None = None,
) -> dict[str, Any]:
    """
    POST /api/v1/execute to the query-router.

    Returns the full JSON response body.
    Raises QueryRouterError on 4xx/5xx with the server's `detail` as the
    message.

    Bug-7745: enforces two fail-closed resource controls on the public
    gateway query path before and after forwarding to the query-router:

    1. **Query rate limit** — checked BEFORE the request is forwarded.
       Raises ``GatewayQueryRateLimitExceeded`` when the per-tenant
       queries/minute ceiling is reached.
    2. **Byte ceiling** — checked AFTER the response is received but
       BEFORE the JSON body is parsed.  Raises
       ``QueryByteCeilingExceeded`` when the response payload exceeds the
       configured ceiling.

    Both controls are configurable via .env / system_settings and
    fail-closed (reject, never truncate or silently pass).

    ``include_hidden`` forwards the visibility cascade skip so persona
    catalogs that set ``includes_hidden_columns`` expose every column.
    ``persona_id`` forwards the resolved persona so the router's gate
    enforces that persona's allow list and default filters (Phase 8
    persona-as-catalog).
    """
    # Bug-7745 (1): per-tenant query rate limit — fail-closed BEFORE
    # forwarding to the query-router so excess queries never reach
    # the execution pipeline at all.
    await check_gateway_query_rate(tenant_slug)

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
    if force_route:
        body["force_route"] = str(force_route)
    # Bug-8285: request friendly member captions for these dimensions. The
    # query-router projects a companion ``<dim>__caption`` column that the XMLA
    # Execute axis reads to render display names instead of raw keys. Field name
    # MUST match ``ExecuteRequest.caption_dimensions`` on the router side.
    if caption_dimensions:
        body["caption_dimensions"] = [str(d) for d in caption_dimensions]
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

        # Bug-7745 (2): cumulative byte ceiling — fail-closed AFTER
        # receiving the response but BEFORE deserialising the JSON body.
        # An oversized result never enters the gateway's memory as parsed
        # dicts, preventing memory exhaustion from a single query.
        _enforce_byte_ceiling(resp)

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


# Short-TTL burst cache for the tenant catalog (Bug-5534 follow-up, Excel
# metadata slowness). Every XMLA request re-enumerated projects + per-project
# models (1 + N model-service GETs) even within one Excel discovery burst.
# Keyed by (tenant, JWT) so a persona/tenant switch or re-login never serves a
# stale scope; the TTL mirrors the metadata burst caches (30 s) — a
# request-burst de-duplicator, not a catalog store.
_TENANT_MODELS_CACHE_TTL_SECONDS = 30
# (stored_at, models, degraded_project_count) — the degraded count travels WITH
# the entry so a strict reader can refuse a partial answer (Bug-9218).
_tenant_models_cache: dict[
    tuple[str, str], tuple[float, list[dict[str, Any]], int]
] = {}
_tenant_models_cache_lock = asyncio.Lock()
# Completeness of the most recent listing per (tenant, jwt) — see
# ``tenant_listing_degraded``.
_tenant_listing_degraded: dict[tuple[str, str], int] = {}


def _tenant_models_cache_ttl() -> float:
    """TTL from the same env lever as the other XMLA burst caches."""
    import os as _os

    raw = _os.getenv("XMLA_METADATA_CACHE_TTL", "")
    try:
        val = float(raw)
        # Bug-9061 / L1-R1-008: documented ``0`` is the explicit cache-disable
        # value used by CLS revalidation.  Keep invalid and negative values on
        # the safe default, but do not turn a deliberate zero back into 30s.
        return val if val >= 0 else float(_TENANT_MODELS_CACHE_TTL_SECONDS)
    except (TypeError, ValueError):
        return float(_TENANT_MODELS_CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Bug-9061 — burst cache for the per-model metadata FAN-OUT
# ---------------------------------------------------------------------------
# ``fetch_model_metadata`` issues six model-service GETs PER MODEL (dimensions,
# measures, personas, snapshot, KPIs, deployed-version snapshot). Only the cheap
# model LIST above was ever cached, so every JDBC connection re-ran the whole
# O(models x 6) fan-out before it could answer its first query — ~8 s on the
# five-model demo tenant, which is what made validate_security.py time out on
# its own test model. It is the same uncached primitive Bug-9112 hit from the
# per-query seam (fixed there by classifying the SQL first, SOL-LAT-001); this
# caches the primitive itself.
#
# Bug-9061's filed diagnosis — "each JDBC connection is served by its own
# process, so in-process caches are cold on every connection" — is incorrect and
# is why the obvious cache was never written. ``PGWireServer._pid`` (jdbc/
# server.py) is a SYNTHETIC per-connection counter used as the PostgreSQL
# backend pid, not an OS process id; every connection is a task in ONE asyncio
# process, so a module-level cache is shared across connections.
#
# FAIL-CLOSED rules, per the discipline a sibling lane's result cache violated:
#   * a failure or a DEGRADED fetch is never stored (see the call site);
#   * the key carries the JWT, so a re-login, tenant switch or persona change
#     can never read another scope's entry;
#   * the TTL is short, non-extendable (a read never refreshes ``stored_at``)
#     and comes from the SAME documented lever as the shipped XMLA metadata
#     cache — no new knob, and the same bounded staleness posture;
#   * it is OPT-IN, so the Bug-7043 CLS revalidation path (whose whole purpose
#     is an immediate re-read at ttl=0) never reads it.
_METADATA_CACHE_MAX_ENTRIES = 256
_metadata_cache: dict[
    tuple[str, str, str, str], tuple[float, tuple]
] = {}


def _metadata_cache_key(
    model_id: str | None,
    tenant_slug: str,
    jwt_token: str,
    project_slug: str | None,
) -> tuple[str, str, str, str]:
    return (tenant_slug, jwt_token, str(model_id or ""), str(project_slug or ""))


def _metadata_cache_get(
    model_id: str | None,
    tenant_slug: str,
    jwt_token: str,
    project_slug: str | None,
) -> tuple | None:
    import time as _time

    ttl = _tenant_models_cache_ttl()
    if ttl <= 0:
        return None
    entry = _metadata_cache.get(
        _metadata_cache_key(model_id, tenant_slug, jwt_token, project_slug)
    )
    if entry is None:
        return None
    stored_at, value = entry
    if (_time.monotonic() - stored_at) >= ttl:
        return None
    return value


def _metadata_cache_put(
    model_id: str | None,
    tenant_slug: str,
    jwt_token: str,
    project_slug: str | None,
    value: tuple,
) -> None:
    import time as _time

    ttl = _tenant_models_cache_ttl()
    if ttl <= 0:
        return
    now = _time.monotonic()
    for key in [k for k, (ts, _v) in _metadata_cache.items() if (now - ts) >= ttl]:
        _metadata_cache.pop(key, None)
    if len(_metadata_cache) >= _METADATA_CACHE_MAX_ENTRIES:
        oldest = min(_metadata_cache, key=lambda k: _metadata_cache[k][0])
        _metadata_cache.pop(oldest, None)
    _metadata_cache[
        _metadata_cache_key(model_id, tenant_slug, jwt_token, project_slug)
    ] = (now, value)


def _reset_metadata_caches_for_tests() -> None:
    """Clear every module-level metadata cache (test isolation only)."""
    _metadata_cache.clear()
    _tenant_models_cache.clear()
    _tenant_listing_degraded.clear()


class ModelMetadataUnavailable(Exception):
    """Model metadata could not be determined — NOT "there are no models".

    Bug-9213 / Bug-9218: the catalogue builders used to answer an upstream
    FAILURE with an ABSENCE — an empty model list, an empty column set, a
    dropped project. Every consumer then reported the absence as fact: a JDBC
    client browsing a tenant saw a catalogue with no tables, and a client that
    named a model got ``FATAL Unknown model`` for a model that exists and is
    deployed (the query-router, which never goes through these calls, served the
    very same model). An empty catalogue returned on an error is a silent lie;
    this exception is the fail-closed alternative.
    """


def tenant_listing_degraded(tenant_slug: str, jwt_token: str) -> int:
    """How many projects the last tenant listing FAILED to read (Bug-9218).

    ``list_all_models_for_tenant`` degrades gracefully by design (F-013-12): one
    flaky project must not blank every BI catalog. That is right for browsing
    and wrong for resolving ONE named model, where a dropped project is
    indistinguishable from "no such model". Recording the count lets the caller
    that cannot tolerate a partial answer refuse, while discovery keeps
    degrading. Zero when the listing was complete, or when it came from a
    caller-supplied stub (a stub returns a complete list by construction).
    """
    return _tenant_listing_degraded.get((tenant_slug, jwt_token), 0)


async def list_all_models_for_tenant(
    tenant_slug: str,
    jwt_token: str,
) -> list[dict[str, Any]]:
    """
    List all models across all projects for the tenant.
    Returns a flat list of model dicts, each augmented with 'project_id'.
    Results are burst-cached for a short TTL (see _tenant_models_cache).

    Completeness is reported separately via ``tenant_listing_degraded`` rather
    than by raising, so broad BI discovery keeps its graceful degradation.
    """
    import time as _time

    cache_key = (tenant_slug, jwt_token)
    ttl = _tenant_models_cache_ttl()
    now = _time.monotonic()
    async with _tenant_models_cache_lock:
        entry = _tenant_models_cache.get(cache_key)
        if entry is not None and (now - entry[0]) < ttl:
            _ts, cached, degraded = entry
            # The degraded count travels WITH the cached entry, so a strict
            # reader is not fooled by a cache hit on a partial listing.
            _tenant_listing_degraded[cache_key] = degraded
            return [dict(m) for m in cached]

    result, degraded = await _list_all_models_for_tenant_uncached(
        tenant_slug, jwt_token,
    )

    async with _tenant_models_cache_lock:
        # Opportunistic sweep so dead JWTs do not accumulate.
        expired = [
            k for k, ent in _tenant_models_cache.items()
            if (now - ent[0]) >= ttl
        ]
        for k in expired:
            _tenant_models_cache.pop(k, None)
            _tenant_listing_degraded.pop(k, None)
        _tenant_models_cache[cache_key] = (
            now, [dict(m) for m in result], degraded,
        )
        _tenant_listing_degraded[cache_key] = degraded
    return result


async def _list_all_models_for_tenant_uncached(
    tenant_slug: str,
    jwt_token: str,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(models, degraded_project_count)``.

    The degraded count is carried alongside the list (and into the burst cache)
    so a caller that cannot tolerate a partial answer can tell "this tenant has
    these models" apart from "this is what we managed to fetch".
    """
    projects = await list_projects(tenant_slug, jwt_token)

    project_slug_map = {str(p["id"]): str(p.get("slug") or p.get("id")) for p in projects}
    _degraded: list[str] = []

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
            # the projects that DID resolve) is the right call for BROAD
            # discovery — one flaky project must not blank every catalog — but
            # the drop must be loud, not a stray warning, because the
            # user-visible symptom is "my models vanished" with no other signal.
            #
            # Bug-9218: it is NOT the right call when the caller is resolving
            # ONE named model. A dropped project makes a model that exists look
            # like a model that does not, and the connection reports
            # "Unknown model" — while the query-router, which does not go
            # through this call, serves that model perfectly well. That is the
            # asymmetry Bug-9218 reports. The drop is COUNTED here and refused
            # by ``fetch_model_metadata`` when a specific model was requested.
            logger.error(
                "Catalog discovery: project %s failed to list models (%s); "
                "its models are HIDDEN from BI tools for this discovery call. "
                "Other projects are unaffected.",
                pid, exc,
            )
            _degraded.append(pid)
            return []

    results = await asyncio.gather(
        *[_fetch_project_models(p["id"]) for p in projects]
    )
    return [m for batch in results for m in batch], len(_degraded)


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
    persona_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/measures
    Returns list of measure dicts.

    Bug-6628: ``persona_id`` forwards the resolved persona so the
    model-service applies persona dimension-scope filtering instead of
    calling ``resolve_effective_persona`` (which 403s for multi-persona
    users when no persona is specified).
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/measures"
    )
    params: dict[str, str] = {}
    if persona_id:
        params["persona_id"] = str(persona_id)
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(
            url, headers=_headers(jwt_token, tenant_slug),
            params=params or None,
        )
        resp.raise_for_status()
        return resp.json()


async def get_model_parameters(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    """GET the model's DECLARED parameters (``model_parameters``).

    Wave C #11: the XMLA Execute path uses this to validate an incoming
    ``<Parameters>`` block — a parameter whose name is not declared on the model
    is rejected with a SOAP client fault (the query-router silently ignores
    undeclared session vars, so the gateway is the enforcement point). Returns the
    list of parameter dicts (each carries at least ``name`` / ``param_type``).
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/parameters"
    )
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(url, headers=_headers(jwt_token, tenant_slug))
        resp.raise_for_status()
        rows = resp.json()
    return rows if isinstance(rows, list) else []


async def get_model_named_sets(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
    persona_id: str | None = None,
) -> list[dict[str, Any]]:
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/named-sets"
    )
    # Bug-6263: forward the resolved persona so the model-service applies the
    # same persona dimension-scope filter it uses for measures/dimensions/KPIs
    # (Bug-5963: list_named_sets ``persona_id`` query param -> _persona_scope).
    # Without it, named-set BI surfaces are persona-blind: a multi-persona
    # viewer trips ``resolve_effective_persona``'s "please select one" 403
    # (swallowed upstream -> silently EMPTY MDSCHEMA_SETS and no Execute-time
    # inlining), and a privileged caller browsing a persona-variant catalog
    # gets the UNFILTERED (over-broad) set list because the resolver treats a
    # missing persona_id as "unrestricted". Passing the persona resolved from
    # the catalog name makes both surfaces correctly persona-filtered.
    # Bug-8384 (F-013-08 part 4): pin BI-facing named sets to the DEPLOYED
    # snapshot. Both consumers of this function are BI surfaces — XMLA
    # MDSCHEMA_SETS discovery and Execute-time MDX inlining — and a named set's
    # expression IS query semantics: without this flag a modeller's unsaved
    # draft edit immediately changed what Excel/Power BI/Tableau computed,
    # before anyone clicked Deploy. Mirrors the KPI deployed-filter
    # (F-017-05, ``get_model_kpis``) and brings the MDX surface in line with the
    # SQL named-list path, which already reads the snapshot
    # (query-router ``params/named_list_resolver``). Sets created since the last
    # deploy are withheld; certification/governance stays live-overlaid, so the
    # deprecated filter below still reacts without a redeploy.
    params: dict[str, str] = {"deployed_only": "true"}
    if persona_id:
        params["persona_id"] = str(persona_id)
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(
            url,
            headers=_headers(jwt_token, tenant_slug),
            params=params,
        )
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


async def evaluate_kpi_governed(
    kpi_id: str,
    model_id: str,
    project_id: str,
    tenant_slug: str,
    jwt_token: str,
    persona_id: str | None = None,
) -> dict[str, Any]:
    """POST /kpis/{id}/evaluate — the SINGLE governed KPI evaluation authority.

    Bug-6608 (un-gated 2026-07-21): the XMLA live KPI paths (KPIValue / KPIGoal /
    KPIStatus / KPITrend) resolve through the SAME model-service ``/evaluate``
    pipeline the SPA scorecard and the Excel custom function use, so every surface
    returns one governed result. This replaces the gateway's own single-measure
    ``_measure_cell`` resolution, which could not resolve composite / ratio value
    expressions (G-002-01) and served the RAW value for status (the F-025-01
    divergence). ``status`` is the governed −1/0/1/None RAG verdict from
    ``kpi_threshold.evaluate_threshold``.

    Returns the ``KPIEvaluateResponse`` dict (``value``, ``target``/``goal``,
    ``status``, ``trend``, ...). The BI user's JWT + resolved persona are
    forwarded so the deployed-snapshot, persona-scope and RLS the pipeline applies
    are identical to the scorecard's.
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/kpis/{kpi_id}/evaluate"
    )
    params: dict[str, str] = {}
    if persona_id:
        params["persona_id"] = str(persona_id)
    async with httpx.AsyncClient(timeout=_t_long()) as client:
        resp = await client.post(
            url,
            headers=_headers(jwt_token, tenant_slug),
            params=params or None,
        )
        # Fable R1 finding 3: wrap HTTP errors in a clean ValueError (Client fault)
        # so the SOAP fault does not leak the internal model-service URL or UUIDs.
        # Routine outcomes (403 persona scope, 404 draft/undeployed, 409 snapshot
        # invalid) become descriptive client-visible errors.
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise ValueError(
                f"KPI evaluation failed ({resp.status_code}): {detail}"
            )
        return resp.json()


async def evaluate_kpi_batch(
    kpi_ids: list[str],
    model_id: str,
    project_id: str,
    tenant_slug: str,
    jwt_token: str,
    *,
    filters: list[dict[str, Any]] | None = None,
    persona_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate governed KPIs through the batch route.

    This is intentionally a user-context request. ``_headers`` adds the
    internal-service marker used by the model-service scheduler and that marker
    authorises publication to the model-wide ``kpi_latest`` cache. A BI user's
    dimension-sliced result must never become the value served to another user,
    so this call forwards only the bearer JWT and is therefore ineligible for
    publication. The route still applies the caller's persona and row security.
    """
    if not kpi_ids:
        return {}
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/kpis/evaluate-batch"
    )
    body: dict[str, Any] = {"kpi_ids": [str(kpi_id) for kpi_id in kpi_ids]}
    if filters:
        body["filters"] = filters
    if persona_id:
        params: dict[str, str] | None = {"persona_id": str(persona_id)}
    else:
        params = None
    # Do not call _headers here: this is a user render, not a scheduler write.
    headers = {"Authorization": f"Bearer {jwt_token}"}
    async with httpx.AsyncClient(timeout=_t_long()) as client:
        resp = await client.post(url, json=body, headers=headers, params=params)
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise ValueError(
                f"KPI batch evaluation failed ({resp.status_code}): {detail}"
            )
        payload = resp.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("KPI batch evaluation returned no results")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kpi_id") is None:
            continue
        result[str(row["kpi_id"])] = row
    return result


async def get_model_dimensions(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
    persona_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/dimensions
    Returns list of dimension dicts.

    Bug-6628: ``persona_id`` forwards the resolved persona so the
    model-service applies persona dimension-scope filtering instead of
    calling ``resolve_effective_persona`` (which 403s for multi-persona
    users when no persona is specified).
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/dimensions"
    )
    params: dict[str, str] = {}
    if persona_id:
        params["persona_id"] = str(persona_id)
    async with httpx.AsyncClient(timeout=_t_default()) as client:
        resp = await client.get(
            url, headers=_headers(jwt_token, tenant_slug),
            params=params or None,
        )
        resp.raise_for_status()
        return resp.json()


async def get_model_hierarchies(
    model_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
    include_details: bool = True,
    persona_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    GET /api/v1/projects/{project_id}/models/{model_id}/hierarchies
    Optionally enrich each hierarchy with detail payload (levels, links).

    Completeness contract (Bug-6623 sub-item 2). With ``include_details=True``
    the returned list is authoritative ONLY when every hierarchy's detail
    payload was fetched. A per-hierarchy detail failure therefore PROPAGATES
    (raises) rather than degrading to a shallow, level-less item. Silently
    appending the shallow item on failure made a partial list look COMPLETE to
    the caller: the XMLA metadata cache
    (``xmla_server._load_model_metadata_cached``, which caches only a
    non-raising return) could then freeze a wrong-shape hierarchy set — a
    multi-level / calendar hierarchy collapsed to a single flat level — for the
    whole cache TTL. Raising keeps the consumer's already-documented "only a
    COMPLETE fetch is cached" contract honest: the partial is not cached and the
    next request re-fetches (picking up the recovered detail).

    Bug-6628: ``persona_id`` forwards the resolved persona so the
    model-service applies persona hierarchy-scope filtering instead of
    calling ``resolve_effective_persona`` (which 403s for multi-persona
    users when no persona is specified).
    """
    if not project_id:
        project_id = await _resolve_project_id(model_id, tenant_slug, jwt_token)
    base_url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
        f"/models/{model_id}/hierarchies"
    )
    params: dict[str, str] = {}
    if persona_id:
        params["persona_id"] = str(persona_id)
    async with httpx.AsyncClient(timeout=_t_medium()) as client:
        resp = await client.get(
            base_url, headers=_headers(jwt_token, tenant_slug),
            params=params or None,
        )
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
                # Bug-6800 / Bug-6801(a): forward persona_id on the per-hierarchy
                # DETAIL fetch. Without it the detail endpoint auto-resolves the
                # caller's effective persona (or none) instead of the catalogue's
                # requested persona, so the detailed `levels` list is NOT filtered
                # to the persona's excluded level attributes. That skewed the
                # gateway's cube level list against the model-service preview
                # (which DOES filter by persona), causing (a) persona-excluded
                # levels to be advertised in MDSCHEMA_LEVELS then denied (403) at
                # Execute, and (b) `expand_level` — an index into the gateway's
                # unfiltered list — to point past/into the wrong level of the
                # preview's filtered list (H5 empty members / wrong-level members).
                # The detail endpoint applies the same excluded-level filtering as
                # the summary list when persona_id is supplied, so both sides now
                # index an identically-filtered level list.
                det = await client.get(
                    f"{base_url}/{hid}",
                    headers=_headers(jwt_token, tenant_slug),
                    params=params or None,
                )
                det.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = (
                    exc.response.status_code if exc.response is not None else 0
                )
                if status in (404, 410):
                    # Stale listing row: the hierarchy no longer exists, so its
                    # absence is authoritative and the enriched list is COMPLETE
                    # without it. Re-raising here (the Bug-6623(2) transient
                    # policy) made a permanently-deleted row block the XMLA
                    # metadata cache from ever filling — every request re-fetched
                    # all hierarchies and re-hit the 404 (Excel metadata
                    # slowness, Bug-5534 follow-up). Skip the dead row instead.
                    logger.warning(
                        "Hierarchy detail %s for model %s returned %d "
                        "(stale listing row); skipping it.",
                        hid, model_id, status,
                    )
                    continue
                # Bug-6623(2): any other detail-fetch failure makes the enriched
                # list INCOMPLETE. Propagate so the caller cannot mistake a
                # partial (shallow) hierarchy set for a complete one and cache it
                # for the TTL; the next request retries and self-heals.
                logger.warning(
                    "Failed to fetch hierarchy detail %s for model %s: %s; "
                    "treating the hierarchy list as incomplete (not cached).",
                    hid, model_id, exc,
                )
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to fetch hierarchy detail %s for model %s: %s; "
                    "treating the hierarchy list as incomplete (not cached).",
                    hid, model_id, exc,
                )
                raise
            detailed.append(det.json())
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
            # A missing persona endpoint (404 above) is a complete "no
            # personas" answer. Any other failure is an incomplete metadata
            # fan-out; let the caller retain the base catalogue if appropriate
            # but mark the result non-cacheable rather than pinning a silently
            # persona-blind catalogue for the burst TTL (Bug-9061).
            logger.warning("Failed to list personas for model %s: %s", model_id, exc)
            raise


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


async def get_deployed_named_queries(
    model_id: str,
    deployed_version_id: str,
    tenant_slug: str,
    jwt_token: str,
    project_id: str = "",
) -> list[dict[str, Any]]:
    """Return the DEPLOYED snapshot's Named Query definitions (Bug-9178).

    The XMLA table catalogue (DBSCHEMA_TABLES / DBSCHEMA_COLUMNS) advertises
    one ``@name`` relation per deployed Named Query, mirroring the JDBC
    catalogue registration in ``fetch_model_metadata`` (invariant 7: only the
    deployed snapshot defines Named Queries, so an undeployed draft never
    enters the catalogue and an edit needs a deploy before clients can
    reference it). The definition dicts carry the derived ``output_columns``
    the catalogue advertises via ``build_named_query_relation_columns``.

    Fail-closed on a transient failure: an empty list, so the XMLA catalogue
    advertises no Named Query tables rather than draft state. A 409
    DEPLOYED_SNAPSHOT_INVALID propagates so the discovery handler can fail
    LOUD with a diagnosable fault instead of rendering a deceptive empty
    table list (Bug-8384 parity: discovery and Execute must agree).
    """
    try:
        snap = await get_model_version_snapshot(
            model_id, deployed_version_id, tenant_slug, jwt_token,
            project_id=project_id,
        )
    except httpx.HTTPStatusError as exc:
        if getattr(exc.response, "status_code", None) == 409:
            raise
        logger.warning(
            "Bug-9178: deployed snapshot fetch failed for model %s "
            "(version %s); XMLA catalogue will advertise no Named Query "
            "tables: %s", model_id, deployed_version_id, exc,
        )
        return []
    except Exception as exc:
        logger.warning(
            "Bug-9178: deployed snapshot fetch failed for model %s "
            "(version %s); XMLA catalogue will advertise no Named Query "
            "tables: %s", model_id, deployed_version_id, exc,
        )
        return []
    return [
        nq for nq in (snap or {}).get("named_queries") or []
        if isinstance(nq, dict)
        and str(nq.get("name") or "").strip()
    ]


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
    project_slug: str | None = None,
    *,
    use_cache: bool = False,
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

    Raises :class:`ModelMetadataUnavailable` when the metadata cannot be
    determined (Bug-9213/9218). It must NOT be reported as "this tenant has no
    tables" or "unknown model" — see that exception's docstring.

    ``use_cache`` serves a short-TTL burst cache of the whole fan-out
    (Bug-9061). It is OPT-IN because the CLS revalidation path
    (``_refresh_catalogue_if_stale``) exists precisely to defeat caching: its
    default TTL is 0 so a tightened persona restriction hides columns
    immediately, and serving that path from a 30 s cache would silently undo a
    security contract.
    """
    # The per-model loop below REBINDS ``project_slug`` (``project_slug =
    # m.get("project_slug", "public")``), so by the time the cache is written
    # the parameter no longer holds the caller's scope. Capture it now and key
    # both the read and the write on this — otherwise every entry is stored
    # under a scope nobody looks it up by, and the cache never hits.
    _scope_project_slug = project_slug
    if use_cache:
        cached = _metadata_cache_get(
            model_id, tenant_slug, jwt_token, _scope_project_slug
        )
        if cached is not None:
            return cached
    # A caller resolving ONE model cannot tolerate a partial listing: a dropped
    # project makes a real model look absent (Bug-9218).
    strict = model_id is not None
    try:
        all_models = await list_all_models_for_tenant(tenant_slug, jwt_token)
    except ModelMetadataUnavailable:
        raise
    except Exception as exc:
        logger.error(
            "Failed to list models for tenant %s: %s — refusing to serve an "
            "empty catalogue (Bug-9213: an absence is not a failure)",
            tenant_slug, exc,
        )
        raise ModelMetadataUnavailable(
            f"Model metadata for tenant {tenant_slug!r} is unavailable: {exc}"
        ) from exc
    if strict:
        _missing_projects = tenant_listing_degraded(tenant_slug, jwt_token)
        if _missing_projects:
            raise ModelMetadataUnavailable(
                f"Model listing for tenant {tenant_slug!r} is incomplete "
                f"({_missing_projects} project(s) could not be listed), so "
                f"model {model_id!r} cannot be resolved."
            )

    # Bug-5878: an optional project scope (3-part JDBC dbname
    # <tenant>/<project>/<model>) narrows every match below.
    def _in_project(m: dict) -> bool:
        return (
            project_slug is None
            or str(m.get("project_slug", "")).lower() == project_slug.lower()
        )

    if model_id:
        target_models = [
            m for m in all_models
            if str(m.get("id")) == str(model_id) and _in_project(m)
        ]
        if not target_models:
            mid_lower = str(model_id).lower()
            target_models = [
                m for m in all_models
                if (
                    str(m.get("slug", "")).lower() == mid_lower
                    or str(m.get("name", "")).lower() == mid_lower
                )
                and _in_project(m)
            ]
    else:
        target_models = [m for m in all_models if _in_project(m)]

    # Bug-9061: a model dropped by the degradation path below makes the result
    # incomplete, so it must not be cached (see the return statement).
    _degraded_models = 0
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
        cross-owner collision (fail closed: never silently overwrite).

        Bug-6773: the disambiguating counter goes BEFORE the ``$KPIs`` marker,
        never after it. ``$KPIs`` is a structural suffix, not decoration —
        ``_is_kpi_table_query`` (jdbc/server.py) and the router's scorecard
        interception both test ``endswith("$kpis")``. Appending the counter at
        the end produced ``alpha__sales$KPIs_2``, which stops being recognised
        as a scorecard relation: the interception misses, the binder cannot
        resolve it, and every query against that advertised relation 422s.
        Fail-closed, but permanently broken for that model.
        """
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
            candidate = _suffix_preserving_counter(prefixed, suffix)
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
                # Bug-9218: dropping the model here is what turns a metadata
                # FAILURE into "FATAL Unknown model" at the connection seam. When
                # the caller asked for THIS model, say the metadata is
                # unavailable; only a broad tenant browse degrades past it.
                if strict:
                    raise ModelMetadataUnavailable(
                        f"Metadata for model {mid} could not be fetched: {exc}"
                    ) from exc
                logger.error(
                    "Failed to fetch metadata for model %s (%s); it is HIDDEN "
                    "from BI tools for this discovery call.", mid, exc,
                )
                _degraded_models += 1
                continue
            _metadata_complete = not any(
                isinstance(_optional_result, BaseException)
                for _optional_result in (
                    persona_result,
                    snapshot_result,
                    kpi_result,
                    deployed_snapshot_result,
                )
            )
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
            # query-router binder independently pins query RESULTS.
            #
            # Bug-7979 / F-013-05 / F-001-02 (fail-closed): a DEPLOYED model's
            # catalogue MUST show the deployed contract, NEVER live draft metadata.
            # When the deployed snapshot is unavailable, empty, or corrupt, the
            # correct response is to advertise an empty field set for this model
            # (not to fall back to live draft, which would leak unpublished edits).
            # Legacy placeholder snapshots should be repaired by re-deploying, not
            # by silently serving draft state at query time.
            if deployed_version_id:
                if isinstance(deployed_snapshot_result, BaseException):
                    logger.warning(
                        "Deployed snapshot unavailable for model %s (Bug-7979 "
                        "fail-closed: catalogue will show empty field set, NOT "
                        "live draft): %s", mid, deployed_snapshot_result,
                    )
                    # Fail closed: empty deployed contract, no live fallback.
                    dimensions = []
                    measures = []
                    snapshot = {}
                else:
                    deployed_snapshot = deployed_snapshot_result or {}
                    dimensions = deployed_snapshot.get("dimensions") or []
                    measures = deployed_snapshot.get("measures") or []
                    # Use the deployed snapshot's full shape (tables, columns,
                    # joins, UDAs) so column metadata, technical persona, and
                    # FK relations also reflect the deployed contract.
                    snapshot = deployed_snapshot
            if not _metadata_complete:
                # Preserve broad discovery's useful base columns, but never
                # cache a catalogue assembled from an optional endpoint
                # failure. The next connection must retry the fan-out and can
                # then restore personas, KPIs, snapshots, or the deployed
                # contract (Bug-9061).
                _degraded_models += 1
        except ModelMetadataUnavailable:
            # A named-model lookup must preserve the typed fail-closed signal
            # raised above. Catching it here would turn an upstream metadata
            # failure back into an empty result and the JDBC layer would lie
            # with ``Unknown model`` (Bug-9218).
            raise
        except Exception as exc:
            logger.warning("Failed to fetch metadata for model %s: %s", mid, exc)
            continue

        semantic_tables = snapshot.get("tables") or []
        # Bug-9110: ONE fact test for the whole codebase. This site used to ask
        # ``.lower() in {"fact", "center"}`` — case-INsensitive, and carrying a
        # phantom "center" alias no producer in this repo has ever emitted. The
        # canonical predicate is deliberately case-SENSITIVE and "fact"-only,
        # matching the partial unique index that caps a model at one fact table,
        # so the looser test could count a ``"Fact"`` row as the fact table for
        # the gateway's row-count estimate while the router, the anchor rule and
        # the storage index all say it is not.
        fact_estimates = [
            table.get("row_count_estimate")
            for table in semantic_tables
            if is_fact_table(table)
            and table.get("row_count_estimate") is not None
        ]
        base_row_estimate = max(fact_estimates) if fact_estimates else None
        column_rows = {
            str(column.get("id")): column
            for column in snapshot.get("columns") or []
        }
        # The query-router binds the deployed physical shape before serving a
        # SELECT *. A semantic dimension/measure can still carry
        # ``is_hidden=False`` while its source column is hidden in that shape
        # (the semantic object and snapshot column are separate producer
        # domains). Treat deployed source-column curation as the authority
        # here too, or the JDBC/XMLA descriptor advertises fields that the
        # execution path deliberately removes (Bug-9433).
        hidden_column_ids = {
            column_id
            for column_id, column in column_rows.items()
            if column.get("is_hidden")
        }

        def _source_column_is_hidden(item: dict) -> bool:
            source_column_id = item.get("source_column_id")
            return (
                source_column_id is not None
                and str(source_column_id) in hidden_column_ids
            )

        def _effective_is_hidden(item: dict) -> bool:
            return bool(item.get("is_hidden")) or _source_column_is_hidden(item)

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
            # F-008-04: hide objects that reach a restricted column TRANSITIVELY
            # (calc/UDA/variant/calc-dimension), not only direct source columns.
            # Reuses the SAME shared closure the query-router serving gate uses,
            # driven by the (deployed) snapshot, so catalogue visibility and
            # runtime enforcement never disagree. Personas/tags are always-live,
            # so the restricted set is current even under deployed pinning.
            _cls_ctx = build_cls_closure_context(
                measures=measures,
                snapshot=snapshot,
                restricted_column_ids=restricted_column_ids,
            )
            cols: list[dict] = []
            ordinal = 1
            for dim in dimensions:
                is_hidden = _effective_is_hidden(dim)
                if is_hidden and not include_hidden:
                    continue
                if allow_dimension_ids and str(dim.get("id", "")) not in allow_dimension_ids:
                    continue
                if object_hidden_by_cls(dim, restricted_column_ids, _cls_ctx):
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
                    "is_hidden": is_hidden,
                    "is_nullable": bool(
                        column_rows.get(str(dim.get("source_column_id")), {}).get("is_nullable", True)
                    ),
                    "is_primary_key": bool(
                        column_rows.get(str(dim.get("source_column_id")), {}).get("is_primary_key")
                    ),
                })
                ordinal += 1
            for measure in measures:
                is_hidden = _effective_is_hidden(measure)
                if is_hidden and not include_hidden:
                    continue
                if allow_measure_ids and str(measure.get("id", "")) not in allow_measure_ids:
                    continue
                # F-008-04: transitive CLS closure (calc/UDA/variant), matching
                # runtime enforcement — not just the direct source column.
                if object_hidden_by_cls(measure, restricted_column_ids, _cls_ctx):
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
                    "is_hidden": is_hidden,
                    "is_nullable": bool(
                        column_rows.get(str(measure.get("source_column_id")), {}).get("is_nullable", True)
                    ),
                    "is_primary_key": False,
                })
                ordinal += 1

            # Inline KPI columns: when expose_kpis_inline is true on the
            # model, inject KPI values as read-only computed measure columns
            # directly in the base table (prefixed with "[KPI] ").
            # Bug-7227: filter inline KPI columns BY LINEAGE for a
            # measure-restricted persona (was Bug-5587, which excluded ALL inline
            # KPI columns — a persona restricted to some measures then saw ZERO
            # KPI columns). Keep exactly the KPIs whose transitive measure lineage
            # is inside the allow-list; fail closed on unverifiable lineage. Same
            # policy as the XMLA catalogue and the query-router $KPIs data path.
            if m.get("expose_kpis_inline") and kpis:
                inline_kpis = filter_kpis_for_persona(
                    kpis, measures, allow_measure_ids,
                )
                for kpi in inline_kpis:
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
        #
        # F-008-05: register a $KPIs relation for the BASE surface AND for each
        # persona variant (``<slug>_<persona>$KPIs``), threading that variant's
        # persona_id. Previously ONE model-level $KPIs (persona_id=None) served
        # every catalogue, so an administrator impersonating a persona through
        # its catalogue got the UNRESTRICTED KPI lineage — the resolved relation
        # persona was always None, so the query-router's persona measure/CLS
        # gate (``_kpi_allowed_by_persona``) never applied. With a persona-scoped
        # relation carrying the persona_id, admin impersonation now exercises the
        # SAME KPI gate a real assigned user hits. (Row-level security on the KPI
        # data itself is still enforced independently in the query-router.)
        if kpis:
            kpi_cols = build_kpi_virtual_table_columns()
            trust = m.get("trust_meta") or {}
            _kpi_trust = {
                "last_refreshed_at": trust.get("last_refreshed_at"),
                "source_system": trust.get("source_system"),
                "owner": trust.get("owner") or "",
            }

            def _register_kpi_relation(rel_base: str, persona_ref: dict) -> None:
                kpi_table_name = f"{rel_base}$KPIs"
                _pid = str(persona_ref["id"]) if persona_ref.get("id") else None
                # Bug-6057 / F-001-20: route the KPI virtual-table name through
                # the same F-008-15 collision guard as the base relations.
                _owner = f"{mid}:{_pid}" if _pid else f"{mid}:base"
                kpi_table_name = _register_relation(
                    kpi_table_name, _owner, m.get("project_slug", "public"),
                )
                model_names.append(kpi_table_name)
                table_columns[kpi_table_name] = kpi_cols
                table_model_id[kpi_table_name] = mid
                # F-008-05: carry the variant persona so admin impersonation of a
                # persona catalogue reaches the query-router KPI persona gate.
                table_persona_id[kpi_table_name] = _pid
                table_include_hidden[kpi_table_name] = False
                # The query_name MUST be the KPI table's own name, not the base
                # model slug. The query-router detects the "$KPIs" suffix to
                # route to the scorecard handler; aliasing it to the base slug
                # would let _rewrite_exposed_relations strip "$KPIs".
                table_query_name[kpi_table_name] = kpi_table_name
                table_row_estimates[kpi_table_name] = None
                table_project_slug[kpi_table_name] = m.get("project_slug", "public")
                _label = persona_ref.get("name") or persona_ref.get("slug")
                table_descriptions[kpi_table_name] = (
                    f"KPI scorecard virtual table ({_label})"
                    if _label else "KPI scorecard virtual table"
                )
                table_trust_meta[kpi_table_name] = _kpi_trust

            # Base surface (persona_id=None) + one per persona variant.
            _register_kpi_relation(base_name, {})
            for _v_suffix, _v_hidden, _v_persona in variants:
                if not (_v_suffix and _v_persona and _v_persona.get("id")):
                    continue
                _register_kpi_relation(f"{base_name}{_v_suffix}", _v_persona)

        # Named Query relations: each deployed Named Query is exposed as an
        # ``@name`` relation so ``SELECT * FROM @name`` resolves a model over
        # the JDBC path (the query-router then serves it materialised-first /
        # source-fallback through its step-1.6 interceptor). Registered only
        # for DEPLOYED models: the snapshot used above IS the deployed
        # snapshot when ``deployed_version_id`` is set (Bug-7979 pin), so an
        # undeployed Named Query draft never enters the catalogue and an edit
        # needs a deploy before clients can reference it (invariant 7).
        if deployed_version_id:
            for nq in snapshot.get("named_queries") or []:
                if not isinstance(nq, dict):
                    continue
                nq_name = str(nq.get("name") or "").strip()
                if not nq_name:
                    continue
                rel = f"@{nq_name}"
                _owner = f"{mid}:base:nq:{str(nq.get('id') or '')}"
                rel = _register_relation(
                    rel, _owner, m.get("project_slug", "public"),
                )
                model_names.append(rel)
                table_columns[rel] = build_named_query_relation_columns(nq)
                table_model_id[rel] = mid
                table_persona_id[rel] = None
                table_include_hidden[rel] = False
                # The query_name is the relation's own @name — the query-router
                # intercepts the literal ``@name`` reference; rewriting it to a
                # model slug would make the reference unresolvable.
                table_query_name[rel] = rel
                table_row_estimates[rel] = None
                table_project_slug[rel] = m.get("project_slug", "public")
                table_descriptions[rel] = (
                    nq.get("description")
                    or f"Named Query @{nq_name} (deployed definition)"
                )
                table_trust_meta[rel] = {
                    "last_refreshed_at": None,
                    "source_system": (m.get("trust_meta") or {}).get("source_system"),
                    "owner": (m.get("trust_meta") or {}).get("owner") or "",
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
            if not settings.LOOKER_GATEWAY_ENABLED:
                # Detection-only surface when the Looker gateway is off: no
                # catalogue maps are written for this relation, so no collision
                # can occur — keep the raw adapter name for exposure/disabled
                # detection.
                looker_relations.add(relation_name)
                continue
            # Bug-6057 / F-001-20: when the relation IS registered into the
            # catalogue maps, route it through the same F-008-15 collision guard
            # as the base relations so a cross-model name clash is
            # project-prefixed instead of silently overwriting the owner. The
            # guarded name is used consistently below — looker_relations set,
            # relation_by_table (which the FK wiring reads), and every map write.
            #
            # Bug-6772: the owner key is per-SURFACE (`table:<table_id>`), not the
            # model-wide `<mid>:base`. A single model exposes ONE semantic-table
            # relation per table, so two tables whose aliases NORMALISE to the
            # same relation name (e.g. `Order-Items` and `Order_Items` both ->
            # `order_items` via `_relation_identifier`) are DIFFERENT owners and
            # must not silently overwrite each other. With the model-wide key the
            # second call saw `prior == owner_key` and reused the name, so
            # `table_columns` / `table_model_id` were overwritten and the FK
            # wiring (`relation_by_table`) attached joins to the wrong table.
            # A per-table key makes the guard detect the collision and suffix the
            # second surface (fail closed); re-registering the SAME table (same
            # id) still no-ops. Cross-model collisions stay handled because a
            # different model's `mid` yields a different key regardless of table.
            relation_name = _register_relation(
                relation_name, f"{mid}:table:{table_id}",
                m.get("project_slug", "public"),
            )
            looker_relations.add(relation_name)
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
                    "is_hidden": _effective_is_hidden(dim),
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
                    "is_hidden": _effective_is_hidden(measure),
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
        # F-008-31 / Bug-6137: the auto-injected ``<slug>_technical`` relation
        # exposes hidden PHYSICAL columns (include_hidden=True, persona_id=None).
        # It must be gated to the same audience as the seeded Technical persona
        # that F-008-04 gated — otherwise a viewer with no ``model_technical``
        # grant re-acquires the hidden-column surface migration 0124 closed.
        # ``personas`` is the model-service audience-filtered list
        # (``for_audience=true`` applies ``is_in_audience``), so a
        # hidden-columns persona is present ONLY when this caller is authorised
        # for the technical view (privileged users see every persona). Absent
        # such authorisation the ungated fallback must NOT be injected.
        caller_authorized_technical = any(
            bool(p.get("includes_hidden_columns")) for p in personas
        )
        if not has_technical and semantic_tables and caller_authorized_technical:
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
                # Bug-9110: same canonical, case-SENSITIVE predicate as above —
                # this choice must agree with the execution path's anchor rule,
                # or the advertised technical columns describe a different table
                # from the one ``SELECT *`` actually reads.
                if is_fact_table(st):
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
                # Bug-6057 / F-001-20: route the auto-injected technical
                # relation through the same F-008-15 collision guard as the base
                # relations. A cross-model clash (another model whose base slug
                # equals this ``<slug>_technical``) would otherwise silently
                # overwrite the catalogue owner and expose the wrong model's
                # physical columns. Owner is this model's base surface.
                tech_table = _register_relation(
                    tech_table, f"{mid}:base", m.get("project_slug", "public"),
                )
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

    result = (
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
    if use_cache and _degraded_models == 0:
        # Bug-9061: only a COMPLETE fetch is cached. A degraded result (a model
        # whose metadata could not be fetched) must be retried on the next
        # connection, never pinned for the TTL — that is how a transient
        # model-service blip would turn into 30 s of "my model vanished".
        _metadata_cache_put(
            model_id, tenant_slug, jwt_token, _scope_project_slug, result
        )
    return result


def _extract_token_from_response(resp: httpx.Response) -> str:
    """Read the JWT from the httpOnly access_token cookie on the response."""
    token = resp.cookies.get("access_token")
    if token:
        return token
    raise ValueError("model-service login response missing access_token cookie")


def _login_retry_attempts() -> int:
    return int(system_snapshot_get("gateway.login_retry_attempts"))


async def _post_login(url: str, body: dict[str, str]) -> httpx.Response:
    """POST a login relay, retrying transient transport timeouts.

    Bug-5534 item B: the first login after idle can hit a Cloud Run cold
    start on model-service and exceed the client timeout. A ReadTimeout
    surfaced as a 401 challenge that MSOLAP/ADOMD clients do not recover
    from, so transport timeouts are retried (the warmed instance answers
    the retry). A real credential rejection (HTTPStatusError) is never
    retried and propagates on the first attempt.
    """
    attempts = 1 + _login_retry_attempts()
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=_t_medium()) as client:
                resp = await client.post(
                    url, json=body, headers=internal_request_headers()
                )
                resp.raise_for_status()
                return resp
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            last_exc = exc
            logger.warning(
                "Login relay to %s timed out (%s), attempt %d/%d",
                url, type(exc).__name__, attempt, attempts,
            )
    assert last_exc is not None
    raise last_exc


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
    # BI clients open many short-lived connections; the gateway's login
    # relay must not be throttled by the model-service login limiter.
    resp = await _post_login(url, body)
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
    resp = await _post_login(url, body)
    return _extract_token_from_response(resp)


def _measure_sql_type(default_agg: str) -> str:
    """Map a measure aggregation type to a SQL data type string."""
    if default_agg in ("count", "count_distinct"):
        return "bigint"
    if default_agg in ("sum", "avg", "min", "max"):
        return "numeric"
    return "numeric"
