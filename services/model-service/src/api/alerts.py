"""Model Alerts API — reads and dismisses alerts for the Model Health tab.

Alerts are written by the revalidator, scheduler, and optimiser via
``shared.semantic.model_alerts``. This module exposes the read/dismiss
endpoints and a manual-revalidate trigger for the modeler's
"Re-check model" button on the Model Health tab.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from shared.audit.logger import audit
from shared.db.models import (
    HierarchyHealthIssue,
    LocalUser,
    ModelAlert,
    PocketDefinition,
    SchemaChangeEvent,
)
from shared.db.session import get_tenant_db
from shared.schema_drift.source_check import queue_model_source_check
from shared.schemas.pydantic_models import (
    ModelAlertResponse,
    ModelRevalidationReportResponse,
)
from shared.semantic.model_alerts import (
    count_filtered_alerts,
    count_open_alerts,
    dismiss_alert,
    list_open_alerts,
)
from shared.semantic.model_validator import revalidate_model
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/alerts",
    tags=["alerts"],
)


@router.get("", response_model=list[ModelAlertResponse])
async def list_alerts(
    project_id: UUID,
    model_id: UUID,
    severity: str | None = Query(None),
    category: str | None = Query(None),
    include_resolved: bool = Query(False),
    include_dismissed: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[ModelAlertResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        alerts = await list_open_alerts(
            db,
            model_id=model_id,
            severity=severity,
            category=category,
            include_resolved=include_resolved,
            include_dismissed=include_dismissed,
            limit=limit,
            offset=offset,
        )
        return [ModelAlertResponse.model_validate(a) for a in alerts]


@router.get("/count", response_model=dict)
async def count_alerts(
    project_id: UUID,
    model_id: UUID,
    severity: str | None = Query(None),
    category: str | None = Query(None),
    include_resolved: bool = Query(False),
    include_dismissed: bool = Query(False),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> dict:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # ``total`` always reports the OPEN count for the summary strip, while
        # ``filtered_total`` honours the same filters as the list endpoint so
        # the alerts table can paginate against a real bound (F-030-23).
        total = await count_open_alerts(db, model_id=model_id)
        filtered_total = await count_filtered_alerts(
            db,
            model_id=model_id,
            severity=severity,
            category=category,
            include_resolved=include_resolved,
            include_dismissed=include_dismissed,
        )
        by_severity_result = await db.execute(
            select(ModelAlert.severity, func.count())
            .where(ModelAlert.model_id == model_id)
            .where(ModelAlert.resolved_at.is_(None))
            .where(ModelAlert.dismissed_at.is_(None))
            .group_by(ModelAlert.severity)
        )
        by_severity = {row[0]: int(row[1]) for row in by_severity_result.all()}
        return {
            "total": total,
            "filtered_total": filtered_total,
            "by_severity": by_severity,
        }


@router.post(
    "/{alert_id}/dismiss",
    response_model=ModelAlertResponse,
    dependencies=[require_role("modeler")],
)
async def dismiss_alert_endpoint(
    project_id: UUID,
    model_id: UUID,
    alert_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelAlertResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-8147: ``current_user.user_id`` is the JWT subject — the actor's
        # EMAIL, not a UUID (sub=email). ``ModelAlert.dismissed_by`` is a UUID
        # column, so writing the email there raises at flush. Resolve the acting
        # user's real UUID for the column, and record the durable human identity
        # (the email) on the audit trail via ``actor_email``.
        actor_uuid = (
            await db.execute(
                select(LocalUser.id).where(
                    func.lower(LocalUser.email)
                    == (current_user.email or "").lower()
                )
            )
        ).scalar_one_or_none()
        alert = await dismiss_alert(
            db, alert_id=alert_id, dismissed_by=actor_uuid
        )
        if alert is None or alert.model_id != model_id:
            raise HTTPException(status_code=404, detail="Alert not found")
        await audit(
            db,
            action="model.alert.dismissed",
            severity="info",
            actor_id=actor_uuid,
            actor_email=current_user.email,
            target_type="model_alert",
            target_id=alert.id,
            target_name=getattr(alert, "category", None),
            detail={"model_id": str(model_id), "alert_id": str(alert_id)},
        )
        await db.commit()
        await db.refresh(alert)
        return ModelAlertResponse.model_validate(alert)


# Manual revalidation lives under the model prefix because it's not
# scoped to a specific alert.
revalidate_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["alerts"],
)


@revalidate_router.post(
    "/revalidate",
    response_model=ModelRevalidationReportResponse,
    dependencies=[require_role("modeler")],
)
async def revalidate_model_endpoint(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelRevalidationReportResponse:
    """Re-run the full structural validator on demand.

    Used by the Model Health tab's "Re-check" button so the modeler
    can verify a fix without waiting for the next edit to trigger a
    hook. Alerts are created / resolved as part of the pass via the
    validator's alert bridge.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        report = await revalidate_model(model_id, db)
        # Bug-8484: the manual re-check must actually contact the source, not
        # merely replay the last scheduler scan. Probe every source table
        # through the routed (gateway-only) path; ``live_source_checked`` then
        # reflects whether the live source was really reached.
        live_source_checked = await queue_model_source_check(
            db, model_id=model_id, tenant_id=current_user.tenant_id
        )
        # Bug-8147: record who ran the re-check on the audit trail via
        # ``actor_email`` (the durable human identity), committed with the pass.
        await audit(
            db,
            action="model.revalidated",
            severity="info",
            actor_email=current_user.email,
            target_type="model",
            target_id=model_id,
            detail={
                "live_source_checked": live_source_checked,
                "invalid_dimension_count": len(report.invalid_dimensions),
                "invalid_measure_count": len(report.invalid_measures),
                "invalid_aggregate_count": len(report.invalid_aggregates),
            },
        )
        await db.commit()

        # Bug-8072: compose the health signals already recorded by their
        # owning subsystems. Bug-8484 additionally performs a live source probe
        # above (``queue_model_source_check``); ``live_source_checked`` reports
        # whether the source was actually reached this pass.
        recorded_result = await db.execute(
            select(
                select(func.count())
                .select_from(HierarchyHealthIssue)
                .where(
                    HierarchyHealthIssue.model_id == model_id,
                    HierarchyHealthIssue.resolved_at.is_(None),
                )
                .scalar_subquery(),
                select(func.count())
                .select_from(PocketDefinition)
                .where(
                    PocketDefinition.model_id == model_id,
                    PocketDefinition.status == "failed",
                    PocketDefinition.retired_at.is_(None),
                )
                .scalar_subquery(),
                select(func.count())
                .select_from(SchemaChangeEvent)
                .where(
                    SchemaChangeEvent.model_id == model_id,
                    SchemaChangeEvent.acknowledged_at.is_(None),
                )
                .scalar_subquery(),
                select(func.max(SchemaChangeEvent.detected_at))
                .where(SchemaChangeEvent.model_id == model_id)
                .scalar_subquery(),
            )
        )
        (
            unresolved_hierarchy_issues,
            failed_pockets,
            unacknowledged_schema_drift,
            latest_recorded_schema_drift_at,
        ) = recorded_result.one()

        # Measure-vs-dimension validation (dual-signal)
        from sqlalchemy.orm import selectinload
        from shared.db.models import Measure, ModelColumn, ModelTable
        from shared.schemas.pydantic_models import MeasureWarningResponse
        from shared.semantic.graph_order import FACT_TABLE_TYPE
        from shared.semantic.table_analyzer import validate_measures

        measure_warnings: list[MeasureWarningResponse] = []
        tables_result = await db.execute(
            select(ModelTable)
            .where(ModelTable.model_id == model_id, ModelTable.table_type == FACT_TABLE_TYPE)
            .options(selectinload(ModelTable.columns))
        )
        fact_tables = {str(t.id): t for t in tables_result.scalars().all()}

        measures_result = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        measures = list(measures_result.scalars().all())
        # F-030-04: Measure has NO source_table_id / source_column_name — it
        # carries source_column_id only. Resolve the fact table and the physical
        # column name through ModelColumn so Re-check does not 500 on any real
        # model. A measure with no source column (a calculated measure) or whose
        # column no longer exists is skipped with no warning, not a crash.
        source_col_ids = [m.source_column_id for m in measures if m.source_column_id]
        cols_by_id: dict = {}
        if source_col_ids:
            cols_result = await db.execute(
                select(ModelColumn).where(ModelColumn.id.in_(source_col_ids))
            )
            cols_by_id = {c.id: c for c in cols_result.scalars().all()}
        measures_by_table: dict[str, list[str]] = {}
        for m in measures:
            col = cols_by_id.get(m.source_column_id) if m.source_column_id else None
            if col is None:
                continue
            tid = str(col.model_table_id)
            if tid in fact_tables:
                measures_by_table.setdefault(tid, []).append(col.column_name)
        for tid, col_names in measures_by_table.items():
            for w in validate_measures(fact_tables[tid], col_names):
                measure_warnings.append(MeasureWarningResponse(
                    column_id=w.column_id, column_name=w.column_name,
                    current_role=w.current_role, suggested_role=w.suggested_role,
                    severity=w.severity, reason=w.reason,
                ))

        return ModelRevalidationReportResponse(
            invalid_dimension_count=len(report.invalid_dimensions),
            invalid_measure_count=len(report.invalid_measures),
            invalid_aggregate_count=len(report.invalid_aggregates),
            newly_valid_dimension_count=len(report.newly_valid_dimensions),
            newly_valid_measure_count=len(report.newly_valid_measures),
            newly_valid_aggregate_count=len(report.newly_valid_aggregates),
            unresolved_hierarchy_issue_count=int(unresolved_hierarchy_issues or 0),
            failed_pocket_count=int(failed_pockets or 0),
            unacknowledged_schema_drift_count=int(
                unacknowledged_schema_drift or 0
            ),
            latest_recorded_schema_drift_at=latest_recorded_schema_drift_at,
            live_source_checked=live_source_checked,
            measure_warnings=measure_warnings,
        )
