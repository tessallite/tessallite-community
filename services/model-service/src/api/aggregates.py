"""
AggregateDefinition CRUD routes.

Enforces:
- max_aggregates cap per model (retire lowest-score on cap breach)
- include_quantiles opt-in flag

Role requirements:
  GET (list / get) → viewer+
  POST / PATCH     → modeler+
  DELETE           → admin
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from shared.config.resolver import get_setting
from shared.aggregate_table_ops import drop_aggregate_physical_table
from shared.aggregate_eviction import (
    DEFAULT_EVICTION_POLICY,
    EVICTION_POLICIES,
    eviction_sort_key,
)
from shared.db.models import (
    AIAggregateRecommendation,
    AggregateColumn,
    AggregateDefinition,
    AggregateLifecycleEvent,
    AggregateRefreshPolicy,
    Dimension,
    Join,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (  # noqa: F401
    AggregateDefinitionCreate,
    AggregateDefinitionResponse,
    AggregateDefinitionUpdate,
)
from shared.semantic.grain_resolver import (
    GrainResolutionError,
    bound_ident,
    resolve_aggregate_layout,
)
from shared.semantic.redundant_partner import compute_redundant_partners
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/aggregates",
    tags=["aggregates"],
)


async def _enforce_max_aggregates(db, model_id: UUID) -> None:
    """
    If the model is at its max_aggregates cap, retire the lowest-scored active aggregate.
    """
    model = await db.get(Model, model_id)
    if model is None:
        return
    count_result = await db.execute(
        select(func.count()).where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
        )
    )
    count = count_result.scalar_one()
    if count < model.max_aggregates:
        return

    policy = getattr(model, "predictive_eviction_policy", DEFAULT_EVICTION_POLICY)
    if policy not in EVICTION_POLICIES:
        policy = DEFAULT_EVICTION_POLICY
    if policy == "never_evict":
        return

    result = await db.execute(
        select(AggregateDefinition).where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
        )
    )
    actives = list(result.scalars().all())
    actives.sort(key=eviction_sort_key(policy))
    to_retire = max(count - model.max_aggregates + 1, 1)
    now = datetime.now(timezone.utc)
    for agg in actives[:to_retire]:
        agg.status = "retired"
        agg.retired_at = now
        db.add(
            AggregateLifecycleEvent(
                model_id=model_id,
                aggregate_id=agg.id,
                event_type="retired",
                reason=f"cap_enforcement:{policy}",
                payload={"creation_reason": agg.creation_reason},
            )
        )
        # Retired means the table is actually dropped — reclaim it now.
        await drop_aggregate_physical_table(agg, db, reason=f"cap_enforcement:{policy}")
    if actives[:to_retire]:
        await db.flush()


@router.post(
    "",
    response_model=AggregateDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_aggregate(
    project_id: UUID,
    model_id: UUID,
    body: AggregateDefinitionCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AggregateDefinitionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        await _enforce_max_aggregates(db, model_id)

        # Resolve the grain + measures before touching the DB so we can
        # reject inconsistent definitions with a clean 400 and persist the
        # physical column name map next to the logical grain.
        dims_result = await db.execute(
            select(Dimension).where(Dimension.model_id == model_id)
        )
        dims = list(dims_result.scalars().all())
        measures_result = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        model_measures = list(measures_result.scalars().all())
        tables_result = await db.execute(
            select(ModelTable).where(ModelTable.model_id == model_id)
        )
        tables = list(tables_result.scalars().all())
        cols_result = await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id.in_([t.id for t in tables])
            )
        ) if tables else None
        model_columns = (
            list(cols_result.scalars().all()) if cols_result is not None else []
        )

        uda_result = await db.execute(
            select(UserDefinedAttribute).where(
                UserDefinedAttribute.model_id == model_id
            )
        )
        udas = list(uda_result.scalars().all())

        measure_by_name = {m.name: m for m in model_measures}
        measure_specs: list[tuple[str, str, str | None]] = []
        _variant_measure_names: list[str] = []
        _requested_variants: list[Measure] = []
        for name in body.measure_names or []:
            m = measure_by_name.get(name)
            if m is None:
                raise HTTPException(
                    status_code=400, detail=f"Unknown measure: {name!r}"
                )
            _variant_kind = getattr(m, "variant_kind", None)
            if _variant_kind is not None:
                _variant_measure_names.append(name)
                _requested_variants.append(m)
                # F-009-02: a variant materialises a SINGLE window-function
                # column keyed on its base default_agg — never the additive
                # sum/count/min/max fan-out or count_distinct. The DDL renderer
                # checks is_variant before stat_type, so any other spec would
                # register COUNT/MIN/MAX coverage rows that serve the lag value.
                from shared.semantic.variant_columns import variant_stat_type
                st = variant_stat_type(m)
                measure_specs.append((m.name, st, st))
                continue
            if getattr(m, "measure_type", None) == "calculated":
                mode = getattr(m, "calc_agg_mode", None) or "expression_as_written"
                if mode == "per_row_then_aggregate":
                    st = (m.default_agg or "sum").lower()
                else:
                    st = "calculated"
                measure_specs.append((m.name, st, st))
            elif not m.is_additive:
                measure_specs.append((m.name, "count_distinct", "count_distinct"))
            else:
                agg_fn = (m.default_agg or "sum").lower()
                measure_specs.append((m.name, agg_fn, agg_fn))

        # Bug-1091: reject non-materialisable variant aggregates at creation
        # rather than registering AggregateColumn rows whose first scheduled
        # refresh fails loud. A window-based variant requires (1) a CTAS-safe
        # variant kind (period-aware variants need a calendar JOIN and are not
        # materialisable in a pre-aggregate), (2) a time dimension in the
        # grain, and (3) a snapshotted base source column. The first two/three
        # are validated by the shared resolver used by the create + refresh
        # DDL paths, so the API and the materialiser agree on the contract.
        if _requested_variants:
            from shared.semantic.variant_columns import (
                CTAS_ALLOWED_VARIANTS,
                resolve_variant_context,
            )

            _not_ctas = [
                m.name for m in _requested_variants
                if m.variant_kind not in CTAS_ALLOWED_VARIANTS
            ]
            if _not_ctas:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Variant measure(s) {_not_ctas!r} are period-aware and "
                        "cannot be materialised in a pre-aggregate. Only "
                        f"window-based variants ({', '.join(sorted(CTAS_ALLOWED_VARIANTS))}) "
                        "are supported in aggregates."
                    ),
                )
            try:
                await resolve_variant_context(
                    model_id=model_id,
                    grain=body.grain or [],
                    measures=_requested_variants,
                    all_dims=dims,
                    db=db,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            layout = resolve_aggregate_layout(
                grain_names=body.grain or [],
                measure_specs=measure_specs,
                dimensions=dims,
                measures=model_measures,
                tables=tables,
                columns=model_columns,
                user_defined_attributes=udas,
            )
        except GrainResolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Redundant-grain guard: reject dim-side columns whose fact-side
        # partner is already a grain choice, unless the caller explicitly
        # overrides with confirm_redundant_grain=true.
        if not body.confirm_redundant_grain:
            joins_result = await db.execute(
                select(Join).where(Join.model_id == model_id)
            )
            joins = list(joins_result.scalars().all())
            table_by_id = {t.id: t for t in tables}
            col_by_id = {c.id: c for c in model_columns}
            partners = compute_redundant_partners(joins, table_by_id, col_by_id)
            redundant = []
            dim_by_name = {d.name: d for d in dims}
            for logical in body.grain or []:
                dim = dim_by_name.get(logical)
                if dim is None or dim.source_column_id is None:
                    continue
                hint = partners.get(dim.source_column_id)
                if hint is None:
                    continue
                redundant.append(
                    {
                        "grain": logical,
                        "partner_column": hint.partner_column_name,
                        "partner_table": hint.partner_physical_table,
                        "join_type": hint.join_type,
                        "reason": hint.reason,
                    }
                )
            if redundant:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "redundant_grain",
                        "message": (
                            "Grain contains one or more dim-side columns whose "
                            "fact-side partner already represents the same data. "
                            "Pass confirm_redundant_grain=true to override."
                        ),
                        "redundant": redundant,
                    },
                )

        # Auto-generate physical_table_name from model seed
        seed = model.seed or secrets.token_hex(6)
        suffix = secrets.token_hex(4)
        physical_table_name = f"agg_{seed}_{suffix}"
        fields = body.model_dump(
            exclude={"measure_names", "confirm_redundant_grain"}
        )
        fields["physical_table_name"] = physical_table_name
        fields["status"] = "active"
        fields["grain_physical_cols"] = layout.grain_physical_cols
        agg = AggregateDefinition(model_id=model_id, **fields)
        db.add(agg)
        await db.flush()  # populate agg.id before creating child records

        # Create AggregateColumn rows from the resolved layout so the
        # physical_col_name is consistent with what the DDL builder will
        # emit (same collision-aware resolver on both sides).
        if layout.measure_cols:
            for mc in layout.measure_cols:
                db.add(AggregateColumn(
                    aggregate_definition_id=agg.id,
                    measure_id=mc.measure_id,
                    physical_col_name=mc.physical_col_name,
                    stat_type=mc.stat_type,
                    aggregation_function=mc.aggregation_function,
                ))

        db.add(AggregateColumn(
            aggregate_definition_id=agg.id,
            measure_id=None,
            physical_col_name="__row_count__count",
            stat_type="count",
        ))

        # Opt-in quantile / dispersion-stat coverage rows. The physical table is
        # built by the first scheduler refresh (which emits matching columns from
        # the same canonical naming); these rows are what the query-router matcher
        # reads to know an exact-grain MEDIAN/STDDEV query can be served here.
        # Added once per numeric (sum/avg) measure, deduped by measure.
        if body.include_quantiles or body.include_stats:
            from shared.aggregate_quantiles import QUANTILE_STAT_TYPES
            from shared.aggregate_stats import STAT_TYPES
            _extra: list[str] = []
            if body.include_quantiles:
                _extra += QUANTILE_STAT_TYPES
            if body.include_stats:
                _extra += STAT_TYPES
            _seen: set = set()
            for mc in layout.measure_cols:
                agg_fn = (mc.aggregation_function or mc.stat_type or "").lower()
                if agg_fn not in ("sum", "avg") or mc.measure_name in _seen:
                    continue
                _seen.add(mc.measure_name)
                for _stat in _extra:
                    db.add(AggregateColumn(
                        aggregate_definition_id=agg.id,
                        measure_id=mc.measure_id,
                        physical_col_name=bound_ident(f"{mc.measure_name}__{_stat}"),
                        stat_type=_stat,
                    ))

        # Create a default refresh policy so the scheduler sweep picks this up.
        # Without a policy, hourly_refresh_sweep skips this aggregate entirely.
        default_cron = await get_setting(
            "aggregate.default_cron",
            tenant_session=db,
            project_id=project_id,
            model_id=model_id,
        )
        db.add(AggregateRefreshPolicy(
            aggregate_definition_id=agg.id,
            refresh_mode="scheduled",
            cron_expression=default_cron,
            is_enabled=True,
        ))

        await db.commit()
        await db.refresh(agg)
        resp = AggregateDefinitionResponse.model_validate(agg)
        if _variant_measure_names:
            resp.warnings.append(
                f"Variant measures ({', '.join(_variant_measure_names)}) are "
                "pre-computed window results and can only be queried at this "
                "aggregate's exact grain. Queries at coarser grain will fall "
                "back to the source table."
            )
        return resp


@router.get("", response_model=list[AggregateDefinitionResponse])
async def list_aggregates(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[AggregateDefinitionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        result = await db.execute(
            select(AggregateDefinition).where(
                AggregateDefinition.model_id == model_id
            )
        )
        aggs = result.scalars().all()
        responses = []
        for a in aggs:
            resp = AggregateDefinitionResponse.model_validate(a)
            resp.measure_names = await _get_measure_names(db, a.id)
            resp.rationale = await _get_ai_rationale(db, a)
            responses.append(resp)
        return responses


@router.get("/{agg_id}", response_model=AggregateDefinitionResponse)
async def get_aggregate(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> AggregateDefinitionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        a = await db.get(AggregateDefinition, agg_id)
        if a is None or a.model_id != model_id:
            raise HTTPException(status_code=404, detail="Aggregate not found")
        resp = AggregateDefinitionResponse.model_validate(a)
        resp.measure_names = await _get_measure_names(db, a.id)
        resp.rationale = await _get_ai_rationale(db, a)
        return resp


@router.patch(
    "/{agg_id}",
    response_model=AggregateDefinitionResponse,
    dependencies=[require_role("modeler")],
)
async def update_aggregate(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    body: AggregateDefinitionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> AggregateDefinitionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        a = await db.get(AggregateDefinition, agg_id)
        if a is None or a.model_id != model_id:
            raise HTTPException(status_code=404, detail="Aggregate not found")

        prev_stats = a.include_stats
        prev_quantiles = a.include_quantiles
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(a, k, v)

        # The query-router matches against AggregateColumn coverage rows, not
        # the physical table. Flipping include_stats / include_quantiles on the
        # boolean alone is therefore inert downstream — and flipping it off
        # leaves orphan rows that the next refresh will not materialise. Keep
        # the coverage rows in lock-step with the flags, then force a full
        # rebuild so the physical columns match before the router serves any
        # exact-grain stat/quantile query.
        from shared.aggregate_quantiles import QUANTILE_STAT_TYPES
        from shared.aggregate_stats import STAT_TYPES

        add_types: list[str] = []
        if bool(a.include_quantiles) and not prev_quantiles:
            add_types += QUANTILE_STAT_TYPES
        if bool(a.include_stats) and not prev_stats:
            add_types += STAT_TYPES
        remove_types: set[str] = set()
        if prev_quantiles and not a.include_quantiles:
            remove_types |= set(QUANTILE_STAT_TYPES)
        if prev_stats and not a.include_stats:
            remove_types |= set(STAT_TYPES)

        if add_types or remove_types:
            existing = (await db.execute(
                select(AggregateColumn).where(
                    AggregateColumn.aggregate_definition_id == a.id
                )
            )).scalars().all()

            for col in existing:
                if (col.stat_type or "").lower() in remove_types:
                    await db.delete(col)

            if add_types:
                existing_pairs = {
                    (col.measure_id, (col.stat_type or "").lower())
                    for col in existing
                    if col.measure_id is not None
                    and (col.stat_type or "").lower() not in remove_types
                }
                # Eligible base measures carry a sum/avg column (mirrors create).
                eligible: list[UUID] = []
                for col in existing:
                    if col.measure_id is None:
                        continue
                    agg_fn = (col.aggregation_function or col.stat_type or "").lower()
                    if agg_fn in ("sum", "avg") and col.measure_id not in eligible:
                        eligible.append(col.measure_id)
                if eligible:
                    measures = (await db.execute(
                        select(Measure).where(Measure.id.in_(eligible))
                    )).scalars().all()
                    # F-009-02 / Bug-1091: variant and calculated measures must
                    # NOT receive stat/quantile coverage rows — the DDL builders
                    # exclude them, so adding rows here would register columns the
                    # refresh never materialises (route-to-missing-column). A
                    # variant's stat_type equals its base default_agg, so the
                    # sum/avg eligibility test above admits it; drop them here.
                    from shared.semantic.variant_columns import is_variant as _is_variant_m
                    _excluded_ids = {
                        m.id for m in measures
                        if _is_variant_m(m)
                        or getattr(m, "measure_type", None) == "calculated"
                    }
                    eligible = [mid for mid in eligible if mid not in _excluded_ids]
                    name_by_id = {m.id: m.name for m in measures}
                    for mid in eligible:
                        mname = name_by_id.get(mid)
                        if not mname:
                            continue
                        for stat in add_types:
                            if (mid, stat) in existing_pairs:
                                continue
                            db.add(AggregateColumn(
                                aggregate_definition_id=a.id,
                                measure_id=mid,
                                physical_col_name=bound_ident(f"{mname}__{stat}"),
                                stat_type=stat,
                            ))
                            existing_pairs.add((mid, stat))

            # Withhold routing until the scheduler re-materialises the table.
            # The matcher's freshness gate skips aggregates with a NULL
            # last_refreshed_at; incremental refresh delegates to full when
            # either include flag is set, so the physical columns are rebuilt.
            a.last_refreshed_at = None
            a.is_stale = True

        await db.commit()
        await db.refresh(a)
        resp = AggregateDefinitionResponse.model_validate(a)
        resp.measure_names = await _get_measure_names(db, a.id)
        resp.rationale = await _get_ai_rationale(db, a)
        return resp


@router.delete(
    "/{agg_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def delete_aggregate(
    project_id: UUID,
    model_id: UUID,
    agg_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")
        a = await db.get(AggregateDefinition, agg_id)
        if a is None or a.model_id != model_id:
            raise HTTPException(status_code=404, detail="Aggregate not found")
        await db.delete(a)
        await db.commit()


async def _get_ai_rationale(db, agg: AggregateDefinition) -> str | None:
    """Return the LLM rationale for an AI-created aggregate, else None.

    The rationale lives on AIAggregateRecommendation, not on the aggregate
    itself; for ``creation_reason == "ai"`` aggregates we join the latest
    recommendation that produced this aggregate so the UI can surface it in the
    aggregate drawer (F-011-03).
    """
    if agg.creation_reason != "ai":
        return None
    result = await db.execute(
        select(AIAggregateRecommendation.rationale)
        .where(AIAggregateRecommendation.aggregate_definition_id == agg.id)
        .order_by(AIAggregateRecommendation.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_measure_names(db, agg_id: UUID) -> list[str]:
    """Load measure names for an aggregate from its AggregateColumn records."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(AggregateColumn)
        .options(selectinload(AggregateColumn.measure))
        .where(AggregateColumn.aggregate_definition_id == agg_id)
    )
    names = []
    seen: set[str] = set()
    for col in result.scalars().all():
        if col.measure is not None and col.measure.name not in seen:
            names.append(col.measure.name)
            seen.add(col.measure.name)
    return names
