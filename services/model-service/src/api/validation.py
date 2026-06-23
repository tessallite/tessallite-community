"""
Model validation endpoint.

POST /api/v1/projects/{project_id}/models/{model_id}/validate

Runs structural validators for all dimensions, measures, and aggregates
belonging to the model. Read-only — does not write is_invalid to the DB.
Returns a list of violation dicts and a top-level valid flag.

Auth: modeler or above.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from sqlalchemy.orm import selectinload

from shared.db.models import AggregateDefinition, Dimension, Measure, ModelTable
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import MeasureWarningResponse
from shared.semantic.model_validator import (
    _load_model_structure,
    validate_aggregate,
    validate_dimension,
    validate_measure,
)
from shared.semantic.table_analyzer import validate_measures
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["validation"],
)


class Violation(BaseModel):
    object_type: str
    object_id: str
    object_name: str
    reason: str


class ValidationResult(BaseModel):
    valid: bool
    violations: list[Violation]
    measure_warnings: list[MeasureWarningResponse] = []


@router.post(
    "/validate",
    response_model=ValidationResult,
    dependencies=[require_role("modeler")],
)
async def validate_model(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ValidationResult:
    violations: list[Violation] = []
    all_measure_warnings: list[MeasureWarningResponse] = []

    async for db in get_tenant_db(current_user.tenant_id):
        structure = await _load_model_structure(model_id, db)

        # Validate dimensions
        dims_result = await db.execute(
            select(Dimension).where(Dimension.model_id == model_id)
        )
        for dim in dims_result.scalars().all():
            reason = validate_dimension(dim, structure)
            if reason:
                violations.append(
                    Violation(
                        object_type="dimension",
                        object_id=str(dim.id),
                        object_name=dim.name,
                        reason=reason,
                    )
                )

        # Validate measures
        measures_result = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        all_measures = list(measures_result.scalars().all())
        for measure in all_measures:
            reason = validate_measure(measure, structure)
            if reason:
                violations.append(
                    Violation(
                        object_type="measure",
                        object_id=str(measure.id),
                        object_name=measure.name,
                        reason=reason,
                    )
                )

        # Measure-vs-dimension validation (dual-signal: name + cardinality)
        # Only run on fact tables — dimension tables should not have measures
        tables_result = await db.execute(
            select(ModelTable)
            .where(ModelTable.model_id == model_id, ModelTable.table_type == "fact")
            .options(selectinload(ModelTable.columns))
        )
        fact_tables = {str(t.id): t for t in tables_result.scalars().all()}

        measures_by_table: dict[str, list[str]] = {}
        for m in all_measures:
            tid = str(m.source_table_id) if m.source_table_id else None
            if tid and tid in fact_tables:
                measures_by_table.setdefault(tid, []).append(
                    m.source_column_name or m.name
                )

        for tid, col_names in measures_by_table.items():
            table = fact_tables[tid]
            warnings = validate_measures(table, col_names)
            for w in warnings:
                all_measure_warnings.append(
                    MeasureWarningResponse(
                        column_id=w.column_id,
                        column_name=w.column_name,
                        current_role=w.current_role,
                        suggested_role=w.suggested_role,
                        severity=w.severity,
                        reason=w.reason,
                    )
                )

        # Validate aggregates (async — pass pre-loaded structure to avoid re-querying)
        aggs_result = await db.execute(
            select(AggregateDefinition).where(
                AggregateDefinition.model_id == model_id
            )
        )
        for agg in aggs_result.scalars().all():
            reason = await validate_aggregate(agg, db, structure=structure)
            if reason:
                violations.append(
                    Violation(
                        object_type="aggregate",
                        object_id=str(agg.id),
                        object_name=agg.physical_table_name,
                        reason=reason,
                    )
                )

        return ValidationResult(
            valid=len(violations) == 0,
            violations=violations,
            measure_warnings=all_measure_warnings,
        )
