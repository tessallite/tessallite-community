"""
Table Preview — paginated data fetch routed through the query-router.

All source-database access goes via the query-router's /introspect
endpoint for centralised audit, execution, and connection management.
"""
from __future__ import annotations

import datetime
import decimal
import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from shared.config.settings import get_settings
from shared.connector_qualify import quote_table_ref, transpile_preview_sql
from shared.db.models import DataSource, ModelTable
from shared.db.session import get_tenant_db
from shared.schemas.connection_type import normalize_connection_type
from src.api._scope import ensure_model_in_project, resolve_source_connection
from src.api._table_qualify import qualify_physical_name
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)

_settings = get_settings()

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/tables",
    tags=["table-preview"],
)

PAGE_SIZE_MAX = 200


class TablePreviewResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    page: int
    page_size: int
    has_more: bool
    total_rows: int | None = None


def _serialize_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    return str(v)


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    raise HTTPException(status_code=401, detail="Bearer token required")


async def _resolve_table_context(db, table_id: UUID, *, expected_project_id: UUID):
    table = await db.get(ModelTable, table_id)
    if table is None:
        return None, None, None, "Table not found."

    source = (
        await db.execute(
            select(DataSource).where(DataSource.id == table.source_id)
        )
    ).scalar_one_or_none()
    if source is None:
        return None, None, None, "No data source configured for this table."

    # Bug-5325: fail closed when the source's connection belongs to a different
    # project (legacy/imported malformed row) instead of previewing data from
    # the wrong project's source. resolve_source_connection raises on mismatch.
    conn = await resolve_source_connection(
        db, source, expected_project_id=expected_project_id
    )

    connector = normalize_connection_type((conn.connection_type or "").lower())

    # Resolve the stored physical_name to a fully-qualified, *unquoted* dotted
    # reference using the source's default schema/dataset (and BQ project).
    # Bug-5470: previously the bare physical_name was quoted with PostgreSQL
    # rules regardless of connector, so BigQuery sources whose physical_name
    # lacked a dataset prefix produced "Table must be qualified with a dataset".
    qualified_name = qualify_physical_name(table.physical_name, conn, source)

    return connector, conn, qualified_name, None


async def _introspect_via_router(
    model_id: str,
    sql: str,
    bearer: str,
    timeout_s: float = 60.0,
) -> tuple[list[dict], list[str]]:
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/introspect"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {"model_id": model_id, "raw_sql": sql}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else resp.text
            except Exception:
                detail = resp.text or f"Introspect returned HTTP {resp.status_code}"
            raise HTTPException(status_code=resp.status_code, detail=detail)
        data = resp.json()
        return data["rows"], data["columns"]


@router.get(
    "/{table_id}/preview",
    response_model=TablePreviewResponse,
    dependencies=[require_role("viewer")],
)
async def preview_table(
    request: Request,
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    page: int = Query(default=0, ge=0),
    page_size: int = Query(default=50, ge=1, le=PAGE_SIZE_MAX),
    count: bool = Query(default=False),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> TablePreviewResponse:
    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        table = await db.get(ModelTable, table_id)
        if table is None or str(table.model_id) != str(model_id):
            raise HTTPException(status_code=404, detail="Table not found")

        connector, _, qualified_name, err = await _resolve_table_context(
            db, table_id, expected_project_id=project_id
        )
        if err:
            raise HTTPException(status_code=422, detail=err)

        # Quote the resolved dotted reference with canonical PostgreSQL rules,
        # then transpile to the connector dialect via sqlglot (single canonical
        # -> dialect path; BigQuery backticks come out of the transpile step).
        pg_quoted = quote_table_ref("postgresql", qualified_name)
        offset = page * page_size
        canonical = f"SELECT * FROM {pg_quoted} LIMIT {page_size + 1} OFFSET {offset}"
        sql = transpile_preview_sql(connector, canonical)

        try:
            rows, col_names = await _introspect_via_router(
                str(model_id), sql, bearer,
            )
            has_more = len(rows) > page_size
            data_rows = rows[:page_size]

            total_rows: int | None = None
            if count:
                count_canonical = f"SELECT COUNT(*) AS cnt FROM {pg_quoted}"
                count_sql = transpile_preview_sql(connector, count_canonical)
                count_rows, _count_cols = await _introspect_via_router(
                    str(model_id), count_sql, bearer,
                )
                total_rows = count_rows[0]["cnt"] if count_rows else 0
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Preview query failed: {exc}"
            )

        serialized = [
            {col: _serialize_value(row.get(col)) for col in col_names}
            for row in data_rows
        ]

        return TablePreviewResponse(
            columns=col_names,
            rows=serialized,
            page=page,
            page_size=page_size,
            has_more=has_more,
            total_rows=total_rows,
        )
