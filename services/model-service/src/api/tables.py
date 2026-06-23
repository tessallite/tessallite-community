"""
ModelTable CRUD routes — tables belonging to a DataSource.
"""
from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from shared.db.models import (
    CalendarTable,
    DataSource,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    Measure,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    ModelTableCreate,
    ModelTableResponse,
    ModelTableUpdate,
    TableAnalysisResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

# Aliases are emitted into SQL as ``"<alias>"`` and into the catalog as a
# bare identifier; the regex keeps them safe in both places.
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_alias_format(alias: str) -> None:
    if not _ALIAS_RE.match(alias):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Alias {alias!r} is invalid. Must start with a lowercase "
                f"letter and contain only lowercase letters, digits, and "
                f"underscores."
            ),
        )


async def _assert_alias_unique_in_model(
    db, model_id: UUID, alias: str, *, exclude_table_id: UUID | None = None
) -> None:
    stmt = select(func.count()).where(
        ModelTable.model_id == model_id,
        ModelTable.alias == alias,
    )
    if exclude_table_id is not None:
        stmt = stmt.where(ModelTable.id != exclude_table_id)
    result = await db.execute(stmt)
    if (result.scalar() or 0) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Alias {alias!r} is already in use within this model.",
        )

async def _derive_auto_alias(db, model_id: UUID, physical_name: str) -> str:
    """Derive the next auto-generated alias for *physical_name* in this model.

    Returns ``base_name`` when no alias with that prefix exists in the model,
    otherwise ``base_name_2``, ``base_name_3``, etc.  Mirrors the
    ``_next_alias`` pattern in ``calendar.py`` -- queries actual aliases in
    the model (not physical_name counts) so cross-physical-name collisions
    are handled correctly during retry.
    """
    base_name = physical_name.split(".")[-1]  # strip schema prefix
    existing = (
        await db.execute(
            select(ModelTable.alias).where(ModelTable.model_id == model_id)
        )
    ).scalars().all()
    used = set(existing)
    if base_name not in used:
        return base_name
    n = 2
    while f"{base_name}_{n}" in used:
        n += 1
    return f"{base_name}_{n}"


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/sources/{source_id}/tables",
    tags=["tables"],
)


async def _assert_at_most_one_fact(
    db,
    model_id: UUID,
    new_table_type: str,
    exclude_table_id: UUID | None = None,
) -> None:
    """Phase 2 of the semantic-layer plan: a model may have at most one
    fact table. Reject creates and updates that would produce a second.

    The runtime aggregate matcher and rewriter assume one fact per model;
    multi-fact models silently produce wrong results today, so this
    constraint closes a real correctness hole.
    """
    if new_table_type != "fact":
        return
    stmt = select(func.count()).where(
        ModelTable.model_id == model_id,
        ModelTable.table_type == "fact",
    )
    if exclude_table_id is not None:
        stmt = stmt.where(ModelTable.id != exclude_table_id)
    existing = (await db.execute(stmt)).scalar() or 0
    if existing >= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_ONE_FACT_MESSAGE,
        )


_ONE_FACT_MESSAGE = (
    "A model may only contain one fact table. "
    "Tessallite represents one business fact per model; create a "
    "second model in the same project for additional facts."
)


def _is_one_fact_violation(exc: IntegrityError) -> bool:
    """True when an IntegrityError is the one-fact partial unique index
    (F-013-11) tripping on a concurrent create / dim->fact PATCH that raced
    past the check-then-act guard. Matched on the index name so an unrelated
    constraint still surfaces normally."""
    return "uq_model_tables_one_fact_per_model" in str(getattr(exc, "orig", exc))


def _is_alias_violation(exc: IntegrityError) -> bool:
    """True when an IntegrityError is the unique (model_id, alias) constraint
    (Bug-3577, migration 0150) tripping on a concurrent alias collision that
    raced past the app-level ``_assert_alias_unique_in_model`` check."""
    return "uq_model_tables_model_id_alias" in str(getattr(exc, "orig", exc))


_MAX_ALIAS_RETRIES = 3


@router.post(
    "",
    response_model=ModelTableResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_table(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    body: ModelTableCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelTableResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        source = await db.get(DataSource, source_id)
        if source is None or source.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")

        await _assert_at_most_one_fact(db, model_id, body.table_type)

        # Auto-generate alias if not provided.
        # Count existing tables with the same physical_name in this model
        # and append a sequence number (e.g. "customers", "customers_2").
        user_supplied_alias = body.alias
        alias = user_supplied_alias
        if alias:
            _validate_alias_format(alias)
            await _assert_alias_unique_in_model(db, model_id, alias)
        else:
            alias = await _derive_auto_alias(db, model_id, body.physical_name)

        # Bug-5426: wrap the alias INSERT + flush in a SAVEPOINT-protected
        # retry loop so a concurrent duplicate alias (racing past the
        # app-level ``_assert_alias_unique_in_model`` check) triggers a
        # graceful re-derive instead of an HTTP 500.  Mirrors the pattern
        # established in ``calendar.py::_create_calendar_alias``.
        for _attempt in range(_MAX_ALIAS_RETRIES):
            table = ModelTable(
                model_id=model_id,
                source_id=source_id,
                table_type=body.table_type,
                physical_name=body.physical_name,
                alias=alias,
                display_name=body.display_name,
            )
            try:
                async with db.begin_nested():
                    db.add(table)
                    await db.flush()  # assign table.id before cloning sibling columns
                break
            except IntegrityError as exc:
                if _is_one_fact_violation(exc):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=_ONE_FACT_MESSAGE,
                    ) from exc
                if not _is_alias_violation(exc):
                    raise
                if user_supplied_alias:
                    # User-supplied alias collision -- do not retry with a
                    # different one; report the conflict.
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Alias {alias!r} is already in use within this model.",
                    ) from exc
                # Auto-generated alias collision -- re-derive and retry.
                alias = await _derive_auto_alias(db, model_id, body.physical_name)
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Could not allocate a unique table alias after retries.",
            )

        # Bug-106 fix: when this is an alias of an already-modeled physical
        # table, inherit the sibling's column metadata so the new alias is
        # not empty in the canvas, dimension picker, or columns tab.
        # UDAs are intentionally not copied — their expressions reference
        # specific physical columns and need explicit redefinition.
        sibling_stmt = (
            select(ModelTable)
            .where(
                ModelTable.model_id == model_id,
                ModelTable.physical_name == body.physical_name,
                ModelTable.id != table.id,
            )
            .limit(1)
        )
        sibling = (await db.execute(sibling_stmt)).scalar_one_or_none()
        if sibling is not None:
            cols_stmt = select(ModelColumn).where(
                ModelColumn.model_table_id == sibling.id
            )
            for col in (await db.execute(cols_stmt)).scalars().all():
                db.add(
                    ModelColumn(
                        model_table_id=table.id,
                        column_name=col.column_name,
                        display_name=col.display_name,
                        description=col.description,
                        is_hidden=col.is_hidden,
                        is_primary_key=col.is_primary_key,
                        data_type=col.data_type,
                        is_nullable=col.is_nullable,
                        cardinality_estimate=col.cardinality_estimate,
                    )
                )

        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if _is_one_fact_violation(exc):
                # Lost the one-fact race after passing the count guard.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_ONE_FACT_MESSAGE,
                ) from exc
            raise
        await db.refresh(table)
        return ModelTableResponse.model_validate(table)


@router.get("", response_model=list[ModelTableResponse])
async def list_tables(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[ModelTableResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        autocreated_ids = (
            select(CalendarTable.id).where(CalendarTable.autocreated == True)  # noqa: E712
        ).scalar_subquery()
        result = await db.execute(
            select(ModelTable).where(
                ModelTable.source_id == source_id,
                ModelTable.model_id == model_id,
                or_(ModelTable.calendar_table_id.is_(None), ModelTable.calendar_table_id.notin_(autocreated_ids)),
            )
        )
        return [ModelTableResponse.model_validate(t) for t in result.scalars().all()]


@router.get("/{table_id}", response_model=ModelTableResponse)
async def get_table(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelTableResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        t = await db.get(ModelTable, table_id)
        if t is None or t.source_id != source_id or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="ModelTable not found")
        return ModelTableResponse.model_validate(t)


@router.patch(
    "/{table_id}",
    response_model=ModelTableResponse,
    dependencies=[require_role("modeler")],
)
async def update_table(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    body: ModelTableUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelTableResponse:
    from shared.semantic.model_validator import revalidate_model

    async for db in get_tenant_db(current_user.tenant_id):
        t = await db.get(ModelTable, table_id)
        if t is None or t.source_id != source_id or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="ModelTable not found")
        updates = body.model_dump(exclude_unset=True)
        type_changed = (
            "table_type" in updates and updates["table_type"] != t.table_type
        )
        if type_changed:
            await _assert_at_most_one_fact(
                db, model_id, updates["table_type"], exclude_table_id=t.id
            )
        if "alias" in updates and updates["alias"] != t.alias:
            new_alias = updates["alias"]
            _validate_alias_format(new_alias)
            await _assert_alias_unique_in_model(
                db, model_id, new_alias, exclude_table_id=t.id
            )
        old_type = t.table_type
        for k, v in updates.items():
            setattr(t, k, v)
        await db.flush()

        if type_changed:
            if updates["table_type"] == "calendar":
                from src.api.calendar import auto_register_calendar_from_classification
                await auto_register_calendar_from_classification(db, t)
            elif old_type == "calendar":
                t.calendar_table_id = None
                await db.flush()
            await revalidate_model(model_id, db)

        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if _is_one_fact_violation(exc):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_ONE_FACT_MESSAGE,
                ) from exc
            raise
        await db.refresh(t)
        return ModelTableResponse.model_validate(t)


@router.delete(
    "/{table_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_table(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    from shared.semantic.model_validator import revalidate_model

    async for db in get_tenant_db(current_user.tenant_id):
        t = await db.get(ModelTable, table_id)
        if t is None or t.source_id != source_id or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="ModelTable not found")

        col_ids = (
            await db.execute(
                select(ModelColumn.id).where(ModelColumn.model_table_id == table_id)
            )
        ).scalars().all()

        uda_ids = (
            await db.execute(
                select(UserDefinedAttribute.id).where(
                    UserDefinedAttribute.table_id == table_id
                )
            )
        ).scalars().all()

        if col_ids:
            await db.execute(
                delete(Dimension).where(Dimension.source_column_id.in_(col_ids))
            )
            await db.execute(
                delete(Measure).where(Measure.source_column_id.in_(col_ids))
            )

        if uda_ids:
            await db.execute(
                delete(Dimension).where(
                    Dimension.user_defined_attribute_id.in_(uda_ids)
                )
            )
            await db.execute(
                delete(Measure).where(
                    Measure.user_defined_attribute_id.in_(uda_ids)
                )
            )
            orphaned_hierarchy_ids = (
                await db.execute(
                    select(HierarchyLevel.hierarchy_id).where(
                        HierarchyLevel.key_attribute_id.in_(uda_ids),
                        HierarchyLevel.key_attribute_source == "user_defined_attribute",
                    )
                )
            ).scalars().all()
            await db.execute(
                delete(HierarchyLevel).where(
                    HierarchyLevel.key_attribute_id.in_(uda_ids),
                    HierarchyLevel.key_attribute_source == "user_defined_attribute",
                )
            )
            if orphaned_hierarchy_ids:
                for hid in set(orphaned_hierarchy_ids):
                    remaining = (
                        await db.execute(
                            select(func.count()).where(
                                HierarchyLevel.hierarchy_id == hid
                            )
                        )
                    ).scalar()
                    if remaining == 0:
                        await db.execute(
                            delete(HierarchyDefinition).where(
                                HierarchyDefinition.id == hid
                            )
                        )

        await db.delete(t)
        await db.flush()
        await revalidate_model(model_id, db)
        await db.commit()


@router.post(
    "/{table_id}/analyze",
    response_model=TableAnalysisResponse,
    dependencies=[require_role("viewer")],
)
async def analyze_table_endpoint(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> TableAnalysisResponse:
    """Run heuristic auto-analysis on the table and return suggestions."""
    from sqlalchemy.orm import selectinload
    from shared.semantic.table_analyzer import analyze_table

    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(ModelTable)
            .where(ModelTable.id == table_id)
            .options(selectinload(ModelTable.columns))
        )
        t = result.scalar_one_or_none()
        if t is None or t.source_id != source_id or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="ModelTable not found")
        analysis = analyze_table(t)
        return TableAnalysisResponse(
            table_id=analysis.table_id,
            suggested_table_type=analysis.suggested_table_type,
            confidence=analysis.confidence,
            reasoning=analysis.reasoning,
            column_suggestions=[
                {
                    "column_id": s.column_id,
                    "column_name": s.column_name,
                    "suggested_role": s.suggested_role,
                    "reason": s.reason,
                }
                for s in analysis.column_suggestions
            ],
            date_columns=analysis.date_columns,
            potential_calendar_column=analysis.potential_calendar_column,
            measure_warnings=[
                {
                    "column_id": w.column_id,
                    "column_name": w.column_name,
                    "current_role": w.current_role,
                    "suggested_role": w.suggested_role,
                    "severity": w.severity,
                    "reason": w.reason,
                }
                for w in analysis.measure_warnings
            ],
        )


class RenamePreviewItem(BaseModel):
    type: str           # "dimension" | "measure"
    id: str
    source_column_name: str
    current_name: str
    suggested_name: str


@router.get(
    "/{table_id}/rename-preview",
    response_model=list[RenamePreviewItem],
    dependencies=[require_role("modeler")],
)
async def rename_preview(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    new_alias: str,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RenamePreviewItem]:
    """Return attributes whose names would change under *new_alias*.

    Only attributes linked to this table via source_column_id are considered.
    Attributes whose suggested name equals their current name are excluded —
    an empty list means no action is needed.
    """
    from src.api._name_resolver import resolve_unique_name

    _validate_alias_format(new_alias)

    async for db in get_tenant_db(current_user.tenant_id):
        t = await db.get(ModelTable, table_id)
        if t is None or t.source_id != source_id or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="ModelTable not found")

        # Column IDs belonging to this table.
        col_id_rows = (
            await db.execute(
                select(ModelColumn.id, ModelColumn.column_name).where(
                    ModelColumn.model_table_id == table_id
                )
            )
        ).all()
        this_col_ids = {row.id for row in col_id_rows}
        col_name_by_id = {row.id: row.column_name for row in col_id_rows}

        # All dimensions in the model.
        all_dims = (
            await db.execute(
                select(Dimension).where(Dimension.model_id == model_id)
            )
        ).scalars().all()

        # All measures in the model.
        all_meas = (
            await db.execute(
                select(Measure).where(Measure.model_id == model_id)
            )
        ).scalars().all()

        # Build taken set from attrs NOT belonging to this table.
        taken: set[str] = set()
        for d in all_dims:
            if d.source_column_id not in this_col_ids:
                taken.add(d.name.lower())
        for m in all_meas:
            if m.source_column_id not in this_col_ids:
                taken.add(m.name.lower())

        # Strip schema prefix for physical_name comparison (e.g. "public.orders" → "orders").
        physical_base = t.physical_name.split(".")[-1]

        results: list[RenamePreviewItem] = []

        for d in all_dims:
            if d.source_column_id not in this_col_ids:
                continue
            col_name = col_name_by_id.get(d.source_column_id, "")
            suggested = resolve_unique_name(col_name, new_alias, physical_base, taken)
            taken.add(suggested.lower())
            if suggested != d.name:
                results.append(RenamePreviewItem(
                    type="dimension",
                    id=str(d.id),
                    source_column_name=col_name,
                    current_name=d.name,
                    suggested_name=suggested,
                ))

        for m in all_meas:
            if m.source_column_id not in this_col_ids:
                continue
            col_name = col_name_by_id.get(m.source_column_id, "")
            suggested = resolve_unique_name(col_name, new_alias, physical_base, taken)
            taken.add(suggested.lower())
            if suggested != m.name:
                results.append(RenamePreviewItem(
                    type="measure",
                    id=str(m.id),
                    source_column_name=col_name,
                    current_name=m.name,
                    suggested_name=suggested,
                ))

        return results


class ClassificationOverride(BaseModel):
    column_id: str
    role: str  # "measure" | "dimension" | "ignore"


class ApplyClassificationRequest(BaseModel):
    table_type: str | None = None
    overrides: list[ClassificationOverride] = []


class ApplyClassificationResponse(BaseModel):
    dimensions_created: int
    measures_created: int
    table_type_applied: str | None


@router.post(
    "/{table_id}/apply-classification",
    response_model=ApplyClassificationResponse,
    dependencies=[require_role("modeler")],
)
async def apply_classification(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    body: ApplyClassificationRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ApplyClassificationResponse:
    """Apply auto-classification results with user overrides.

    Runs the heuristic analyzer, merges user overrides, then creates
    dimensions and measures for each accepted column.
    """
    from sqlalchemy.orm import selectinload
    from shared.semantic.table_analyzer import analyze_table

    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(ModelTable)
            .where(ModelTable.id == table_id)
            .options(selectinload(ModelTable.columns))
        )
        t = result.scalar_one_or_none()
        if t is None or t.source_id != source_id or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="ModelTable not found")

        analysis = analyze_table(t)
        override_map = {o.column_id: o.role for o in body.overrides}

        role_map: dict[str, str] = {}
        for s in analysis.column_suggestions:
            role_map[s.column_id] = s.suggested_role
        for col_id, role in override_map.items():
            role_map[col_id] = role

        if body.table_type:
            t.table_type = body.table_type

        col_by_id = {str(c.id): c for c in t.columns}

        col_ids = {c.id for c in t.columns}
        existing_dims = {
            r.name for r in (await db.execute(
                select(Dimension.name).where(
                    Dimension.model_id == model_id,
                    Dimension.source_column_id.in_(col_ids),
                )
            )).all()
        }
        existing_meas = {
            r.name for r in (await db.execute(
                select(Measure.name).where(
                    Measure.model_id == model_id,
                    Measure.source_column_id.in_(col_ids),
                )
            )).all()
        }

        dims_created = 0
        meas_created = 0

        for col_id, role in role_map.items():
            col = col_by_id.get(col_id)
            if not col:
                continue

            if role == "dimension" or role == "date_key":
                if col.column_name not in existing_dims:
                    db.add(Dimension(
                        model_id=model_id,
                        source_column_id=col.id,
                        name=col.column_name,
                        display_name=col.column_name.replace("_", " ").title(),
                        data_type=col.data_type or "string",
                        is_time_dim=(role == "date_key"),
                    ))
                    dims_created += 1
            elif role == "measure":
                if col.column_name not in existing_meas:
                    db.add(Measure(
                        model_id=model_id,
                        source_column_id=col.id,
                        name=col.column_name,
                        display_name=col.column_name.replace("_", " ").title(),
                        data_type=col.data_type or "numeric",
                        default_agg="sum",
                    ))
                    meas_created += 1

        await db.commit()
        return ApplyClassificationResponse(
            dimensions_created=dims_created,
            measures_created=meas_created,
            table_type_applied=body.table_type,
        )
