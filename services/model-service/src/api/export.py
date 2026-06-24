"""
Model export route — returns full model definition as JSON.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from shared.db.models import (
    AggregateDefinition,
    DataSource,
    DataTarget,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Join,
    ModelColumn,
    ModelTable,
    Measure,
    Model,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from datetime import datetime, timezone

from shared.schemas.pydantic_models import (
    AggregateDefinitionResponse,
    DataSourceResponse,
    DataTargetResponse,
    DimensionResponse,
    HierarchyExportResponse,
    HierarchyLevelAttributeExportResponse,
    HierarchyLevelExportResponse,
    JoinResponse,
    MeasureResponse,
    ModelExportResponse,
    ModelImportRequest,
    ModelImportResponse,
    ModelResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}", tags=["export"]
)


@router.get("/export", response_model=ModelExportResponse)
async def export_model(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelExportResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")

        sources = (
            await db.execute(select(DataSource).where(DataSource.model_id == model_id))
        ).scalars().all()

        targets = (
            await db.execute(select(DataTarget).where(DataTarget.model_id == model_id))
        ).scalars().all()

        dimensions = (
            await db.execute(
                select(Dimension).where(Dimension.model_id == model_id)
            )
        ).scalars().all()

        measures = (
            await db.execute(select(Measure).where(Measure.model_id == model_id))
        ).scalars().all()

        joins = (
            await db.execute(select(Join).where(Join.model_id == model_id))
        ).scalars().all()

        aggregates = (
            await db.execute(
                select(AggregateDefinition).where(
                    AggregateDefinition.model_id == model_id
                )
            )
        ).scalars().all()

        hierarchy_defs = (
            await db.execute(
                select(HierarchyDefinition)
                .where(HierarchyDefinition.model_id == model_id)
                .order_by(HierarchyDefinition.name)
            )
        ).scalars().all()

        hierarchy_items: list[HierarchyExportResponse] = []
        for hierarchy in hierarchy_defs:
            levels = (
                await db.execute(
                    select(HierarchyLevel)
                    .where(HierarchyLevel.hierarchy_id == hierarchy.id)
                    .order_by(HierarchyLevel.ordinal)
                )
            ).scalars().all()

            level_items: list[HierarchyLevelExportResponse] = []
            for level in levels:
                level_attrs = (
                    await db.execute(
                        select(HierarchyLevelAttribute)
                        .where(HierarchyLevelAttribute.level_id == level.id)
                        .order_by(HierarchyLevelAttribute.id)
                    )
                ).scalars().all()

                level_items.append(
                    HierarchyLevelExportResponse(
                        id=level.id,
                        hierarchy_id=level.hierarchy_id,
                        name=level.name,
                        ordinal=level.ordinal,
                        key_attribute_id=level.key_attribute_id,
                        key_attribute_source=level.key_attribute_source,
                        description=level.description,
                        time_unit=level.time_unit,
                        allowed_time_calcs=list(level.allowed_time_calcs or []),
                        created_at=level.created_at,
                        updated_at=level.updated_at,
                        attributes=[
                            HierarchyLevelAttributeExportResponse(
                                id=attr.id,
                                attribute_id=attr.attribute_id,
                                attribute_source=attr.attribute_source,
                                role=attr.role,
                            )
                            for attr in level_attrs
                        ],
                    )
                )

            hierarchy_items.append(
                HierarchyExportResponse(
                    id=hierarchy.id,
                    model_id=hierarchy.model_id,
                    name=hierarchy.name,
                    type=hierarchy.type,
                    dimension_kind=hierarchy.dimension_kind,
                    description=hierarchy.description,
                    segment_config=hierarchy.segment_config,
                    date_config=hierarchy.date_config,
                    created_at=hierarchy.created_at,
                    updated_at=hierarchy.updated_at,
                    levels=level_items,
                )
            )

        return ModelExportResponse(
            exported_at=datetime.now(timezone.utc),
            model=ModelResponse.model_validate(model),
            sources=[DataSourceResponse.model_validate(s) for s in sources],
            targets=[DataTargetResponse.model_validate(t) for t in targets],
            dimensions=[DimensionResponse.model_validate(d) for d in dimensions],
            measures=[MeasureResponse.model_validate(m) for m in measures],
            joins=[JoinResponse.model_validate(j) for j in joins],
            aggregates=[AggregateDefinitionResponse.model_validate(a) for a in aggregates],
            hierarchies=hierarchy_items,
        )


async def _attribute_in_model(
    db,
    *,
    model_id: UUID,
    attribute_id: UUID,
    source: str,
) -> bool:
    if source == "physical_column":
        col = await db.get(ModelColumn, attribute_id)
        if col is None:
            return False
        table = await db.get(ModelTable, col.model_table_id)
        return table is not None and table.model_id == model_id
    if source == "user_defined_attribute":
        uda = await db.get(UserDefinedAttribute, attribute_id)
        return uda is not None and uda.model_id == model_id
    return False


@router.post(
    "/import",
    response_model=ModelImportResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("modeler")],
)
async def import_model_hierarchies(
    project_id: UUID,
    model_id: UUID,
    body: ModelImportRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelImportResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")

        warnings: list[str] = []
        imported_hierarchies = 0
        imported_levels = 0
        if body.replace_hierarchies:
            existing_hierarchies = (
                await db.execute(
                    select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id)
                )
            ).scalars().all()
            for existing in existing_hierarchies:
                await db.delete(existing)
            await db.flush()

        try:
            for hierarchy in body.hierarchies:
                row = HierarchyDefinition(
                    model_id=model_id,
                    name=hierarchy.name,
                    type=hierarchy.type,
                    dimension_kind=getattr(hierarchy, "dimension_kind", None),
                    description=hierarchy.description,
                    segment_config=hierarchy.segment_config,
                    date_config=hierarchy.date_config,
                )
                db.add(row)
                await db.flush()
                imported_hierarchies += 1

                for level in sorted(hierarchy.levels, key=lambda item: item.ordinal):
                    key_ok = await _attribute_in_model(
                        db,
                        model_id=model_id,
                        attribute_id=level.key_attribute_id,
                        source=level.key_attribute_source,
                    )
                    if not key_ok:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Hierarchy '{hierarchy.name}' level '{level.name}' references "
                                "a key attribute that does not exist in this model"
                            ),
                        )

                    level_row = HierarchyLevel(
                        hierarchy_id=row.id,
                        name=level.name,
                        ordinal=level.ordinal,
                        key_attribute_id=level.key_attribute_id,
                        key_attribute_source=level.key_attribute_source,
                        description=level.description,
                        time_unit=getattr(level, "time_unit", None),
                        allowed_time_calcs=list(getattr(level, "allowed_time_calcs", []) or []),
                    )
                    db.add(level_row)
                    await db.flush()
                    imported_levels += 1

                    for attribute in level.attributes:
                        attr_ok = await _attribute_in_model(
                            db,
                            model_id=model_id,
                            attribute_id=attribute.attribute_id,
                            source=attribute.attribute_source,
                        )
                        if not attr_ok:
                            raise HTTPException(
                                status_code=422,
                                detail=(
                                    f"Hierarchy '{hierarchy.name}' level '{level.name}' references "
                                    "a level attribute that does not exist in this model"
                                ),
                            )
                        db.add(
                            HierarchyLevelAttribute(
                                level_id=level_row.id,
                                attribute_id=attribute.attribute_id,
                                attribute_source=attribute.attribute_source,
                                role=attribute.role,
                            )
                        )

            await db.commit()
        except HTTPException:
            await db.rollback()
            raise
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Hierarchy import failed due to a constraint conflict: {exc.orig}",
            )

        return ModelImportResponse(
            imported_hierarchies=imported_hierarchies,
            imported_levels=imported_levels,
            warnings=warnings,
        )
