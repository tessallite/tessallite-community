"""Snapshot rehydrator.

Pours a snapshot dict back into the live per-model tables. Used by
Revert (Phase 4), Import (Phase 6), and the data-migration script that
backfills v1 snapshots for existing models (Phase 5).

Strategy
--------
Inside one transaction:

  1. Optionally retire aggregate physical tables whose definitions don't
     appear in the snapshot (per F-8: revert deletes orphan aggregates).
  2. Truncate every per-model child table for this model.
  3. Insert snapshot rows in dependency order.
  4. Update model_settings rows.
  5. Update models.canvas_layout + scalar fields.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    AggregateLifecycleEvent,
    AggregateRefreshPolicy,
    AggregateRefreshRun,
    CalendarTable,
    DataQualityRule,
    DataSource,
    DataTag,
    DataTarget,
    Dimension,
    DrillThroughSet,
    EntityTranslation,
    GlossaryAttachment,
    GlossaryEntry,
    GlossarySynonym,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Join,
    KPI,
    LineageMapping,
    Measure,
    Model,
    ModelAISchedulerConfig,
    ModelAlert,
    ModelAliasMap,
    ModelColumn,
    ModelParameter,
    ModelSetting,
    ModelTable,
    NamedSet,
    Persona,
    PersonaTagRestriction,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshPolicy,
    PocketRefreshRun,
    RefreshSLAConfig,
    RowSecurityRule,
    SourceColumnStatistics,
    SourceJoinStatistics,
    SourceStatistics,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
    data_tag_columns,
)
from shared.model_snapshot.serialiser import SNAPSHOT_SCHEMA_VERSION


class SnapshotSchemaError(ValueError):
    """Raised when the snapshot is missing required keys or fields."""


class SnapshotVersionError(ValueError):
    """Raised when the snapshot's schema_version is newer than this build."""


def _guard_date_intelligence_wipe(
    snapshot: dict, model_id: Any, *, has_existing_hierarchies: bool
) -> None:
    """Bug-5348 — abort rather than silently wipe date intelligence.

    A snapshot that OMITS the ``hierarchies`` key (a legacy v1 snapshot) would,
    under the rehydrator's delete-then-insert, drop a model's existing
    hierarchy/calendar levels with no error. We refuse loudly. An explicit empty
    ``hierarchies`` list is a deliberate clear and is allowed through.
    """
    if "hierarchies" not in snapshot and has_existing_hierarchies:
        raise SnapshotSchemaError(
            f"Refusing to rehydrate model {model_id}: the snapshot omits the "
            "'hierarchies' key but the model already has date intelligence. "
            "Rehydrating would silently drop its hierarchy/calendar levels. "
            "Re-export the snapshot from a current model version, or pass an "
            "explicit empty 'hierarchies' list to clear them deliberately."
        )


# F-013-07: the set of Model columns that are NOT rehydrated from a snapshot.
# Everything else on Model.__table__ travels. Deriving the field list from the
# ORM (rather than a hand-maintained inclusion tuple) means a newly-added Model
# column cannot silently fall out of the snapshot contract — the previous fixed
# tuple of 16 names dropped 8 newer columns (predictive_*, pocket_size_budget_*,
# glossary_max_distinct, expose_kpis_inline, fiscal_year_start_month) on every
# import and revert.
#
#   - id / project_id        — identity, set when the Model row is created
#   - deployed_version_id    — the deploy pointer, managed by deploy/undeploy
#   - last_deployed_at       — stamped by deploy, not part of model shape
#   - created_at / updated_at — DB-managed timestamps
#
# ``seed`` IS rehydrated by default (revert must restore the model's own seed),
# but is skipped on import via the ``preserve_destination_seed`` flag so a clone
# keeps its fresh seed (F-013-05). The serialiser already excludes the deploy
# pointer + timestamps from the snapshot; this exclusion set is the rehydrate
# mirror so the contract stays symmetric.
_MODEL_SCALAR_EXCLUDE: frozenset[str] = frozenset(
    {
        "id",
        "project_id",
        "deployed_version_id",
        "last_deployed_at",
        "created_at",
        "updated_at",
    }
)


def _model_scalar_fields() -> tuple[str, ...]:
    """Every rehydratable Model scalar column, derived live from the ORM."""
    return tuple(
        c.name
        for c in Model.__table__.columns
        if c.name not in _MODEL_SCALAR_EXCLUDE
    )


# Columns that carry a UUID value and must be coerced from the snapshot's
# stringified form before the UPDATE. Derived from the ORM so a new FK column
# is handled without a code edit.
def _model_uuid_fields() -> frozenset[str]:
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

    return frozenset(
        c.name
        for c in Model.__table__.columns
        if c.name not in _MODEL_SCALAR_EXCLUDE
        and isinstance(c.type, PG_UUID)
    )


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


async def rehydrate_into_live(
    model_id: UUID,
    snapshot: dict[str, Any],
    tenant_db: AsyncSession,
    *,
    drop_orphan_aggregates: bool = True,  # deprecated, use preserve_aggregates
    actor: str = "rehydrator",
    connection_id_remap: dict[str, str] | None = None,
    llm_id_remap: dict[str, str] | None = None,
    force_aggregate_pending: bool = False,
    force_pocket_stale: bool = False,
    preserve_aggregates: bool = False,
    preserve_pockets: bool = False,
    preserve_named_sets: bool = False,
    preserve_destination_seed: bool = False,
) -> None:
    """Replace the live state of ``model_id`` with the snapshot's contents.

    Wraps the work in a single transaction (the caller's session); commit
    is the caller's responsibility.

    When ``preserve_aggregates`` is True, existing aggregates are left
    untouched — they are neither retired nor deleted. This allows
    aggregates to retire naturally via the scheduler when they are no
    longer used, rather than being force-retired on revert.

    When ``preserve_pockets`` is True, existing pocket tables are left
    untouched for the same reason.

    When ``preserve_named_sets`` is True, existing named sets are left
    untouched. Named sets that reference removed dimensions will become
    invalid at query time but are not force-deleted.
    """
    if not isinstance(snapshot, dict):
        raise SnapshotSchemaError("snapshot must be a dict")
    schema_version = snapshot.get("schema_version")
    if schema_version is None:
        raise SnapshotSchemaError("snapshot missing schema_version")
    if int(schema_version) > SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotVersionError(
            f"snapshot schema_version={schema_version} exceeds runtime "
            f"version {SNAPSHOT_SCHEMA_VERSION} — upgrade Tessallite first"
        )
    model_row = await tenant_db.get(Model, model_id)
    if model_row is None:
        raise SnapshotSchemaError(f"model {model_id} not found in tenant DB")
    # Capture the destination model's seed before any scalar overwrite so the
    # aggregate/pocket physical-name reseed (F-013-05) always uses the fresh
    # destination value, regardless of statement ordering.
    destination_seed = model_row.seed

    # 1. Optionally retire orphan aggregates (definitions that exist in
    #    current state but NOT in the snapshot). When NOT preserving, the
    #    truncate deletes every aggregate anyway, so marking is redundant.
    #    When preserving (revert), a revert to an older version legitimately
    #    orphans newer aggregates, which must be marked retired (the sweep
    #    drops the physical table) — preserved aggregates that ARE in the
    #    snapshot survive untouched. Marking (not deleting) is transaction-safe
    #    and avoids the data_targets FK chain entirely.
    if drop_orphan_aggregates and preserve_aggregates:
        snapshot_agg_ids: set[UUID] = {
            _coerce_uuid(a.get("id"))
            for a in snapshot.get("aggregates", [])
            if a.get("id")
        }
        live_q = await tenant_db.execute(
            select(AggregateDefinition.id, AggregateDefinition.physical_table_name)
            .where(AggregateDefinition.model_id == model_id)
        )
        for live_id, _phys in live_q.all():
            if live_id not in snapshot_agg_ids:
                # Mark as retired so the next retirement sweep drops the
                # physical table; we don't drop it here because that would
                # require a connection to the target DB.
                await tenant_db.execute(
                    update(AggregateDefinition)
                    .where(AggregateDefinition.id == live_id)
                    .values(status="retired", retired_at=datetime.now(timezone.utc))
                )

    # F-013-02: when aggregates or pockets are preserved, the surviving
    # aggregate_definitions / pocket_definitions rows carry NOT-NULL FKs to
    # data_targets (and data_sources) with no ondelete action. Deleting those
    # parents in the truncate raises an FK violation at statement time → the
    # revert request 500s and rolls back. So in the preserve case we keep
    # data_targets / data_sources in place and UPSERT them from the snapshot
    # (same PKs travel) rather than delete+reinsert.
    preserve_targets = preserve_aggregates or preserve_pockets

    # F-013-02 follow-on (Bug-1093): AggregateColumn.measure_id FK to measures
    # is ON DELETE SET NULL. The truncate deletes every measure (then reinserts
    # with identical PKs), which would null out the measure links on preserved
    # aggregate columns — silently disabling the aggregate in the matcher
    # (which routes via col.measure). Capture the links before truncate and
    # restore them after measures are reinserted.
    preserved_agg_col_measure_links: dict[UUID, UUID] = {}
    if preserve_aggregates:
        link_q = await tenant_db.execute(
            select(AggregateColumn.id, AggregateColumn.measure_id)
            .join(
                AggregateDefinition,
                AggregateColumn.aggregate_definition_id == AggregateDefinition.id,
            )
            .where(
                AggregateDefinition.model_id == model_id,
                AggregateColumn.measure_id.isnot(None),
            )
        )
        preserved_agg_col_measure_links = {
            row[0]: row[1] for row in link_q.all()
        }

    # Bug-5348 — a legacy snapshot that OMITS the date-intelligence keys (key
    # absent, not an explicit empty list) must not silently wipe a model's
    # existing hierarchies. Fail loud so the operator re-exports from a current
    # version (which always carries 'hierarchies') instead of losing month/year
    # levels with no error. An explicit empty list still clears deliberately.
    if "hierarchies" not in snapshot:
        existing = await tenant_db.execute(
            select(HierarchyDefinition.id)
            .where(HierarchyDefinition.model_id == model_id)
            .limit(1)
        )
        _guard_date_intelligence_wipe(
            snapshot, model_id, has_existing_hierarchies=existing.first() is not None
        )

    # 2. Truncate every per-model child table for this model.
    await _truncate_model_children(
        model_id, tenant_db,
        preserve_aggregates=preserve_aggregates,
        preserve_pockets=preserve_pockets,
        preserve_named_sets=preserve_named_sets,
        preserve_targets=preserve_targets,
    )

    # 3. Insert snapshot rows in dependency order. Sources / targets
    #    must precede calendar_tables (FK data_source_id). Calendar
    #    tables must precede model_tables (FK calendar_table_id). UDAs
    #    reference columns, so they come after tables_and_columns.
    await _insert_data_sources_and_targets(
        model_id, snapshot, tenant_db, connection_id_remap,
        upsert=preserve_targets,
    )
    await _insert_calendar_tables(model_id, snapshot, tenant_db)
    await _insert_tables_and_columns(model_id, snapshot, tenant_db)
    await _insert_udas(model_id, snapshot, tenant_db)
    await _insert_joins(model_id, snapshot, tenant_db)
    await _insert_hierarchies(model_id, snapshot, tenant_db)
    await _insert_dimensions(model_id, snapshot, tenant_db)
    await _synthesize_missing_hierarchy_dimensions(model_id, snapshot, tenant_db)
    await _insert_measures(model_id, snapshot, tenant_db)

    # F-013-02 follow-on (Bug-1093): restore preserved aggregate-column measure
    # links the measure-delete cascade nulled, but only to measures that still
    # exist after reinsert (a measure dropped in the reverted-to version stays
    # NULL, and _validate_preserved_aggregates marks that aggregate invalid).
    if preserved_agg_col_measure_links:
        live_measure_ids = {
            r[0] for r in (
                await tenant_db.execute(
                    select(Measure.id).where(Measure.model_id == model_id)
                )
            ).all()
        }
        for agg_col_id, measure_id in preserved_agg_col_measure_links.items():
            if measure_id in live_measure_ids:
                await tenant_db.execute(
                    update(AggregateColumn)
                    .where(AggregateColumn.id == agg_col_id)
                    .values(measure_id=measure_id)
                )

    if not preserve_named_sets:
        await _insert_named_sets(model_id, snapshot, tenant_db)
    await _insert_kpis(model_id, snapshot, tenant_db)
    await _insert_drill_through_sets(model_id, snapshot, tenant_db)
    if not preserve_aggregates:
        # F-013-05: on an import (force_aggregate_pending), the snapshot's
        # aggregate rows carry the SOURCE model's physical table names
        # (agg_<source_seed>_<suffix>). Rebind them to the destination model's
        # seed so a clone's refresh never writes onto the source's physical
        # table — the seed segment is the destination model row's fresh seed.
        forced_pending = await _insert_aggregates(
            model_id, snapshot, tenant_db,
            force_pending=force_aggregate_pending,
            reseed=destination_seed if force_aggregate_pending else None,
        )
        await _insert_aggregate_lifecycle(model_id, snapshot, tenant_db)
        # Bug-5346: import/reseed forces healthy aggregates to ``pending``
        # (F-013-05 — their snapshot points at the source model's tables, so
        # they must be rebuilt against this destination before serving).
        # Previously this was silent, so the active set appeared to vanish.
        # Record one lifecycle event per forced aggregate AND one summary
        # alert so the transition is auditable and the operator knows a
        # rebuild is pending (the scheduler refresh sweep promotes pending →
        # active on its next run; see execution_aggregate-health-and-import-recovery).
        if forced_pending:
            now = datetime.now(timezone.utc)
            for agg_id, prior_status in forced_pending:
                tenant_db.add(
                    AggregateLifecycleEvent(
                        model_id=model_id,
                        aggregate_id=agg_id,
                        event_type="pending_on_import",
                        reason="forced pending on import; awaiting rebuild",
                        payload={"prior_status": prior_status},
                    )
                )
            tenant_db.add(
                ModelAlert(
                    model_id=model_id,
                    severity="info",
                    category="aggregate_lifecycle",
                    title=(
                        f"{len(forced_pending)} aggregate(s) set pending after import"
                    ),
                    detail=(
                        f"{len(forced_pending)} aggregate(s) were set to 'pending' "
                        "because their materialised tables must be rebuilt against "
                        "this model's target before they can serve queries. They "
                        "are not lost — the scheduled refresh sweep rebuilds them "
                        "(pending → active) on its next run, or trigger a refresh "
                        "from the Aggregates panel."
                    ),
                    related_object_type="aggregate_import",
                    # Fresh id per import so the partial-unique dedup index
                    # (model_id, category, related_object_type, related_object_id)
                    # never collides across repeated imports/reseeds.
                    related_object_id=uuid.uuid4(),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
    if not preserve_pockets:
        await _insert_personas(model_id, snapshot, tenant_db)
        await _insert_pockets(
            model_id, snapshot, tenant_db,
            force_stale=force_pocket_stale,
            reseed=destination_seed if force_pocket_stale else None,
        )
    await _insert_data_tags(model_id, snapshot, tenant_db)
    await _insert_row_security(model_id, snapshot, tenant_db)
    await _insert_glossary(model_id, snapshot, tenant_db)
    await _insert_source_statistics(model_id, snapshot, tenant_db)
    await _insert_source_join_statistics(model_id, snapshot, tenant_db)
    await _insert_ai_scheduler(model_id, snapshot, tenant_db, llm_id_remap=llm_id_remap)
    await _insert_lineage(model_id, snapshot, tenant_db)
    # v3 model-scoped config families (F-013-06). Translations land last:
    # their entity_id may reference a measure/dimension/glossary entry that was
    # reinserted above, but EntityTranslation has no DB FK on entity_id (it is
    # a soft reference), so order is not load-bearing — kept here for clarity.
    await _insert_model_parameters(model_id, snapshot, tenant_db)
    await _insert_model_alias_map(model_id, snapshot, tenant_db)
    await _insert_refresh_sla_config(model_id, snapshot, tenant_db)
    await _insert_data_quality_rules(model_id, snapshot, tenant_db)
    await _insert_entity_translations(model_id, snapshot, tenant_db)

    # 4. Replace model_settings rows.
    await tenant_db.execute(
        delete(ModelSetting).where(ModelSetting.model_id == model_id)
    )
    for key, value in (snapshot.get("model_settings") or {}).items():
        tenant_db.add(
            ModelSetting(
                model_id=model_id,
                key=key,
                value_json=value,
                updated_by=actor,
            )
        )

    # 5. Update Model scalar fields + canvas_layout. Excludes id, project_id,
    #    deployed_version_id, last_deployed_at — those are stable / managed
    #    elsewhere.
    snap_model = snapshot.get("model") or {}
    update_kwargs: dict[str, Any] = {}
    uuid_fields = _model_uuid_fields()
    for field in _model_scalar_fields():
        # F-013-05: on import the destination model carries a fresh seed and
        # its aggregate/pocket physical names were rebound to it. Overwriting
        # `seed` with the source model's value (it IS in the snapshot) would
        # make future optimizer-created aggregates collide with the source's
        # `agg_<source_seed>_*` namespace again. Keep the destination seed.
        if field == "seed" and preserve_destination_seed:
            continue
        if field in snap_model:
            val = snap_model[field]
            if field in uuid_fields:
                val = _coerce_uuid(val)
            update_kwargs[field] = val
    if update_kwargs:
        await tenant_db.execute(
            update(Model).where(Model.id == model_id).values(**update_kwargs)
        )

    # 6. Validate preserved materializations and mark invalid ones.
    if preserve_aggregates:
        await _validate_preserved_aggregates(model_id, tenant_db)
    if preserve_pockets:
        await _validate_preserved_pockets(model_id, tenant_db)
    if preserve_named_sets:
        await _validate_preserved_named_sets(model_id, tenant_db)


# ---------------------------------------------------------------------------
# Validation for preserved materializations
# ---------------------------------------------------------------------------

async def _validate_preserved_aggregates(model_id: UUID, db: AsyncSession) -> None:
    """Mark aggregates as invalid if their grain dimensions no longer exist."""
    # Get current dimension names for this model
    dim_q = await db.execute(
        select(Dimension.name).where(Dimension.model_id == model_id)
    )
    valid_dims = {r[0] for r in dim_q.all()}

    # Check each aggregate's grain against valid dimensions
    agg_q = await db.execute(
        select(AggregateDefinition).where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status.notin_(["retired", "invalid"]),
        )
    )
    for agg in agg_q.scalars().all():
        grain = agg.grain or []
        missing = [g for g in grain if g not in valid_dims]
        if missing:
            await db.execute(
                update(AggregateDefinition)
                .where(AggregateDefinition.id == agg.id)
                .values(
                    status="invalid",
                    invalid_reason=f"Missing dimensions after revert: {', '.join(missing)}",
                )
            )


async def _validate_preserved_pockets(model_id: UUID, db: AsyncSession) -> None:
    """Mark pockets as stale if their predicate dimensions no longer exist."""
    # Get current dimension names for this model
    dim_q = await db.execute(
        select(Dimension.name).where(Dimension.model_id == model_id)
    )
    valid_dims = {r[0] for r in dim_q.all()}

    # Check each pocket's predicates
    pocket_q = await db.execute(
        select(PocketDefinition).where(PocketDefinition.model_id == model_id)
    )
    for pocket in pocket_q.scalars().all():
        pred_q = await db.execute(
            select(PocketPredicate.dimension_name).where(
                PocketPredicate.pocket_definition_id == pocket.id
            )
        )
        pred_dims = {r[0] for r in pred_q.all()}
        missing = pred_dims - valid_dims
        if missing:
            await db.execute(
                update(PocketDefinition)
                .where(PocketDefinition.id == pocket.id)
                .values(is_stale=True)
            )


async def _validate_preserved_named_sets(model_id: UUID, db: AsyncSession) -> None:
    """Mark named sets as deprecated if their referenced dimensions no longer exist."""
    # Get current dimension names for this model
    dim_q = await db.execute(
        select(Dimension.name).where(Dimension.model_id == model_id)
    )
    valid_dim_names = {r[0] for r in dim_q.all()}

    # Check each named set's dimensions field (comma-separated or JSON)
    ns_q = await db.execute(
        select(NamedSet).where(
            NamedSet.model_id == model_id,
            NamedSet.certification_status != "deprecated",
        )
    )
    for ns in ns_q.scalars().all():
        if not ns.dimensions:
            continue
        # dimensions field may be comma-separated list
        dim_refs = [d.strip() for d in ns.dimensions.split(",") if d.strip()]
        missing = [d for d in dim_refs if d not in valid_dim_names]
        if missing:
            await db.execute(
                update(NamedSet)
                .where(NamedSet.id == ns.id)
                .values(certification_status="deprecated")
            )


# ---------------------------------------------------------------------------
# Truncate
# ---------------------------------------------------------------------------

async def _truncate_model_children(
    model_id: UUID,
    db: AsyncSession,
    *,
    preserve_aggregates: bool = False,
    preserve_pockets: bool = False,
    preserve_named_sets: bool = False,
    preserve_targets: bool = False,
) -> None:
    """Delete every per-model child row in dependency-safe order.

    When ``preserve_aggregates`` is True, aggregate definitions and their
    children (columns, policies, runs) are left untouched. They will be
    validated after rehydration and marked invalid if dependencies are
    missing.

    When ``preserve_pockets`` is True, pocket definitions and their
    children are left untouched.

    When ``preserve_named_sets`` is True, named sets are left untouched.
    """

    # Aggregate columns + refresh policies + refresh runs are children of
    # aggregate_definitions. Skip if preserving aggregates.
    if not preserve_aggregates:
        agg_ids_q = await db.execute(
            select(AggregateDefinition.id).where(AggregateDefinition.model_id == model_id)
        )
        agg_ids = [r[0] for r in agg_ids_q.all()]
        if agg_ids:
            await db.execute(
                delete(AggregateColumn).where(AggregateColumn.aggregate_definition_id.in_(agg_ids))
            )
            await db.execute(
                delete(AggregateRefreshPolicy).where(
                    AggregateRefreshPolicy.aggregate_definition_id.in_(agg_ids)
                )
            )
            await db.execute(
                delete(AggregateRefreshRun).where(
                    AggregateRefreshRun.aggregate_definition_id.in_(agg_ids)
                )
            )
        # Aggregate lifecycle events: FK to model_id (CASCADE) + aggregate_id
        # (SET NULL). Delete by model_id so we don't leave stale rows.
        await db.execute(
            delete(AggregateLifecycleEvent).where(
                AggregateLifecycleEvent.model_id == model_id
            )
        )
        await db.execute(
            delete(AggregateDefinition).where(AggregateDefinition.model_id == model_id)
        )

    # Hierarchy children
    hier_ids_q = await db.execute(
        select(HierarchyDefinition.id).where(HierarchyDefinition.model_id == model_id)
    )
    hier_ids = [r[0] for r in hier_ids_q.all()]
    if hier_ids:
        level_ids_q = await db.execute(
            select(HierarchyLevel.id).where(HierarchyLevel.hierarchy_id.in_(hier_ids))
        )
        level_ids = [r[0] for r in level_ids_q.all()]
        if level_ids:
            await db.execute(
                delete(HierarchyLevelAttribute).where(
                    HierarchyLevelAttribute.level_id.in_(level_ids)
                )
            )
        await db.execute(
            delete(HierarchyLevel).where(HierarchyLevel.hierarchy_id.in_(hier_ids))
        )
    await db.execute(
        delete(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id)
    )

    # Drill-through sets (per-measure; CASCADE from measures, but delete
    # explicitly so the order is deterministic).
    measure_ids_q = await db.execute(
        select(Measure.id).where(Measure.model_id == model_id)
    )
    measure_ids = [r[0] for r in measure_ids_q.all()]
    if measure_ids:
        await db.execute(
            delete(DrillThroughSet).where(
                DrillThroughSet.measure_id.in_(measure_ids)
            )
        )

    # Named sets + KPIs (must come before measures because KPIs FK to measures)
    await db.execute(delete(KPI).where(KPI.model_id == model_id))
    if not preserve_named_sets:
        await db.execute(delete(NamedSet).where(NamedSet.model_id == model_id))

    # Measures + dimensions
    await db.execute(delete(Measure).where(Measure.model_id == model_id))
    await db.execute(delete(Dimension).where(Dimension.model_id == model_id))

    # Source-join statistics depend on Join.id (CASCADE). Delete before joins.
    join_ids_q = await db.execute(
        select(Join.id).where(Join.model_id == model_id)
    )
    join_ids = [r[0] for r in join_ids_q.all()]
    if join_ids:
        await db.execute(
            delete(SourceJoinStatistics).where(
                SourceJoinStatistics.join_id.in_(join_ids)
            )
        )

    # Joins
    await db.execute(delete(Join).where(Join.model_id == model_id))

    # Personas (referenced by pockets via SET NULL — order doesn't strictly
    # matter, but we delete pockets-and-friends after personas elsewhere).
    # Glossary (CASCADE on entry_id from synonyms/attachments)
    glossary_ids_q = await db.execute(
        select(GlossaryEntry.id).where(GlossaryEntry.model_id == model_id)
    )
    glossary_ids = [r[0] for r in glossary_ids_q.all()]
    if glossary_ids:
        await db.execute(
            delete(GlossaryAttachment).where(
                GlossaryAttachment.entry_id.in_(glossary_ids)
            )
        )
        await db.execute(
            delete(GlossarySynonym).where(
                GlossarySynonym.entry_id.in_(glossary_ids)
            )
        )
    await db.execute(
        delete(GlossaryEntry).where(GlossaryEntry.model_id == model_id)
    )

    # Data tags (F-008-09): column-assignment rows and persona tag
    # restrictions cascade from the tag side. Tags hang off model columns,
    # which are always rebuilt, so tags are always rebuilt too.
    await db.execute(delete(DataTag).where(DataTag.model_id == model_id))

    # Pocket children + pockets, then personas.
    # Personas are preserved alongside pockets since pockets reference them.
    if not preserve_pockets:
        pocket_ids_q = await db.execute(
            select(PocketDefinition.id).where(PocketDefinition.model_id == model_id)
        )
        pocket_ids = [r[0] for r in pocket_ids_q.all()]
        if pocket_ids:
            await db.execute(
                delete(PocketPredicate).where(
                    PocketPredicate.pocket_definition_id.in_(pocket_ids)
                )
            )
            await db.execute(
                delete(PocketRefreshPolicy).where(
                    PocketRefreshPolicy.pocket_definition_id.in_(pocket_ids)
                )
            )
            await db.execute(
                delete(PocketRefreshRun).where(
                    PocketRefreshRun.pocket_definition_id.in_(pocket_ids)
                )
            )
        await db.execute(
            delete(PocketDefinition).where(PocketDefinition.model_id == model_id)
        )
        await db.execute(delete(Persona).where(Persona.model_id == model_id))

    # Row-security rules: FK to model_tables.mapping_table_id is RESTRICT, so
    # this MUST come before model_tables are deleted.
    await db.execute(
        delete(RowSecurityRule).where(RowSecurityRule.model_id == model_id)
    )

    # AI scheduler config
    await db.execute(
        delete(ModelAISchedulerConfig).where(ModelAISchedulerConfig.model_id == model_id)
    )

    # v3 model-scoped config families (F-013-06). Each cascades on model_id;
    # DataQualityViolation children cascade off DataQualityRule, so deleting
    # the rules is enough. Replaced wholesale from the snapshot below.
    await db.execute(
        delete(ModelParameter).where(ModelParameter.model_id == model_id)
    )
    await db.execute(
        delete(ModelAliasMap).where(ModelAliasMap.model_id == model_id)
    )
    await db.execute(
        delete(RefreshSLAConfig).where(RefreshSLAConfig.model_id == model_id)
    )
    await db.execute(
        delete(DataQualityRule).where(DataQualityRule.model_id == model_id)
    )
    await db.execute(
        delete(EntityTranslation).where(EntityTranslation.model_id == model_id)
    )

    # Lineage mappings
    await db.execute(delete(LineageMapping).where(LineageMapping.model_id == model_id))

    # UDAs (must come before columns because UDAs reference columns by id)
    uda_ids_q = await db.execute(
        select(UserDefinedAttribute.id).where(UserDefinedAttribute.model_id == model_id)
    )
    uda_ids = [r[0] for r in uda_ids_q.all()]
    if uda_ids:
        await db.execute(
            delete(UserDefinedAttributeColumnRef).where(
                UserDefinedAttributeColumnRef.attribute_id.in_(uda_ids)
            )
        )
    await db.execute(
        delete(UserDefinedAttribute).where(UserDefinedAttribute.model_id == model_id)
    )

    # Columns + tables. Source statistics first (FK to model_table_id and
    # data_source_id, both CASCADE — but delete explicitly so a v1 snapshot
    # rehydrate doesn't surprise us with leftover rows).
    table_ids_q = await db.execute(
        select(ModelTable.id).where(ModelTable.model_id == model_id)
    )
    table_ids = [r[0] for r in table_ids_q.all()]
    source_ids_q = await db.execute(
        select(DataSource.id).where(DataSource.model_id == model_id)
    )
    source_ids = [r[0] for r in source_ids_q.all()]
    if source_ids:
        # Column stats are children of source_statistics; delete first.
        stat_ids_q = await db.execute(
            select(SourceStatistics.id).where(
                SourceStatistics.data_source_id.in_(source_ids)
            )
        )
        stat_ids = [r[0] for r in stat_ids_q.all()]
        if stat_ids:
            await db.execute(
                delete(SourceColumnStatistics).where(
                    SourceColumnStatistics.source_statistics_id.in_(stat_ids)
                )
            )
        await db.execute(
            delete(SourceStatistics).where(
                SourceStatistics.data_source_id.in_(source_ids)
            )
        )
        await db.execute(
            delete(CalendarTable).where(CalendarTable.data_source_id.in_(source_ids))
        )
    if table_ids:
        await db.execute(
            delete(ModelColumn).where(ModelColumn.model_table_id.in_(table_ids))
        )
    await db.execute(delete(ModelTable).where(ModelTable.model_id == model_id))

    # Sources + targets (last because tables reference sources).
    # F-013-02: when aggregates/pockets are preserved, their surviving rows
    # FK-reference data_targets (NOT NULL, no ondelete), so deleting the
    # targets here raises an FK violation. Keep them in place; they are
    # upserted from the snapshot by _insert_data_sources_and_targets instead.
    if not preserve_targets:
        await db.execute(delete(DataTarget).where(DataTarget.model_id == model_id))
        await db.execute(delete(DataSource).where(DataSource.model_id == model_id))


# ---------------------------------------------------------------------------
# Inserters
# ---------------------------------------------------------------------------

_ISO_DT_LEN_MIN = 19  # "YYYY-MM-DDTHH:MM:SS"


def _looks_like_iso_datetime(value: str) -> bool:
    if len(value) < _ISO_DT_LEN_MIN:
        return False
    return value[4] == "-" and value[7] == "-" and value[10] in ("T", " ")


def _strip_pk_and_uuids(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce stringified UUIDs and ISO datetimes back into native Python
    types so asyncpg accepts them at the dialect layer.

    Also drops keys with ``None`` values so columns fall back to SQL NULL
    via column defaults, rather than asyncpg's JSONB adapter encoding
    Python None as the JSONB ``'null'`` literal — which would defeat
    ``IS NULL`` shape-check constraints (e.g. row_security_rules).
    """
    out: dict[str, Any] = {}
    for k, v in row.items():
        if v is None:
            continue
        if isinstance(v, str):
            if len(v) == 36 and v.count("-") == 4:
                try:
                    out[k] = UUID(v)
                    continue
                except ValueError:
                    pass
            if _looks_like_iso_datetime(v):
                try:
                    out[k] = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    continue
                except ValueError:
                    pass
        out[k] = v
    return out


async def _insert_data_sources_and_targets(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    connection_id_remap: dict[str, str] | None = None,
    *,
    upsert: bool = False,
) -> None:
    """Insert (or, when ``upsert`` is True, upsert) data sources and targets.

    F-013-02: revert preserves aggregates/pockets, whose rows FK-reference
    data_targets / data_sources. Those parents are therefore NOT truncated;
    they are upserted by id here so any snapshot-level change to a source /
    target is applied without breaking the surviving FK references.
    """
    async def _write(model_cls: type, row: dict[str, Any]) -> None:
        if not upsert:
            await db.execute(insert(model_cls).values(**row))
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        pk = row.get("id")
        if pk is None:
            await db.execute(insert(model_cls).values(**row))
            return
        update_cols = {k: v for k, v in row.items() if k != "id"}
        stmt = pg_insert(model_cls).values(**row)
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
        await db.execute(stmt)

    for s in snap.get("data_sources", []):
        row = _strip_pk_and_uuids(s)
        row["model_id"] = model_id
        # Pre-RA-1 snapshots may carry the dropped column data_sources.calendar_table_id;
        # the column no longer exists on DataSource. Drop defensively.
        row.pop("calendar_table_id", None)
        if connection_id_remap:
            old_cid = str(row.get("project_connection_id", ""))
            if old_cid in connection_id_remap:
                row["project_connection_id"] = UUID(connection_id_remap[old_cid])
        await _write(DataSource, row)
    for t in snap.get("data_targets", []):
        row = _strip_pk_and_uuids(t)
        row["model_id"] = model_id
        if connection_id_remap:
            old_cid = str(row.get("project_connection_id", ""))
            if old_cid in connection_id_remap:
                row["project_connection_id"] = UUID(connection_id_remap[old_cid])
        await _write(DataTarget, row)


async def _insert_tables_and_columns(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for t in snap.get("tables", []):
        row = _strip_pk_and_uuids(t)
        row["model_id"] = model_id
        await db.execute(insert(ModelTable).values(**row))
    for c in snap.get("columns", []):
        row = _strip_pk_and_uuids(c)
        await db.execute(insert(ModelColumn).values(**row))


async def _insert_udas(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for u in snap.get("user_defined_attributes", []):
        row = _strip_pk_and_uuids(u)
        row["model_id"] = model_id
        await db.execute(insert(UserDefinedAttribute).values(**row))
    for r in snap.get("uda_column_refs", []):
        row = _strip_pk_and_uuids(r)
        await db.execute(insert(UserDefinedAttributeColumnRef).values(**row))


async def _insert_joins(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for j in snap.get("joins", []):
        row = _strip_pk_and_uuids(j)
        row["model_id"] = model_id
        await db.execute(insert(Join).values(**row))


async def _insert_hierarchies(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Insert hierarchies, levels, and level-attributes."""
    _HIER_NESTED = {"levels"}
    _LEVEL_NESTED = {"attributes"}
    for h in snap.get("hierarchies", []):
        levels = h.get("levels", [])
        h_flat = {k: v for k, v in h.items() if k not in _HIER_NESTED}
        row = _strip_pk_and_uuids(h_flat)
        row["model_id"] = model_id
        await db.execute(insert(HierarchyDefinition).values(**row))
        for lvl in levels:
            attrs = lvl.get("attributes", [])
            l_flat = {k: v for k, v in lvl.items() if k not in _LEVEL_NESTED}
            l_row = _strip_pk_and_uuids(l_flat)
            await db.execute(insert(HierarchyLevel).values(**l_row))
            for a in attrs:
                a_row = _strip_pk_and_uuids(a)
                await db.execute(insert(HierarchyLevelAttribute).values(**a_row))


async def _insert_dimensions(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for d in snap.get("dimensions", []):
        row = _strip_pk_and_uuids(d)
        row["model_id"] = model_id
        await db.execute(insert(Dimension).values(**row))


_TIME_UNIT_TO_GRAIN: dict[str, str] = {
    "year": "year",
    "half": "half",
    "quarter": "quarter",
    "month": "month",
    "week": "week",
    "day": "day",
}


async def _synthesize_missing_hierarchy_dimensions(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Create Dimension rows for date_embedded hierarchy levels whose UDA
    has no corresponding Dimension in the snapshot.

    Older snapshots (or bootstrap scripts) stored the UDA and hierarchy
    level but forgot the Dimension row that surfaces the generated
    column in the pivot panel and query interfaces.
    """
    existing_uda_dims: set[str] = set()
    for d in snap.get("dimensions", []):
        uda_id = d.get("user_defined_attribute_id")
        if uda_id:
            existing_uda_dims.add(str(uda_id))

    uda_by_id: dict[str, dict[str, Any]] = {}
    for u in snap.get("user_defined_attributes", []):
        uda_by_id[str(u["id"])] = u

    for h in snap.get("hierarchies", []):
        if h.get("type") != "date_embedded":
            continue
        h_name = h.get("name", "")
        for lvl in h.get("levels", []):
            if lvl.get("key_attribute_source") != "user_defined_attribute":
                continue
            uda_id_str = str(lvl["key_attribute_id"])
            if uda_id_str in existing_uda_dims:
                continue
            uda = uda_by_id.get(uda_id_str)
            if not uda:
                continue
            time_unit = lvl.get("time_unit", "")
            grain = _TIME_UNIT_TO_GRAIN.get(time_unit)
            level_name = lvl.get("name", time_unit)
            gen_name = uda["name"]
            await db.execute(
                insert(Dimension).values(
                    id=uuid.uuid4(),
                    model_id=model_id,
                    name=gen_name,
                    display_name=f"{level_name} ({h_name})",
                    user_defined_attribute_id=UUID(uda_id_str),
                    is_time_dim=True,
                    time_grain=grain,
                    description=f"Auto-generated for hierarchy '{h_name}' ({time_unit})",
                )
            )
            existing_uda_dims.add(uda_id_str)


def _validate_measure_enums(row: dict[str, Any]) -> None:
    """Fail loud if a measure row carries an invalid enum value (F-020-09).

    The rehydrator inserts measure rows directly, so the Pydantic API
    validators never run. Ecosystem importers historically invented
    semi_additive_behavior strings (e.g. "last_value", "max_over_order_date")
    that the rewriter could not interpret, producing unpredictable query-time
    behaviour with no error at import. This gate rejects them at the snapshot
    boundary instead of silently storing junk.
    """
    from shared.schemas.domains.dimensions_measures import (
        VALID_SEMI_ADDITIVE_BEHAVIORS,
    )

    behavior = row.get("semi_additive_behavior")
    if behavior is not None and behavior not in VALID_SEMI_ADDITIVE_BEHAVIORS:
        raise SnapshotSchemaError(
            f"measure {row.get('name', row.get('id'))!r} has invalid "
            f"semi_additive_behavior {behavior!r}; must be one of "
            f"{sorted(VALID_SEMI_ADDITIVE_BEHAVIORS)} or null"
        )


async def _insert_measures(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Insert measures with variants AFTER their base measures.

    ``measures.variant_of_measure_id`` is a self-FK; raw snapshot order
    can place a variant before its base. We topologically order by
    inserting bases first, then iteratively any rows whose parent is
    already inserted, until none remain.
    """
    pending = list(snap.get("measures", []))
    inserted_ids: set[str] = set()

    def _ready(row: dict[str, Any]) -> bool:
        parent = row.get("variant_of_measure_id")
        if not parent:
            return True
        return str(parent) in inserted_ids

    while pending:
        next_round = [m for m in pending if _ready(m)]
        if not next_round:
            unresolved = [m.get("id") for m in pending]
            raise SnapshotSchemaError(
                f"measure variant_of_measure_id chain unresolvable: {unresolved}"
            )
        for m in next_round:
            row = _strip_pk_and_uuids(m)
            row["model_id"] = model_id
            _validate_measure_enums(row)
            await db.execute(insert(Measure).values(**row))
            inserted_ids.add(str(m["id"]))
        pending = [m for m in pending if str(m.get("id")) not in inserted_ids]


async def _insert_named_sets(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for ns in snap.get("named_sets", []) or []:
        row = _strip_pk_and_uuids(ns)
        row["model_id"] = model_id
        await db.execute(insert(NamedSet).values(**row))


async def _insert_kpis(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    pending = list(snap.get("kpis", []) or [])
    inserted_ids: set[str] = set()

    def _ready(row: dict[str, Any]) -> bool:
        parent = row.get("parent_kpi_id")
        if not parent:
            return True
        return str(parent) in inserted_ids

    while pending:
        next_round = [k for k in pending if _ready(k)]
        if not next_round:
            for k in pending:
                row = _strip_pk_and_uuids(k)
                row["model_id"] = model_id
                row.pop("parent_kpi_id", None)
                await db.execute(insert(KPI).values(**row))
                inserted_ids.add(str(k["id"]))
            break
        for k in next_round:
            row = _strip_pk_and_uuids(k)
            row["model_id"] = model_id
            await db.execute(insert(KPI).values(**row))
            inserted_ids.add(str(k["id"]))
        pending = [k for k in pending if str(k.get("id")) not in inserted_ids]


def _reseed_physical_table_name(
    name: Any, prefix: str, new_seed: str | None
) -> Any:
    """Rebind the seed segment of a ``<prefix>_<seed>_<suffix>`` physical
    table name to ``new_seed`` (F-013-05).

    On import the destination model gets a fresh seed, but snapshot aggregate
    / pocket rows still carry the SOURCE model's seed embedded in their
    physical name. Routing/refresh keys off ``physical_table_name``, so an
    un-rebound clone would read and rewrite the source model's physical
    tables. We replace only the middle (seed) segment and keep the trailing
    suffix so the name stays unique within the destination model. Seeds are
    hex tokens or UUIDs (neither contains ``_``), so splitting on ``_`` is
    safe. Names that don't match the expected 3-part shape are left as-is.
    """
    if not new_seed or not isinstance(name, str):
        return name
    parts = name.split("_")
    if len(parts) != 3 or parts[0] != prefix:
        return name
    return f"{prefix}_{new_seed}_{parts[2]}"


async def _insert_aggregates(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    force_pending: bool = False,
    reseed: str | None = None,
) -> list[tuple[UUID, str]]:
    """Insert aggregate definitions from a snapshot.

    Returns the list of ``(aggregate_id, prior_status)`` for aggregates that
    were FORCED from a healthy status (active/disabled) to ``pending`` by
    ``force_pending`` — i.e. the ones that "disappear" from the active set on
    import (F-013-05) and must be rebuilt. The caller uses this to emit a
    lifecycle event + alert so the transition is observable, not silent.
    """
    _AGG_NESTED = {"columns", "refresh_policy"}
    forced_pending: list[tuple[UUID, str]] = []
    for a in snap.get("aggregates", []):
        cols = a.get("columns", [])
        policy = a.get("refresh_policy", None)
        a_flat = {k: v for k, v in a.items() if k not in _AGG_NESTED}
        row = _strip_pk_and_uuids(a_flat)
        row["model_id"] = model_id
        row.pop("retired_at", None)
        prior_status = str(a.get("status") or "")
        if force_pending:
            row["status"] = "pending"
            agg_id = row.get("id")
            if isinstance(agg_id, UUID) and prior_status in ("active", "disabled"):
                forced_pending.append((agg_id, prior_status))
        if reseed:
            row["physical_table_name"] = _reseed_physical_table_name(
                row.get("physical_table_name"), "agg", reseed
            )
        await db.execute(insert(AggregateDefinition).values(**row))
        for c in cols:
            c_row = _strip_pk_and_uuids(c)
            await db.execute(insert(AggregateColumn).values(**c_row))
        if policy:
            p_row = _strip_pk_and_uuids(policy)
            await db.execute(insert(AggregateRefreshPolicy).values(**p_row))
    return forced_pending


async def _insert_ai_scheduler(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    llm_id_remap: dict[str, str] | None = None,
) -> None:
    sched = snap.get("ai_scheduler_config")
    if sched:
        row = _strip_pk_and_uuids(sched)
        row["model_id"] = model_id
        if llm_id_remap:
            for fk in ("llm_config_id", "glossary_llm_config_id"):
                if fk in row:
                    old_llm = str(row[fk])
                    if old_llm in llm_id_remap:
                        row[fk] = UUID(llm_id_remap[old_llm])
                    else:
                        # Source LLM config not imported into this tenant — clear FK
                        del row[fk]
        await db.execute(insert(ModelAISchedulerConfig).values(**row))


async def _insert_lineage(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for l in snap.get("lineage_mappings", []):
        row = _strip_pk_and_uuids(l)
        row["model_id"] = model_id
        await db.execute(insert(LineageMapping).values(**row))


# ---------------------------------------------------------------------------
# v2 inserters (Bug-106): personas, pockets, row-security, glossary,
# aggregate lifecycle, source statistics, drill-through, calendar tables.
# Missing keys are treated as empty so v1 snapshots still rehydrate cleanly.
# ---------------------------------------------------------------------------


async def _insert_calendar_tables(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for c in snap.get("calendar_tables", []) or []:
        row = _strip_pk_and_uuids(c)
        await db.execute(insert(CalendarTable).values(**row))


async def _insert_drill_through_sets(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for d in snap.get("drill_through_sets", []) or []:
        row = _strip_pk_and_uuids(d)
        await db.execute(insert(DrillThroughSet).values(**row))


async def _insert_personas(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for p in snap.get("personas", []) or []:
        row = _strip_pk_and_uuids(p)
        row["model_id"] = model_id
        await db.execute(insert(Persona).values(**row))


async def _insert_data_tags(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Data tags, their column assignments, and persona tag restrictions
    (F-008-09).

    Column links are filtered to columns that exist after the
    table/column re-insert; restrictions are filtered to personas present
    in the live model — this also covers the ``preserve_pockets`` branch,
    where personas are preserved in place rather than re-inserted.
    """
    tags = snap.get("data_tags", []) or []
    if not tags:
        return

    col_rows = await db.execute(
        select(ModelColumn.id)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelTable.model_id == model_id)
    )
    live_column_ids = {r[0] for r in col_rows.all()}

    inserted_tag_ids: set[UUID] = set()
    for t in tags:
        column_ids = [_coerce_uuid(c) for c in (t.get("column_ids") or [])]
        t_flat = {k: v for k, v in t.items() if k != "column_ids"}
        row = _strip_pk_and_uuids(t_flat)
        row["model_id"] = model_id
        await db.execute(insert(DataTag).values(**row))
        tag_id = _coerce_uuid(t.get("id"))
        if tag_id is None:
            continue
        inserted_tag_ids.add(tag_id)
        for cid in column_ids:
            if cid in live_column_ids:
                await db.execute(
                    insert(data_tag_columns).values(
                        tag_id=tag_id, model_column_id=cid,
                    )
                )

    persona_rows = await db.execute(
        select(Persona.id).where(Persona.model_id == model_id)
    )
    live_persona_ids = {r[0] for r in persona_rows.all()}
    for r in snap.get("persona_tag_restrictions", []) or []:
        pid = _coerce_uuid(r.get("persona_id"))
        tid = _coerce_uuid(r.get("data_tag_id"))
        if pid in live_persona_ids and tid in inserted_tag_ids:
            await db.execute(
                insert(PersonaTagRestriction).values(
                    persona_id=pid, data_tag_id=tid,
                )
            )


async def _insert_pockets(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    force_stale: bool = False,
    reseed: str | None = None,
) -> None:
    _POCKET_NESTED = {"predicates", "refresh_policy"}
    for p in snap.get("pockets", []) or []:
        preds = p.get("predicates", []) or []
        policy = p.get("refresh_policy", None)
        p_flat = {k: v for k, v in p.items() if k not in _POCKET_NESTED}
        row = _strip_pk_and_uuids(p_flat)
        row["model_id"] = model_id
        if force_stale:
            row["status"] = "stale"
        if reseed:
            row["physical_table_name"] = _reseed_physical_table_name(
                row.get("physical_table_name"), "pocket", reseed
            )
        await db.execute(insert(PocketDefinition).values(**row))
        for pr in preds:
            pr_row = _strip_pk_and_uuids(pr)
            await db.execute(insert(PocketPredicate).values(**pr_row))
        if policy:
            pol_row = _strip_pk_and_uuids(policy)
            await db.execute(insert(PocketRefreshPolicy).values(**pol_row))


async def _insert_row_security(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for r in snap.get("row_security_rules", []) or []:
        row = _strip_pk_and_uuids(r)
        row["model_id"] = model_id
        await db.execute(insert(RowSecurityRule).values(**row))


async def _insert_glossary(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    _GLOSSARY_NESTED = {"synonyms", "attachments"}
    for g in snap.get("glossary_entries", []) or []:
        synonyms = g.get("synonyms", []) or []
        attachments = g.get("attachments", []) or []
        g_flat = {k: v for k, v in g.items() if k not in _GLOSSARY_NESTED}
        row = _strip_pk_and_uuids(g_flat)
        row["model_id"] = model_id
        await db.execute(insert(GlossaryEntry).values(**row))
        for s in synonyms:
            s_row = _strip_pk_and_uuids(s)
            await db.execute(insert(GlossarySynonym).values(**s_row))
        for a in attachments:
            a_row = _strip_pk_and_uuids(a)
            await db.execute(insert(GlossaryAttachment).values(**a_row))


async def _insert_aggregate_lifecycle(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for e in snap.get("aggregate_lifecycle_events", []) or []:
        row = _strip_pk_and_uuids(e)
        row["model_id"] = model_id
        await db.execute(insert(AggregateLifecycleEvent).values(**row))


async def _insert_source_statistics(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for st in snap.get("source_statistics", []) or []:
        cols = st.get("columns", []) or []
        st_flat = {k: v for k, v in st.items() if k != "columns"}
        row = _strip_pk_and_uuids(st_flat)
        await db.execute(insert(SourceStatistics).values(**row))
        for c in cols:
            c_row = _strip_pk_and_uuids(c)
            for text_col in ("min_value", "max_value"):
                if text_col in c_row and not isinstance(c_row[text_col], str):
                    c_row[text_col] = str(c_row[text_col])
            await db.execute(insert(SourceColumnStatistics).values(**c_row))


async def _insert_source_join_statistics(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for j in snap.get("source_join_statistics", []) or []:
        row = _strip_pk_and_uuids(j)
        await db.execute(insert(SourceJoinStatistics).values(**row))


# ---------------------------------------------------------------------------
# v3 inserters (F-013-06): model parameters, alias map, refresh SLA config,
# data-quality rules, entity translations. Missing keys are treated as empty
# so v1/v2 snapshots still rehydrate cleanly.
# ---------------------------------------------------------------------------


async def _insert_model_parameters(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for p in snap.get("model_parameters", []) or []:
        row = _strip_pk_and_uuids(p)
        row["model_id"] = model_id
        await db.execute(insert(ModelParameter).values(**row))


async def _insert_model_alias_map(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    alias = snap.get("model_alias_map")
    if not alias:
        return
    row = _strip_pk_and_uuids(alias)
    row["model_id"] = model_id
    await db.execute(insert(ModelAliasMap).values(**row))


async def _insert_refresh_sla_config(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    sla = snap.get("refresh_sla_config")
    if not sla:
        return
    row = _strip_pk_and_uuids(sla)
    row["model_id"] = model_id
    await db.execute(insert(RefreshSLAConfig).values(**row))


async def _insert_data_quality_rules(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for r in snap.get("data_quality_rules", []) or []:
        row = _strip_pk_and_uuids(r)
        row["model_id"] = model_id
        await db.execute(insert(DataQualityRule).values(**row))


async def _insert_entity_translations(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for t in snap.get("entity_translations", []) or []:
        row = _strip_pk_and_uuids(t)
        row["model_id"] = model_id
        await db.execute(insert(EntityTranslation).values(**row))


# ---------------------------------------------------------------------------
# ModelVersion inserter (project-level import)
# ---------------------------------------------------------------------------

async def insert_model_versions(
    model_id: UUID,
    versions: list[dict[str, Any]],
    db: AsyncSession,
) -> dict[str, UUID]:
    """Insert ModelVersion rows, returning {old_id_str: new_id} map.

    Bug-5354 — the bundle's per-version ``snapshot_json`` carries the SOURCE
    tenant's column / dimension / measure ids. Import rehydrates fresh live
    rows in THIS tenant with new ids, so the bundle snapshot's ids are foreign
    here. The query-router binds dimensions/measures from the deployed snapshot
    but resolves columns/tables from LIVE meta (B15 deploy-snapshot-pinning
    design), so a foreign-id snapshot can never map a field to a physical table
    — every query fails 422 "columns or measures ... none could be mapped to a
    physical source table".

    Fix: regenerate ``snapshot_json`` from the just-rehydrated LIVE model state
    (identical to the normal Save path, where ``create_version`` stores
    ``snapshot_model(live)`` as ``snapshot_json``). This keeps the deployed
    snapshot id-consistent with live meta. The live state is the same for every
    imported version of one model, so it is serialised once. (Imported version
    history shapes are inherently from another tenant and were never queryable
    or revertable with their foreign ids; collapsing them onto the live shape is
    both safe and necessary for the deployed version to resolve.)
    """
    from shared.db.models import ModelVersion
    from shared.model_snapshot.serialiser import snapshot_model

    remap: dict[str, UUID] = {}
    live_snapshot: dict[str, Any] | None = None
    if versions:
        live_snapshot = await snapshot_model(model_id, db)
    for v in versions:
        row = _strip_pk_and_uuids(v)
        row["model_id"] = model_id
        old_id = v.get("id", "")
        new_id = uuid.uuid4()
        row["id"] = new_id
        # Always use the live-derived snapshot, not the bundle's foreign-id one.
        row["snapshot_json"] = live_snapshot if live_snapshot is not None else {}
        await db.execute(insert(ModelVersion).values(**row))
        remap[str(old_id)] = new_id
    return remap
