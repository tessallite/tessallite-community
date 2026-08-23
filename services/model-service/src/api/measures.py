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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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
    UserDefinedAttributeColumnRef,
)
from shared.db.session import get_tenant_db
from shared.schemas.measure_formats import (
    SEMI_ADDITIVE_INELIGIBLE_FAMILIES,
    TIME_VARIANT_FAMILY,
    TIME_VARIANT_REQUIRED_UNIT,
    TIME_VARIANTS_NEEDING_CALENDAR,
    admissible_variant_kinds,
    is_valid_format,
    is_window_variant,
    variant_display_label,
)
from shared.connector_qualify import is_date_anchor_type
from shared.schemas.domains.dimensions_measures import derive_is_additive
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
    MeasureRenameImpactItem,
    MeasureRenameImpactResponse,
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
from shared.security.restricted_column_closure import (
    ClosureContext,
    compute_transitive_hidden_measure_names,
    normalise_id_set,
    object_touches_restricted,
)
from shared.semantic.join_keyword import edge_cardinality
from shared.semantic.redundant_partner import compute_redundant_partners
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import require_role
from src.api._column_helpers import resolve_column
from src.api._persona_scope import (
    get_restricted_column_ids,
    parse_allowed_ids,
    resolve_effective_persona,
)
from src.api._scope import (
    ensure_model_in_project,
    ensure_ref_in_model,
    glossary_text_for_target as _glossary_text_for_target,
    glossary_texts_for_targets as _glossary_texts_for_targets,
    purge_entity_soft_references,
)
from src.measure_rename import (
    UnsafeMeasureRename,
    plan_measure_renames,
    propagate_measure_renames,
)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/measures", tags=["measures"]
)


def _restricted_uda_ids(
    restricted_cols: set[UUID],
    uda_col_map: dict[UUID, set[UUID]] | None,
) -> set[str]:
    """UDA ids whose referenced columns intersect the restricted set (as str).

    The shared closure keys UDA restriction by UDA id, not by the per-UDA
    column map, so collapse ``uda_col_map`` (UDA id -> column ids) here.
    """
    if not uda_col_map or not restricted_cols:
        return set()
    restricted = set(restricted_cols)
    return {
        str(uda_id)
        for uda_id, cols in uda_col_map.items()
        if cols & restricted
    }


def _measure_closure_context(
    restricted_cols: set[UUID],
    uda_col_map: dict[UUID, set[UUID]] | None,
    all_measures=None,
) -> ClosureContext:
    """Build the shared ``ClosureContext`` for the model-service measures gate.

    Bug-7608 / Bug-7045: the closure algorithm lives in
    ``shared.security.restricted_column_closure`` so this catalogue-hide
    predicate and the query-router serving gate stay in lockstep. When
    ``all_measures`` is supplied, the transitive calc-measure / variant lookups
    are populated (needed for the transitive closure); otherwise a per-object
    direct check needs only the UDA restriction set.
    """
    ctx = ClosureContext(
        restricted_uda_ids=_restricted_uda_ids(restricted_cols, uda_col_map),
    )
    if all_measures:
        for m in all_measures:
            ctx.measures_by_id[str(m.id)] = m
            ctx.measures_by_name[m.name] = m
    return ctx


def _compute_transitive_hidden_names(
    all_measures,
    restricted_cols: set[UUID],
    uda_col_map: dict[UUID, set[UUID]] | None = None,
) -> set[str]:
    """Bug-6896: full transitive closure of CLS-hidden measure names.

    A measure is hidden if it directly touches a restricted column OR if its
    expression references any measure that is (transitively) hidden. Bug-7608 /
    Bug-7045: the fixed-point algorithm now lives in the shared closure module
    (``compute_transitive_hidden_measure_names``) so both services derive the
    hidden set from ONE algorithm. Bug-7606: ``uda_col_map`` is folded into the
    shared context's UDA restriction set.
    """
    ctx = _measure_closure_context(restricted_cols, uda_col_map, all_measures)
    return compute_transitive_hidden_measure_names(
        all_measures, normalise_id_set(restricted_cols), ctx,
    )


async def _load_uda_column_map(
    db, model_id: UUID,
) -> dict[UUID, set[UUID]]:
    """Load a mapping of UDA id -> set of referenced column ids for the model.

    Bug-7606: UDA-backed measures/dimensions reference physical columns
    through ``user_defined_attribute_column_refs``. CLS metadata hiding must
    check these transitive column references, not just ``source_column_id``.
    Pre-loading avoids N+1 queries in list endpoints and transitive closure.
    """
    rows = await db.execute(
        select(
            UserDefinedAttributeColumnRef.attribute_id,
            UserDefinedAttributeColumnRef.column_id,
        ).join(
            UserDefinedAttribute,
            UserDefinedAttributeColumnRef.attribute_id == UserDefinedAttribute.id,
        ).where(UserDefinedAttribute.model_id == model_id)
    )
    mapping: dict[UUID, set[UUID]] = {}
    for uda_id, col_id in rows.all():
        mapping.setdefault(uda_id, set()).add(col_id)
    return mapping


def _measure_touches_restricted_column(
    measure,
    restricted_cols: set[UUID],
    uda_col_map: dict[UUID, set[UUID]] | None = None,
) -> bool:
    """True when a measure is backed by a CLS-restricted column.

    Fail-closed (Bug-6141): a measure whose source column is restricted for the
    persona must be hidden entirely, because ``_build_response`` echoes the
    column name (``source_column_name``) and would otherwise leak a restricted
    column name on the measures surface — the same invariant already enforced
    for dimensions. Measures bind to a single physical column (no separate
    display column), so only ``source_column_id`` is checked.

    Bug-7606: UDA-backed measures have ``source_column_id=None`` but reference
    physical columns through their UDA expression column refs. If ANY of those
    columns is CLS-restricted, the measure must be hidden. The caller must
    pre-load ``uda_col_map`` via ``_load_uda_column_map`` when restricting.

    Bug-7608 / Bug-7045: the direct-membership + UDA closure now delegates to the
    shared ``object_touches_restricted`` so both services apply ONE algorithm.
    (Transitive calc-measure hiding is handled separately by
    ``_compute_transitive_hidden_names``; this per-object check is the direct
    seed, so no measure map is supplied here.)
    """
    if not restricted_cols:
        return False
    ctx = _measure_closure_context(restricted_cols, uda_col_map)
    return object_touches_restricted(
        measure, normalise_id_set(restricted_cols), ctx,
    )


async def _ensure_measure_visible_to_persona(
    db,
    *,
    measure: Measure,
    current_user: CurrentUser,
    model_id: UUID,
    persona_id: UUID | None,
) -> set[UUID]:
    """Fail-closed 404 when *measure* is outside the effective persona's scope.

    Bug-6141/Bug-6614: every viewer-reachable measure-metadata surface must apply
    the SAME visibility invariant as get_measure — the persona
    ``included_measure_ids`` allow-list AND the CLS restricted-column check — so a
    hidden measure never discloses its existence, config, variant names, or join
    topology on a sibling endpoint. Returns the persona's restricted column-id
    set (empty when there is no persona / no restriction) for callers that go on
    to resolve column names.
    """
    persona = await resolve_effective_persona(
        db, current_user=current_user, model_id=model_id,
        requested_persona_id=persona_id,
    )
    restricted_cols: set[UUID] = set()
    if persona:
        allowed = parse_allowed_ids(persona.included_measure_ids)
        if allowed is not None and measure.id not in allowed:
            raise HTTPException(status_code=404, detail="Measure not found")
        restricted_cols = await get_restricted_column_ids(db, persona.id)
        # Bug-7606: load UDA column refs so UDA-backed measures are
        # CLS-checked against the columns their expression references.
        uda_col_map = await _load_uda_column_map(db, model_id) if restricted_cols else None
        if _measure_touches_restricted_column(measure, restricted_cols, uda_col_map):
            raise HTTPException(status_code=404, detail="Measure not found")
        # Bug-6617 / Bug-6896: lineage-transitive CLS with fixed-point
        # closure.  A calculated measure whose expression references any
        # transitively-hidden measure must also be hidden, regardless of
        # chain depth (A hidden -> B refs A -> C refs B -> all hidden).
        if restricted_cols and getattr(measure, "measure_type", None) == "calculated":
            all_measures = (await db.execute(
                select(Measure).where(Measure.model_id == model_id)
            )).scalars().all()
            hidden_names = _compute_transitive_hidden_names(
                all_measures, restricted_cols, uda_col_map,
            )
            if measure.name in hidden_names:
                raise HTTPException(status_code=404, detail="Measure not found")
    return restricted_cols


async def _build_response(
    db,
    measure: Measure,
    redundant_partners: dict | None = None,
    *,
    include_eligible: bool = False,
    glossary_texts: dict[UUID, str] | None = None,
    restricted_cols: set[UUID] | None = None,
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
        # Bug-9392: no direct attachment falls back to the physical column's
        # term, matching the deployed snapshot (serialiser) exactly.
        glossary_text = await _glossary_text_for_target(
            db, measure.model_id, "measure", measure.id,
            fallback_column_id=measure.source_column_id,
        )
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
        # Bug-6141 (fail-closed): the redundant-partner hint echoes the partner
        # column's NAME (and a reason string built from it). If that partner
        # column is CLS-restricted for the effective persona, suppress the hint
        # entirely so a restricted column name does not leak on an otherwise
        # visible measure (mirrors the dimensions surface).
        partner_restricted = (
            restricted_cols is not None
            and getattr(hint, "partner_column_id", None) in restricted_cols
        )
        if not partner_restricted:
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
        resolved_calendar_id=getattr(measure, "resolved_calendar_id", None),
        resolved_date_col_id=getattr(measure, "resolved_date_col_id", None),
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


async def _ensure_calendar_alias_table(
    db,
    *,
    calendar_model_table_id: UUID | None,
    model_id: UUID,
    project_id: UUID,
) -> ModelTable | None:
    """Prove a body-supplied ``calendar_model_table_id`` names a calendar alias
    ModelTable owned by the path project+model.

    Single home for a check that used to be written out twice — once inside
    ``_resolve_calendar_alias_for_measure`` (create) and once inline in
    ``update_measure`` — each with its own ``db.get`` + ``!= model_id``
    comparison. That shape is the "unscoped body foreign key" defect class's
    hand-rolled cousin, so the ownership half now delegates to the canonical
    primitive in ``_scope`` and only the calendar-alias STATE check (a
    statement about the row, not about who owns it) stays here.

    Returns ``None`` when no id was supplied; otherwise the resolved row.
    """
    if calendar_model_table_id is None:
        return None
    alias_table = await ensure_ref_in_model(
        db,
        ModelTable,
        ref_id=calendar_model_table_id,
        model_id=model_id,
        project_id=project_id,
        field_name="calendar_model_table_id",
        noun="a table in this model",
        error_code="CALENDAR_ALIAS_TABLE_NOT_IN_MODEL",
    )
    if alias_table.calendar_table_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error_code": "NOT_A_CALENDAR_ALIAS",
                "field": "calendar_model_table_id",
                "ids": [str(calendar_model_table_id)],
                "message": (
                    "ModelTable referenced by calendar_model_table_id is not a "
                    "calendar alias (its calendar_table_id is NULL)."
                ),
            },
        )
    return alias_table


async def _resolve_calendar_alias_for_measure(
    db,
    *,
    model_id: UUID,
    project_id: UUID,
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

    Both body references (``variant_of_measure_id`` and
    ``calendar_model_table_id``) are proven through ``_scope``'s canonical
    body-FK primitive. This function previously carried its own
    ``db.get(...) ... != model_id`` pair — one of the four hand-rolled
    re-inventions the primitive family was built to retire. ``project_id``
    must come from the URL path, never the body.
    """
    if variant_kind is not None:
        if variant_of_measure_id is None:
            return None
        base = await ensure_ref_in_model(
            db,
            Measure,
            ref_id=variant_of_measure_id,
            model_id=model_id,
            project_id=project_id,
            field_name="variant_of_measure_id",
            noun="a base measure in this model",
            error_code="VARIANT_BASE_NOT_IN_MODEL",
        )
        return base.calendar_model_table_id

    alias_table = await _ensure_calendar_alias_table(
        db,
        calendar_model_table_id=calendar_model_table_id,
        model_id=model_id,
        project_id=project_id,
    )
    return alias_table.id if alias_table is not None else None


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

    kinds = admissible_variant_kinds(
        level_units=units,
        level_calcs=calcs,
        has_calendar_rules=has_calendar_rules,
    )

    # Bug-6222: mirror the semi-additive admission gate so the
    # eligible_variant_kinds response field agrees with /available-variants
    # and the create endpoint.
    semi_additive = getattr(measure, "semi_additive_behavior", None)
    if semi_additive:
        kinds = [
            k for k in kinds
            if TIME_VARIANT_FAMILY.get(k) not in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
        ]

    return kinds


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

    # Bug-7183: a calculated measure that references a semi-additive measure
    # (e.g. last_non_empty balance) is semantically invalid. The query-router
    # expands calc expressions via the raw column wrapped in the default agg,
    # but semi-additive measures require specialised window aggregation
    # (LAST_VALUE / FIRST_VALUE partitioned by account). Wrapping a semi-
    # additive column in SUM/COUNT produces wrong numbers. Reject at save
    # time with a clear message rather than accept-and-compute-wrong.
    semi_additive_refs = [
        m.name
        for m in name_to_measure.values()
        if getattr(m, "semi_additive_behavior", None) is not None
    ]
    if semi_additive_refs:
        raise HTTPException(
            status_code=400,
            detail=(
                "Calculated measures cannot reference semi-additive measures "
                "(e.g. last_non_empty / first_non_empty balances). Semi-additive "
                "measures require specialised aggregation that cannot be "
                "preserved inside a calculated expression: "
                f"{', '.join(sorted(set(semi_additive_refs)))}"
            ),
        )

    referenced_ids = [name_to_measure[n].id for n in referenced_names]

    # Cycle detection — build the dependency graph of all calculated
    # measures in the model, include the candidate, and reject if a cycle
    # appears. With single-pass enforced above this cannot trigger today,
    # but the check is retained so multi-pass (v2) inherits the guard.
    await _check_cycle(db, model_id, self_measure_id, referenced_ids)

    return parsed, referenced_ids


async def _check_cross_table_safety(
    db, model_id: UUID, referenced_ids: list[UUID],
) -> None:
    """Bug-7180: in per_row_then_aggregate mode, all referenced measures
    must share the same source table to avoid double-counting.

    Resolves table ownership via both source_column_id (physical column
    measures) and user_defined_attribute_id (UDA measures). Both paths
    produce a model_table_id; if the union spans multiple tables, the
    per-row expression would operate on a join-inflated row set.
    """
    if not referenced_ids:
        return
    result = await db.execute(
        select(Measure).where(Measure.id.in_(referenced_ids))
    )
    measures = list(result.scalars().all())
    table_ids: set[UUID] = set()
    for m in measures:
        # Physical column path
        col_id = getattr(m, "source_column_id", None)
        if col_id is not None:
            col = await db.get(ModelColumn, col_id)
            if col is not None:
                table_ids.add(col.model_table_id)
                continue
        # UDA path (codex F2): UDA-backed measures have no source_column_id
        # but resolve to a table via UserDefinedAttribute.table_id.
        uda_id = getattr(m, "user_defined_attribute_id", None)
        if uda_id is not None:
            uda = await db.get(UserDefinedAttribute, uda_id)
            if uda is not None and getattr(uda, "table_id", None) is not None:
                table_ids.add(uda.table_id)
    if len(table_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "per_row_then_aggregate calculated measures require all "
                "referenced measures to be on the same source table. "
                "Use 'expression_as_written' mode instead."
            ),
        )


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
    # Bug-6621(c): enforce model scope so a viewer scoped to model A
    # cannot probe measure names in model B via expression validation.
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        if body.user_defined_attribute_id and (body.source_table_id or body.source_column_name):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        if not is_valid_format(body.format):
            raise HTTPException(status_code=400, detail=f"Unknown format token: {body.format}")

        # Body-supplied foreign keys that land on the persisted row without any
        # other owner check. ``require_role`` proved the caller owns the PATH
        # project; it never inspects the body, so without these a modeller in
        # project A could point a measure's semi-additive account column or
        # window ordering column at project B's column id. Guarded here, BEFORE
        # any branch, because a guard placed on one branch is exactly how the
        # gap reappears when a new branch is added.
        await ensure_ref_in_model(
            db,
            ModelColumn,
            ref_id=body.semi_additive_account_column_id,
            model_id=model_id,
            project_id=project_id,
            field_name="semi_additive_account_column_id",
            noun="a column in this model",
        )
        await ensure_ref_in_model(
            db,
            ModelColumn,
            ref_id=body.date_dimension_column_id,
            model_id=model_id,
            project_id=project_id,
            field_name="date_dimension_column_id",
            noun="a column in this model",
        )

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
            _parsed, _ref_ids = await _resolve_calculated_expression(
                db, model_id, body.expression or "", self_measure_id=None,
            )
            # Bug-7180: cross-table check for per_row_then_aggregate.
            if body.calc_agg_mode == "per_row_then_aggregate":
                await _check_cross_table_safety(db, model_id, _ref_ids)
        elif body.variant_kind is not None:
            # F-015-11: the variant's source/agg/type snapshot is resolved
            # server-side from the base measure AFTER the eligibility gate
            # below (so an ineligible variant still returns 422 first). Client-
            # supplied source fields are ignored — the API is the authority for
            # the snapshot, not the caller.
            pass
        else:
            if body.source_table_id and body.source_column_name:
                col = await resolve_column(
                    db,
                    body.source_table_id,
                    body.source_column_name,
                    body.data_type,
                    model_id=model_id,
                    project_id=project_id,
                )
                source_column_id = col.id
            elif user_defined_attribute_id:
                uda = await db.get(UserDefinedAttribute, user_defined_attribute_id)
                if uda is None or uda.model_id != model_id:
                    raise HTTPException(status_code=404, detail="User-defined attribute not found")

        calendar_model_table_id = await _resolve_calendar_alias_for_measure(
            db,
            model_id=model_id,
            project_id=project_id,
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
        # ...and the measure it names must live IN that source model. The pair
        # is set-or-null together (MeasureCreate._check_cross_model_ref), so
        # proving the model is in-project and then proving the measure is in
        # that model closes the whole chain. The model_id passed here comes
        # from the body BY DESIGN — this reference is deliberately cross-model
        # — which is safe only because the line above has just proven that body
        # model belongs to the PATH project.
        await ensure_ref_in_model(
            db,
            Measure,
            ref_id=body.cross_model_source_measure_id,
            model_id=body.cross_model_source_model_id,
            project_id=project_id,
            field_name="cross_model_source_measure_id",
            noun="a measure in the referenced source model",
        )

        # Bug-6224: a supplied hierarchy_id must reference a hierarchy owned by
        # THIS model. Without the check a measure can pin to a foreign or
        # non-existent hierarchy id, which later resolves calendar/variant
        # context against a hierarchy the model does not own (or silently against
        # nothing).
        await _validate_hierarchy_in_model(db, model_id, body.hierarchy_id)

        resolved_calendar_id: UUID | None = None
        resolved_date_col_id: UUID | None = None
        if body.variant_kind is not None:
            reason = await _check_variant_eligibility(
                db, model_id, body.variant_kind, body.variant_of_measure_id,
                override_hierarchy_id=body.hierarchy_id,
                window_date_col_id=body.date_dimension_column_id,
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


async def _validate_hierarchy_in_model(
    db, model_id: UUID, hierarchy_id: UUID | None
) -> None:
    """Bug-6224: ensure a supplied hierarchy_id belongs to this model.

    No-op when ``hierarchy_id`` is None (the field is optional). Raises 400 when
    the hierarchy does not exist or is owned by a different model.
    """
    if hierarchy_id is None:
        return
    hier = await db.get(HierarchyDefinition, hierarchy_id)
    if hier is None or hier.model_id != model_id:
        raise HTTPException(
            status_code=400,
            detail="hierarchy_id must reference a hierarchy in this model.",
        )


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
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
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
        # Bug-6141: fail-closed CLS on the measures surface. Hide any measure
        # backed by a restricted column and suppress restricted partner hints,
        # mirroring the dimensions list. The query-router blocks restricted
        # *values*; without this the *column name* leaked via the catalogue.
        restricted_cols: set[UUID] = set()
        uda_col_map: dict[UUID, set[UUID]] | None = None
        if persona:
            restricted_cols = await get_restricted_column_ids(db, persona.id)
            if restricted_cols:
                # Bug-7606: load UDA column refs so UDA-backed measures
                # are CLS-checked against the columns their expression
                # references — not just source_column_id.
                uda_col_map = await _load_uda_column_map(db, model_id)
                # Bug-6141 / Bug-6617 / Bug-6896: CLS hiding with
                # transitive fixed-point closure.  Fetch the full model
                # measure set (needed for graph reachability), compute the
                # complete hidden name set (direct + transitive), then
                # filter the visible list in one pass.
                all_measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                all_model_measures = all_measures_result.scalars().all()
                hidden_names = _compute_transitive_hidden_names(
                    all_model_measures, restricted_cols, uda_col_map,
                )
                if hidden_names:
                    measures = [
                        m for m in measures
                        if m.name not in hidden_names
                    ]
        glossary_texts = await _glossary_texts_for_targets(
            db, model_id, "measure", [m.id for m in measures],
            fallback_column_ids={m.id: m.source_column_id for m in measures},
        )
        out: list[MeasureResponse] = []
        for m in measures:
            out.append(
                await _build_response(
                    db, m, partners, glossary_texts=glossary_texts,
                    restricted_cols=restricted_cols,
                )
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
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        m = await db.get(Measure, measure_id)
        if m is None or m.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")
        restricted_cols: set[UUID] = set()
        if persona:
            allowed = parse_allowed_ids(persona.included_measure_ids)
            if allowed is not None and m.id not in allowed:
                raise HTTPException(status_code=404, detail="Measure not found")
            # Bug-6141: fail-closed CLS — a measure backed by a restricted
            # column is hidden entirely (its source_column_name would otherwise
            # leak), mirroring the single dimension GET.
            restricted_cols = await get_restricted_column_ids(db, persona.id)
            # Bug-7606: load UDA column refs so UDA-backed measures are
            # CLS-checked against the columns their expression references.
            uda_col_map = await _load_uda_column_map(db, model_id) if restricted_cols else None
            if _measure_touches_restricted_column(m, restricted_cols, uda_col_map):
                raise HTTPException(status_code=404, detail="Measure not found")
            # Bug-6896: transitive CLS — a calculated measure referencing
            # any transitively-hidden measure is also hidden.
            if restricted_cols and getattr(m, "measure_type", None) == "calculated":
                all_measures = (await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )).scalars().all()
                hidden_names = _compute_transitive_hidden_names(
                    all_measures, restricted_cols, uda_col_map,
                )
                if m.name in hidden_names:
                    raise HTTPException(status_code=404, detail="Measure not found")
        partners = await _load_redundant_partners(db, model_id)
        return await _build_response(
            db, m, partners, include_eligible=True,
            restricted_cols=restricted_cols,
        )


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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
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
                _parsed, _ref_ids = await _resolve_calculated_expression(
                    db, model_id, updates["expression"], self_measure_id=m.id,
                )
                # Bug-7180: cross-table check on update too.
                effective_mode = updates.get("calc_agg_mode", m.calc_agg_mode)
                if effective_mode == "per_row_then_aggregate":
                    await _check_cross_table_safety(db, model_id, _ref_ids)
            # Bug-7180 (R1 finding): when calc_agg_mode is changed to
            # per_row_then_aggregate WITHOUT updating the expression, the
            # existing expression's cross-table safety must still be checked.
            elif (
                "calc_agg_mode" in updates
                and updates["calc_agg_mode"] == "per_row_then_aggregate"
                and m.expression
            ):
                _parsed, _ref_ids = await _resolve_calculated_expression(
                    db, model_id, m.expression, self_measure_id=m.id,
                )
                await _check_cross_table_safety(db, model_id, _ref_ids)
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
            # Same guard as create, from the same helper — this block used to
            # carry its own copy of the db.get + model_id comparison.
            await _ensure_calendar_alias_table(
                db,
                calendar_model_table_id=new_cal,
                model_id=model_id,
                project_id=project_id,
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
                col = await resolve_column(
                    db, table_id, col_name,
                    model_id=model_id, project_id=project_id,
                )
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
        old_name = m.name
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
        # The same body foreign keys create guards, guarded again on the PATCH
        # path. ``setattr(m, k, v)`` below writes every remaining key of
        # ``updates`` straight onto the row, so a field that is only checked in
        # create_measure is fully re-openable through PATCH.
        if "semi_additive_account_column_id" in updates:
            await ensure_ref_in_model(
                db,
                ModelColumn,
                ref_id=updates["semi_additive_account_column_id"],
                model_id=model_id,
                project_id=project_id,
                field_name="semi_additive_account_column_id",
                noun="a column in this model",
            )
        if "date_dimension_column_id" in updates:
            await ensure_ref_in_model(
                db,
                ModelColumn,
                ref_id=updates["date_dimension_column_id"],
                model_id=model_id,
                project_id=project_id,
                field_name="date_dimension_column_id",
                noun="a column in this model",
            )
        if "cross_model_source_measure_id" in updates:
            # Scope is the MERGED source model — a PATCH may re-point the
            # measure without re-sending the model (or vice versa), so reading
            # only ``updates`` would validate against the wrong model. The
            # merged model id is either the value just proven in-project above,
            # or a value already proven when it was persisted.
            # ``MeasureUpdate._check_cross_model_ref_update`` pairs the two
            # fields inside one payload, so when the measure id is present and
            # non-null the model id is too; the persisted fallback is what keeps
            # this correct if that pairing is ever relaxed. A null measure id
            # short-circuits in the helper before the model id is read.
            effective_source_model_id = updates.get(
                "cross_model_source_model_id", m.cross_model_source_model_id
            )
            await ensure_ref_in_model(
                db,
                Measure,
                ref_id=updates["cross_model_source_measure_id"],
                model_id=effective_source_model_id,
                project_id=project_id,
                field_name="cross_model_source_measure_id",
                noun="a measure in the referenced source model",
            )
        # Bug-6224: a re-pointed hierarchy_id must also belong to this model.
        if "hierarchy_id" in updates:
            await _validate_hierarchy_in_model(db, model_id, updates["hierarchy_id"])
        if new_name and new_name != old_name:
            try:
                await propagate_measure_renames(
                    db, model_id, {m.id: (old_name, new_name)}
                )
            except UnsafeMeasureRename as exc:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=exc.detail_for(
                        current_user.email or current_user.user_id
                    ),
                ) from exc
        for k, v in updates.items():
            setattr(m, k, v)
        # Bug-8257: re-derive effective additivity AFTER the merge. A PATCH is
        # partial, so the schema validator cannot see the combination that
        # matters — a caller may flip ``default_agg`` to ``avg`` without
        # touching ``is_additive`` (leaving a persisted True on a measure that
        # can no longer be summed), or set ``is_additive: true`` on a measure
        # whose persisted agg is already non-additive. Deriving from the MERGED
        # row is the only place both halves are visible.
        #
        # Deep-review R3 finding 9: a False that this coercion DERIVED must
        # not be sticky. After create-time coercion an ``avg`` measure holds
        # a derived False; if the modeller later PATCHes default_agg to
        # ``sum`` without mentioning is_additive, passing that False through
        # as ``declared`` would keep the now-plain-sum measure permanently
        # un-totalable with no way back through the UI. Only a False the
        # CALLER supplied in this request is a declaration; otherwise let
        # the merged shape decide.
        _declared_additive = body.model_dump(exclude_unset=True).get(
            "is_additive"
        )
        m.is_additive = derive_is_additive(
            default_agg=m.default_agg,
            measure_type=m.measure_type,
            variant_kind=m.variant_kind,
            semi_additive_behavior=getattr(
                m, "semi_additive_behavior", None
            ),
            declared=_declared_additive,
        )
        raw = body.model_dump(exclude_unset=True)
        _CASCADE_TRIGGERS = {
            "source_table_id", "source_column_name",
            "user_defined_attribute_id", "default_agg",
            "is_additive", "data_type",
            # Bug-7618: semi_additive_behavior changes must cascade to
            # variant snapshots and re-check variant admission.
            "semi_additive_behavior", "semi_additive_account_column_id",
        }
        _SOURCE_TRIGGERS = {"source_table_id", "source_column_name", "user_defined_attribute_id"}
        if m.variant_kind is None and _CASCADE_TRIGGERS & raw.keys():
            source_changed = bool(_SOURCE_TRIGGERS & raw.keys())
            cascade_vals: dict = {}
            if source_changed:
                cascade_vals["source_column_id"] = m.source_column_id
                cascade_vals["user_defined_attribute_id"] = m.user_defined_attribute_id
            for k in ("default_agg", "is_additive", "data_type",
                       "semi_additive_behavior", "semi_additive_account_column_id"):
                if k in raw:
                    cascade_vals[k] = getattr(m, k)
            # Bug-8257: the cascade targets are VARIANT rows, and a variant is
            # non-additive across periods by construction (rule 2 of
            # ``derive_is_additive``) — stacking PY/YTD/trailing values across
            # periods double-counts even when the base aggregation is a plain
            # sum. Copying the base's flag verbatim would write a True onto
            # every variant of an additive base. Derive with the variant's own
            # nature instead. Also fires when only ``default_agg`` cascaded, so
            # the variant's flag cannot be left describing the old aggregation.
            if "is_additive" in cascade_vals or "default_agg" in cascade_vals:
                cascade_vals["is_additive"] = derive_is_additive(
                    default_agg=m.default_agg,
                    measure_type=m.measure_type,
                    variant_kind="__variant__",
                    # Deep-review R6 finding 6: passed deliberately, not
                    # because it can change the answer -- ``variant_kind``
                    # already forces False on its own, so this argument is
                    # inert TODAY. It is here so that every call site of
                    # ``derive_is_additive`` supplies the full shape: a
                    # future rule ordered BEFORE the variant rule would
                    # otherwise silently read None here and this cascade
                    # would drift from the other three producers.
                    semi_additive_behavior=getattr(
                        m, "semi_additive_behavior", None
                    ),
                    declared=m.is_additive,
                )
            if cascade_vals:
                await db.execute(
                    Measure.__table__.update()
                    .where(Measure.variant_of_measure_id == m.id)
                    .values(**cascade_vals)
                )
        # Bug-7618: when semi_additive_behavior changes on a base measure,
        # re-run variant admission for every existing variant. Variants in
        # the SEMI_ADDITIVE_INELIGIBLE_FAMILIES set (cumulation / window)
        # become invalid when the base is now semi-additive, and variants
        # that were invalid solely because of a previous semi-additive
        # restriction are re-admitted when the behavior is cleared.
        if m.variant_kind is None and "semi_additive_behavior" in raw:
            variant_rows = await db.execute(
                select(Measure).where(Measure.variant_of_measure_id == m.id)
            )
            for v in variant_rows.scalars().all():
                if v.variant_kind is None:
                    continue
                reason = await _check_variant_eligibility(
                    db, model_id, v.variant_kind, m.id,
                    override_hierarchy_id=getattr(v, "hierarchy_id", None),
                    # F-015-02 (Opus review): pass the variant's own selected
                    # window date column so a window variant anchored on a
                    # column outside the base's table is not falsely flagged
                    # "no date column" during this semi-additive re-validation.
                    window_date_col_id=getattr(v, "date_dimension_column_id", None),
                )
                if reason:
                    v.is_invalid = True
                    v.invalid_reason = reason
                else:
                    # Only clear invalidity that was set by a previous
                    # semi-additive admission check, not other causes.
                    if getattr(v, "is_invalid", False) and getattr(v, "invalid_reason", None) and (
                        "semi-additive" in (v.invalid_reason or "").lower()
                        or "cumulation" in (v.invalid_reason or "").lower()
                    ):
                        v.is_invalid = False
                        v.invalid_reason = None
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
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    from shared.semantic.model_validator import revalidate_model
    from src.api.personas import strip_id_from_personas

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        m = await db.get(Measure, measure_id)
        if m is None or m.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")

        # Bug-7787 Phase 3: impact guard — block or require acknowledgement
        # before proceeding with the delete.
        from src.dependencies.guard import evaluate_delete_impact

        await evaluate_delete_impact(
            db, current_user.tenant_id, project_id, model_id,
            "measure", measure_id, request=request,
        )
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


async def _validate_window_date_column(
    db, model_id: UUID, base: Measure, date_col_id: UUID | None,
) -> str | None:
    """F-015-02: validate the ORDER BY date column for a window variant.

    Window variants (lag / trailing_n / moving_avg_n) need a DATE/TIMESTAMP
    column to order by, but no hierarchy or calendar. Returns an error string
    when the selected column is missing, of the wrong type, or (when none is
    selected) the base measure has no usable date column to fall back on.
    """
    if date_col_id is not None:
        col = await db.get(ModelColumn, date_col_id)
        if col is None:
            return (
                "The selected date dimension column was not found in this "
                "model. Choose a date/timestamp column for window ordering."
            )
        # The column must belong to a table in this model.
        table = await db.get(ModelTable, col.model_table_id)
        if table is None or table.model_id != model_id:
            return (
                "The selected date dimension column does not belong to this "
                "model."
            )
        if not is_date_anchor_type(getattr(col, "data_type", None)):
            return (
                f"The selected ordering column '{col.column_name}' is of type "
                f"'{getattr(col, 'data_type', None)}', which is not a "
                "date/timestamp. Window variants must order by a "
                "date/timestamp column."
            )
        return None
    # No explicit column selected: require that the base measure's own table
    # carries at least one date/timestamp column to anchor the window.
    base_col = (
        await db.get(ModelColumn, base.source_column_id)
        if base.source_column_id
        else None
    )
    if base_col is not None:
        date_rows = await db.execute(
            select(ModelColumn.data_type).where(
                ModelColumn.model_table_id == base_col.model_table_id
            )
        )
        if any(is_date_anchor_type(dt) for dt in date_rows.scalars().all()):
            return None
    return (
        "Window variants require a date or timestamp column to order by. "
        "Select a date dimension column, or add one to the measure's table."
    )


async def _check_variant_eligibility(
    db, model_id: UUID, variant_kind: str, variant_of_measure_id: UUID | None,
    *, override_hierarchy_id: UUID | None = None,
    window_date_col_id: UUID | None = None,
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

    # F-015-02: window variants (lag / trailing_n / moving_avg_n) need only a
    # validated date column — never a hierarchy or calendar. Branch by family
    # BEFORE the hierarchy precondition so the advertised calendar-free
    # rolling-measure workflow is admissible. Semi-additive ineligibility is
    # still enforced by _variant_reason for the moving-window family.
    if is_window_variant(variant_kind):
        col_reason = await _validate_window_date_column(
            db, model_id, base, window_date_col_id
        )
        if col_reason:
            return col_reason
        family = TIME_VARIANT_FAMILY[variant_kind]
        base_semi_additive = getattr(base, "semi_additive_behavior", None)
        if base_semi_additive and family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES:
            return (
                f"Cumulation and window variants ({variant_kind}) are not "
                f"supported for semi-additive measures (behavior: "
                f"{base_semi_additive}). Semi-additive measures represent "
                "balances, not flows; cumulating them produces incorrect "
                "results. Use lag or prior-period variants instead."
            )
        return None

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

    # Bug-6222: pass the base's semi-additive behavior so the admission
    # gate can reject cumulation/window variants on balance measures.
    base_semi_additive = getattr(base, "semi_additive_behavior", None)

    return _variant_reason(
        variant_kind,
        has_hierarchy=has_hierarchy,
        units=units,
        calcs=calcs,
        has_calendar_rules=has_calendar_rules,
        semi_additive_behavior=base_semi_additive,
    )



# Bug-6574: removed duplicate SEMI_ADDITIVE_INELIGIBLE_FAMILIES definition.
# Now imported from shared.schemas.measure_formats (single source of truth).


def _variant_reason(
    kind: str,
    *,
    has_hierarchy: bool,
    units: set[str],
    calcs: set[str],
    has_calendar_rules: bool,
    semi_additive_behavior: str | None = None,
    has_date_column: bool = False,
) -> str | None:
    """None when the variant is admissible; otherwise a short explanation."""
    family = TIME_VARIANT_FAMILY[kind]
    # F-015-02: window variants (lag / trailing_n / moving_avg_n) need only a
    # date column to order by — never a hierarchy or calendar. Evaluate the
    # window family BEFORE the hierarchy precondition.
    if is_window_variant(kind):
        if not has_date_column:
            return (
                "This measure has no date or timestamp column to order a "
                "window by. Select a date dimension column, or add one to "
                "the measure's table."
            )
        if semi_additive_behavior and family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES:
            return (
                f"Cumulation and window variants ({kind}) are not supported "
                f"for semi-additive measures (behavior: {semi_additive_behavior}). "
                "Semi-additive measures represent balances, not flows; "
                "cumulating them produces incorrect results. "
                "Use lag or prior-period variants instead."
            )
        return None
    if not has_hierarchy:
        return (
            "This measure has no associated time hierarchy. "
            "Create a time hierarchy with the required levels on "
            "the same table as the measure's source column."
        )
    # Bug-6222: reject cumulation/window variants on semi-additive bases.
    # A semi-additive measure represents a balance (e.g. account balance,
    # inventory count) where the correct period aggregate is the last (or
    # first) value, not a sum. Cumulating these via SUM OVER windows
    # produces meaningless running totals of balances.
    if semi_additive_behavior and family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES:
        return (
            f"Cumulation and window variants ({kind}) are not supported "
            f"for semi-additive measures (behavior: {semi_additive_behavior}). "
            "Semi-additive measures represent balances, not flows; "
            "cumulating them produces incorrect results. "
            "Use lag or prior-period variants instead."
        )
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


@router.get(
    "/{measure_id}/rename-impact",
    response_model=MeasureRenameImpactResponse,
    dependencies=[require_role("modeler")],
)
async def measure_rename_impact(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    new_name: str = Query(
        min_length=1,
        description="The candidate new measure name.",
    ),
    current_user: CurrentUser = Depends(get_current_user),
) -> MeasureRenameImpactResponse:
    """Preview what renaming this measure would change (Bug-9394).

    The KPI DSL binds measures by NAME, so a rename silently orphans every KPI
    expression that referenced the old one unless the cascade rewrites it. The
    cascade exists (``propagate_measure_renames``); what was missing is the
    modeller's ability to SEE the affected KPIs before committing. This endpoint
    answers exactly that, from the SAME enumeration the rename itself runs, so
    the preview can never disagree with what the PATCH does.

    Read-only: nothing is written and no row lock is taken.
    """
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        measure = await db.get(Measure, measure_id)
        if measure is None or measure.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")

        candidate = new_name.strip()
        if not candidate:
            raise HTTPException(
                status_code=400, detail="new_name must not be blank",
            )

        _updates, _coverage, _unsafe, plan = await plan_measure_renames(
            db, model_id, {measure.id: (measure.name, candidate)},
            for_update=False,
        )
        plan = plan.for_viewer(current_user.email or current_user.user_id)
        return MeasureRenameImpactResponse(
            measure_id=measure.id,
            current_name=measure.name,
            new_name=candidate,
            safe=plan.safe,
            rewrites=[
                MeasureRenameImpactItem(
                    consumer_type=item.consumer_type,
                    consumer_id=item.consumer_id,
                    consumer_name=item.consumer_name,
                    field=item.field,
                )
                for item in plan.rewrites
            ],
            blockers=[
                MeasureRenameImpactItem(
                    consumer_type=item.consumer_type,
                    consumer_id=item.consumer_id,
                    consumer_name=item.consumer_name,
                    field=item.field,
                )
                for item in plan.blockers
            ],
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/{measure_id}/available-variants")
async def list_available_variants(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    persona_id: UUID | None = Query(default=None),
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
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        measure = await db.get(Measure, measure_id)
        if measure is None or measure.model_id != model_id:
            raise HTTPException(status_code=404, detail="Measure not found")
        # Bug-6141: the suggested_name/suggested_display_name are built from the
        # base measure's name (often mirroring its source column). A measure
        # hidden by the persona allow-list or CLS must 404 here too.
        await _ensure_measure_visible_to_persona(
            db, measure=measure, current_user=current_user,
            model_id=model_id, persona_id=persona_id,
        )

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

        # Bug-6621(b): filter existing_by_kind against the persona allow-list
        # so a persona-excluded variant row's UUID+kind is not disclosed. The
        # CLS column check is not needed here — a variant shares the base's
        # column, and the base already passed the visibility check above.
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_measure_ids)
            if allowed is not None:
                existing_by_kind = {
                    k: mid for k, mid in existing_by_kind.items()
                    if mid in allowed
                }

        # Bug-6222: pass semi-additive behavior so the catalog correctly
        # marks cumulation/window variants as ineligible on balance measures.
        base_semi_additive = getattr(measure, "semi_additive_behavior", None)

        # F-015-02: window variants need a date column, not a hierarchy. The
        # base's own table carries a usable ordering column when at least one
        # of its columns is DATE/TIMESTAMP.
        has_date_column = False
        base_src_col = (
            await db.get(ModelColumn, measure.source_column_id)
            if measure.source_column_id
            else None
        )
        if base_src_col is not None:
            _dt_rows = await db.execute(
                select(ModelColumn.data_type).where(
                    ModelColumn.model_table_id == base_src_col.model_table_id
                )
            )
            has_date_column = any(
                is_date_anchor_type(dt) for dt in _dt_rows.scalars().all()
            )

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
                semi_additive_behavior=base_semi_additive,
                has_date_column=has_date_column,
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
    # Bug-6621(a): return 404 (not-found) before 400 (bad-request) so a
    # calculated-measure probe cannot distinguish "exists but calculated"
    # from "not found" — the 400-before-404 ordering was a coarse
    # existence/type oracle.
    if measure.measure_type == "calculated":
        raise HTTPException(
            status_code=404,
            detail="Measure not found",
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
    db, detail_columns: list[UUID], effective_table: ModelTable | None, model_id: UUID
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

    # Bug-5933 (F-019-02): belonging to the effective table is necessary but
    # not sufficient — query-router's drill builder can only project model
    # DIMENSION names in its semantic SQL (`FROM "<model slug>"`); a physical
    # column with no Dimension defined over it raises
    # DRILL_DETAIL_COLUMN_NOT_PROJECTABLE at drill time. Reject the same
    # columns here, at save time, so the curation contract matches the
    # runtime contract instead of failing later for the analyst.
    dim_result = await db.execute(
        select(Dimension.source_column_id).where(
            Dimension.model_id == model_id,
            Dimension.source_column_id.in_(detail_columns),
        )
    )
    projectable = {row for row in dim_result.scalars().all()}
    not_projectable = [cid for cid in detail_columns if cid not in projectable]
    if not_projectable:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_DETAIL_COLUMN_NOT_PROJECTABLE",
                "message": (
                    "detail_columns must each have a dimension defined over "
                    "them so query-router can project them through the "
                    "semantic layer. Create a dimension for each column, or "
                    "remove it from the selection."
                ),
                "invalid_ids": [str(cid) for cid in not_projectable],
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
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DrillThroughSetResponse:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # F-015-20: read-only GET — synthesize the default if no row exists,
        # never write under the viewer role.
        measure, drill = await _load_drill_through_set_or_404(
            db, measure_id, model_id, create_if_missing=False
        )
        # Bug-6614: a measure hidden on get_measure must 404 here too.
        await _ensure_measure_visible_to_persona(
            db, measure=measure, current_user=current_user,
            model_id=model_id, persona_id=persona_id,
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
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DrillThroughSetEnrichedResponse:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # F-015-20: read-only GET — no write under the viewer role.
        measure, drill = await _load_drill_through_set_or_404(
            db, measure_id, model_id, create_if_missing=False
        )
        # Bug-6614: fail-closed 404 for a measure outside the persona's scope
        # (allow-list or CLS). The returned restricted-column set then drives the
        # detail-column name redaction below (Bug-6141).
        restricted_cols = await _ensure_measure_visible_to_persona(
            db, measure=measure, current_user=current_user,
            model_id=model_id, persona_id=persona_id,
        )
        # drill.detail_columns is JSONB stored as list[str] (see PATCH writer),
        # while get_restricted_column_ids returns set[UUID]. Compare as strings
        # so the fail-closed skip actually matches — a UUID-vs-str comparison
        # would silently never match and re-open the leak.
        restricted_col_strs: set[str] = {str(c) for c in restricted_cols}
        resolved_cols: list[DrillThroughSetColumnInfo] = []
        if drill.detail_columns:
            for col_id in drill.detail_columns:
                if str(col_id) in restricted_col_strs:
                    continue
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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        measure, drill = await _load_drill_through_set_or_404(db, measure_id, model_id)
        updates = body.model_dump(exclude_unset=True)

        # Bug-6275: detail_columns, joined_dimension_ids and source_join_path are
        # all scoped to the current source table (columns of it, dimensions
        # reachable from it, a join path that starts at it). Track whether the
        # override table changes in this PATCH so retained curation the caller
        # did NOT re-supply can be reset — otherwise it lingers as stale
        # references against a table it no longer belongs to.
        previous_source_table_id = drill.source_table_id

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

        source_table_changed = drill.source_table_id != previous_source_table_id

        # Re-resolve the effective source table after the potential override so
        # subsequent validation uses the caller's intended table.
        effective_table = await _resolve_effective_source_table(db, measure, drill)

        if "detail_columns" in updates:
            new_list = updates["detail_columns"] or []
            await _validate_detail_columns(db, list(new_list), effective_table, model_id)
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
            # Bug-5935 (F-019-04): bounds (positive, <= DRILL_MAX_ROW_LIMIT)
            # are enforced by the DrillThroughSetUpdate schema's Field(ge=1,
            # le=DRILL_MAX_ROW_LIMIT) — a value outside that range never
            # reaches this handler (FastAPI returns 422 during body parsing).
            # This keeps the save-time ceiling identical to the runtime
            # clamp in query-router's semantic_builder.py.
            drill.row_limit_override = updates["row_limit_override"]

        # Bug-6275: reset table-scoped curation the caller did not re-supply in
        # the same PATCH when the override source table changed, so stale
        # references from the previous table never survive the edit. Cleared
        # source_join_path is repopulated by the sole-path auto-resolver below
        # when exactly one path exists for the new table.
        if source_table_changed:
            if "detail_columns" not in updates:
                drill.detail_columns = None
            if "joined_dimension_ids" not in updates:
                drill.joined_dimension_ids = None
            if "source_join_path" not in updates:
                drill.source_join_path = None

        if "source_join_path" in updates:
            path = updates["source_join_path"] or []
            if path:
                fact_for_path = await _resolve_intrinsic_fact_table(db, measure)
                await _validate_join_path(
                    db, list(path), model_id, effective_table,
                    fact_table_id=fact_for_path.id if fact_for_path else None,
                )
                drill.source_join_path = [str(jid) for jid in path]
            else:
                drill.source_join_path = None

        # F-019-09 / Bug-5932 (F-019-01): now that the override has runtime
        # effect, an override source table with no chosen join path is only
        # safe when exactly one path exists. With zero or several candidate
        # paths the drill cannot deterministically join back to the fact, so
        # the save is rejected — matching the documented
        # DRILL_OVERRIDE_NO_JOIN_PATH rule and the editor's client-side guard.
        #
        # Bug-5932 root cause: this block used to VALIDATE that exactly one
        # path exists but never PERSISTED it, leaving drill.source_join_path
        # empty. Query-router's runtime builder (semantic_builder.py
        # _load_curation) has no auto-resolution of its own — it raises
        # DRILL_JOIN_PATH_REQUIRED whenever source_table_id differs from the
        # intrinsic table and source_join_path is empty. That left a save
        # that the editor and this endpoint both accepted as valid, but that
        # always failed the first time an analyst actually drilled. The
        # schema already documented the intended contract ("null =
        # single-path auto-resolvable" on DrillThroughSetUpdate.
        # source_join_path) — persisting the sole BFS path here is what
        # fulfils it.
        if drill.source_table_id is not None and not drill.source_join_path:
            intrinsic = await _resolve_intrinsic_fact_table(db, measure)
            if intrinsic is not None and drill.source_table_id != intrinsic.id:
                paths = await _enumerate_join_paths(
                    db, model_id, drill.source_table_id, intrinsic.id
                )
                # Bug-7267: filter to forward-direction-only paths before
                # auto-resolving. A reverse-hop path would produce incorrect
                # drill SQL (wrong ON conditions), so only auto-assign a
                # path if every hop is forward (left_table_id -> right).
                forward_paths = []
                for path in paths:
                    cursor = drill.source_table_id
                    all_forward = True
                    for j in path:
                        if j.left_table_id == cursor:
                            cursor = j.right_table_id
                        else:
                            all_forward = False
                            break
                    if all_forward:
                        forward_paths.append(path)
                if len(forward_paths) == 1:
                    drill.source_join_path = [str(j.id) for j in forward_paths[0]]
                else:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "DRILL_OVERRIDE_NO_JOIN_PATH",
                            "message": (
                                "An override source table needs an explicit "
                                "join path unless exactly one forward-direction "
                                f"path exists ({len(forward_paths)} found). "
                                "Pick a join path."
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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
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
    """Best-effort fan-out summary across the hops.

    Reads the join's CARDINALITY, not its ``join_type``. Those were one column
    until the join-orientation contract split them (invariant 3), so this used
    to classify from ``join_type`` — which meant every join carrying a real
    orientation token (``inner``/``left``/``right``/``full``: everything the
    write API has accepted since Bug-7775) fell straight through to "mixed",
    regardless of whether the path actually fans out.
    ``shared.semantic.join_keyword.edge_cardinality`` reads the declared field
    and falls back to a legacy token still sitting in ``join_type``.

    An UNDECLARED cardinality stays "mixed": not knowing whether a hop expands
    is not the same as knowing it does not, and an expanding hop repeats the
    parent measure across child rows.
    """
    types: set[str] = set()
    cursor = start_table_id
    for j in path:
        declared = edge_cardinality(j) or "unknown"
        if cursor == j.left_table_id:
            types.add(declared)
            cursor = j.right_table_id
        else:
            # Reverse direction; many_to_one becomes one_to_many.
            inverted = {
                "many_to_one": "one_to_many",
                "one_to_many": "many_to_one",
            }.get(declared, declared)
            types.add(inverted)
            cursor = j.left_table_id
    if types == {"many_to_one"}:
        return "many-to-one"
    if types == {"one_to_many"}:
        return "one-to-many"
    if types == {"one_to_one"}:
        return "one-to-one"
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
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DrillJoinPathsResponse:
    """Enumerate join paths from ``source_table_id`` back to the fact's base table.

    Used by the editor's source-table-override picker (Phase 8.A.4).
    """
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-2570 (residual F-015-20): GET must not write. Use a transient
        # default drill-through set if none exists — never auto-create + commit
        # a DrillThroughSet under the read-only viewer role.
        measure, _drill = await _load_drill_through_set_or_404(
            db, measure_id, model_id, create_if_missing=False
        )
        # Bug-6614: a measure hidden on get_measure must not disclose its join
        # topology here either — 404 fail-closed for out-of-persona-scope.
        await _ensure_measure_visible_to_persona(
            db, measure=measure, current_user=current_user,
            model_id=model_id, persona_id=persona_id,
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
        # Bug-7267: filter to forward-direction paths only. The BFS
        # enumerates ALL reachable paths (bidirectional), but the drill
        # SQL builder emits joins in the defined left->right direction.
        # Returning a reverse-hop path would let the user select a path
        # that the PATCH endpoint then rejects with 400. Only show
        # paths the user can actually save.
        # Bug-2570: read-only GET — no commit on this path.
        out: list[DrillJoinPath] = []
        for path in raw_paths:
            cursor = source_table_id
            all_forward = True
            for j in path:
                if j.left_table_id == cursor:
                    cursor = j.right_table_id
                else:
                    all_forward = False
                    break
            if not all_forward:
                continue
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
    db,
    join_ids: list[UUID],
    model_id: UUID,
    effective_table: ModelTable | None,
    fact_table_id: UUID | None = None,
) -> None:
    """Each id must be a Join in this model, and the chained edges must
    connect ``effective_table`` (the override source) end-to-end **in
    forward direction** (left_table_id -> right_table_id at each hop).

    Bug-6276: a contiguous chain is necessary but not sufficient. When
    ``fact_table_id`` is supplied the walk must also TERMINATE at the fact
    table, otherwise the path forms a valid chain that wanders off to some
    unrelated table and the drill can never join back to the fact.

    Bug-7267: the chain must also be directional. A join is defined as
    ``left_table_id -> right_table_id``; the drill SQL builder emits
    ``FROM left JOIN right ON left.col = right.col`` in that order. A
    reverse traversal (cursor matches ``right_table_id``) would produce
    a semantically incorrect join condition (wrong ON clause direction,
    inverted cardinality). Reject paths that require any reverse hop.
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
    reversed_hops: list[str] = []
    for jid in join_ids:
        j = joins[jid]
        if j.left_table_id == cursor:
            cursor = j.right_table_id
        elif j.right_table_id == cursor:
            # Bug-7267: record the reverse traversal.
            reversed_hops.append(str(jid))
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
    # Bug-7267: reject paths with reverse-direction hops. The drill SQL
    # builder emits joins in the defined direction (left -> right); a
    # reversed hop would produce an incorrect ON condition and inverted
    # cardinality, yielding wrong drill results.
    if reversed_hops:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_JOIN_PATH_WRONG_DIRECTION",
                "message": (
                    "source_join_path traverses one or more joins in reverse "
                    "direction (right-to-left), which would produce incorrect "
                    "drill SQL. Reverse the join definition or choose a "
                    "forward-only path."
                ),
                "reversed_join_ids": reversed_hops,
            },
        )
    # Bug-6276: the chain must land on the fact table, not merely be contiguous.
    if fact_table_id is not None and cursor != fact_table_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DRILL_OVERRIDE_NO_JOIN_PATH",
                "message": (
                    "source_join_path must terminate at the measure's fact "
                    "table so the drill can join back to it."
                ),
            },
        )
