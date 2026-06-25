"""
Query log and miss log read routes.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from shared.db.models import Model, QueryLog, QueryMissLog, RouteLog
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    PaginatedQueryLogResponse,
    QueryLogResponse,
    QueryMissLogResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(prefix="/projects/{project_id}/logs", tags=["logs"])


def _apply_query_log_filters(
    stmt,
    *,
    model_id: UUID | None,
    status: str | None,
    error_type: str | None,
    route_type: str | None,
    client_kind: str | None,
    user_identity: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
):
    if model_id is not None:
        stmt = stmt.where(QueryLog.model_id == model_id)
    if status is not None and status != "all":
        stmt = stmt.where(QueryLog.status == status)
    if error_type is not None:
        stmt = stmt.where(QueryLog.error_type == error_type)
    if route_type is not None:
        stmt = stmt.where(QueryLog.route_type == route_type)
    if client_kind is not None:
        stmt = stmt.where(QueryLog.client_kind == client_kind)
    if user_identity is not None:
        escaped = user_identity.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(QueryLog.user_identity.ilike(f"%{escaped}%"))
    if date_from is not None:
        stmt = stmt.where(QueryLog.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(QueryLog.created_at <= date_to)
    return stmt


@router.get("/queries", response_model=PaginatedQueryLogResponse)
async def list_query_logs(
    project_id: UUID,
    model_id: UUID | None = Query(None),
    status: str | None = Query(None, description="Filter: success, error, or all"),
    error_type: str | None = Query(None, description="Filter by error_type"),
    route_type: str | None = Query(None, description="Filter: source, aggregate, pocket"),
    client_kind: Literal["looker_studio", "looker_cloud", "plugin"] | None = Query(
        None, description="Filter: looker_studio or looker_cloud"
    ),
    user_identity: str | None = Query(None, description="Partial match on user identity"),
    date_from: datetime | None = Query(None, description="Start of date range (ISO 8601)"),
    date_to: datetime | None = Query(None, description="End of date range (ISO 8601)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    current_user: CurrentUser = Depends(forbid_embed_user),
    # F-030-02: project RBAC — query logs expose raw SQL text, rewritten
    # SQL, and user identities; only a caller with a binding to this project
    # may read them (a viewer of an unrelated project gets 403).
    _: None = require_role("viewer"),
) -> PaginatedQueryLogResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model_ids_q = select(Model.id).where(Model.project_id == project_id)
        offset = (page - 1) * page_size
        base = QueryLog.model_id.in_(model_ids_q)
        stmt = (
            select(QueryLog)
            .where(base)
            .order_by(QueryLog.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        count_stmt = select(func.count()).where(base)

        filter_kw = dict(
            model_id=model_id, status=status, error_type=error_type,
            route_type=route_type, client_kind=client_kind, user_identity=user_identity,
            date_from=date_from, date_to=date_to,
        )
        stmt = _apply_query_log_filters(stmt, **filter_kw)
        count_stmt = _apply_query_log_filters(count_stmt, **filter_kw)

        result = await db.execute(stmt)
        logs = [QueryLogResponse.model_validate(r) for r in result.scalars().all()]

        count_result = await db.execute(count_stmt)
        total = count_result.scalar_one()

        return PaginatedQueryLogResponse(items=logs, total=total, page=page, page_size=page_size)


_CSV_COLUMNS = [
    "created_at", "user_identity", "protocol", "client_kind", "raw_query", "route_type",
    "status", "error_type", "execution_ms", "rows_returned", "query_fingerprint",
]

# F-030-13: columns whose values are attacker-controllable free text and must
# be guarded against spreadsheet formula injection before export.
_CSV_TEXT_COLUMNS = frozenset({"user_identity", "raw_query", "error_type"})

# Leading characters that Excel / LibreOffice / Google Sheets interpret as the
# start of a formula. A value beginning with any of these is neutralised by
# prefixing a single quote so it is treated as literal text.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Neutralise CSV/spreadsheet formula injection in free-text fields (F-030-13).

    A query author who can place ``=HYPERLINK(...)`` (or ``+``/``-``/``@``) at the
    start of their SQL or identity would otherwise have it execute when an admin
    opens the export in a spreadsheet. Prefixing a single quote forces literal text.
    """
    if not isinstance(value, str) or value == "":
        return value
    if value[0] in _CSV_INJECTION_PREFIXES:
        return "'" + value
    return value


_MAX_EXPORT_ROWS = 50_000


@router.get("/queries/export")
async def export_query_logs_csv(
    project_id: UUID,
    model_id: UUID | None = Query(None),
    status: str | None = Query(None),
    error_type: str | None = Query(None),
    route_type: str | None = Query(None),
    client_kind: Literal["looker_studio", "looker_cloud", "plugin"] | None = Query(None),
    user_identity: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    current_user: CurrentUser = Depends(forbid_embed_user),
    # F-030-02: bulk CSV export of up to 50,000 raw query rows is a stricter
    # operation than reading a page — require modeler+ (matches the report's
    # "or stricter for export" recommendation).
    _: None = require_role("modeler"),
) -> StreamingResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model_ids_q = select(Model.id).where(Model.project_id == project_id)
        stmt = (
            select(QueryLog)
            .where(QueryLog.model_id.in_(model_ids_q))
            .order_by(QueryLog.created_at.desc())
            .limit(_MAX_EXPORT_ROWS)
        )
        stmt = _apply_query_log_filters(
            stmt, model_id=model_id, status=status, error_type=error_type,
            route_type=route_type, client_kind=client_kind, user_identity=user_identity,
            date_from=date_from, date_to=date_to,
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        for r in rows:
            writer.writerow([
                _csv_safe(getattr(r, col, "")) if col in _CSV_TEXT_COLUMNS
                else getattr(r, col, "")
                for col in _CSV_COLUMNS
            ])

        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=query_log_export.csv"},
        )


class RouteTraceStage(BaseModel):
    route_stage: str
    detail: dict


@router.get("/queries/{query_log_id}/trace", response_model=list[RouteTraceStage])
async def get_query_trace(
    project_id: UUID,
    query_log_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[RouteTraceStage]:
    """Return the stored parse/bind/route trace for one logged query (F-030-16).

    RouteLog rows were write-only — three stages were written per query and read
    by nothing, so the post-hoc trace support needs was unreachable without raw
    DB access. This exposes them, scoped to the caller's project (the QueryLog
    must belong to a model in ``project_id``), ordered parse -> bind -> route.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        # Confirm the query log belongs to a model in this project.
        log = await db.get(QueryLog, query_log_id)
        if log is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Query log not found")
        if log.model_id is not None:
            in_project = await db.execute(
                select(Model.id).where(
                    Model.id == log.model_id, Model.project_id == project_id
                )
            )
            if in_project.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Query log not found"
                )

        _STAGE_ORDER = {"parse": 0, "bind": 1, "route": 2}
        rows = (
            await db.execute(
                select(RouteLog).where(RouteLog.query_log_id == query_log_id)
            )
        ).scalars().all()
        ordered = sorted(rows, key=lambda r: _STAGE_ORDER.get(r.route_stage, 99))
        return [
            RouteTraceStage(route_stage=r.route_stage, detail=dict(r.detail or {}))
            for r in ordered
        ]


@router.get("/misses", response_model=list[QueryMissLogResponse])
async def list_miss_logs(
    project_id: UUID,
    model_id: UUID | None = Query(None),
    # F-030-24: the miss table already carries a persona and an occurrence
    # count, but neither was filterable — a modeler triaging recurring misses
    # could not isolate a persona's misses or drop one-off noise.
    persona_id: UUID | None = Query(None),
    min_occurrence: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[QueryMissLogResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        model_ids_q = select(Model.id).where(Model.project_id == project_id)
        stmt = select(QueryMissLog).order_by(QueryMissLog.last_seen_at.desc())
        if model_id is not None:
            stmt = stmt.where(
                QueryMissLog.model_id == model_id,
                QueryMissLog.model_id.in_(model_ids_q),
            )
        else:
            stmt = stmt.where(QueryMissLog.model_id.in_(model_ids_q))
        if persona_id is not None:
            stmt = stmt.where(QueryMissLog.persona_id == persona_id)
        if min_occurrence > 1:
            stmt = stmt.where(QueryMissLog.occurrence_count >= min_occurrence)
        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        return [QueryMissLogResponse.model_validate(r) for r in result.scalars().all()]
