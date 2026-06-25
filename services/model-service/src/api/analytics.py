"""
Usage analytics API: query volume, top measures, top aggregates, summary.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select, text

from shared.db.models import (
    AggregateDefinition,
    QueryLog,
    QueryMissLog,
)
from shared.db.session import get_tenant_db
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/analytics",
    tags=["analytics"],
)

# F-030-09: introspect probe rows are written to QueryLog for auditability but
# are table-preview/member-sampling probes, not user queries — they must not
# count toward any usage statistic, hit rate, top-user, or routing breakdown.
_EXCLUDED_ROUTE_TYPES = ("introspect",)


def _exclude_probes(stmt):
    """Filter introspect probe rows out of any QueryLog aggregation (F-030-09)."""
    return stmt.where(QueryLog.route_type.notin_(_EXCLUDED_ROUTE_TYPES))


class QueryVolumeBucket(BaseModel):
    bucket: str
    count: int


class TopMeasure(BaseModel):
    measure_name: str
    query_count: int


class TopAggregate(BaseModel):
    aggregate_id: str
    physical_table_name: str
    query_count: int
    grain: list[str] | None = None


class AnalyticsSummary(BaseModel):
    total_queries: int
    # F-030-08: the headline KPI is the combined acceleration rate (aggregate +
    # pocket routes) — it is the single number a business user reads as "how
    # often did acceleration help". ``aggregate_hit_rate`` is retained as a
    # secondary breakdown (aggregate-only) and no longer the headline, so the
    # dashboard can no longer show "Aggregate hit rate 20%" beside "Accelerated
    # 70%" with no reconciling figure.
    acceleration_rate: float
    aggregate_hit_rate: float
    top_measure: str | None
    avg_response_ms: float | None


def _date_range(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/query-volume", response_model=list[QueryVolumeBucket])
async def query_volume(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(30, ge=1, le=365),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[QueryVolumeBucket]:
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        # F-030-02: the model must belong to the path project (404 otherwise) —
        # without this an analytics path with a foreign model_id would read
        # another project's usage.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        stmt = (
            select(
                func.date_trunc("day", QueryLog.created_at).label("bucket"),
                func.count().label("cnt"),
            )
            .where(QueryLog.model_id == model_id, QueryLog.created_at >= since, QueryLog.status == "success")
            .group_by(text("1"))
            .order_by(text("1"))
        )
        stmt = _exclude_probes(stmt)
        result = await db.execute(stmt)
        return [
            QueryVolumeBucket(bucket=str(row.bucket.date()), count=row.cnt)
            for row in result.all()
        ]
    return []


@router.get("/top-measures", response_model=list[TopMeasure])
async def top_measures(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[TopMeasure]:
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        stmt = (
            select(
                QueryMissLog.requested_measures,
                func.sum(QueryMissLog.occurrence_count).label("total"),
            )
            .where(
                QueryMissLog.model_id == model_id,
                QueryMissLog.last_seen_at >= since,
                QueryMissLog.requested_measures.isnot(None),
            )
            .group_by(QueryMissLog.requested_measures)
            .order_by(text("total DESC"))
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.all()
        out: list[TopMeasure] = []
        for row in rows:
            measures = row.requested_measures
            if isinstance(measures, list):
                name = ", ".join(str(m) for m in measures[:3])
            else:
                name = str(measures)
            out.append(TopMeasure(measure_name=name, query_count=int(row.total)))
        return out
    return []


@router.get("/top-aggregates", response_model=list[TopAggregate])
async def top_aggregates(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[TopAggregate]:
    # F-030-24: honour the same ``days`` window as every sibling endpoint and
    # exclude introspect probes, so the top-aggregates panel is consistent with
    # the rest of Usage Analytics instead of reporting all-time totals.
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        stmt = (
            select(
                QueryLog.aggregate_id,
                func.count().label("cnt"),
            )
            .where(
                QueryLog.model_id == model_id,
                QueryLog.aggregate_id.isnot(None),
                QueryLog.created_at >= since,
                QueryLog.status == "success",
            )
            .group_by(QueryLog.aggregate_id)
            .order_by(text("cnt DESC"))
            .limit(limit)
        )
        stmt = _exclude_probes(stmt)
        result = await db.execute(stmt)
        rows = result.all()
        agg_ids = [row.aggregate_id for row in rows]
        counts = {row.aggregate_id: row.cnt for row in rows}

        if not agg_ids:
            return []

        agg_stmt = select(AggregateDefinition).where(
            AggregateDefinition.id.in_(agg_ids)
        )
        agg_result = await db.execute(agg_stmt)
        aggs_by_id = {a.id: a for a in agg_result.scalars().all()}

        out: list[TopAggregate] = []
        for aid in agg_ids:
            agg = aggs_by_id.get(aid)
            if agg:
                out.append(TopAggregate(
                    aggregate_id=str(agg.id),
                    physical_table_name=agg.physical_table_name,
                    query_count=counts[aid],
                    grain=agg.grain,
                ))
        return out
    return []


@router.get("/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(7, ge=1, le=365),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> AnalyticsSummary:
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        count_stmt = _exclude_probes(select(func.count()).where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.status == "success",
        ))
        total_result = await db.execute(count_stmt)
        total_queries = total_result.scalar_one()

        # F-030-08: count both acceleration mechanisms. ``aggregate_hits`` keeps
        # the aggregate-only breakdown (aggregate_id IS NOT NULL); ``accel_hits``
        # counts aggregate + pocket routes for the headline acceleration rate.
        hit_stmt = _exclude_probes(select(
            func.count().label("total"),
            func.sum(
                case((QueryLog.aggregate_id.isnot(None), 1), else_=0)
            ).label("agg_hits"),
            func.sum(
                case((QueryLog.route_type.in_(["aggregate", "pocket"]), 1), else_=0)
            ).label("accel_hits"),
        ).where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.status == "success",
        ))
        hit_result = await db.execute(hit_stmt)
        hit_row = hit_result.one()
        total_for_rate = int(hit_row.total) if hit_row.total else 0
        aggregate_hit_rate = (
            (int(hit_row.agg_hits) / total_for_rate * 100) if total_for_rate > 0 else 0.0
        )
        acceleration_rate = (
            (int(hit_row.accel_hits) / total_for_rate * 100) if total_for_rate > 0 else 0.0
        )

        avg_stmt = _exclude_probes(select(func.avg(QueryLog.execution_ms)).where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.execution_ms.isnot(None),
            QueryLog.status == "success",
        ))
        avg_result = await db.execute(avg_stmt)
        avg_ms = avg_result.scalar_one()

        top_stmt = (
            select(
                QueryMissLog.requested_measures,
                func.sum(QueryMissLog.occurrence_count).label("total"),
            )
            .where(
                QueryMissLog.model_id == model_id,
                QueryMissLog.last_seen_at >= since,
                QueryMissLog.requested_measures.isnot(None),
            )
            .group_by(QueryMissLog.requested_measures)
            .order_by(text("total DESC"))
            .limit(1)
        )
        top_result = await db.execute(top_stmt)
        top_row = top_result.first()
        top_measure = None
        if top_row and top_row.requested_measures:
            measures = top_row.requested_measures
            top_measure = (
                ", ".join(str(m) for m in measures[:3])
                if isinstance(measures, list)
                else str(measures)
            )

        return AnalyticsSummary(
            total_queries=total_queries,
            acceleration_rate=round(acceleration_rate, 1),
            aggregate_hit_rate=round(aggregate_hit_rate, 1),
            top_measure=top_measure,
            avg_response_ms=round(float(avg_ms), 1) if avg_ms is not None else None,
        )
    return AnalyticsSummary(
        total_queries=0,
        acceleration_rate=0.0,
        aggregate_hit_rate=0.0,
        top_measure=None,
        avg_response_ms=None,
    )


class RoutingBreakdown(BaseModel):
    route_type: str
    count: int
    pct: float


@router.get("/routing-breakdown", response_model=list[RoutingBreakdown])
async def routing_breakdown(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(30, ge=1, le=365),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[RoutingBreakdown]:
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        stmt = (
            select(
                QueryLog.route_type,
                func.count().label("cnt"),
            )
            .where(
                QueryLog.model_id == model_id,
                QueryLog.created_at >= since,
                QueryLog.status == "success",
            )
            .group_by(QueryLog.route_type)
            .order_by(text("cnt DESC"))
        )
        stmt = _exclude_probes(stmt)
        result = await db.execute(stmt)
        rows = result.all()
        total = sum(r.cnt for r in rows) or 1
        return [
            RoutingBreakdown(
                route_type=r.route_type or "unknown",
                count=r.cnt,
                pct=round(r.cnt / total * 100, 1),
            )
            for r in rows
        ]
    return []


class EstimatedSavings(BaseModel):
    accelerated_queries: int
    total_queries: int
    time_saved_ms: int
    avg_source_ms: float | None
    avg_accelerated_ms: float | None


@router.get("/estimated-savings", response_model=EstimatedSavings)
async def estimated_savings(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(30, ge=1, le=365),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> EstimatedSavings:
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        total_stmt = _exclude_probes(select(func.count()).where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.status == "success",
        ))
        total_result = await db.execute(total_stmt)
        total = total_result.scalar_one()

        accel_stmt = select(
            func.count().label("cnt"),
            func.avg(QueryLog.execution_ms).label("avg_ms"),
        ).where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.route_type.in_(["aggregate", "pocket"]),
            QueryLog.status == "success",
        )
        accel_result = await db.execute(accel_stmt)
        accel = accel_result.one()

        source_stmt = select(
            func.avg(QueryLog.execution_ms).label("avg_ms"),
        ).where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.route_type == "source",
            QueryLog.status == "success",
        )
        source_result = await db.execute(source_stmt)
        source_avg = source_result.scalar_one()

        accel_count = int(accel.cnt or 0)
        accel_avg = float(accel.avg_ms) if accel.avg_ms is not None else None
        source_avg_ms = float(source_avg) if source_avg is not None else None

        time_saved = 0
        if source_avg_ms is not None and accel_avg is not None and accel_count > 0:
            time_saved = int((source_avg_ms - accel_avg) * accel_count)

        return EstimatedSavings(
            accelerated_queries=accel_count,
            total_queries=total,
            time_saved_ms=max(time_saved, 0),
            # F-030-18: ``is not None`` — a legitimate 0.0 ms average is a real
            # value, not "no data". The old truthiness test mapped 0.0 to None,
            # rendering "—" in the UI for a sub-millisecond query.
            avg_source_ms=round(source_avg_ms, 1) if source_avg_ms is not None else None,
            avg_accelerated_ms=round(accel_avg, 1) if accel_avg is not None else None,
        )
    return EstimatedSavings(
        accelerated_queries=0,
        total_queries=0,
        time_saved_ms=0,
        avg_source_ms=None,
        avg_accelerated_ms=None,
    )


class TopUser(BaseModel):
    user_identity: str
    query_count: int


@router.get("/top-users", response_model=list[TopUser])
async def top_users(
    project_id: UUID,
    model_id: UUID,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),  # F-030-02
) -> list[TopUser]:
    since = _date_range(days)
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        stmt = (
            select(
                QueryLog.user_identity,
                func.count().label("cnt"),
            )
            .where(
                QueryLog.model_id == model_id,
                QueryLog.created_at >= since,
                QueryLog.user_identity.isnot(None),
                QueryLog.status == "success",
            )
            .group_by(QueryLog.user_identity)
            .order_by(text("cnt DESC"))
            .limit(limit)
        )
        stmt = _exclude_probes(stmt)
        result = await db.execute(stmt)
        return [
            TopUser(user_identity=row.user_identity, query_count=row.cnt)
            for row in result.all()
        ]
    return []
