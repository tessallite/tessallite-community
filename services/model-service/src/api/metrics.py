"""Model metrics endpoint for query routing observability."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import case, func, select

from shared.db.models import (
    AggregateDefinition,
    AggregateRefreshPolicy,
    AggregateRefreshRun,
    PocketDefinition,
    Model,
    QueryLog,
    QueryMissLog,
)
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["metrics"],
)

# F-030-09: probe/introspection rows (route_type="introspect") are written to
# QueryLog for auditability but must never count toward usage metrics — they
# are table-preview/member-sampling probes, not user queries. Every metric
# aggregation in this module filters them out via this guard.
_ANALYTICS_EXCLUDED_ROUTE_TYPES = ("introspect",)


class HourlyVolume(BaseModel):
    hour: str
    total: int
    aggregate_hits: int
    pocket_hits: int
    source_hits: int


class RefreshHealthItem(BaseModel):
    aggregate_id: str
    physical_table_name: str
    status: str
    last_status: str | None
    last_completed_at: str | None
    next_refresh_cron: str | None
    rows_written: int | None


class MissSummaryItem(BaseModel):
    query_fingerprint: str
    occurrence_count: int
    last_seen_at: str
    grain: list[str]
    measures: list[str]


class PocketSummaryItem(BaseModel):
    pocket_id: str
    physical_table_name: str
    status: str
    hit_count: int
    ttl_days: int
    last_refresh_at: str | None
    last_access_at: str | None


class ModelMetrics(BaseModel):
    model_id: str
    window_hours: int
    total_queries: int
    aggregate_hits: int
    pocket_hits: int
    source_hits: int
    hit_rate: float
    bytes_avoided: int
    hourly_volume: list[HourlyVolume]
    refresh_health: list[RefreshHealthItem]
    miss_summary: list[MissSummaryItem]
    pocket_hit_rate: float
    pocket_time_saved_ms: int
    pocket_storage_bytes: int
    pocket_evictions_24h: int
    top_pockets: list[PocketSummaryItem]


@router.get("/metrics", response_model=ModelMetrics)
async def get_model_metrics(
    project_id: UUID,
    model_id: UUID,
    window_hours: int = Query(24, ge=1, le=720),
    current_user: CurrentUser = Depends(forbid_embed_user),
    # F-030-02: project RBAC — model metrics expose per-model usage; a caller
    # with no binding to this project gets 403. The model-in-project check
    # below stays (404 for a foreign/missing model id).
    _: None = require_role("viewer"),
) -> ModelMetrics:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            # F-030-18: a non-existent or foreign model is a client error, not
            # "no traffic". Returning an all-zero payload masked wrong IDs as a
            # quiet model; 404 surfaces the mistake (matches lineage.py).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Model not found"
            )

        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=window_hours)

        # F-030-09: exclude introspect probe rows from every count.
        # F-030-12: aggregate route counts in SQL rather than loading the whole
        # window into memory.
        base_filters = (
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.status == "success",
            QueryLog.route_type.notin_(_ANALYTICS_EXCLUDED_ROUTE_TYPES),
        )

        rollup_stmt = select(
            func.count().label("total"),
            func.coalesce(
                func.sum(case((QueryLog.route_type == "aggregate", 1), else_=0)), 0
            ).label("aggregate_hits"),
            func.coalesce(
                func.sum(case((QueryLog.route_type == "pocket", 1), else_=0)), 0
            ).label("pocket_hits"),
        ).where(*base_filters)
        rollup = (await db.execute(rollup_stmt)).one()

        total_queries = int(rollup.total or 0)
        aggregate_hits = int(rollup.aggregate_hits or 0)
        pocket_hits = int(rollup.pocket_hits or 0)
        source_hits = total_queries - aggregate_hits - pocket_hits
        hit_rate = (aggregate_hits + pocket_hits) / total_queries if total_queries else 0.0
        bytes_avoided = await _calculate_bytes_avoided(db, model_id, since)

        hourly_volume = await _build_hourly_volume(db, base_filters, since, now)

        aggs_result = await db.execute(
            select(AggregateDefinition)
            .where(AggregateDefinition.model_id == model_id)
        )
        aggs = list(aggs_result.scalars().all())

        refresh_health: list[RefreshHealthItem] = []
        for agg in aggs:
            run_result = await db.execute(
                select(AggregateRefreshRun)
                .where(AggregateRefreshRun.aggregate_definition_id == agg.id)
                .order_by(AggregateRefreshRun.started_at.desc())
                .limit(1)
            )
            latest_run = run_result.scalar_one_or_none()

            policy_result = await db.execute(
                select(AggregateRefreshPolicy)
                .where(AggregateRefreshPolicy.aggregate_definition_id == agg.id)
            )
            policy = policy_result.scalar_one_or_none()

            agg_status = agg.status
            if agg_status == "active" and latest_run and latest_run.completed_at:
                completed = latest_run.completed_at
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=timezone.utc)
                if (now - completed) > timedelta(hours=48):
                    agg_status = "stale"

            refresh_health.append(
                RefreshHealthItem(
                    aggregate_id=str(agg.id),
                    physical_table_name=agg.physical_table_name,
                    status=agg_status,
                    last_status=latest_run.status if latest_run else None,
                    last_completed_at=(latest_run.completed_at.isoformat() if latest_run and latest_run.completed_at else None),
                    next_refresh_cron=policy.cron_expression if policy else None,
                    rows_written=latest_run.rows_written if latest_run else None,
                )
            )

        miss_result = await db.execute(
            select(QueryMissLog)
            .where(QueryMissLog.model_id == model_id)
            .order_by(QueryMissLog.occurrence_count.desc())
            .limit(10)
        )
        miss_logs = list(miss_result.scalars().all())
        miss_summary = [
            MissSummaryItem(
                query_fingerprint=ml.query_fingerprint,
                occurrence_count=ml.occurrence_count,
                last_seen_at=ml.last_seen_at.isoformat() if ml.last_seen_at else "",
                grain=list(ml.requested_grain or []),
                measures=list(ml.requested_measures or []),
            )
            for ml in miss_logs
        ]

        pockets_result = await db.execute(
            select(PocketDefinition)
            .where(PocketDefinition.model_id == model_id)
            .order_by(PocketDefinition.hit_count.desc(), PocketDefinition.last_access_at.desc().nullslast())
        )
        pockets = list(pockets_result.scalars().all())

        pocket_hit_total = sum(int(p.hit_count or 0) for p in pockets)
        pocket_time_saved_ms = sum(int(p.time_saved_ms_total or 0) for p in pockets)
        pocket_storage_bytes = sum(int(p.storage_bytes or 0) for p in pockets)
        day_ago = now - timedelta(hours=24)
        pocket_evictions_24h = sum(
            1
            for p in pockets
            if p.retired_at is not None
            and (p.retired_at if p.retired_at.tzinfo else p.retired_at.replace(tzinfo=timezone.utc)) >= day_ago
        )
        pocket_hit_rate = pocket_hits / total_queries if total_queries else 0.0

        top_pockets = [
            PocketSummaryItem(
                pocket_id=str(p.id),
                physical_table_name=p.physical_table_name,
                status=p.status,
                hit_count=int(p.hit_count or 0),
                ttl_days=int(p.ttl_days or 0),
                last_refresh_at=p.last_refresh_at.isoformat() if p.last_refresh_at else None,
                last_access_at=p.last_access_at.isoformat() if p.last_access_at else None,
            )
            for p in pockets[:10]
        ]

        return ModelMetrics(
            model_id=str(model_id),
            window_hours=window_hours,
            total_queries=total_queries,
            aggregate_hits=aggregate_hits,
            pocket_hits=pocket_hits,
            source_hits=source_hits,
            hit_rate=round(hit_rate, 4),
            bytes_avoided=bytes_avoided,
            hourly_volume=hourly_volume,
            refresh_health=refresh_health,
            miss_summary=miss_summary,
            pocket_hit_rate=round(pocket_hit_rate, 4),
            pocket_time_saved_ms=pocket_time_saved_ms,
            pocket_storage_bytes=pocket_storage_bytes,
            pocket_evictions_24h=pocket_evictions_24h,
            top_pockets=top_pockets,
        )


async def _build_hourly_volume(
    db, base_filters: tuple, since: datetime, now: datetime
) -> list[HourlyVolume]:
    """Aggregate per-hour route-type counts in SQL (``date_trunc('hour', ...)``
    GROUP BY) so the window's rows are never loaded into memory (F-030-12).

    The window is already bounded by ``window_hours <= 720`` on the route, so the
    returned histogram is at most 720 rows. Empty hours within the window are
    backfilled to zero so the bar strip renders a continuous timeline.
    """
    hour_col = func.date_trunc("hour", QueryLog.created_at)
    stmt = (
        select(
            hour_col.label("hour"),
            func.count().label("total"),
            func.coalesce(
                func.sum(case((QueryLog.route_type == "aggregate", 1), else_=0)), 0
            ).label("aggregate_hits"),
            func.coalesce(
                func.sum(case((QueryLog.route_type == "pocket", 1), else_=0)), 0
            ).label("pocket_hits"),
        )
        .where(*base_filters)
        .group_by(hour_col)
        .order_by(hour_col)
    )
    rows = (await db.execute(stmt)).all()

    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        ts = row.hour
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        key = ts.strftime("%Y-%m-%dT%H:00:00Z")
        total = int(row.total or 0)
        agg = int(row.aggregate_hits or 0)
        pocket = int(row.pocket_hits or 0)
        counts[key] = {
            "total": total,
            "aggregate_hits": agg,
            "pocket_hits": pocket,
            "source_hits": total - agg - pocket,
        }

    out: list[HourlyVolume] = []
    cursor = since.replace(minute=0, second=0, microsecond=0)
    while cursor <= now:
        key = cursor.strftime("%Y-%m-%dT%H:00:00Z")
        vals = counts.get(
            key, {"total": 0, "aggregate_hits": 0, "pocket_hits": 0, "source_hits": 0}
        )
        out.append(
            HourlyVolume(
                hour=key,
                total=vals["total"],
                aggregate_hits=vals["aggregate_hits"],
                pocket_hits=vals["pocket_hits"],
                source_hits=vals["source_hits"],
            )
        )
        cursor += timedelta(hours=1)

    return out


async def _calculate_bytes_avoided(db, model_id: UUID, since: datetime) -> int:
    """Estimate bytes avoided from source-route baselines by query fingerprint.

    QueryLog.bytes_processed is the actual route's scanned bytes. To report a
    savings number, compare each accelerated fingerprint against the average
    source-route scan for that same fingerprint and clamp negative deltas to
    zero. Fingerprints without a source baseline contribute no avoided bytes.
    """
    source_stmt = (
        select(
            QueryLog.query_fingerprint.label("query_fingerprint"),
            func.avg(QueryLog.bytes_processed).label("source_bytes"),
        )
        .where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.status == "success",
            QueryLog.route_type == "source",
            QueryLog.bytes_processed.is_not(None),
        )
        .group_by(QueryLog.query_fingerprint)
    )
    source_rows = (await db.execute(source_stmt)).all()
    source_baseline = {
        row.query_fingerprint: float(row.source_bytes or 0)
        for row in source_rows
        if row.query_fingerprint
    }
    if not source_baseline:
        return 0

    accelerated_stmt = (
        select(
            QueryLog.query_fingerprint.label("query_fingerprint"),
            func.count().label("accelerated_count"),
            func.coalesce(func.sum(QueryLog.bytes_processed), 0).label("accelerated_bytes"),
        )
        .where(
            QueryLog.model_id == model_id,
            QueryLog.created_at >= since,
            QueryLog.status == "success",
            QueryLog.route_type.in_(("aggregate", "pocket")),
            QueryLog.bytes_processed.is_not(None),
        )
        .group_by(QueryLog.query_fingerprint)
    )
    accelerated_rows = (await db.execute(accelerated_stmt)).all()

    avoided = 0.0
    for row in accelerated_rows:
        baseline = source_baseline.get(row.query_fingerprint)
        if baseline is None:
            continue
        avoided += max((baseline * int(row.accelerated_count or 0)) - float(row.accelerated_bytes or 0), 0)
    return int(avoided)
