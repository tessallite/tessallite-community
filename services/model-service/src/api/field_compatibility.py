"""Field compatibility contract endpoint."""
from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    Dimension,
    Join,
    Measure,
    ModelVersion,
    ModelColumn,
    ModelTable,
    PersonaTagRestriction,
    UserDefinedAttribute,
    data_tag_columns,
)
from shared.db.session import get_tenant_db
from shared.semantic.field_compatibility import (
    FieldAccessPolicy,
    evaluate_field_compatibility,
)
from src.api._persona_scope import parse_allowed_ids, resolve_effective_persona
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import require_role


@dataclass(frozen=True)
class _SemanticRows:
    tables: list[Any]
    columns: list[Any]
    joins: list[Any]
    dimensions: list[Any]
    measures: list[Any]
    user_defined_attributes: list[Any]
    aggregate_definitions: list[Any]
    aggregate_columns: list[Any]


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["field-compatibility"],
)


@router.get("/field-compatibility")
async def get_field_compatibility(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    include_hidden: bool = Query(default=False),
    measure_ids: list[str] | None = Query(default=None),
    dimension_ids: list[str] | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> dict:
    """Return the semantic measure/dimension compatibility matrix.

    The route enforces the same model viewer and persona gates used by the
    dimension and measure metadata routes before suggestions are formatted.
    """
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    selected_measure_ids = _parse_query_ids(measure_ids, "measure_ids")
    selected_dimension_ids = _parse_query_ids(dimension_ids, "dimension_ids")

    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id
        )
        persona = await resolve_effective_persona(
            db,
            current_user=current_user,
            model_id=model_id,
            requested_persona_id=persona_id,
        )
        effective_include_hidden = bool(
            include_hidden
            and persona is not None
            and getattr(persona, "includes_hidden_columns", False)
        )
        allowed_measure_list = (
            parse_allowed_ids(persona.included_measure_ids) if persona else None
        )
        allowed_dimension_list = (
            parse_allowed_ids(persona.included_dimension_ids) if persona else None
        )
        allowed_measure_ids = (
            set(allowed_measure_list) if allowed_measure_list is not None else None
        )
        allowed_dimension_ids = (
            set(allowed_dimension_list) if allowed_dimension_list is not None else None
        )
        restricted_column_ids = (
            await _restricted_column_ids(db, persona.id) if persona else set()
        )

        semantic_rows = await _load_semantic_rows(
            db, model_id, model.deployed_version_id
        )

        result = evaluate_field_compatibility(
            model_id=model_id,
            version_id=getattr(model, "deployed_version_id", None),
            measures=semantic_rows.measures,
            dimensions=semantic_rows.dimensions,
            tables=semantic_rows.tables,
            columns=semantic_rows.columns,
            joins=semantic_rows.joins,
            user_defined_attributes=semantic_rows.user_defined_attributes,
            aggregate_definitions=semantic_rows.aggregate_definitions,
            aggregate_columns=semantic_rows.aggregate_columns,
            policy=FieldAccessPolicy(
                allowed_measure_ids=allowed_measure_ids,
                allowed_dimension_ids=allowed_dimension_ids,
                restricted_column_ids=restricted_column_ids,
                include_hidden=effective_include_hidden,
                persona_scoped=persona is not None,
            ),
            selected_measure_ids=selected_measure_ids,
            selected_dimension_ids=selected_dimension_ids,
        )
        return result.to_dict()

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Tenant database session did not yield",
    )


async def _restricted_column_ids(db, persona_id: UUID) -> set[UUID]:
    rows = await db.execute(
        select(data_tag_columns.c.model_column_id)
        .join(
            PersonaTagRestriction,
            PersonaTagRestriction.data_tag_id == data_tag_columns.c.tag_id,
        )
        .where(PersonaTagRestriction.persona_id == persona_id)
    )
    return set(rows.scalars().all())


async def _load_semantic_rows(
    db,
    model_id: UUID,
    deployed_version_id: UUID | None,
) -> _SemanticRows:
    if deployed_version_id is not None:
        version = await db.get(ModelVersion, deployed_version_id)
        if version is None or version.model_id != model_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The deployed model version could not be found.",
            )
        return _semantic_rows_from_snapshot(version.snapshot_json or {})

    tables = await _load_scalars(
        db, select(ModelTable).where(ModelTable.model_id == model_id)
    )
    columns = await _load_scalars(
        db,
        select(ModelColumn).where(
            ModelColumn.model_table_id.in_([table.id for table in tables])
        ),
    )
    joins = await _load_scalars(db, select(Join).where(Join.model_id == model_id))
    dimensions = await _load_scalars(
        db, select(Dimension).where(Dimension.model_id == model_id)
    )
    measures = await _load_scalars(
        db, select(Measure).where(Measure.model_id == model_id)
    )
    udas = await _load_scalars(
        db,
        select(UserDefinedAttribute).where(UserDefinedAttribute.model_id == model_id),
    )
    aggregate_definitions = await _load_scalars(
        db,
        select(AggregateDefinition).where(
            AggregateDefinition.model_id == model_id
        ),
    )
    aggregate_columns = await _load_aggregate_columns(
        db, [aggregate.id for aggregate in aggregate_definitions]
    )
    return _SemanticRows(
        tables=tables,
        columns=columns,
        joins=joins,
        dimensions=dimensions,
        measures=measures,
        user_defined_attributes=udas,
        aggregate_definitions=aggregate_definitions,
        aggregate_columns=aggregate_columns,
    )


def _semantic_rows_from_snapshot(snapshot: dict[str, Any]) -> _SemanticRows:
    aggregate_definitions: list[Any] = []
    aggregate_columns: list[Any] = []
    for aggregate in snapshot.get("aggregates", []) or []:
        if not isinstance(aggregate, dict):
            continue
        aggregate_definitions.append(
            _row_obj(aggregate, exclude={"columns", "refresh_policy"})
        )
        for column in aggregate.get("columns", []) or []:
            if isinstance(column, dict):
                aggregate_columns.append(_row_obj(column))

    return _SemanticRows(
        tables=[_row_obj(row) for row in snapshot.get("tables", []) or []],
        columns=[_row_obj(row) for row in snapshot.get("columns", []) or []],
        joins=[_row_obj(row) for row in snapshot.get("joins", []) or []],
        dimensions=[_row_obj(row) for row in snapshot.get("dimensions", []) or []],
        measures=[_row_obj(row) for row in snapshot.get("measures", []) or []],
        user_defined_attributes=[
            _row_obj(row) for row in snapshot.get("user_defined_attributes", []) or []
        ],
        aggregate_definitions=aggregate_definitions,
        aggregate_columns=aggregate_columns,
    )


def _row_obj(row: dict[str, Any], exclude: set[str] | None = None) -> Any:
    excluded = exclude or set()
    return types.SimpleNamespace(
        **{key: value for key, value in row.items() if key not in excluded}
    )


async def _load_scalars(db, stmt) -> list:
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_aggregate_columns(db, aggregate_ids: list[UUID]) -> list:
    if not aggregate_ids:
        return []
    return await _load_scalars(
        db,
        select(AggregateColumn).where(
            AggregateColumn.aggregate_definition_id.in_(aggregate_ids)
        ),
    )


def _parse_query_ids(values: list[str] | None, param_name: str) -> list[UUID] | None:
    if not values:
        return None
    out: list[UUID] = []
    for value in values:
        for raw in str(value).split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(UUID(raw))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{param_name} must contain UUID values",
                )
    return out
