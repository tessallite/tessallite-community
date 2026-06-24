"""
Measure CRUD routes.

Role requirements:
  GET (list / get) → viewer+
  POST / PATCH     → modeler+
  DELETE           → modeler+
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from shared.db.models import (
    Dimension,
    DrillThroughSet,
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from shared.schemas.measure_formats import (
    TIME_VARIANT_FAMILY,
    TIME_VARIANT_REQUIRED_UNIT,
    TIME_VARIANTS_NEEDING_CALENDAR,
    admissible_variant_kinds,
    is_valid_format,
    variant_display_label,
)
from shared.schemas.pydantic_models import (
    CALCULATED_AGG_MODES,
    CalculatedExpressionValidateRequest,
    CalculatedExpressionValidateResponse,
    DrillJoinPath,
    DrillJoinPathHop,
    DrillJoinPathsResponse,
    DrillThroughSetColumnInfo,
    DrillThroughSetEnrichedResponse,
    DrillThroughSetResponse,
    DrillThroughSetUpdate,
    MeasureCreate,
    MeasureResponse,
    MeasureUpdate,
    RedundantPartnerInfo,
)
from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    ParsedExpression,
    detect_cycles,
    parse_expression,
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
    prefix="/projects/{project_id}/models/{model_id}/measures", tags=["measures"]
)


async def _build_response(
    db,
    measure: Measure,
    redundant_partners: dict | None = None,
    *,
    include_eligible: bool = False,
    glossary_texts: dict[UUID, str] | None = None,
) -> MeasureResponse:
    """Build a MeasureResponse enriched with column name and table id.

    Cascades the source column's `is_hidden` flag onto the measure itself
    so the gateway can filter the catalog without a second round trip
    (Phase 1 of the semantic-layer plan).

    Resolves `effective_description` from the glossary precedence chain
    `approved glossary entry > measure.description > none` so the gateway
    can surface curated glossary text in Excel tooltips automatically
    (Phase 4 of the semantic-layer plan).
    """
    col_name = None
    table_id = None
    uda_name = None
    is_hidden = False
    if measure.source_column_id:
        col = await db.get(ModelColumn, measure.source_column_id)
        if col:
            col_name = col.column_name
            table_id = col.model_table_id
            is_hidden = bool(col.is_hidden)
    if measure.user_defined_attribute_id:
        uda = await db.get(UserDefinedAttribute, measure.user_defined_attribute_id)
        if uda:
            uda_name = uda.name
            table_id = uda.table_id

    if glossary_texts is not None:
        glossary_text = glossary_texts.get(measure.id)
    else:
        glossary_text = await _glossary_text_for_target(db, measure.model_id, "measure", measure.id)
    effective_description = glossary_text or measure.description

    # F-015-12: eligibility is an N+1 cost (2 extra queries per measure) and
    # is consumed by nothing on the list path. Compute it only when explicitly
    # requested (single-measure GET); the catalog list path skips it entirely.
    # The canonical eligibility source for pickers is /available-variants.
    eligible_kinds = await _eligible_variant_kinds(db, measure) if include_eligible else None

    resp_hierarchy_id = getattr(measure, "hierarchy_id", None)
    resp_date_dim_col_id = getattr(measure, "date_dimension_column_id", None)

    partner_info = None
    if (
        redundant_partners is not None
        and measure.source_column_id is not None
        and measure.source_column_id in redundant_partners
    ):
        hint = redundant_partners[measure.source_column_id]
        partner_info = RedundantPartnerInfo(
            partner_column_name=hint.partner_column_name,
            partner_table_name=hint.partner_table_name,
            partner_physical_table=hint.partner_physical_table,
            join_type=hint.join_type,
            reason=hint.reason,
        )

    return MeasureResponse(
        id=measure.id,
        model_id=measure.model_id,
        name=measure.name,
        display_name=measure.display_name,
        description=measure.description,
        effective_description=effective_description,
        display_folder=measure.display_folder,
        is_hidden=is_hidden,
        source_column_id=measure.source_column_id,
        source_column_name=col_name,
        source_table_id=table_id,
        user_defined_attribute_id=measure.user_defined_attribute_id,
        user_defined_attribute_name=uda_name,
        measure_type=measure.measure_type,
        expression=measure.expression,
        calc_agg_mode=getattr(measure, "calc_agg_mode", None),
        data_type=measure.data_type,
        default_agg=measure.default_agg,
        format=measure.format,
        variant_kind=measure.variant_kind,
        variant_of_measure_id=measure.variant_of_measure_id,
        variant_n=measure.variant_n,
        eligible_variant_kinds=eligible_kinds,
        is_additive=measure.is_additive,
        semi_additive_behavior=getattr(measure, "semi_additive_behavior", None),
        semi_additive_account_column_id=getattr(measure, "semi_additive_account_column_id", None),
        calendar_model_table_id=getattr(measure, "calendar_model_table_id", None),
        hierarchy_id=resp_hierarchy_id,
        date_dimension_column_id=resp_date_dim_col_id,
        is_invalid=bool(getattr(measure, "is_invalid", False)),
        invalid_reason=getattr(measure, "invalid_reason", None),
        cross_model_source_model_id=getattr(measure, "cross_model_source_model_id", None),
        cross_model_source_measure_id=getattr(measure, "cross_model_source_measure_id", None),
        redundant_partner=partner_info,
        created_at=measure.created_at,
        updated_at=measure.updated_at,
    )


async def _resolve_variant_calendar_snapshot(
    db, model_id: UUID, hierarchy_id: UUID | None,
) -> tuple[UUID | None, UUID | None]:
    """Resolve the denormalised calendar snapshot for a period-aware variant.

    The multi-calendar design (architecture_multi-calendar.md) stores the
    resolved CalendarTable and date column directly on the variant measure so
    query-time resolution is a single FK hop. This is the producer step that
    was never built (F-016-02): walk the variant's ``hierarchy_id`` to the
    calendar alias ModelTable and read its ``calendar_table_id``.

    Chain: HierarchyDefinition → HierarchyLevel.key_attribute_id → owning
    ModelTable (via ModelColumn for physical levels, via
    UserDefinedAttribute.table_id for UDA-keyed generated hierarchies) →
    ModelTable.calendar_table_id → CalendarTable.

    Returns ``(resolved_calendar_id, resolved_date_col_id)``. Either may be
    None: an expression-only calendar (standard / fiscal / iso_week /
    thai_buddhist hierarchy built directly on a fact column, with no calendar
    alias) has no calendar table — the snapshot stays NULL and the query path
    computes period boundaries by expression.
    """
    from shared.db.models import CalendarTable

    if hierarchy_id is None:
        return (None, None)

    level_rows = await db.execute(
        select(
            HierarchyLevel.key_attribute_id,
            HierarchyLevel.key_attribute_source,
        ).where(HierarchyLevel.hierarchy_id == hierarchy_id)
    )
    # Collect the distinct owning ModelTable for every level key.
    table_ids: list[UUID] = []
    for attr_id, source in level_rows.all():
        owning_table_id: UUID | None = None
        if source == "physical_column":
            col = await db.get(ModelColumn, attr_id)
            owning_table_id = col.model_table_id if col is not None else None
        elif source == "user_defined_attribute":
            uda = await db.get(UserDefinedAttribute, attr_id)
            owning_table_id = uda.table_id if uda is not None else None
        if owning_table_id is not None and owning_table_id not in table_ids:
            table_ids.append(owning_table_id)

    for table_id in table_ids:
        mt = await db.get(ModelTable, table_id)
        if mt is None or mt.calendar_table_id is None:
            continue
        cal = await db.get(CalendarTable, mt.calendar_table_id)
        if cal is None:
            continue
        # The date column is the alias-table ModelColumn matching the
        # calendar's date_column (mirrors migration 0079's backfill).
        date_col_id: UUID | None = None
        if cal.date_column:
            col_row = await db.execute(
                select(ModelColumn.id).where(
                    ModelColumn.model_table_id == table_id,
                    ModelColumn.column_name == cal.date_column,
                ).limit(1)
            )
            date_col_id = col_row.scalar_one_or_none()
        return (mt.calendar_table_id, date_col_id)

    return (None, None)


async def _resolve_calendar_alias_for_measure(
    db,
    *,
    model_id: UUID,
    variant_kind: str | None,
    variant_of_measure_id: UUID | None,
    calendar_model_table_id: UUID | None,
) -> UUID | None:
    """Validate and resolve the ModelTable alias acting as the measure's
    calendar.

    For variant rows (``variant_kind`` set): the variant inherits its
    calendar from the base measure. Any value supplied on the variant is
    ignored. Period-aware variants require the base to have a calendar.

    For base measures (``variant_kind`` is None): the supplied
    ``calendar_model_table_id`` is validated to point at a ModelTable in
    this model whose ``calendar_table_id`` is set.
    """
    if variant_kind is not None:
        if variant_of_measure_id is None:
            return None
        base = await db.get(Measure, variant_of_measure_id)
        if base is None or base.model_id != model_id:
            raise HTTPException(
                status_code=400,
                detail="variant_of_measure_id does not refer to a base measure in this model",
            )
        return base.calendar_model_table_id

    if calendar_model_table_id is None:
        return None
    alias_table = await db.get(ModelTable, calendar_model_table_id)
    if alias_table is None or alias_table.model_id != model_id:
        raise HTTPException(
            status_code=400,
            detail="calendar_model_table_id does not refer to a ModelTable in this model",
        )
    if alias_table.calendar_table_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ModelTable referenced by calendar_model_table_id is not a "
                "calendar alias (its calendar_table_id is NULL)."
            ),
        )
    return calendar_model_table_id


async def _eligible_variant_kinds(db, measure: Measure) -> list[str] | None:
    """Variant kinds the frontend may offer as ticks on this base measure.

    Intersects the associated hierarchy's level capabilities with the
    source-level calendar binding. Returns ``None`` for variant rows and
    for measures with no associated hierarchy (the two cases where the
    concept of eligibility does not apply).
    """
    if measure.variant_kind is not None:
        return None

    hierarchy_id = getattr(measure, "hierarchy_id", None)
    if hierarchy_id is None:
        return None

    level_rows = await db.execute(
        select(HierarchyLevel.time_unit, HierarchyLevel.allowed_time_calcs).where(
            HierarchyLevel.hierarchy_id == hierarchy_id
        )
    )
    units: set[str] = set()
    calcs: set[str] = set()
    for time_unit, allowed in level_rows.all():
        if time_unit:
            units.add(time_unit)
        for token in (allowed or []):
            calcs.add(token)

    cal_row = await db.execute(
        select(HierarchyDefinition.calendar_type).where(
            HierarchyDefinition.id == hierarchy_id
        )
    )
    has_calendar_rules = any(ct is not None for ct in cal_row.scalars().all())

    return admissible_variant_kinds(
        level_units=units,
        level_calcs=calcs,
        has_calendar_rules=has_calendar_rules,
    )


async def _load_redundant_partners(db, model_id: UUID) -> dict:
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


async def _resolve_calculated_expression(
    db,
    model_id: UUID,
    expression: str,
    *,
    self_measure_id: UUID | None,
) -> tuple[ParsedExpression, list[UUID]]:
    """Parse a calculated-measure expression and resolve its references.

    Returns ``(parsed, referenced_ids)``. Raises 400 if the expression is
    malformed, references an unknown / calculated measure (single-pass
    rule, Q10=A), or would introduce a cyclic dependency among other
    calculated measures in the same model.
    """
    try:
        parsed = parse_expression(expression)
    except ExpressionValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid expression: {exc}") from exc

    referenced_names = list(parsed.referenced_names)
    if not referenced_names:
        raise HTTPException(
            status_code=400,
            detail="Calculated measure expression must reference at least one measure via measure(\"name\")",
        )

    result = await db.execute(
        select(Measure).where(
            Measure.model_id == model_id,
            Measure.name.in_(referenced_names),
        )
    )
    name_to_measure = {m.name: m for m in result.scalars().all()}

    missing = sorted(set(referenced_names) - name_to_measure.keys())
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown measure references: {', '.join(missing)}",
        )

    calculated_refs = [
        m.name for m in name_to_measure.values() if m.measure_type == "calculated"
    ]
    if calculated_refs:
        raise HTTPException(
            status_code=400,
            detail=(
                "Calculated measures cannot reference other calculated measures "
                f"in v1 (single-pass): {', '.join(sorted(set(calculated_refs)))}"
            ),
        )

    # F-015-04: a calculated measure that references a time-variant measure
    # silently rewrites to the variant's BASE column (the rewriter expands
    # references via the raw snapshot column wrapped in the default agg and
    # never consults variant_kind), so e.g. measure("revenue_ytd") rendered as
    # SUM(revenue) and safe_div(measure("revenue_ytd"), measure("revenue"))
    # collapsed to the constant 1.0. A window expression also cannot legally
    # nest inside another aggregate. Reject variant references at save time
    # with a clear message rather than accept-and-compute-wrong.
    variant_refs = [
        m.name
        for m in name_to_measure.values()
        if getattr(m, "variant_kind", None) is not None
    ]
    if variant_refs:
        raise HTTPException(
            status_code=400,
            detail=(
                "Calculated measures cannot reference time-variant measures "
                "(e.g. YTD / YoY / prior-period). Reference the base measure "
                "and apply the variant to the calculated measure itself if "
                f"needed: {', '.join(sorted(set(variant_refs)))}"
            ),
        )

    referenced_ids = [name_to_measure[n].id for n in referenced_names]

    # Cycle detection — build the dependency graph of all calculated
    # measures in the model, include the candidate, and reject if a cycle
    # appears. With single-pass enforced above this cannot trigger today,
    # but the check is retained so multi-pass (v2) inherits the guard.
    await _check_cycle(db, model_id, self_measure_id, referenced_ids)

    return parsed, referenced_ids


async def _check_cycle(
    db,
    model_id: UUID,
    self_measure_id: UUID | None,
    candidate_refs: list[UUID],
) -> None:
    existing = await db.execute(
        select(Measure).where(
            Measure.model_id == model_id,
            Measure.measure_type == "calculated",
        )
    )
    calc_measures = list(existing.scalars().all())

    # Build dependency map over measure ids. Re-parse each existing
    # calculated measure's expression to discover its references; a
    # rewrite failure here is logged as soft-invalid elsewhere and
    # contributes no edges.
    name_to_id = {}
    result = await db.execute(
        select(Measure.id, Measure.name).where(Measure.model_id == model_id)
    )
    for row in result.all():
        name_to_id[row.name] = row.id

    deps: dict[UUID, list[UUID]] = {}
    for m in calc_measures:
        if not m.expression:
            continue
        if self_measure_id is not None and m.id == self_measure_id:
            continue
        try:
            parsed = parse_expression(m.expression)
        except ExpressionValidationError:
            continue
        deps[m.id] = [
            name_to_id[n] for n in parsed.referenced_names if n in name_to_id
        ]

    candidate_id = self_measure_id or UUID(int=0)  # placeholder id for new rows
    deps[candidate_id] = list(candidate_refs)

    cycles = detect_cycles(deps)
    if cycles:
        raise HTTPException(
            status_code=400,
            detail="Cyclic dependency detected among calculated measures",
        )


# F-018-20: `_glossary_text_for_target` was copy-pasted here and in
# dimensions.py. It now lives in `_scope.py` and is imported (aliased) at the
# top of this module.


@router.post(
    "/validate-expression",
    response_model=CalculatedExpressionValidateResponse,
    dependencies=[require_role("viewer")],
)
async def validate_calculated_expression(
    project_id: UUID,
    model_id: UUID,
    body: CalculatedExpressionValidateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> CalculatedExpressionValidateResponse:
    """Validate a calculated-measure expression without saving it.

    Used by the Measures panel to give modellers live feedback while they
    author a calculated measure. Reuses the same resolver the create /
    update endpoints use, so the rules (single-pass, allow-listed
    functions, cycle detection) stay in one place.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        try:
            parsed, referenced_ids = await _resolve_calculated_expression(
                db, model_id, body.expression, self_measure_id=body.self_measure_id,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return CalculatedExpressionValidateResponse(valid=False, error=detail)
        return CalculatedExpressionValidateResponse(
            valid=True,
            referenced_measure_ids=list(referenced_ids),
            referenced_measure_names=list(parsed.referenced_names),
        )


@router.post(
    "",
    response_model=MeasureResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_measure(
    project_id: UUID,
    model_id: UUID,
    body: MeasureCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> MeasureResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        if body.user_defined_attribute_id and (body.source_table_id or body.source_column_name):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        if not is_valid_format(body.format):
            raise HTTPException(status_code=400, detail=f"Unknown format token: {body.format}")
        source_column_id = None
        user_defined_attribute_id = body.user_defined_attribute_id
        # Mutable working copies of the snapshot fields. For variants these
        # are overwritten server-side from the base measure (F-015-11) so the
        # API never trusts client-supplied source/agg/type values.
        variant_default_agg = body.default_agg
        variant_data_type = body.data_type
        variant_semi_additive_behavior = body.semi_additive_behavior
        variant_semi_additive_account_column_id = body.semi_additive_account_column_id

        if body.measure_type == "calculated":
            await _resolve_calculated_expression(
                db, model_id, body.expression or "", self_measure_id=None,
            )
        elif body.variant_kind is not None:
            # F-015-11: the variant's source/agg/type snapshot is resolved
            # server-side from the base measure AFTER the eligibility gate
            # below (so an ineligible variant still returns 422 first). Client-
            # supplied source fields are ignored — the API is the authority for
            # the snapshot, not the caller.
            pass
        else:
            if body.source_table_id and body.source_column_name:
                col = await resolve_column(db, body.source_table_id, body.source_column_name, body.data_type)
                source_column_id = col.id
            elif user_defined_attribute_id:
                uda = await db.get(UserDefinedAttribute, user_defined_attribute_id)
                if uda is None or uda.model_id != model_id:
                    raise HTTPException(status_code=404, detail="User-defined attribute not found")

        calendar_model_table_id = await _resolve_calendar_alias_for_measure(
            db,
            model_id=model_id,
            variant_kind=body.variant_kind,
            variant_of_measure_id=body.variant_of_measure_id,
            calendar_model_table_id=body.calendar_model_table_id,
        )

        # Validate cross-model reference: source model must be in the same project
        if body.cross_model_source_model_id is not None:
            source_model = await db.get(Model, body.cross_model_source_model_id)
            if source_model is None or source_model.project_id != project_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "cross_model_source_model_id must reference a model in the same project. "
                        "Cross-project references are not supported."
                    ),
                )

        resolved_calendar_id: UUID | None = None
        resolved_date_col_id: UUID | None = None
        if body.variant_kind is not None:
            reason = await _check_variant_eligibility(
                db, model_id, body.variant_kind, body.variant_of_measure_id,
                override_hierarchy_id=body.hierarchy_id,
            )
            if reason:
                raise HTTPException(status_code=422, detail=reason)
            # F-015-11: snapshot source/agg/type from the base now that the
            # variant is eligible. _check_variant_eligibility already verified
            # the base exists in this model, so the fetch is safe.
            base = await db.get(Measure, body.variant_of_measure_id)
            if base is not None:
                source_column_id = base.source_column_id
                user_defined_attribute_id = base.user_defined_attribute_id
                variant_default_agg = base.default_agg
                variant_data_type = base.data_type
                variant_semi_additive_behavior = getattr(base, "semi_additive_behavior", None)
                variant_semi_additive_account_column_id = getattr(
                    base, "semi_additive_account_column_id", None
                )
            # F-016-02: resolve and store the denormalised calendar snapshot so
            # query-time calendar binding is a single FK hop. The variant's
            # hierarchy (explicit body.hierarchy_id, else inherited from the
            # base) is walked to its calendar alias table.
            _snapshot_hierarchy_id = body.hierarchy_id
            if _snapshot_hierarchy_id is None and body.variant_of_measure_id is not None:
                _base = await db.get(Measure, body.variant_of_measure_id)
                _snapshot_hierarchy_id = getattr(_base, "hierarchy_id", None) if _base else None
            resolved_calendar_id, resolved_date_col_id = (
                await _resolve_variant_calendar_snapshot(
                    db, model_id, _snapshot_hierarchy_id
                )
            )

        measure = Measure(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name or body.name,
            description=body.description,
            display_folder=body.display_folder,
            source_column_id=source_column_id,
            user_defined_attribute_id=user_defined_attribute_id,
            measure_type=body.measure_type,
            expression=body.expression,
            calc_agg_mode=body.calc_agg_mode,
            data_type=variant_data_type,
            default_agg=variant_default_agg,
            format=body.format,
            variant_kind=body.variant_kind,
            variant_of_measure_id=body.variant_of_measure_id,
            variant_n=body.variant_n,
            is_additive=body.is_additive,
            semi_additive_behavior=variant_semi_additive_behavior,
            semi_additive_account_column_id=variant_semi_additive_account_column_id,
            calendar_model_table_id=calendar_model_table_id,
            hierarchy_id=body.hierarchy_id,
            resolved_calendar_id=resolved_calendar_id,
            resolved_date_col_id=resolved_date_col_id,
            date_dimension_column_id=body.date_dimension_column_id,
            cross_model_source_model_id=body.cross_model_source_model_id,
            cross_model_source_measure_id=body.cross_model_source_measure_id,
        )
        db.add(measure)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if "measures_model_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"A measure named '{body.name}' already exists in this model.",
                )
            raise
        await _ensure_drill_through_set(db, measure)
        await db.commit()
        await db.refresh(measure)
        return await _build_response(db, measure)


async def _ensure_drill_through_set(db, measure: Measure) -> None:
    """Auto-create the implicit drill-through row for a standard / variant
    measure. Calculated measures have no single source fact and get no row.

    Phase 4C.1: see ``work/phase-4-drill-through-and-calculated-members-action-plan.md``.
    """
    if measure.measure_type == "calculated":
        return
    source_table_id: UUID | None = None
    if measure.source_column_id:
        col = await db.get(ModelColumn, measure.source_column_id)
        if col is not None:
            source_table_id = col.model_table_id
    elif measure.user_defined_attribute_id:
        uda = await db.get(UserDefinedAttribute, measure.user_defined_attribute_id)
        if uda is not None:
            source_table_id = uda.table_id
    db.add(
        DrillThroughSet(
            measure_id=measure.id,
            source_table_id=source_table_id,
        )
    )


@router.get("", response_model=list[MeasureResponse])
async def list_measures(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[MeasureResponse]:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        stmt = select(Measure).where(Measure.model_id == model_id)
        if persona:
            allowed = parse_allowed_ids(persona.included_measure_ids)
            if allowed is not None:
                stmt = stmt.where(Measure.id.in_(allowed))
        stmt = stmt.order_by(Measure.name)
        partners = await _load_redundant_partners(db, model_id)
        result = await db.execute(stmt)
        measures = result.scalars().all()
        glossary_texts = await _glossary_texts_for_targets(
            db, model_id, "measure", [m.id for m in measures]
        )
        out: list[MeasureResponse] = []
        for m in measures:
            out.append(
                await _build_response(db, m, partners, glossary_texts=glossary_texts)
            )
        return out


@router.get("/{measure_id}", response_model=MeasureResponse)
async def get_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> MeasureResponse:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        m = await db.get(Measure, measure_id)
        if m is None or m.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")
        if persona:
            allowed = parse_allowed_ids(persona.included_measure_ids)
            if allowed is not None and m.id not in allowed:
                raise HTTPException(status_code=404, detail="Measure not found")
        partners = await _load_redundant_partners(db, model_id)
        return await _build_response(db, m, partners, include_eligible=True)


@router.patch(
    "/{measure_id}",
    response_model=MeasureResponse,
    dependencies=[require_role("modeler")],
)
async def update_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    body: MeasureUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> MeasureResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        m = await db.get(Measure, measure_id)
        if m is None or m.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")
        updates = body.model_dump(exclude_unset=True)
        # measure_type is immutable on PATCH. Converting a standard measure
        # into a calculated one (or vice versa) changes too many invariants
        # to do safely in-place; delete + recreate is the supported path.
        if "measure_type" in updates and updates["measure_type"] != m.measure_type:
            raise HTTPException(
                status_code=400,
                detail="measure_type cannot be changed; delete and recreate the measure",
            )
        # Variant rows are immutable except for label/format/variant_n. The
        # snapshot fields (source, agg, data_type, ...) inherit from the base
        # at creation and must not drift away from it.
        if m.variant_kind is not None:
            _VARIANT_MUTABLE_FIELDS = {
                "name",
                "display_name",
                "description",
                "display_folder",
                "format",
                "variant_n",
            }
            forbidden = sorted(set(updates.keys()) - _VARIANT_MUTABLE_FIELDS)
            if forbidden:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Variant measures inherit "
                        + ", ".join(forbidden)
                        + " from the base measure and cannot be modified directly."
                    ),
                )
        # variant_n is only meaningful for parametric variants (trailing_n,
        # moving_avg_n). Reject on every other row.
        if "variant_n" in updates and updates["variant_n"] is not None:
            if m.variant_kind not in {"trailing_n", "moving_avg_n"}:
                raise HTTPException(
                    status_code=400,
                    detail="variant_n is only valid for trailing_n / moving_avg_n variants",
                )
        if "format" in updates and not is_valid_format(updates["format"]):
            raise HTTPException(status_code=400, detail=f"Unknown format token: {updates['format']}")

        # Calculated-measure-specific updates.
        if m.measure_type == "calculated":
            forbidden_calc = {
                "source_table_id", "source_column_name", "user_defined_attribute_id",
                "variant_kind", "variant_of_measure_id", "variant_n",
            }
            conflict = sorted(set(updates.keys()) & forbidden_calc)
            if conflict:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Calculated measures cannot carry " + ", ".join(conflict)
                    ),
                )
            if "calc_agg_mode" in updates:
                new_mode = updates["calc_agg_mode"]
                if new_mode is None or new_mode not in CALCULATED_AGG_MODES:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "calc_agg_mode must be one of "
                            f"{sorted(CALCULATED_AGG_MODES)}"
                        ),
                    )
            if "expression" in updates and updates["expression"] is not None:
                await _resolve_calculated_expression(
                    db, model_id, updates["expression"], self_measure_id=m.id,
                )
            # Clear any stale soft-invalid state when the modeller edits the
            # expression; if it's still broken, downstream revalidate will
            # re-flag it.
            if "expression" in updates:
                m.is_invalid = False
                m.invalid_reason = None
        else:
            # Standard / variant measures cannot carry calculated-only fields.
            stray = sorted(
                {k for k in ("expression", "calc_agg_mode") if k in updates and updates[k] is not None}
            )
            if stray:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        ", ".join(stray) + " only apply to calculated measures"
                    ),
                )
        if "user_defined_attribute_id" in updates and (
            "source_table_id" in updates or "source_column_name" in updates
        ):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        if "calendar_model_table_id" in updates:
            if m.variant_kind is not None:
                raise HTTPException(
                    status_code=400,
                    detail="calendar_model_table_id is inherited from the base measure and cannot be set on a variant.",
                )
            new_cal = updates["calendar_model_table_id"]
            if new_cal is not None:
                alias_table = await db.get(ModelTable, new_cal)
                if alias_table is None or alias_table.model_id != model_id:
                    raise HTTPException(
                        status_code=400,
                        detail="calendar_model_table_id does not refer to a ModelTable in this model",
                    )
                if alias_table.calendar_table_id is None:
                    raise HTTPException(
                        status_code=400,
                        detail="ModelTable referenced by calendar_model_table_id is not a calendar alias.",
                    )
            await db.execute(
                Measure.__table__.update()
                .where(Measure.variant_of_measure_id == m.id)
                .values(calendar_model_table_id=new_cal)
            )
        # Resolve column name if provided. F-015-21: a half-supplied source
        # pair (only one of source_table_id / source_column_name) must fail
        # loud rather than being silently discarded — create requires the
        # pair, so update must too.
        has_table = "source_table_id" in updates
        has_col = "source_column_name" in updates
        if has_table != has_col:
            raise HTTPException(
                status_code=400,
                detail="source_table_id and source_column_name must be supplied together.",
            )
        if has_table and has_col:
            table_id = updates.pop("source_table_id")
            col_name = updates.pop("source_column_name")
            if table_id and col_name:
                col = await resolve_column(db, table_id, col_name)
                m.source_column_id = col.id
                m.user_defined_attribute_id = None
            elif table_id or col_name:
                # One half set, the other explicitly null — also incoherent.
                raise HTTPException(
                    status_code=400,
                    detail="source_table_id and source_column_name must be supplied together.",
                )
            else:
                m.source_column_id = None
        if "user_defined_attribute_id" in updates:
            uda_id = updates["user_defined_attribute_id"]
            if uda_id:
                uda = await db.get(UserDefinedAttribute, uda_id)
                if uda is None or uda.model_id != model_id:
                    raise HTTPException(status_code=404, detail="User-defined attribute not found")
                m.source_column_id = None
        new_name = updates.get("name")
        if new_name and "display_name" not in updates and (not m.display_name or m.display_name == m.name):
            updates["display_name"] = new_name
        if "display_name" in updates and (updates["display_name"] is None or not str(updates["display_name"]).strip()):
            updates["display_name"] = new_name or m.name
        # Validate same-project constraint on cross-model reference update
        if "cross_model_source_model_id" in updates and updates["cross_model_source_model_id"] is not None:
            source_model = await db.get(Model, updates["cross_model_source_model_id"])
            if source_model is None or source_model.project_id != project_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "cross_model_source_model_id must reference a model in the same project. "
                        "Cross-project references are not supported."
                    ),
                )
        for k, v in updates.items():
            setattr(m, k, v)
        raw = body.model_dump(exclude_unset=True)
        _CASCADE_TRIGGERS = {
            "source_table_id", "source_column_name",
            "user_defined_attribute_id", "default_agg",
            "is_additive", "data_type",
        }
        _SOURCE_TRIGGERS = {"source_table_id", "source_column_name", "user_defined_attribute_id"}
        if m.variant_kind is None and _CASCADE_TRIGGERS & raw.keys():
            source_changed = bool(_SOURCE_TRIGGERS & raw.keys())
            cascade_vals: dict = {}
            if source_changed:
                cascade_vals["source_column_id"] = m.source_column_id
                cascade_vals["user_defined_attribute_id"] = m.user_defined_attribute_id
            for k in ("default_agg", "is_additive", "data_type"):
                if k in raw:
                    cascade_vals[k] = getattr(m, k)
            if cascade_vals:
                await db.execute(
                    Measure.__table__.update()
                    .where(Measure.variant_of_measure_id == m.id)
                    .values(**cascade_vals)
                )
        # F-016-02: re-resolve the calendar snapshot when the calendar binding
        # of a base measure changes (hierarchy re-pointed or calendar alias
        # changed). The architecture mandates the snapshot is re-resolved on
        # edit; cascade the fresh snapshot to every variant of this base.
        if m.variant_kind is None and (
            "hierarchy_id" in raw or "calendar_model_table_id" in raw
        ):
            new_cal_id, new_date_col_id = await _resolve_variant_calendar_snapshot(
                db, model_id, getattr(m, "hierarchy_id", None)
            )
            await db.execute(
                Measure.__table__.update()
                .where(Measure.variant_of_measure_id == m.id)
                .values(
                    resolved_calendar_id=new_cal_id,
                    resolved_date_col_id=new_date_col_id,
                )
            )
        try:
            if (
                "source_table_id" in raw
                or "source_column_name" in raw
                or "user_defined_attribute_id" in raw
                or "name" in raw
                or "expression" in raw
            ):
                from shared.semantic.model_validator import revalidate_model

                await db.flush()
                await revalidate_model(model_id, db)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if "measures_model_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"A measure named '{updates.get('name', m.name)}' already exists in this model.",
                )
            raise
        await db.refresh(m)
        return await _build_response(db, m)


@router.delete(
    "/{measure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    from shared.semantic.model_validator import revalidate_model
    from src.api.personas import strip_id_from_personas

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        m = await db.get(Measure, measure_id)
        if m is None or m.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")
        # F-015-17: variant rows are removed by the variant_of_measure_id
        # CASCADE FK when the base is deleted, but the DB cascade does not
        # touch persona allow-lists. Enumerate the cascade-deleted variant ids
        # and strip each from personas first, so deleting a base never leaves
        # dangling variant UUIDs inside included_measure_ids (which would then
        # silently shrink filtered allow-lists and travel in export/import).
        variant_id_rows = await db.execute(
            select(Measure.id).where(Measure.variant_of_measure_id == measure_id)
        )
        cascade_ids = [measure_id, *variant_id_rows.scalars().all()]
        for obj_id in cascade_ids:
            await strip_id_from_personas(
                db, model_id=model_id, object_id=obj_id, object_class="measure"
            )
            # Soft-referencing translation/preference rows have no FK back to
            # the measure and would otherwise linger forever (F-029-15).
            await purge_entity_soft_references(db, model_id=model_id, entity_id=obj_id)
        await db.delete(m)
        await db.flush()
        await revalidate_model(model_id, db)
        await db.commit()


# ---------------------------------------------------------------------------
# Available variants — drives the pivot's "fx+" popover (Phase 6.B / B8).
# ---------------------------------------------------------------------------

_CANONICAL_VARIANT_ORDER: tuple[str, ...] = (
    "lag",
    "prior_year",
    "prior_quarter",
    "prior_month",
    "prior_week",
    "ytd",
    "qtd",
    "mtd",
    "wtd",
    "ytd_prior_year",
    "yoy_growth",
    "yoy_growth_pct",
    "trailing_n",
    "moving_avg_n",
)


async def _check_variant_eligibility(
    db, model_id: UUID, variant_kind: str, variant_of_measure_id: UUID | None,
    *, override_hierarchy_id: UUID | None = None,
) -> str | None:
    """Return an error string if the variant is ineligible, or None if OK."""
    if variant_of_measure_id is None:
        return "variant_of_measure_id is required for variant measures."
    base = await db.get(Measure, variant_of_measure_id)
    if base is None or base.model_id != model_id:
        return "Base measure not found in this model."
    if base.variant_kind is not None:
        return "Cannot create a variant of a variant measure."
    if base.measure_type == "calculated":
        return "Cannot create a time variant of a calculated measure."

    hierarchy_id = override_hierarchy_id or getattr(base, "hierarchy_id", None)
    has_hierarchy = hierarchy_id is not None
    units: set[str] = set()
    calcs: set[str] = set()
    if has_hierarchy:
        level_rows = await db.execute(
            select(
                HierarchyLevel.time_unit, HierarchyLevel.allowed_time_calcs
            ).where(HierarchyLevel.hierarchy_id == hierarchy_id)
        )
        for time_unit, allowed in level_rows.all():
            if time_unit:
                units.add(time_unit)
            for token in (allowed or []):
                calcs.add(token)

    has_calendar_rules = False
    if has_hierarchy:
        cal_row = await db.execute(
            select(HierarchyDefinition.calendar_type).where(
                HierarchyDefinition.id == hierarchy_id
            )
        )
        has_calendar_rules = any(
            ct is not None for ct in cal_row.scalars().all()
        )

    return _variant_reason(
        variant_kind,
        has_hierarchy=has_hierarchy,
        units=units,
        calcs=calcs,
        has_calendar_rules=has_calendar_rules,
    )


def _variant_reason(
    kind: str,
    *,
    has_hierarchy: bool,
    units: set[str],
    calcs: set[str],
    has_calendar_rules: bool,
) -> str | None:
    """None when the variant is admissible; otherwise a short explanation."""
    if not has_hierarchy:
        return (
            "This measure has no associated time hierarchy. "
            "Create a time hierarchy with the required levels on "
            "the same table as the measure's source column."
        )
    family = TIME_VARIANT_FAMILY[kind]
    if family not in calcs:
        return (
            f"The associated time hierarchy does not declare the '{family}' "
            "capability on any level. Edit the hierarchy and enable "
            f"'{family}' in the allowed time calculations for the "
            "appropriate level."
        )
    required_unit = TIME_VARIANT_REQUIRED_UNIT[kind]
    if required_unit is not None and required_unit not in units:
        return (
            f"The associated time hierarchy has no '{required_unit}'-grain level. "
            f"Add a level with time unit '{required_unit}' to the hierarchy."
        )
    if kind in TIME_VARIANTS_NEEDING_CALENDAR and not has_calendar_rules:
        return (
            "The associated time hierarchy has no calendar type configured. "
            "Edit the hierarchy and set a calendar type (standard, fiscal, "
            "ISO week, retail 4-5-4, Hijri, or Thai Buddhist). Retail and "
            "Hijri calendars also require a bound calendar table."
        )
    return None


@router.get("/{measure_id}/available-variants")
async def list_available_variants(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> dict:
    """Per-variant eligibility table for a base measure.

    Returns the 14 canonical kinds in catalog order, each annotated with
    ``eligible``, a human-readable ``reason`` when ineligible, and
    ``existing_measure_id`` when a variant row already exists for this
    base (so the client can disable the entry and link to it).

    Variant rows themselves (``variant_kind is not None``) return an empty
    list — a variant of a variant is not permitted.
    """
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        measure = await db.get(Measure, measure_id)
        if measure is None or measure.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")

        if measure.variant_kind is not None:
            return {"variants": []}

        hierarchy_id = getattr(measure, "hierarchy_id", None)
        has_hierarchy = hierarchy_id is not None

        units: set[str] = set()
        calcs: set[str] = set()
        if has_hierarchy:
            level_rows = await db.execute(
                select(
                    HierarchyLevel.time_unit, HierarchyLevel.allowed_time_calcs
                ).where(HierarchyLevel.hierarchy_id == hierarchy_id)
            )
            for time_unit, allowed in level_rows.all():
                if time_unit:
                    units.add(time_unit)
                for token in (allowed or []):
                    calcs.add(token)

        if has_hierarchy:
            cal_row = await db.execute(
                select(HierarchyDefinition.calendar_type).where(
                    HierarchyDefinition.id == hierarchy_id
                )
            )
            has_calendar_rules = any(
                ct is not None for ct in cal_row.scalars().all()
            )
        else:
            has_calendar_rules = False

        existing_rows = await db.execute(
            select(Measure.id, Measure.variant_kind).where(
                Measure.variant_of_measure_id == measure.id
            )
        )
        existing_by_kind: dict[str, UUID] = {
            kind: mid for mid, kind in existing_rows.all() if kind
        }

        base_name = measure.name
        base_display = measure.display_name or measure.name

        variants = []
        for kind in _CANONICAL_VARIANT_ORDER:
            reason = _variant_reason(
                kind,
                has_hierarchy=has_hierarchy,
                units=units,
                calcs=calcs,
                has_calendar_rules=has_calendar_rules,
            )
            existing_id = existing_by_kind.get(kind)
            variants.append({
                "kind": kind,
                "eligible": reason is None,
                "reason": reason,
                "suggested_name": f"{base_name}_{kind}",
                # F-015-22: human-readable label, not the raw kind token, so
                # the BI catalog shows "Revenue (YTD Prior Year)" rather than
                # "Revenue (ytd_prior_year)".
                "suggested_display_name": f"{base_display} ({variant_display_label(kind)})",
                "existing_measure_id": str(existing_id) if existing_id else None,
            })
        return {"variants": variants}


# ---------------------------------------------------------------------------
# Drill-through set curation (Phase 8.A)
# ---------------------------------------------------------------------------


async def _load_drill_through_set_or_404(
    db, measure_id: UUID, model_id: UUID,
    *,
    create_if_missing: bool = True,
) -> tuple[Measure, DrillThroughSet]:
    measure = await db.get(Measure, measure_id)
    if measure is None or measure.model_id != model_id:
        raise HTTPException(status_code=404, detail="Measure not found")
    if measure.measure_type == "calculated":
        raise HTTPException(
            status_code=400,
            detail="Calculated measures have no drill-through set; drill on the underlying measures instead.",
        )
    result = await db.execute(
        select(DrillThroughSet).where(DrillThroughSet.measure_id == measure_id)
    )
    drill = result.scalar_one_or_none()
    if drill is None:
        if not create_if_missing:
            # F-015-20: GET must not write. Synthesize a transient default
            # (the implicit drill-through set) without persisting it; the row
            # is created only when a modeler PATCHes the set.
            source_table_id: UUID | None = None
            if measure.source_column_id:
                col = await db.get(ModelColumn, measure.source_column_id)
                if col is not None:
                    source_table_id = col.model_table_id
            elif measure.user_defined_attribute_id:
                uda = await db.get(UserDefinedAttribute, measure.user_defined_attribute_id)
                if uda is not None:
                    source_table_id = uda.table_id
            # Carry a synthetic id + timestamps so the response schema (which
            # requires id/created_at/updated_at) validates without persisting.
            now = datetime.now(timezone.utc)
            return measure, DrillThroughSet(
                id=uuid4(),
                measure_id=measure.id,
                source_table_id=source_table_id,
                created_at=now,
                updated_at=now,
            )
        await _ensure_drill_through_set(db, measure)
        await db.flush()
        result = await db.execute(
            select(DrillThroughSet).where(DrillThroughSet.measure_id == measure_id)
        )
        drill = result.scalar_one_or_none()
        if drill is None:
            raise HTTPException(
                status_code=500,
                detail="Drill-through set could not be auto-created for this measure.",
            )
    return measure, drill


async def _resolve_effective_source_table(
    db, measure: Measure, drill: DrillThroughSet
) -> ModelTable | None:
    if drill.source_table_id is not None:
        return await db.get(ModelTable, drill.source_table_id)
    if measure.source_column_id is not None:
        col = await db.get(ModelColumn, measure.source_column_id)
        if col is not None and col.model_table_id is not None:
            return await db.get(ModelTable, col.model_table_id)
    if measure.user_defined_attribute_id is not None:
        uda = await db.get(UserDefinedAttribute, measure.user_defined_attribute_id)
        if uda is not None:
            return await db.get(ModelTable, uda.table_id)
    return None


async def _validate_detail_columns(
    db, detail_columns: list[UUID], effective_table: ModelTable | None
) -> None:
    if not detail_columns:
        return
    if effective_table is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_NO_SOURCE_TABLE",
                "message": "Cannot validate detail_columns — measure has no resolvable source table.",
            },
        )
    result = await db.execute(
        select(ModelColumn.id).where(
            ModelColumn.id.in_(detail_columns),
            ModelColumn.model_table_id == effective_table.id,
        )
    )
    found = {row for row in result.scalars().all()}
    missing = [cid for cid in detail_columns if cid not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_DETAIL_COLUMN_OFF_TABLE",
                "message": (
                    "detail_columns must all reference columns on the effective "
                    f"source table '{effective_table.physical_name}'."
                ),
                "invalid_ids": [str(cid) for cid in missing],
            },
        )


async def _validate_joined_dimensions(
    db, dimension_ids: list[UUID], model_id: UUID, fact_table: ModelTable | None
) -> None:
    if not dimension_ids:
        return
    result = await db.execute(
        select(Dimension).where(
            Dimension.id.in_(dimension_ids),
            Dimension.model_id == model_id,
        )
    )
    dims = list(result.scalars().all())
    found_ids = {d.id for d in dims}
    missing = [cid for cid in dimension_ids if cid not in found_ids]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_DIMENSION_NOT_IN_MODEL",
                "message": "joined_dimension_ids must all belong to this model.",
                "invalid_ids": [str(cid) for cid in missing],
            },
        )
    if fact_table is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_NO_SOURCE_TABLE",
                "message": "Cannot validate joined_dimension_ids — measure has no resolvable source table.",
            },
        )
    # Reachability check: every joined dimension's base table must have a Join
    # edge connecting it (directly, 1 hop) to the fact table. Phase 8.A.3 keeps
    # this strict to mirror the drill builder's emitted SQL. Multi-hop paths
    # land with 8.A.4.
    dim_table_ids: dict[UUID, UUID | None] = {}
    for d in dims:
        col = await db.get(ModelColumn, d.source_column_id) if d.source_column_id else None
        dim_table_ids[d.id] = col.model_table_id if col is not None else None
    join_rows = await db.execute(
        select(Join.left_table_id, Join.right_table_id).where(Join.model_id == model_id)
    )
    edges: set[frozenset] = {frozenset((l, r)) for l, r in join_rows.all() if l and r}
    unreachable: list[UUID] = []
    for dim_id, tbl_id in dim_table_ids.items():
        if tbl_id is None:
            unreachable.append(dim_id)
            continue
        if tbl_id == fact_table.id:
            continue
        if frozenset((tbl_id, fact_table.id)) not in edges:
            unreachable.append(dim_id)
    if unreachable:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_DIMENSION_NO_JOIN",
                "message": (
                    "joined_dimension_ids include a dimension whose base table "
                    "has no direct join to the fact table."
                ),
                "invalid_ids": [str(cid) for cid in unreachable],
            },
        )


@router.get(
    "/{measure_id}/drill-through-set",
    response_model=DrillThroughSetResponse,
)
async def get_drill_through_set(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DrillThroughSetResponse:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # F-015-20: read-only GET — synthesize the default if no row exists,
        # never write under the viewer role.
        _, drill = await _load_drill_through_set_or_404(
            db, measure_id, model_id, create_if_missing=False
        )
        return DrillThroughSetResponse.model_validate(drill)


@router.get(
    "/{measure_id}/drill-through-set/enriched",
    response_model=DrillThroughSetEnrichedResponse,
)
async def get_drill_through_set_enriched(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DrillThroughSetEnrichedResponse:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # F-015-20: read-only GET — no write under the viewer role.
        _, drill = await _load_drill_through_set_or_404(
            db, measure_id, model_id, create_if_missing=False
        )
        resolved_cols: list[DrillThroughSetColumnInfo] = []
        if drill.detail_columns:
            for col_id in drill.detail_columns:
                col = await db.get(ModelColumn, col_id)
                if col:
                    resolved_cols.append(DrillThroughSetColumnInfo(
                        id=col.id,
                        name=col.column_name,
                        display_name=col.display_name,
                        source_type="column",
                    ))
        return DrillThroughSetEnrichedResponse(
            id=drill.id,
            measure_id=drill.measure_id,
            source_table_id=drill.source_table_id,
            detail_columns=resolved_cols,
            joined_dimension_ids=drill.joined_dimension_ids,
            row_limit_override=drill.row_limit_override,
            source_join_path=drill.source_join_path,
        )


@router.patch(
    "/{measure_id}/drill-through-set",
    response_model=DrillThroughSetResponse,
    dependencies=[require_role("modeler")],
)
async def update_drill_through_set(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    body: DrillThroughSetUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DrillThroughSetResponse:
    """Curate the drill-through payload for one measure.

    Phase 8.A.1 + 8.A.2 + 8.A.3: detail-column allow-list, joined-dimension
    expansion, and optional source-table override. All fields are optional;
    null means "apply the implicit default".
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        measure, drill = await _load_drill_through_set_or_404(db, measure_id, model_id)
        updates = body.model_dump(exclude_unset=True)

        if "source_table_id" in updates and updates["source_table_id"] is not None:
            new_table = await db.get(ModelTable, updates["source_table_id"])
            if new_table is None or new_table.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "DRILL_SOURCE_TABLE_NOT_IN_MODEL",
                        "message": "source_table_id must reference a table in this model.",
                    },
                )
            drill.source_table_id = new_table.id
        elif "source_table_id" in updates:
            drill.source_table_id = None

        # Re-resolve the effective source table after the potential override so
        # subsequent validation uses the caller's intended table.
        effective_table = await _resolve_effective_source_table(db, measure, drill)

        if "detail_columns" in updates:
            new_list = updates["detail_columns"] or []
            await _validate_detail_columns(db, list(new_list), effective_table)
            drill.detail_columns = [str(cid) for cid in new_list] if new_list else None

        if "joined_dimension_ids" in updates:
            new_list = updates["joined_dimension_ids"] or []
            await _validate_joined_dimensions(
                db, list(new_list), model_id, effective_table
            )
            drill.joined_dimension_ids = (
                [str(cid) for cid in new_list] if new_list else None
            )

        if "row_limit_override" in updates:
            val = updates["row_limit_override"]
            if val is not None and val <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="row_limit_override must be a positive integer or null.",
                )
            drill.row_limit_override = val

        if "source_join_path" in updates:
            path = updates["source_join_path"] or []
            if path:
                await _validate_join_path(
                    db, list(path), model_id, effective_table
                )
                drill.source_join_path = [str(jid) for jid in path]
            else:
                drill.source_join_path = None

        # F-019-09: now that the override has runtime effect (F-019-01), an
        # override source table with no chosen join path is only safe when
        # exactly one path exists. With zero or several candidate paths the
        # drill cannot deterministically join back to the fact, so the save is
        # rejected — matching the documented DRILL_OVERRIDE_NO_JOIN_PATH rule
        # and the editor's client-side guard.
        if drill.source_table_id is not None and not drill.source_join_path:
            intrinsic = await _resolve_intrinsic_fact_table(db, measure)
            if intrinsic is not None and drill.source_table_id != intrinsic.id:
                paths = await _enumerate_join_paths(
                    db, model_id, drill.source_table_id, intrinsic.id
                )
                if len(paths) != 1:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "DRILL_OVERRIDE_NO_JOIN_PATH",
                            "message": (
                                "An override source table needs an explicit "
                                "join path unless exactly one path exists "
                                f"({len(paths)} found). Pick a join path."
                            ),
                        },
                    )

        await db.commit()
        await db.refresh(drill)
        return DrillThroughSetResponse.model_validate(drill)


@router.delete(
    "/{measure_id}/drill-through-set",
    response_model=DrillThroughSetResponse,
    dependencies=[require_role("modeler")],
)
async def reset_drill_through_set(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> DrillThroughSetResponse:
    """Clear curation — the row itself is retained so the measure keeps its
    implicit drill-through. Curated fields reset to null (defaults resume).
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        _, drill = await _load_drill_through_set_or_404(db, measure_id, model_id)
        drill.source_table_id = None
        drill.detail_columns = None
        drill.joined_dimension_ids = None
        drill.row_limit_override = None
        drill.source_join_path = None
        await db.commit()
        await db.refresh(drill)
        return DrillThroughSetResponse.model_validate(drill)


# ---------------------------------------------------------------------------
# Join-path resolver (Phase 8.A.4)
# ---------------------------------------------------------------------------


async def _enumerate_join_paths(
    db, model_id: UUID, source_table_id: UUID, fact_table_id: UUID, max_hops: int = 4
) -> list[list[Join]]:
    """BFS over the model's Join graph from ``source_table_id`` to
    ``fact_table_id``. Returns every simple path (no repeated tables)
    bounded at ``max_hops`` hops.
    """
    if source_table_id == fact_table_id:
        return [[]]
    rows = await db.execute(select(Join).where(Join.model_id == model_id))
    joins = list(rows.scalars().all())
    # Adjacency: table_id -> list[(neighbour_table_id, Join)]
    adj: dict[UUID, list[tuple[UUID, Join]]] = {}
    for j in joins:
        adj.setdefault(j.left_table_id, []).append((j.right_table_id, j))
        adj.setdefault(j.right_table_id, []).append((j.left_table_id, j))

    paths: list[list[Join]] = []

    def _dfs(node: UUID, visited_tables: set[UUID], visited_joins: set[UUID], trail: list[Join]):
        if len(trail) > max_hops:
            return
        if node == fact_table_id and trail:
            paths.append(list(trail))
            return
        for nxt, edge in adj.get(node, []):
            if nxt in visited_tables or edge.id in visited_joins:
                continue
            trail.append(edge)
            visited_tables.add(nxt)
            visited_joins.add(edge.id)
            _dfs(nxt, visited_tables, visited_joins, trail)
            trail.pop()
            visited_tables.discard(nxt)
            visited_joins.discard(edge.id)

    _dfs(source_table_id, {source_table_id}, set(), [])
    return paths


def _cardinality_hint_from_path(path: list[Join], start_table_id: UUID) -> str:
    """Best-effort summary across the hops based on Join.join_type."""
    types: set[str] = set()
    cursor = start_table_id
    for j in path:
        if cursor == j.left_table_id:
            types.add(j.join_type)
            cursor = j.right_table_id
        else:
            # Reverse direction; many_to_one becomes one_to_many.
            inverted = {
                "many_to_one": "one_to_many",
                "one_to_many": "many_to_one",
            }.get(j.join_type, j.join_type)
            types.add(inverted)
            cursor = j.left_table_id
    if types == {"many_to_one"}:
        return "many-to-one"
    if types == {"one_to_many"}:
        return "one-to-many"
    return "mixed"


@router.get(
    "/{measure_id}/drill-through-set/join-paths",
    response_model=DrillJoinPathsResponse,
)
async def list_drill_join_paths(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    source_table_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DrillJoinPathsResponse:
    """Enumerate join paths from ``source_table_id`` back to the fact's base table.

    Used by the editor's source-table-override picker (Phase 8.A.4).
    """
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-2570 (residual F-015-20): GET must not write. Use a transient
        # default drill-through set if none exists — never auto-create + commit
        # a DrillThroughSet under the read-only viewer role.
        measure, _drill = await _load_drill_through_set_or_404(
            db, measure_id, model_id, create_if_missing=False
        )
        # Fact table = the measure's intrinsic source table (NOT the override),
        # so the picker can show paths even when the override hasn't been saved yet.
        fact_table = await _resolve_intrinsic_fact_table(db, measure)
        if fact_table is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "DRILL_NO_SOURCE_TABLE",
                    "message": "Cannot enumerate join paths — the measure has no resolvable fact table.",
                },
            )
        # Verify the override candidate belongs to the model.
        override_table = await db.get(ModelTable, source_table_id)
        if override_table is None or override_table.model_id != model_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "DRILL_SOURCE_TABLE_NOT_IN_MODEL",
                    "message": "source_table_id must reference a table in this model.",
                },
            )
        raw_paths = await _enumerate_join_paths(
            db, model_id, source_table_id, fact_table.id
        )
        # Bug-2570: read-only GET — no commit on this path.
        out: list[DrillJoinPath] = []
        for path in raw_paths:
            hops = [
                DrillJoinPathHop(
                    join_id=j.id,
                    left_table_id=j.left_table_id,
                    right_table_id=j.right_table_id,
                )
                for j in path
            ]
            out.append(
                DrillJoinPath(
                    hops=hops,
                    cardinality_hint=_cardinality_hint_from_path(path, source_table_id),
                )
            )
        return DrillJoinPathsResponse(paths=out)


async def _resolve_intrinsic_fact_table(db, measure: Measure) -> ModelTable | None:
    """Resolve the measure's intrinsic fact table (ignoring any override)."""
    if measure.source_column_id is not None:
        col = await db.get(ModelColumn, measure.source_column_id)
        if col is not None and col.model_table_id is not None:
            return await db.get(ModelTable, col.model_table_id)
    if measure.user_defined_attribute_id is not None:
        uda = await db.get(UserDefinedAttribute, measure.user_defined_attribute_id)
        if uda is not None:
            return await db.get(ModelTable, uda.table_id)
    return None


async def _validate_join_path(
    db, join_ids: list[UUID], model_id: UUID, effective_table: ModelTable | None
) -> None:
    """Each id must be a Join in this model, and the chained edges must
    connect ``effective_table`` (the override / fact) end-to-end. The
    chain is undirected — the validator just checks that each consecutive
    pair shares a table id.
    """
    if effective_table is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_NO_SOURCE_TABLE",
                "message": "Cannot validate source_join_path — no resolvable source table.",
            },
        )
    rows = await db.execute(
        select(Join).where(Join.id.in_(join_ids), Join.model_id == model_id)
    )
    joins = {j.id: j for j in rows.scalars().all()}
    missing = [jid for jid in join_ids if jid not in joins]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_OVERRIDE_NO_JOIN_PATH",
                "message": "source_join_path references unknown joins.",
                "invalid_ids": [str(j) for j in missing],
            },
        )
    # Walk the chain starting at the override source table.
    cursor = effective_table.id
    for jid in join_ids:
        j = joins[jid]
        if j.left_table_id == cursor:
            cursor = j.right_table_id
        elif j.right_table_id == cursor:
            cursor = j.left_table_id
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "DRILL_OVERRIDE_NO_JOIN_PATH",
                    "message": (
                        "source_join_path is not a contiguous chain starting "
                        "at the source table."
                    ),
                },
            )
