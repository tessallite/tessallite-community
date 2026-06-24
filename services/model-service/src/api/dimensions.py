"""
Dimension CRUD routes.

Role requirements:
  GET (list / get) → viewer+
  POST / PATCH     → modeler+
  DELETE           → modeler+
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pydantic import BaseModel

from shared.db.models import (
    AggregateDefinition,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    Persona,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DimensionCreate,
    DimensionResponse,
    DimensionUpdate,
    RedundantPartnerInfo,
)
from shared.semantic.redundant_partner import compute_redundant_partners
from src.auth.middleware import CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import require_role
from src.api._column_helpers import resolve_column
from src.api._persona_scope import parse_allowed_ids, resolve_effective_persona
from src.api._scope import (
    ensure_model_in_project,
    glossary_text_for_target as _glossary_text_for_target,
    glossary_texts_for_targets as _glossary_texts_for_targets,
    purge_entity_soft_references,
)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/dimensions", tags=["dimensions"]
)

# Model-level router for cross-attribute operations (different prefix).
bulk_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}", tags=["dimensions"]
)


async def _build_response(
    db,
    dim: Dimension,
    redundant_partners: dict | None = None,
    warnings: list[str] | None = None,
    glossary_texts: dict[UUID, str] | None = None,
) -> DimensionResponse:
    """Build a DimensionResponse enriched with column name and table id.

    Cascades the source column's `is_hidden` flag onto the dimension itself
    so the gateway can filter the catalog without a second round trip
    (Phase 1 of the semantic-layer plan).

    Resolves `effective_description` from the glossary precedence chain
    `approved glossary entry > dimension.description > none` so the gateway
    can surface curated glossary text in Excel tooltips automatically
    (Phase 4 of the semantic-layer plan).

    Attaches a ``redundant_partner`` hint when the source column is on the
    dim side of an inner/left join to the fact table. The aggregate
    picker uses this to disable the entry and suggest the fact-side
    canonical column.
    """
    col_name = None
    col_data_type = None
    table_id = None
    table_alias = None
    table_display_name = None
    uda_name = None
    is_hidden = False
    high_cardinality: bool | None = None
    cardinality_estimate: int | None = None
    if dim.source_column_id:
        col = await db.get(ModelColumn, dim.source_column_id)
        if col:
            col_name = col.column_name
            col_data_type = col.data_type
            table_id = col.model_table_id
            is_hidden = bool(col.is_hidden)
            cardinality_estimate = col.cardinality_estimate
    if dim.user_defined_attribute_id:
        uda = await db.get(UserDefinedAttribute, dim.user_defined_attribute_id)
        if uda:
            uda_name = uda.name
            table_id = uda.table_id
    if table_id is not None:
        mt = await db.get(ModelTable, table_id)
        if mt:
            table_alias = mt.alias
            table_display_name = mt.display_name
            if cardinality_estimate is not None and mt.row_count_estimate and mt.row_count_estimate > 0:
                high_cardinality = (cardinality_estimate / mt.row_count_estimate) > 0.5

    if glossary_texts is not None:
        glossary_text = glossary_texts.get(dim.id)
    else:
        glossary_text = await _glossary_text_for_target(db, dim.model_id, "dimension", dim.id)
    effective_description = glossary_text or dim.description

    partner_info = None
    if (
        redundant_partners is not None
        and dim.source_column_id is not None
        and dim.source_column_id in redundant_partners
    ):
        hint = redundant_partners[dim.source_column_id]
        partner_info = RedundantPartnerInfo(
            partner_column_name=hint.partner_column_name,
            partner_table_name=hint.partner_table_name,
            partner_physical_table=hint.partner_physical_table,
            join_type=hint.join_type,
            reason=hint.reason,
        )

    return DimensionResponse(
        id=dim.id,
        model_id=dim.model_id,
        name=dim.name,
        display_name=dim.display_name,
        description=dim.description,
        effective_description=effective_description,
        display_folder=dim.display_folder,
        is_hidden=is_hidden,
        source_column_id=dim.source_column_id,
        source_column_name=col_name,
        data_type=col_data_type,
        source_table_id=table_id,
        source_table_alias=table_alias,
        source_table_display_name=table_display_name,
        user_defined_attribute_id=dim.user_defined_attribute_id,
        user_defined_attribute_name=uda_name,
        is_time_dim=dim.is_time_dim,
        time_grain=dim.time_grain,
        is_invalid=bool(getattr(dim, "is_invalid", False)),
        invalid_reason=getattr(dim, "invalid_reason", None),
        redundant_partner=partner_info,
        high_cardinality=high_cardinality,
        warnings=warnings or [],
        created_at=dim.created_at,
        updated_at=dim.updated_at,
    )


async def _load_redundant_partners(db, model_id: UUID) -> dict:
    """Load model joins / tables / columns and compute the partner map."""
    tables_result = await db.execute(
        select(ModelTable).where(ModelTable.model_id == model_id)
    )
    tables = {t.id: t for t in tables_result.scalars().all()}
    joins_result = await db.execute(
        select(Join).where(Join.model_id == model_id)
    )
    joins = list(joins_result.scalars().all())
    col_ids: set = set()
    for j in joins:
        col_ids.add(j.left_column_id)
        col_ids.add(j.right_column_id)
    columns: dict = {}
    if col_ids:
        cols_result = await db.execute(
            select(ModelColumn).where(ModelColumn.id.in_(list(col_ids)))
        )
        columns = {c.id: c for c in cols_result.scalars().all()}
    return compute_redundant_partners(joins, tables, columns)


# F-018-20: `_glossary_text_for_target` was copy-pasted here and in measures.py.
# It now lives in `_scope.py` and is imported above (aliased to the same name).


@router.post(
    "",
    response_model=DimensionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_dimension(
    project_id: UUID,
    model_id: UUID,
    body: DimensionCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DimensionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        if body.user_defined_attribute_id and (body.source_table_id or body.source_column_name):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        source_column_id = None
        user_defined_attribute_id = body.user_defined_attribute_id
        if body.source_table_id and body.source_column_name:
            col = await resolve_column(db, body.source_table_id, body.source_column_name, body.data_type or "unknown")
            source_column_id = col.id
        elif user_defined_attribute_id:
            uda = await db.get(UserDefinedAttribute, user_defined_attribute_id)
            if uda is None or uda.model_id != model_id:
                raise HTTPException(status_code=404, detail="User-defined attribute not found")

        dim = Dimension(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name or body.name,
            source_column_id=source_column_id,
            user_defined_attribute_id=user_defined_attribute_id,
            is_time_dim=body.is_time_dim,
            time_grain=body.time_grain,
        )
        db.add(dim)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if "dimensions_model_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"A dimension named '{body.name}' already exists in this model.",
                )
            raise
        await db.refresh(dim)
        return await _build_response(db, dim)


@router.get("", response_model=list[DimensionResponse])
async def list_dimensions(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[DimensionResponse]:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        stmt = select(Dimension).where(Dimension.model_id == model_id)
        if persona:
            allowed = parse_allowed_ids(persona.included_dimension_ids)
            if allowed is not None:
                stmt = stmt.where(Dimension.id.in_(allowed))
        stmt = stmt.order_by(Dimension.name)
        partners = await _load_redundant_partners(db, model_id)
        result = await db.execute(stmt)
        dims = result.scalars().all()
        glossary_texts = await _glossary_texts_for_targets(
            db, model_id, "dimension", [d.id for d in dims]
        )
        return [
            await _build_response(db, d, partners, glossary_texts=glossary_texts)
            for d in dims
        ]


@router.get("/{dimension_id}", response_model=DimensionResponse)
async def get_dimension(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DimensionResponse:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        d = await db.get(Dimension, dimension_id)
        if d is None or d.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        if persona:
            allowed = parse_allowed_ids(persona.included_dimension_ids)
            if allowed is not None and d.id not in allowed:
                raise HTTPException(status_code=404, detail="Dimension not found")
        partners = await _load_redundant_partners(db, model_id)
        return await _build_response(db, d, partners)


@router.patch(
    "/{dimension_id}",
    response_model=DimensionResponse,
    dependencies=[require_role("modeler")],
)
async def update_dimension(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    body: DimensionUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DimensionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        d = await db.get(Dimension, dimension_id)
        if d is None or d.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        updates = body.model_dump(exclude_unset=True)
        if "user_defined_attribute_id" in updates and (
            "source_table_id" in updates or "source_column_name" in updates
        ):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        # Resolve column name if provided
        if "source_table_id" in updates and "source_column_name" in updates:
            table_id = updates.pop("source_table_id")
            col_name = updates.pop("source_column_name")
            if table_id and col_name:
                col = await resolve_column(db, table_id, col_name)
                d.source_column_id = col.id
                d.user_defined_attribute_id = None
            else:
                d.source_column_id = None
        else:
            updates.pop("source_table_id", None)
            updates.pop("source_column_name", None)
        if "user_defined_attribute_id" in updates:
            uda_id = updates["user_defined_attribute_id"]
            if uda_id:
                uda = await db.get(UserDefinedAttribute, uda_id)
                if uda is None or uda.model_id != model_id:
                    raise HTTPException(status_code=404, detail="User-defined attribute not found")
                d.source_column_id = None
        new_name = updates.get("name")
        old_name = d.name
        if new_name and "display_name" not in updates and (not d.display_name or d.display_name == d.name):
            updates["display_name"] = new_name
        if "display_name" in updates and (updates["display_name"] is None or not str(updates["display_name"]).strip()):
            updates["display_name"] = new_name or d.name
        for k, v in updates.items():
            setattr(d, k, v)
        if new_name and new_name != old_name:
            agg_result = await db.execute(
                select(AggregateDefinition).where(
                    AggregateDefinition.model_id == model_id
                )
            )
            for agg in agg_result.scalars().all():
                if isinstance(agg.grain, list) and old_name in agg.grain:
                    agg.grain = [
                        new_name if g == old_name else g for g in agg.grain
                    ]
                    # Physical columns in the materialized table still use
                    # the old name — mark aggregate pending so it is rebuilt
                    # before routing uses the new layout.
                    if agg.status == "active":
                        agg.status = "pending"
            persona_result = await db.execute(
                select(Persona).where(Persona.model_id == model_id)
            )
            for persona in persona_result.scalars().all():
                df = persona.default_filters
                if isinstance(df, dict) and old_name in df:
                    df[new_name] = df.pop(old_name)
                    persona.default_filters = df
        rename_warnings: list[str] = []
        if new_name and new_name != old_name:
            hlevel_result = await db.execute(
                select(HierarchyLevel.name, HierarchyDefinition.name)
                .join(HierarchyDefinition, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
                .where(HierarchyDefinition.model_id == model_id)
                .where(HierarchyLevel.name == old_name)
            )
            for lvl_name, hier_name in hlevel_result.all():
                rename_warnings.append(
                    f"Hierarchy level '{lvl_name}' on hierarchy "
                    f"'{hier_name}' still uses the old name"
                )
        # If the source binding changed, re-run the model validator so
        # the dim and any aggregates that reference it flip between
        # invalid/valid based on the new structural state.
        if (
            "source_table_id" in body.model_dump(exclude_unset=True)
            or "source_column_name" in body.model_dump(exclude_unset=True)
            or "user_defined_attribute_id" in body.model_dump(exclude_unset=True)
        ):
            from shared.semantic.model_validator import revalidate_model

            await db.flush()
            await revalidate_model(model_id, db)
        await db.commit()
        await db.refresh(d)
        return await _build_response(db, d, warnings=rename_warnings)


@router.delete(
    "/{dimension_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_dimension(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    from shared.semantic.model_validator import revalidate_model
    from src.api.personas import strip_id_from_personas

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        d = await db.get(Dimension, dimension_id)
        if d is None or d.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        await strip_id_from_personas(
            db, model_id=model_id, object_id=dimension_id, object_class="dimension"
        )
        # Soft-referencing translation/preference rows have no FK back to the
        # dimension and would otherwise linger forever (F-029-15).
        await purge_entity_soft_references(db, model_id=model_id, entity_id=dimension_id)
        await db.delete(d)
        await db.flush()
        await revalidate_model(model_id, db)
        await db.commit()


# ---------------------------------------------------------------------------
# Bulk rename — model-level endpoint, shared between dimensions and measures
# ---------------------------------------------------------------------------

def _auto_display_name(name: str) -> str:
    """Auto-generate display_name from a snake_case identifier."""
    return name.replace("_", " ").title()


class BulkRenameItem(BaseModel):
    type: str    # "dimension" | "measure"
    id: UUID
    name: str


class BulkRenameRequest(BaseModel):
    renames: list[BulkRenameItem]


class BulkRenameResult(BaseModel):
    renamed: int


@bulk_router.post(
    "/bulk-rename-attributes",
    response_model=BulkRenameResult,
    dependencies=[require_role("modeler")],
)
async def bulk_rename_attributes(
    project_id: UUID,
    model_id: UUID,
    body: BulkRenameRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> BulkRenameResult:
    """Atomically rename a batch of dimensions and/or measures.

    Validates model-wide uniqueness before committing.  Preserves
    display_name when it was manually customised (i.e. differs from
    the auto-generated prettification of the current name).
    """
    if not body.renames:
        return BulkRenameResult(renamed=0)

    # --- basic validation ---
    incoming_names = [r.name.strip() for r in body.renames]
    for n in incoming_names:
        if not n:
            raise HTTPException(status_code=422, detail="Rename name must not be empty.")
        if len(n) > 255:
            raise HTTPException(status_code=422, detail=f"Name too long: {n!r}")

    if len(set(n.lower() for n in incoming_names)) != len(incoming_names):
        raise HTTPException(
            status_code=422, detail="Duplicate names within the request."
        )

    dim_ids = {r.id for r in body.renames if r.type == "dimension"}
    meas_ids = {r.id for r in body.renames if r.type == "measure"}

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)

        # Build the set of names already taken by attrs NOT in this batch.
        existing_dim_names = set(
            (
                await db.execute(
                    select(Dimension.name).where(
                        Dimension.model_id == model_id,
                        Dimension.id.not_in(dim_ids) if dim_ids else True,
                    )
                )
            ).scalars().all()
        )
        existing_meas_names = set(
            (
                await db.execute(
                    select(Measure.name).where(
                        Measure.model_id == model_id,
                        Measure.id.not_in(meas_ids) if meas_ids else True,
                    )
                )
            ).scalars().all()
        )
        taken = {n.lower() for n in existing_dim_names | existing_meas_names}

        for n in incoming_names:
            if n.lower() in taken:
                raise HTTPException(
                    status_code=409,
                    detail=f"An attribute named '{n}' already exists in this model.",
                )
            taken.add(n.lower())  # reserve within the batch

        # Apply renames.
        rename_map = {r.id: r.name.strip() for r in body.renames}
        renamed = 0

        for dim_id in dim_ids:
            d = await db.get(Dimension, dim_id)
            if d is None or d.model_id != model_id:
                raise HTTPException(status_code=404, detail=f"Dimension {dim_id} not found.")
            new_name = rename_map[dim_id]
            if _auto_display_name(d.name) == (d.display_name or ""):
                d.display_name = _auto_display_name(new_name)
            d.name = new_name
            renamed += 1

        for meas_id in meas_ids:
            m = await db.get(Measure, meas_id)
            if m is None or m.model_id != model_id:
                raise HTTPException(status_code=404, detail=f"Measure {meas_id} not found.")
            new_name = rename_map[meas_id]
            if _auto_display_name(m.name) == (m.display_name or ""):
                m.display_name = _auto_display_name(new_name)
            m.name = new_name
            renamed += 1

        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if "model_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail="One or more names conflict with existing attributes.",
                )
            raise

        await db.commit()
        return BulkRenameResult(renamed=renamed)
