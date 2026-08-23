"""Routed source-table existence probes.

The probe is deliberately shared by model-service calendar binding and the
scheduler schema-drift job.  It executes the canonical ``SELECT 1`` through
the query-router, so callers never open a source connection just to decide
whether a table exists.

Only an explicit query-router ``source_table_not_found`` response means that
the relation is absent.  Transport failures, router 5xx responses, timeouts,
permission errors, and malformed responses are all unavailable outcomes.
"""
from __future__ import annotations

from typing import Any

import httpx

from shared.auth.service_principal import (
    SCOPE_DATA_QUALITY,
    create_service_access_token,
)
from shared.config.settings import get_settings
from shared.connector_qualify import quote_table_ref, transpile_preview_sql
from shared.schemas.connection_type import normalize_connection_type

SOURCE_TABLE_NOT_FOUND_CODE = "source_table_not_found"


class SourceTableNotFoundError(LookupError):
    """The routed source probe confirmed that a table is absent."""

    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name
        super().__init__(
            f"Source table {qualified_name!r} does not exist on the source database."
        )


class SourceProbeUnavailableError(RuntimeError):
    """The source-table probe could not establish an answer."""


def _attribute_value(value: Any) -> Any:
    """Read exception metadata whether a driver exposes it as a value or call."""
    if not callable(value):
        return value
    try:
        return value()
    except Exception:
        return None


def is_source_table_not_found_error(exc: BaseException) -> bool:
    """Return whether a driver error unambiguously means a missing table.

    This deliberately uses driver-neutral error metadata rather than a
    connector branch.  PostgreSQL exposes SQLSTATE ``42P01`` and BigQuery's
    not-found exception exposes HTTP code ``404``.  Other errors, including
    permissions and network failures, remain unavailable.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))

        for attr in ("sqlstate", "pgcode"):
            code = _attribute_value(getattr(current, attr, None))
            if str(code or "") in {"42P01", "42S02"}:
                return True

        for attr in ("status_code", "status", "code"):
            code = _attribute_value(getattr(current, attr, None))
            if code == 404 or str(code or "") == "404":
                return True

        reason = _attribute_value(getattr(current, "reason", None))
        if str(reason or "").lower() in {"notfound", "not_found", "not found"}:
            return True

        current = current.__cause__ or current.__context__

    return False


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _is_confirmed_missing_response(response: httpx.Response) -> bool:
    payload = _response_payload(response)
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return isinstance(detail, dict) and detail.get("code") == SOURCE_TABLE_NOT_FOUND_CODE


def _mint_probe_token(tenant_id: str) -> str:
    return create_service_access_token(
        principal="data-quality-validator",
        tenant_id=tenant_id,
        role="system_admin",
        ttl_minutes=5,
        scopes=[SCOPE_DATA_QUALITY],
    )


async def verify_source_table_exists(
    qualified_name: str,
    connection: Any,
    *,
    model_id: Any,
    source_id: Any,
    bearer: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Verify a source table through query-router's routed introspection path.

    A successful ``SELECT 1 ... LIMIT 1`` proves that the relation resolved,
    even when the table contains zero rows.  The response row count therefore
    is intentionally not used as an existence test.
    """
    connector = normalize_connection_type((connection.connection_type or "").lower())
    if not connector:
        raise SourceProbeUnavailableError("Source connector is not configured")

    try:
        pg_quoted = quote_table_ref("postgresql", qualified_name)
        canonical = f"SELECT 1 AS chk FROM {pg_quoted} LIMIT 1"
        sql = transpile_preview_sql(connector, canonical)
    except Exception as exc:
        raise SourceProbeUnavailableError(
            f"Could not build the source-table probe: {type(exc).__name__}"
        ) from exc

    if bearer is None:
        if not tenant_id:
            raise SourceProbeUnavailableError("Tenant context is required for the routed probe")
        try:
            bearer = _mint_probe_token(tenant_id)
        except Exception as exc:
            raise SourceProbeUnavailableError(
                f"Could not authenticate the routed probe: {type(exc).__name__}"
            ) from exc

    try:
        settings = get_settings()
    except Exception as exc:
        raise SourceProbeUnavailableError(
            f"Could not configure the routed probe: {type(exc).__name__}"
        ) from exc
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/introspect"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {
        "model_id": str(model_id),
        "raw_sql": sql,
        "source_id": str(source_id),
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=body, headers=headers)
    except Exception as exc:
        raise SourceProbeUnavailableError(
            f"The query-router could not answer the source-table probe: {type(exc).__name__}"
        ) from exc

    if response.status_code == 404 and _is_confirmed_missing_response(response):
        raise SourceTableNotFoundError(qualified_name)

    if response.status_code >= 400:
        raise SourceProbeUnavailableError(
            f"The query-router returned HTTP {response.status_code} for the source-table probe"
        )

    # Parse the success body so a proxy returning a non-JSON success response
    # cannot be mistaken for a confirmed existence result.
    if _response_payload(response) is None:
        raise SourceProbeUnavailableError(
            "The query-router returned an invalid source-table probe response"
        )
