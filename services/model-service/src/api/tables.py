"""
ModelTable CRUD routes — tables belonging to a DataSource.
"""
from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from shared.db.models import (
    CalendarTable,
    DataSource,
    Dimension,
    Measure,
    ModelColumn,
    ModelTable,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    ModelTableClassification,
    ModelTableCreate,
    ModelTableResponse,
    ModelTableUpdate,
    TableAnalysisResponse,
    TableAttributeResponse,
)
from shared.semantic.graph_order import FACT_TABLE_TYPE
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import ensure_calendar_table_in_model, ensure_model_in_project
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

# ModelBuilder opens every table node at once.  Keep that consumer off the
# source-scoped CRUD route so it can hydrate all table attributes in one
# model-scoped request instead of issuing one attributes request per node
# (Bug-9158).
batch_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/tables",
    tags=["tables"],
)


class ModelTableWithAttributesResponse(BaseModel):
    table: ModelTableResponse
    attributes: list[TableAttributeResponse]


def _table_attribute_responses(table: ModelTable) -> list[TableAttributeResponse]:
    """Serialize physical columns and UDAs for the batch canvas response."""
    physical = [
        TableAttributeResponse(
            kind="physical",
            id=column.id,
            table_id=table.id,
            name=column.column_name,
            display_name=column.display_name,
            description=column.description,
            is_hidden=column.is_hidden,
            hidden_reason=column.hidden_reason,
            is_primary_key=column.is_primary_key,
            data_type=column.data_type,
            is_user_defined=False,
            validated=None,
            validation_error=None,
        )
        for column in table.columns
    ]
    user_defined = [
        TableAttributeResponse(
            kind="user_defined",
            id=attribute.id,
            table_id=table.id,
            name=attribute.name,
            display_name=None,
            description=attribute.description,
            is_hidden=False,
            is_primary_key=False,
            data_type=attribute.output_data_type,
            is_user_defined=True,
            is_generated=attribute.is_generated,
            expression=attribute.expression,
            validated=attribute.validated,
            validation_error=attribute.validation_error,
        )
        for attribute in table.user_defined_attributes
    ]
    return sorted(physical + user_defined, key=lambda attribute: attribute.name.lower())


async def _get_scoped_source(
    db, project_id: UUID, model_id: UUID, source_id: UUID
) -> DataSource:
    """Prove the project -> model -> source chain before any read or write.

    Bug-8862: see :func:`_get_scoped_table` for why the project link is not
    optional.
    """
    await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
    source = await db.get(DataSource, source_id)
    if source is None or source.model_id != model_id:
        raise HTTPException(status_code=404, detail="DataSource not found")
    return source


async def _get_scoped_table(
    db,
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    *,
    with_columns: bool = False,
) -> ModelTable:
    """Prove the full project -> model -> source -> table chain.

    Bug-8862: these handlers took ``project_id`` as a path parameter and never
    used it, chaining only source -> model. RBAC (``require_role``) reads
    ``project_id`` from the path and checks the CALLER'S binding for that
    project; it never proves the nested resource belongs to it. So a same-tenant
    caller authorised for one project could read or mutate another project's
    tables purely by substituting ids.

    404 rather than 403 on a mismatch: confirming the resource exists elsewhere
    in the tenant would itself leak cross-project information.

    The project -> model hop goes through the shared
    ``_scope.ensure_model_in_project`` rather than a private clone: Bug-8870
    records that ``refresh.py``/``pockets.py``/``row_security.py`` each carry a
    byte-equivalent ``_get_scoped_model``, and that two competing names for one
    primitive is what makes a repo-wide "does every nested handler bind its
    chain" coverage guard structurally blind to half its targets.

    ``with_columns`` eager-loads ``ModelTable.columns`` for the analyzer paths,
    which read them outside the lazy-load-safe async context.
    """
    await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
    if with_columns:
        table = (
            await db.execute(
                select(ModelTable)
                .where(ModelTable.id == table_id)
                .options(selectinload(ModelTable.columns))
            )
        ).scalar_one_or_none()
    else:
        table = await db.get(ModelTable, table_id)
    if table is None or table.source_id != source_id or table.model_id != model_id:
        raise HTTPException(status_code=404, detail="ModelTable not found")
    return table


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
    if new_table_type != FACT_TABLE_TYPE:
        return
    stmt = select(func.count()).where(
        ModelTable.model_id == model_id,
        ModelTable.table_type == FACT_TABLE_TYPE,
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
        # Bug-8862: prove project -> model BEFORE taking the model lock, so an
        # unauthorised caller cannot contend on another project's advisory lock.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        await _get_scoped_source(db, project_id, model_id, source_id)

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


@router.get("", response_model=list[ModelTableResponse], dependencies=[require_role("viewer")])
async def list_tables(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[ModelTableResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-8862: the WHERE clause below already conjoins ``model_id``, so
        # proving project -> model is what closes the cross-project read.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
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


@batch_router.get(
    "/with-attributes",
    response_model=list[ModelTableWithAttributesResponse],
    dependencies=[require_role("viewer")],
)
async def list_tables_with_attributes(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[ModelTableWithAttributesResponse]:
    """Return every visible model table and its attributes in one request.

    The Builder canvas renders an attribute list for every table.  The old
    client fetched the table catalogue per source and then fetched attributes
    per table, producing a request fan-out proportional to model size.  This
    endpoint preserves the same autocreated-calendar filtering as
    ``list_tables`` while using select-in loading for the two attribute
    collections, so the client has one stable batch contract (Bug-9158).
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        autocreated_ids = (
            select(CalendarTable.id).where(CalendarTable.autocreated == True)  # noqa: E712
        ).scalar_subquery()
        result = await db.execute(
            select(ModelTable)
            .options(
                selectinload(ModelTable.columns),
                selectinload(ModelTable.user_defined_attributes),
            )
            .where(
                ModelTable.model_id == model_id,
                or_(
                    ModelTable.calendar_table_id.is_(None),
                    ModelTable.calendar_table_id.notin_(autocreated_ids),
                ),
            )
            .order_by(ModelTable.alias, ModelTable.id)
        )
        return [
            ModelTableWithAttributesResponse(
                table=ModelTableResponse.model_validate(table),
                attributes=_table_attribute_responses(table),
            )
            for table in result.scalars().unique().all()
        ]


@router.get("/{table_id}", response_model=ModelTableResponse, dependencies=[require_role("viewer")])
async def get_table(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ModelTableResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        t = await _get_scoped_table(db, project_id, model_id, source_id, table_id)
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
        # Bug-8862: prove project -> model BEFORE taking the model lock.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        t = await _get_scoped_table(db, project_id, model_id, source_id, table_id)
        updates = body.model_dump(exclude_unset=True)

        # Bug-8878: ``calendar_table_id`` is a body foreign key applied by the
        # blanket ``setattr`` loop below. Bug-8862 closed the PATH chain
        # (project -> model -> source -> table); this is the same defect class
        # entering through the PAYLOAD, which the path chain cannot reach.
        # ``calendar_tables.id`` is tenant-schema-wide, so a project-B calendar
        # id satisfies the FK constraint, and the reference is dereferenced
        # without an ownership re-check in at least four places: hierarchies.py
        # (:2966, :3070), hierarchy_health.py (:197-203), measures.py (:434) —
        # and, in a DIFFERENT SERVICE, query-router
        # rewrite/calendar_support.py:_resolve_calendar_binding, whose result
        # reaches rewrite/source_sql.py:3217 as
        # ``LEFT JOIN <calendar.table_name> AS cal``. An unvalidated value here
        # therefore does not merely mis-associate a row; it puts another
        # project's physical table name and column meanings into emitted SQL.
        #
        # The check must run BEFORE the setattr loop: the helper's SELECT
        # autoflushes, so guarding afterwards would send the unvalidated value
        # to the database before it could be refused.
        #
        # An explicit ``null`` is legal and means "unbind" — the helper returns
        # None for it — so the guard keys on the field being PRESENT, not on it
        # being truthy.
        if "calendar_table_id" in updates:
            await ensure_calendar_table_in_model(
                db,
                calendar_table_id=updates["calendar_table_id"],
                model_id=model_id,
                project_id=project_id,
            )

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
    from src.api._table_cleanup import (
        cleanup_table_dependents,
        is_rls_mapping_integrity_error,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-8862: prove project -> model BEFORE taking the model lock.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        t = await _get_scoped_table(db, project_id, model_id, source_id, table_id)

        # Bug-6225 [SECURITY]: the FK cascade below removes measures/dimensions/
        # hierarchy levels without touching the persona allow-lists,
        # default_filters or soft-referencing rows that point at them. Run the
        # shared strip + purge + hierarchy orphan-sweep BEFORE the rows vanish
        # (same helper the whole-source delete uses — Bug-7794). The helper also
        # locks this table row and 409s if it is an RLS mapping table.
        await cleanup_table_dependents(db, model_id=model_id, table_id=table_id)

        await db.delete(t)
        try:
            await db.flush()
            await revalidate_model(model_id, db)
            await db.commit()
        except IntegrityError as exc:
            # Safety net: a residual race (an RLS mapping-rule insert past the
            # guard + row lock) surfaces as a clean 409, not a raw 500.
            await db.rollback()
            if is_rls_mapping_integrity_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot delete this table; it is the mapping table for "
                        "a row-security rule. Retire or re-point those rules "
                        "first."
                    ),
                ) from exc
            raise


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
    from shared.semantic.table_analyzer import analyze_table

    async for db in get_tenant_db(current_user.tenant_id):
        t = await _get_scoped_table(
            db, project_id, model_id, source_id, table_id, with_columns=True
        )
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
        t = await _get_scoped_table(db, project_id, model_id, source_id, table_id)

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
    # Bug-8626: this endpoint is a second public ModelTable.table_type writer,
    # so it must share the create/update domain rather than accept a free string
    # such as "Fact" that bypasses the case-sensitive one-fact index.
    table_type: ModelTableClassification | None = None
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
    from shared.semantic.table_analyzer import analyze_table

    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-8862: prove project -> model BEFORE taking the model lock.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        t = await _get_scoped_table(
            db, project_id, model_id, source_id, table_id, with_columns=True
        )

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
                    # Bug-8864: ``Dimension`` carries no ``data_type`` column
                    # (``Measure`` does); the dimension's type is read from its
                    # ``source_column_id``. Passing it raised TypeError in the
                    # declarative constructor, so this endpoint returned HTTP
                    # 500 for every dimension/date_key column.
                    db.add(Dimension(
                        model_id=model_id,
                        source_column_id=col.id,
                        name=col.column_name,
                        display_name=col.column_name.replace("_", " ").title(),
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
