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

import logging
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
    DataTarget,
    Dimension,
    Join,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from shared.schemas.domains.aggregates_security import USER_SETTABLE_AGGREGATE_STATUSES
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
from src.api._scope import ensure_ref_in_model
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/aggregates",
    tags=["aggregates"],
)


async def _enforce_max_aggregates(
    db, model_id: UUID
) -> list[AggregateDefinition]:
    """Stage cap victims as non-routable and return them for post-commit purge.

    This function deliberately performs no physical I/O.  Its caller commits
    the victims' ``retired`` state together with the replacement definition,
    then drops the returned tables.  A rollback can therefore never restore an
    ``active`` victim whose external table has already been removed (Bug-8939).
    """
    model = await db.get(Model, model_id)
    if model is None:
        return []
    count_result = await db.execute(
        select(func.count()).where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
        )
    )
    count = count_result.scalar_one()
    if count < model.max_aggregates:
        return []

    policy = getattr(model, "predictive_eviction_policy", DEFAULT_EVICTION_POLICY)
    if policy not in EVICTION_POLICIES:
        policy = DEFAULT_EVICTION_POLICY
    if policy == "never_evict":
        return []

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
    victims = actives[:to_retire]
    for agg in victims:
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
    if victims:
        await db.flush()
    return victims


async def _purge_committed_cap_victims(
    db, victims: list[AggregateDefinition]
) -> None:
    """Drop cap victims only after their non-routable state is durable.

    The shared drop helper records the purge stamp and terminal lifecycle event.
    Those writes are committed separately.  Failure cannot resurrect a victim:
    its retirement is already durable, and an unstamped physical table remains
    a storage leak rather than a routable missing-table/data-loss condition.
    """
    if not victims:
        return
    for agg in victims:
        try:
            await drop_aggregate_physical_table(
                agg, db, reason="cap_enforcement"
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.warning(
                "Bug-8939: post-commit purge of cap victim %s failed: %s",
                agg.id,
                exc,
            )
    try:
        await db.commit()
    except Exception as exc:  # pragma: no cover - terminal evidence retry path
        await db.rollback()
        logger.warning(
            "Bug-8939: purge evidence commit failed after cap retirement: %s",
            exc,
        )


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

        # Bug-8026 (API half): ``target_id`` is a NOT NULL body foreign key to
        # ``data_targets`` and was written straight into the definition. RBAC
        # only proves the CALLER may act in the PATH project; nothing proved
        # the submitted target belonged to it. A foreign target_id therefore
        # pointed this aggregate's materialisation — a CREATE TABLE AS plus
        # every scheduled refresh — at ANOTHER PROJECT'S warehouse connection,
        # writing this model's data through credentials the caller has no
        # rights to (DataTarget.project_connection_id).
        #
        # The optimizer's twin lifecycle boundary
        # (services/optimizer/src/lifecycle/creator.py:create_aggregate) has
        # validated this since Bug-8026; only the model-service API path was
        # left open. This closes it with the canonical primitive, which proves
        # project -> model -> target in one query rather than the optimizer's
        # two-step get-then-compare.
        #
        # It runs with the rest of request validation BEFORE cap retirement.
        # Even though Bug-8939 now defers the physical DROP until after the
        # retirement commit, a refused request must not mutate a victim at all.
        await ensure_ref_in_model(
            db,
            DataTarget,
            ref_id=body.target_id,
            model_id=model_id,
            project_id=project_id,
            field_name="target_id",
            required=True,
            noun="a data target",
        )

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
            else:
                # Bug-8257: the stored stat type is the measure's OWN
                # aggregation, never a stand-in derived from ``is_additive``.
                #
                # This branch used to read ``elif not m.is_additive: ->
                # count_distinct``, which conflated two unrelated things: WHICH
                # statistic to materialise, and WHETHER the stored statistic may
                # be rolled up to a coarser grain. Only the first belongs here.
                # A measure declared non-additive with ``default_agg='max'``
                # stored a COUNT(DISTINCT) column that nothing ever asks for, so
                # the aggregate could never serve the measure at all; and once
                # ``is_additive`` is correctly coerced for avg/min/max/quantile
                # measures, EVERY such aggregate would have silently become a
                # count-distinct aggregate.
                #
                # Roll-up safety is a SEPARATE question, decided at serve time
                # by ``aggregate_matcher.compute_has_non_additive`` — which
                # forces an EXACT-grain match for a non-additive measure — never
                # by which columns exist. (That function short-circuits on the
                # flag before consulting the routing registry; the consequences
                # of that are an open decision, see
                # docs/questions/questions_measure-additivity-vs-rollup.md.)
                # Either way, dropping the conflation here cannot let a stored
                # statistic be re-aggregated where it must not be.
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

        # Bug-8939: every rejectable property above is validated before the cap
        # transition is even selected.  The helper stages the victim as retired
        # but performs no external DROP.  The metadata commit below makes that
        # non-routable transition durable together with the replacement; only
        # then may the physical table be removed and terminal evidence written.
        cap_victims = await _enforce_max_aggregates(db, model_id)

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
            # Bug-5891: only the routable percentile subset (p50 today) gets a
            # coverage row. Non-median percentiles are NOT registered because SQL
            # routing cannot reach them yet — registering them would create
            # coverage rows the refresh materialises into dead columns. See
            # shared.aggregate_quantiles.ROUTABLE_QUANTILE_STAT_TYPES.
            from shared.aggregate_quantiles import (
                ROUTABLE_QUANTILE_STAT_TYPES,
                QUANTILE_STAT_TYPES,
            )
            from shared.aggregate_stats import STAT_TYPES
            _extra: list[str] = []
            if body.include_quantiles:
                _extra += ROUTABLE_QUANTILE_STAT_TYPES
                _deferred = [
                    s for s in QUANTILE_STAT_TYPES
                    if s not in ROUTABLE_QUANTILE_STAT_TYPES
                ]
                if _deferred:
                    logger.info(
                        "Aggregate %s: include_quantiles requested; materialising "
                        "only routable percentiles %s. Non-median percentiles %s "
                        "are deferred pending Bug-5891 (sql_parser/binder routing).",
                        agg.id, ROUTABLE_QUANTILE_STAT_TYPES, _deferred,
                    )
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
        await _purge_committed_cap_victims(db, cap_victims)
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
        if not aggs:
            return []

        agg_ids = [a.id for a in aggs]

        # Bug-6554: batch measure-name and AI-rationale queries instead of
        # issuing ~2 per aggregate on a thrice-polled endpoint.
        # 1) Batch load all AggregateColumns + their measures in one query.
        from sqlalchemy.orm import selectinload
        cols_result = await db.execute(
            select(AggregateColumn)
            .options(selectinload(AggregateColumn.measure))
            .where(AggregateColumn.aggregate_definition_id.in_(agg_ids))
        )
        all_cols = cols_result.scalars().all()
        measure_names_by_agg: dict[UUID, list[str]] = {aid: [] for aid in agg_ids}
        seen_by_agg: dict[UUID, set[str]] = {aid: set() for aid in agg_ids}
        for col in all_cols:
            if col.measure is not None and col.measure.name not in seen_by_agg[col.aggregate_definition_id]:
                measure_names_by_agg[col.aggregate_definition_id].append(col.measure.name)
                seen_by_agg[col.aggregate_definition_id].add(col.measure.name)

        # 2) Batch load AI rationales for ai-created aggregates in one query.
        ai_agg_ids = [a.id for a in aggs if a.creation_reason == "ai"]
        rationale_by_agg: dict[UUID, str | None] = {}
        if ai_agg_ids:
            # Get the latest rationale per aggregate_definition_id.
            rationale_rows = await db.execute(
                select(
                    AIAggregateRecommendation.aggregate_definition_id,
                    AIAggregateRecommendation.rationale,
                )
                .where(AIAggregateRecommendation.aggregate_definition_id.in_(ai_agg_ids))
                .order_by(
                    AIAggregateRecommendation.aggregate_definition_id,
                    AIAggregateRecommendation.created_at.desc(),
                )
            )
            for agg_def_id, rationale in rationale_rows.all():
                # First row per agg_def_id wins (ordered desc by created_at).
                if agg_def_id not in rationale_by_agg:
                    rationale_by_agg[agg_def_id] = rationale

        responses = []
        for a in aggs:
            resp = AggregateDefinitionResponse.model_validate(a)
            resp.measure_names = measure_names_by_agg.get(a.id, [])
            resp.rationale = rationale_by_agg.get(a.id) if a.creation_reason == "ai" else None
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

        update_values = body.model_dump(exclude_unset=True)
        # Bug-6549: defence in depth — the AggregateDefinitionUpdate schema
        # rejects unknown status values at parse time, but guard here too (fail
        # fast, before touching the row) so a bad status can never reach the
        # persisted setattr and silently pull the aggregate out of the
        # routing/refresh paths. An explicit ``null`` is also rejected: status
        # is a NOT NULL lifecycle column, so a PATCH {"status": null} must fail
        # closed with 422, never hit a NOT NULL 500.
        # Bug-6817: only user-settable statuses accepted via API. System-managed
        # states (invalid, pending) are set directly by internal callers.
        if (
            "status" in update_values
            and update_values["status"] not in USER_SETTABLE_AGGREGATE_STATUSES
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "status must be one of: "
                    + ", ".join(USER_SETTABLE_AGGREGATE_STATUSES)
                ),
            )

        # Bug-7903 (Fable HIGH #1): a status PATCH must NOT lift an aggregate OUT
        # of a system-managed lifecycle state (``pending``/``invalid``). Those
        # states mean a refresh is in flight or the physical table is in doubt: the
        # uniform refresh pending-guard commits ``pending`` before it durably
        # replaces the target rows, so a user PATCH to ``active`` in that window
        # would re-open the DG99-CRITICAL-01 window — serving the new physical rows
        # under the PRIOR run's VERIFIED proof (wrong numbers). Only the refresh
        # engine may transition out of ``pending``/``invalid`` (restoring the
        # aggregate to its durable prior status once the new build + manifest +
        # evidence commit atomically). A user who wants to disable/retire such an
        # aggregate must wait for the in-flight refresh to settle. Reject with 409.
        if (
            "status" in update_values
            and a.status in ("pending", "invalid")
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Aggregate is in the system-managed '{a.status}' state (a "
                    "refresh is in progress or the physical table is being rebuilt). "
                    "Its status cannot be changed until the refresh engine settles "
                    "it; retry once it returns to 'active' or 'disabled'."
                ),
            )

        prev_status = a.status
        prev_stats = a.include_stats
        prev_quantiles = a.include_quantiles
        prev_target_schema = a.target_schema
        for k, v in update_values.items():
            setattr(a, k, v)

        # The physical location is part of the materialisation's build
        # identity. Redirecting an active definition to another schema cannot
        # make the already-built table exist there, so withhold routing until a
        # refresh rebuilds and verifies the aggregate at the new location.
        if (
            "target_schema" in update_values
            and a.target_schema != prev_target_schema
        ):
            a.last_refreshed_at = None
            a.is_stale = True

        # Bug-6170 enable-path: when an aggregate transitions from disabled
        # to active, its AggregateRefreshPolicy must be re-enabled and the
        # aggregate marked stale so the scheduler picks it up for its first
        # build.  Without this, an AI aggregate enabled via the UI stays
        # permanently unscheduled because the optimizer created the policy
        # row with is_enabled=False for disabled aggregates.
        if prev_status == "disabled" and a.status == "active":
            policy_result = await db.execute(
                select(AggregateRefreshPolicy).where(
                    AggregateRefreshPolicy.aggregate_definition_id == a.id
                )
            )
            policy = policy_result.scalar_one_or_none()
            if policy is not None:
                policy.is_enabled = True
            else:
                # No policy row exists — create one following the same
                # pattern as the create endpoint so the scheduler sweep
                # can schedule this aggregate.
                default_cron = await get_setting(
                    "aggregate.default_cron",
                    tenant_session=db,
                    project_id=project_id,
                    model_id=model_id,
                )
                db.add(AggregateRefreshPolicy(
                    aggregate_definition_id=a.id,
                    refresh_mode="scheduled",
                    cron_expression=default_cron,
                    is_enabled=True,
                ))
            a.is_stale = True

        # The query-router matches against AggregateColumn coverage rows, not
        # the physical table. Flipping include_stats / include_quantiles on the
        # boolean alone is therefore inert downstream — and flipping it off
        # leaves orphan rows that the next refresh will not materialise. Keep
        # the coverage rows in lock-step with the flags, then force a full
        # rebuild so the physical columns match before the router serves any
        # exact-grain stat/quantile query.
        # Bug-5891: ADD only the routable percentile subset (p50 today) so a
        # newly-enabled include_quantiles never registers a dead non-median
        # column. REMOVE still uses the full set so turning include_quantiles
        # off cleans any legacy p90/p95/... coverage rows a prior build left.
        from shared.aggregate_quantiles import (
            ROUTABLE_QUANTILE_STAT_TYPES,
            QUANTILE_STAT_TYPES,
        )
        from shared.aggregate_stats import STAT_TYPES

        add_types: list[str] = []
        if bool(a.include_quantiles) and not prev_quantiles:
            add_types += ROUTABLE_QUANTILE_STAT_TYPES
            _deferred = [
                s for s in QUANTILE_STAT_TYPES
                if s not in ROUTABLE_QUANTILE_STAT_TYPES
            ]
            if _deferred:
                logger.info(
                    "Aggregate %s: include_quantiles enabled; materialising only "
                    "routable percentiles %s. Non-median percentiles %s deferred "
                    "pending Bug-5891 (sql_parser/binder routing).",
                    a.id, ROUTABLE_QUANTILE_STAT_TYPES, _deferred,
                )
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
