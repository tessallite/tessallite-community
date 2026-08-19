"""Model snapshot serialiser.

Produces a JSON-friendly dict capturing every per-model row plus the
canvas layout and per-model settings. Excludes credentials, source data,
runtime logs, and anything stored at tenant / project / system scope —
per the F-7 answer in docs/archive/archive_deploy-versioning-questions.md.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    AggregateLifecycleEvent,
    AggregateRefreshPolicy,
    CalendarTable,
    DataQualityRule,
    DataSource,
    DataTag,
    DataTarget,
    Dimension,
    DimensionAttributeRelationship,
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
    ModelAliasMap,
    ModelColumn,
    ModelParameter,
    ModelSetting,
    ModelTable,
    ModelVersion,
    NamedQuery,
    NamedQueryArtifact,
    NamedQueryRefreshPolicy,
    NamedSet,
    Persona,
    PersonaTagRestriction,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshPolicy,
    QuantileCoverage,
    RefreshSLAConfig,
    RowSecurityRule,
    SourceColumnStatistics,
    SourceJoinStatistics,
    SourceStatistics,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
    data_tag_columns,
)
from shared.semantic.graph_order import MODEL_JOIN_ORDER, MODEL_TABLE_ORDER


# v3 (F-013-06): adds the six previously-missing model-scoped configuration
# families — model_parameters, model_alias_maps, refresh_sla_configs,
# data_quality_rules, entity_translations. (DataTag + persona_tag_restrictions
# were already folded in at v2 by H24/F-008-09.) Bumped from 2.
# v4 (Bug-7359, derived-grain routing §5.3): adds attribute_relationships —
# modeller-declared dimension key-to-detail relationships. These are PINNED
# semantic model content (declaration shape), so they travel in the snapshot.
# Runtime verification EVIDENCE and active-refresh pointers are deliberately
# EXCLUDED: they are live operational state tied to a physical artifact run, not
# model content, and are re-established by the Phase-2 verifier after rehydrate.
# v5 (Bug-8615, join population governance phase G1): adds
# ``joins[].population_participation`` — the modeller-declared population
# intent, pinned model content that travels like ``join_type``. The bump is
# NOT cosmetic: the rehydrator builds its INSERT from the snapshot dict's keys,
# so a v5 snapshot carried back to a pre-G1 build would raise an opaque
# "unconsumed column names" error instead of the version gate's clear "upgrade
# Tessallite first". Older snapshots (v1-v4) still rehydrate unchanged — their
# joins simply omit the key and take the column's server default, which is the
# historical behaviour. The deploy-time CLASSIFICATION evidence
# (``join_population_checks``) is live operational state and deliberately does
# NOT travel; it is re-established by the next deploy.
SNAPSHOT_SCHEMA_VERSION = 5


def _j(value: Any) -> Any:
    """Coerce SQLAlchemy / UUID / datetime values into JSON-friendly types."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        # asyncpg returns NUMERIC columns (e.g. KPI target_value /
        # trend_threshold) as Decimal, which stdlib json — used by the
        # asyncpg JSONB codec on the version-save write path — cannot
        # serialise. float matches the app-level contract (the ORM maps
        # these columns as Mapped[Optional[float]]) and keeps version
        # snapshots byte-consistent with the export path, where FastAPI's
        # jsonable_encoder already coerces Decimal to float.
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_j(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _j(v) for k, v in value.items()}
    return value


def _row_to_dict(row: Any, *, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    """ORM row -> dict using SQLAlchemy column names."""
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        name = col.name
        if name in exclude:
            continue
        out[name] = _j(getattr(row, name))
    return out


#: Public alias for the snapshot row normaliser (Bug-8250 re-gate).
#:
#: ``shared.deployed_definition_drift`` compares LIVE ORM rows against the rows
#: stored in a deployed snapshot. Those two sides are only comparable if they
#: are normalised by the SAME function — a second, near-identical normaliser is
#: exactly how a comparison silently stops catching a field. Exported here so
#: the drift checker uses this module's normalisation rather than reproducing
#: it, and so any future change to ``_j`` reaches both sides at once.
row_to_snapshot_dict = _row_to_dict


def _merge_effective_descriptions(snap: dict[str, Any]) -> None:
    """Merge approved/show glossary definitions into dimension and measure
    snapshot rows as an additive ``effective_description`` field.

    Bug-7959: both JDBC and XMLA catalogue surfaces must serve the SAME
    deployment-pinned effective description.  Previously XMLA read live
    model-service routes (glossary could change before deploy) and JDBC
    used raw snapshot rows (glossary text never appeared at all).

    The merge mirrors the live ``glossary_text_for_target`` logic in
    ``model-service/src/api/_scope.py``:
      - Only entries with ``status == "approved"`` and ``superseded_by``
        absent (None / null) qualify.
      - Only entries with ``visibility == "show"`` (or legacy NULL) qualify
        (Bug-5926).
      - For each qualifying entry, its attachments link it to dimensions or
        measures via ``target_type`` / ``target_id``.
      - When multiple qualifying entries attach to the same target, the
        one with the highest ``version`` wins.
      - The glossary ``definition`` text becomes ``effective_description``
        on the dimension/measure dict.  When no glossary entry attaches to
        a target, the field is set to the raw ``description`` so that
        consumers always have a consistent field to read.
    """
    glossary_entries = snap.get("glossary_entries") or []

    # Build a lookup: (target_type, target_id) -> best definition text.
    # "Best" = approved, non-superseded, show/null visibility, highest version.
    best: dict[tuple[str, str], tuple[int, str]] = {}
    for entry in glossary_entries:
        if entry.get("status") != "approved":
            continue
        if entry.get("superseded_by") is not None:
            continue
        visibility = entry.get("visibility")
        if visibility is not None and visibility != "show":
            continue
        version = entry.get("version", 0)
        definition = entry.get("definition") or ""
        for att in entry.get("attachments") or []:
            target_type = att.get("target_type")
            target_id = str(att.get("target_id") or "")
            if not target_type or not target_id:
                continue
            key = (target_type, target_id)
            prev = best.get(key)
            if prev is None or version > prev[0]:
                best[key] = (version, definition)

    # Merge into dimensions.
    for dim in snap.get("dimensions") or []:
        dim_id = str(dim.get("id") or "")
        entry = best.get(("dimension", dim_id))
        glossary_text = entry[1] if entry else None
        dim["effective_description"] = glossary_text or dim.get("description") or ""

    # Merge into measures.
    for meas in snap.get("measures") or []:
        meas_id = str(meas.get("id") or "")
        entry = best.get(("measure", meas_id))
        glossary_text = entry[1] if entry else None
        meas["effective_description"] = glossary_text or meas.get("description") or ""


async def snapshot_model(
    model_id: UUID,
    tenant_db: AsyncSession,
    *,
    include_versions: bool = False,
) -> dict[str, Any]:
    """Walk every per-model table and produce the snapshot dict.

    Deterministic ordering: rows are sorted by (created_at, id) so the
    *model-state* portion of two snapshots of the same state is
    byte-identical.

    Bug-8605 exception: ``tables`` and ``joins`` sort by ``id`` alone, matching
    ``shared/semantic/graph_order.py``'s canonical graph order. Those two
    families feed the FROM anchor and the JOIN expansion, so the snapshot's
    stored list order must BE the canonical order — otherwise the system
    carries two different orders for the same rows and a consumer that trusts
    list order anchors somewhere the builders do not. ``created_at`` cannot be
    the shared key because it is excluded from the row bodies below and
    ``rehydrate_into_live`` re-stamps it on every revert; ``id`` survives both.

    The top-level ``exported_at`` field is a wall-clock
    timestamp and therefore differs between any two calls; it is excluded
    from rehydrate (F-013-15). Callers comparing snapshots for "did the
    model change" must drop ``exported_at`` (and ``schema_version``) first
    — see ``shared.model_snapshot.differ.diff_snapshots`` which already
    ignores both metadata keys.
    """
    model = await tenant_db.get(Model, model_id)
    if model is None:
        raise ValueError(f"Model {model_id} not found")

    snap: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "model": _row_to_dict(
            model,
            exclude=(
                "created_at",
                "updated_at",
                "deployed_version_id",
                "last_deployed_at",
                "deploy_epoch",
                # Bug-8708: predictive build stamps identify an optimizer
                # artifact, not model definition state. Exporting either half
                # lets an imported/reverted model claim that its current
                # deployment was already considered by the optimizer.
                "predictive_built_for_version_id",
                "predictive_built_for_epoch",
                # Bug-7982 R7 (review round 3, B3): same monotonic-counter
                # contract as deploy_epoch — carrying data_epoch in the snapshot
                # let a revert move the KPI cache key BACKWARDS and re-serve a
                # pre-refresh scorecard value. Symmetric with the rehydrate
                # exclusion (_MODEL_SCALAR_EXCLUDE).
                "data_epoch",
                # Bug-7787 §6.3: draft control metadata, not model shape. The
                # destination re-earns its own dependency_revision via the
                # mutation lock after rehydrate/import; carrying a stale counter
                # in the snapshot would race it. Symmetric with the rehydrate
                # exclusion (_MODEL_SCALAR_EXCLUDE).
                "dependency_revision",
            ),
        ),
    }

    # Tables + columns
    tables_q = await tenant_db.execute(
        select(ModelTable)
        .where(ModelTable.model_id == model_id)
        .order_by(*MODEL_TABLE_ORDER)  # Bug-8605: canonical graph order
    )
    tables = list(tables_q.scalars().all())
    snap["tables"] = [_row_to_dict(t, exclude=("created_at", "updated_at")) for t in tables]

    table_ids = [t.id for t in tables] or [uuid.uuid4()]  # avoid empty-IN
    cols_q = await tenant_db.execute(
        select(ModelColumn)
        .where(ModelColumn.model_table_id.in_(table_ids))
        .order_by(ModelColumn.model_table_id, ModelColumn.id)
    )
    snap["columns"] = [
        _row_to_dict(c, exclude=("created_at", "updated_at"))
        for c in cols_q.scalars().all()
    ]

    # User-defined attributes + their column refs
    udas_q = await tenant_db.execute(
        select(UserDefinedAttribute)
        .where(UserDefinedAttribute.model_id == model_id)
        .order_by(UserDefinedAttribute.created_at, UserDefinedAttribute.id)
    )
    udas = list(udas_q.scalars().all())
    snap["user_defined_attributes"] = [
        _row_to_dict(u, exclude=("created_at", "updated_at")) for u in udas
    ]
    uda_ids = [u.id for u in udas] or [uuid.uuid4()]
    refs_q = await tenant_db.execute(
        select(UserDefinedAttributeColumnRef)
        .where(UserDefinedAttributeColumnRef.attribute_id.in_(uda_ids))
    )
    snap["uda_column_refs"] = [_row_to_dict(r) for r in refs_q.scalars().all()]

    # Joins
    joins_q = await tenant_db.execute(
        select(Join)
        .where(Join.model_id == model_id)
        .order_by(*MODEL_JOIN_ORDER)  # Bug-8605: canonical graph order
    )
    snap["joins"] = [_row_to_dict(j, exclude=("created_at", "updated_at")) for j in joins_q.scalars().all()]

    # Hierarchies + nested structures
    hier_q = await tenant_db.execute(
        select(HierarchyDefinition)
        .where(HierarchyDefinition.model_id == model_id)
        .order_by(HierarchyDefinition.created_at, HierarchyDefinition.id)
    )
    hierarchies = list(hier_q.scalars().all())
    hierarchy_dicts: list[dict[str, Any]] = []
    for h in hierarchies:
        h_dict = _row_to_dict(h, exclude=("created_at", "updated_at"))
        levels_q = await tenant_db.execute(
            select(HierarchyLevel)
            .where(HierarchyLevel.hierarchy_id == h.id)
            .order_by(HierarchyLevel.ordinal)
        )
        levels = list(levels_q.scalars().all())
        h_dict["levels"] = []
        for lvl in levels:
            l_dict = _row_to_dict(lvl, exclude=("created_at", "updated_at"))
            attrs_q = await tenant_db.execute(
                select(HierarchyLevelAttribute)
                .where(HierarchyLevelAttribute.level_id == lvl.id)
            )
            l_dict["attributes"] = [_row_to_dict(a) for a in attrs_q.scalars().all()]
            h_dict["levels"].append(l_dict)
        hierarchy_dicts.append(h_dict)
    snap["hierarchies"] = hierarchy_dicts

    # Dimensions + measures
    dims_q = await tenant_db.execute(
        select(Dimension)
        .where(Dimension.model_id == model_id)
        .order_by(Dimension.created_at, Dimension.id)
    )
    snap["dimensions"] = [
        _row_to_dict(d, exclude=("created_at", "updated_at"))
        for d in dims_q.scalars().all()
    ]

    # Dimension attribute relationships (derived-grain routing §5.3). PINNED
    # declaration content only — the declaration shape (key/detail column,
    # cardinality, null policy, declaration_hash). Verification evidence and
    # active-run pointers are NOT serialised: they are live operational state.
    attr_rel_q = await tenant_db.execute(
        select(DimensionAttributeRelationship)
        .where(DimensionAttributeRelationship.model_id == model_id)
        .order_by(
            DimensionAttributeRelationship.created_at,
            DimensionAttributeRelationship.id,
        )
    )
    snap["attribute_relationships"] = [
        _row_to_dict(r, exclude=("created_at", "updated_at"))
        for r in attr_rel_q.scalars().all()
    ]

    meas_q = await tenant_db.execute(
        select(Measure)
        .where(Measure.model_id == model_id)
        .order_by(Measure.created_at, Measure.id)
    )
    measures = list(meas_q.scalars().all())
    snap["measures"] = [
        _row_to_dict(m, exclude=("created_at", "updated_at")) for m in measures
    ]
    measure_ids = [m.id for m in measures] or [uuid.uuid4()]

    # Named sets
    ns_q = await tenant_db.execute(
        select(NamedSet)
        .where(NamedSet.model_id == model_id)
        .order_by(NamedSet.created_at, NamedSet.id)
    )
    snap["named_sets"] = [
        _row_to_dict(ns, exclude=("created_at", "updated_at"))
        for ns in ns_q.scalars().all()
    ]

    # Named Queries (definition + artifact identity pointer + refresh policy).
    # The DEFINITION travels (governed model content); the artifact pointer
    # carries ONLY the physical identity (table name, schema, target, status)
    # plus the row manifest and liveness pointer — the manifest travels
    # UNTRUSTED exactly like the pocket row_manifest: the rehydrator clears
    # the liveness pointer and forces ``stale``, so a travelled manifest can
    # never admit a rehydrated artifact under row security (the serve-time
    # gate requires status=fresh AND the version binding, both re-earned by a
    # rebuild).
    nq_q = await tenant_db.execute(
        select(NamedQuery)
        .where(NamedQuery.model_id == model_id)
        .order_by(NamedQuery.created_at, NamedQuery.id)
    )
    nq_dicts: list[dict[str, Any]] = []
    for nq in nq_q.scalars().all():
        nq_dict = _row_to_dict(nq, exclude=("created_at", "updated_at"))
        art = await tenant_db.execute(
            select(NamedQueryArtifact).where(
                NamedQueryArtifact.named_query_id == nq.id
            )
        )
        art_row = art.scalar_one_or_none()
        if art_row is not None:
            nq_dict["artifact"] = {
                "physical_table_name": art_row.physical_table_name,
                "target_schema": art_row.target_schema,
                "status": art_row.status,
                "row_manifest": art_row.row_manifest,
                "active_refresh_run_id": (
                    str(art_row.active_refresh_run_id)
                    if art_row.active_refresh_run_id
                    else None
                ),
                "target_id": str(art_row.target_id),
            }
        else:
            nq_dict["artifact"] = None
        pol = await tenant_db.execute(
            select(NamedQueryRefreshPolicy).where(
                NamedQueryRefreshPolicy.named_query_id == nq.id
            )
        )
        pol_row = pol.scalar_one_or_none()
        nq_dict["refresh_policy"] = (
            {
                "cron_expression": pol_row.cron_expression,
                "is_enabled": pol_row.is_enabled,
            }
            if pol_row is not None
            else None
        )
        nq_dicts.append(nq_dict)
    snap["named_queries"] = nq_dicts

    # KPIs
    kpi_q = await tenant_db.execute(
        select(KPI)
        .where(KPI.model_id == model_id)
        .order_by(KPI.created_at, KPI.id)
    )
    snap["kpis"] = [
        _row_to_dict(k, exclude=("created_at", "updated_at"))
        for k in kpi_q.scalars().all()
    ]

    # Drill-through sets (one optional row per measure; v2)
    drill_q = await tenant_db.execute(
        select(DrillThroughSet)
        .where(DrillThroughSet.measure_id.in_(measure_ids))
        .order_by(DrillThroughSet.created_at, DrillThroughSet.id)
    )
    snap["drill_through_sets"] = [
        _row_to_dict(d, exclude=("created_at", "updated_at"))
        for d in drill_q.scalars().all()
    ]

    # Sources + targets (by id; the project_connection_id reference travels
    # but the connection itself stays in project_connections — credentials
    # never leave the source DB).
    sources_q = await tenant_db.execute(
        select(DataSource)
        .where(DataSource.model_id == model_id)
        .order_by(DataSource.created_at, DataSource.id)
    )
    sources = list(sources_q.scalars().all())
    snap["data_sources"] = [
        _row_to_dict(s, exclude=("created_at", "updated_at")) for s in sources
    ]
    source_ids = [s.id for s in sources] or [uuid.uuid4()]
    targets_q = await tenant_db.execute(
        select(DataTarget)
        .where(DataTarget.model_id == model_id)
        .order_by(DataTarget.created_at, DataTarget.id)
    )
    snap["data_targets"] = []
    for t in targets_q.scalars().all():
        row = _row_to_dict(t, exclude=("created_at", "updated_at"))
        # Bug-8790: strip the deprecated config.project_id from serialised
        # snapshots so exports, imports, and clones do not carry a second
        # project authority.
        config = row.get("config", {})
        if isinstance(config, dict) and "project_id" in config:
            config.pop("project_id")
        snap["data_targets"].append(row)

    # Calendar tables (per-data-source; v2)
    cal_q = await tenant_db.execute(
        select(CalendarTable)
        .where(CalendarTable.data_source_id.in_(source_ids))
        .order_by(CalendarTable.created_at, CalendarTable.id)
    )
    snap["calendar_tables"] = [
        _row_to_dict(c, exclude=("created_at", "updated_at"))
        for c in cal_q.scalars().all()
    ]

    # Lineage mappings
    lineage_q = await tenant_db.execute(
        select(LineageMapping)
        .where(LineageMapping.model_id == model_id)
        .order_by(LineageMapping.id)
    )
    snap["lineage_mappings"] = [_row_to_dict(l) for l in lineage_q.scalars().all()]

    # Aggregate definitions + columns + refresh policies
    aggs_q = await tenant_db.execute(
        select(AggregateDefinition)
        .where(AggregateDefinition.model_id == model_id)
        .order_by(AggregateDefinition.created_at, AggregateDefinition.id)
    )
    aggs = list(aggs_q.scalars().all())
    agg_dicts: list[dict[str, Any]] = []
    for agg in aggs:
        a_dict = _row_to_dict(
            agg,
            exclude=(
                "created_at", "updated_at", "last_refreshed_at", "retired_at",
                "source_row_count", "agg_row_count",
                # Derived-grain (Bug-7359, §5.3): active_refresh_run_id is a LIVE
                # pointer to a physical run that will not exist after import/
                # clone/rehydrate, so it is snapshot-EXCLUDED and re-earned by a
                # rebuild. The manifests (grain_keys/attribute_edges/
                # passenger_columns) DO travel as descriptive build metadata.
                "active_refresh_run_id",
                # F-013-02 (Bug-8250): built_for_version_id/built_for_epoch are LIVE
                # build metadata tied to a physical run that will not exist after
                # import/clone/rehydrate. Snapshot-EXCLUDED and re-earned by a
                # rebuild — a rehydrated aggregate has no build for the current
                # version and stays non-servable until it rebuilds.
                "built_for_version_id",
                "built_for_epoch",
                # Bug-8481: physical target/connection routing identity is LIVE
                # build metadata. A snapshot has no table at that location and
                # must re-earn the binding through a successful rebuild.
                "built_for_storage_binding",
                # Bug-8602: the SOURCE routing identity is LIVE build metadata
                # for the same reason as the storage one — a rehydrated
                # aggregate has no rows read from any recorded database and must
                # re-earn the binding through a successful rebuild.
                "built_for_source_binding",
                # Bug-7903: refresh_prior_status is LIVE in-flight refresh state
                # (the durable pre-refresh status snapshot). The rehydrator SETS it
                # itself when it forces active/disabled aggregates to pending on
                # import, so a stale exported value must never override that.
                "refresh_prior_status",
            ),
        )
        # Bug-7903 (Fable MED #5): export the EFFECTIVE status, not the transient
        # in-flight "pending". A refresh may be mid-flight when the bundle is
        # exported, holding status=="pending" with the true prior status durable in
        # refresh_prior_status. Exporting raw "pending" would launder a "disabled"
        # aggregate through an export→import chain into "active" (the prior is lost
        # in the pending state, and the rehydrator only records active/disabled
        # priors). Substitute the durable prior status so a bundle always carries
        # the aggregate's settled user-visible status.
        _prior = getattr(agg, "refresh_prior_status", None)
        if a_dict.get("status") in ("pending", "invalid") and _prior in ("active", "disabled"):
            a_dict["status"] = _prior
        cols_q = await tenant_db.execute(
            select(AggregateColumn)
            .where(AggregateColumn.aggregate_definition_id == agg.id)
        )
        a_dict["columns"] = [_row_to_dict(c) for c in cols_q.scalars().all()]
        policy_q = await tenant_db.execute(
            select(AggregateRefreshPolicy)
            .where(AggregateRefreshPolicy.aggregate_definition_id == agg.id)
        )
        policy = policy_q.scalar_one_or_none()
        a_dict["refresh_policy"] = (
            _row_to_dict(policy, exclude=("created_at", "updated_at")) if policy else None
        )
        # Bug-7852 / Bug-6969: include QuantileCoverage rows so the proof-
        # carrying coverage travels with export/import/version snapshots.
        # The consumer's proof gate reads from the DB, so rehydrate must
        # recreate these rows for the imported aggregate to be routable.
        qc_q = await tenant_db.execute(
            select(QuantileCoverage)
            .where(QuantileCoverage.aggregate_definition_id == agg.id)
        )
        a_dict["quantile_coverage"] = [
            _row_to_dict(qc, exclude=(
                "created_at", "updated_at",
                # refresh_run_id references AggregateRefreshRun which is NOT
                # serialised — rehydrating it would insert a dangling FK and
                # break import/restore (Bug-7852 R1 fix, finding 2).
                "refresh_run_id",
            ))
            for qc in qc_q.scalars().all()
        ]
        agg_dicts.append(a_dict)
    snap["aggregates"] = agg_dicts
    agg_ids = [a.id for a in aggs] or [uuid.uuid4()]

    # ----- v2 additions (Bug-106) -----------------------------------------
    # Personas (v2)
    personas_q = await tenant_db.execute(
        select(Persona)
        .where(Persona.model_id == model_id)
        .order_by(Persona.created_at, Persona.id)
    )
    personas = list(personas_q.scalars().all())
    snap["personas"] = [
        _row_to_dict(p, exclude=("created_at", "updated_at")) for p in personas
    ]

    # Data tags + column assignments + persona tag restrictions (F-008-09).
    # Without these, export/import and version restore silently strip
    # column-level security while keeping the personas.
    tags_q = await tenant_db.execute(
        select(DataTag)
        .where(DataTag.model_id == model_id)
        .order_by(DataTag.created_at, DataTag.id)
    )
    tags = list(tags_q.scalars().all())
    tag_ids = [t.id for t in tags] or [uuid.uuid4()]  # avoid empty-IN
    tag_cols_q = await tenant_db.execute(
        select(data_tag_columns.c.tag_id, data_tag_columns.c.model_column_id)
        .where(data_tag_columns.c.tag_id.in_(tag_ids))
        .order_by(data_tag_columns.c.tag_id, data_tag_columns.c.model_column_id)
    )
    cols_by_tag: dict[str, list[str]] = {}
    for tag_id, column_id in tag_cols_q.all():
        cols_by_tag.setdefault(str(tag_id), []).append(str(column_id))
    tag_dicts: list[dict[str, Any]] = []
    for t in tags:
        t_dict = _row_to_dict(t, exclude=("created_at",))
        t_dict["column_ids"] = cols_by_tag.get(str(t.id), [])
        tag_dicts.append(t_dict)
    snap["data_tags"] = tag_dicts

    persona_ids = [p.id for p in personas] or [uuid.uuid4()]
    restr_q = await tenant_db.execute(
        select(PersonaTagRestriction)
        .where(PersonaTagRestriction.persona_id.in_(persona_ids))
        .order_by(
            PersonaTagRestriction.persona_id, PersonaTagRestriction.data_tag_id,
        )
    )
    snap["persona_tag_restrictions"] = [
        _row_to_dict(r) for r in restr_q.scalars().all()
    ]

    # Pockets + nested predicates + nested refresh_policy (v2)
    pockets_q = await tenant_db.execute(
        select(PocketDefinition)
        .where(PocketDefinition.model_id == model_id)
        .order_by(PocketDefinition.created_at, PocketDefinition.id)
    )
    pockets = list(pockets_q.scalars().all())
    pocket_dicts: list[dict[str, Any]] = []
    for p in pockets:
        p_dict = _row_to_dict(
            p,
            exclude=(
                "created_at", "updated_at", "retired_at",
                "last_refresh_at", "last_access_at", "last_match_at",
                "hit_count", "time_saved_ms_total",
                # Derived-grain (Bug-7359, §5.3): active_refresh_run_id is a LIVE
                # pointer, snapshot-EXCLUDED + re-earned by a rebuild.
                # row_manifest travels UNTRUSTED: it is security-load-bearing at
                # serve time (Bug-8018/Bug-8393) but the query-router only trusts
                # it while its build_refresh_run_id matches this pointer, and the
                # pointer is excluded here and cleared on import — so a travelled
                # manifest can never admit an imported pocket under row security.
                # Excluding the pointer is what makes that safe; do not start
                # carrying it.
                "active_refresh_run_id",
                # F-013-03 (Bug-8250): built-for binding is LIVE build metadata,
                # snapshot-EXCLUDED + re-earned by a rebuild (same as the aggregate
                # columns). A rehydrated pocket has no build for the current
                # version and stays non-servable until it rebuilds.
                "built_for_version_id",
                "built_for_epoch",
            ),
        )
        preds_q = await tenant_db.execute(
            select(PocketPredicate)
            .where(PocketPredicate.pocket_definition_id == p.id)
            .order_by(PocketPredicate.created_at, PocketPredicate.id)
        )
        p_dict["predicates"] = [
            _row_to_dict(pr, exclude=("created_at",))
            for pr in preds_q.scalars().all()
        ]
        rp_q = await tenant_db.execute(
            select(PocketRefreshPolicy)
            .where(PocketRefreshPolicy.pocket_definition_id == p.id)
        )
        rp = rp_q.scalar_one_or_none()
        p_dict["refresh_policy"] = (
            _row_to_dict(rp, exclude=("created_at", "updated_at")) if rp else None
        )
        pocket_dicts.append(p_dict)
    snap["pockets"] = pocket_dicts

    # Row-security rules (v2)
    rls_q = await tenant_db.execute(
        select(RowSecurityRule)
        .where(RowSecurityRule.model_id == model_id)
        .order_by(RowSecurityRule.created_at, RowSecurityRule.id)
    )
    snap["row_security_rules"] = [
        _row_to_dict(r, exclude=("created_at", "updated_at"))
        for r in rls_q.scalars().all()
    ]

    # Glossary entries + nested synonyms + attachments (v2)
    glossary_q = await tenant_db.execute(
        select(GlossaryEntry)
        .where(GlossaryEntry.model_id == model_id)
        .order_by(GlossaryEntry.created_at, GlossaryEntry.id)
    )
    glossary_entries = list(glossary_q.scalars().all())
    glossary_dicts: list[dict[str, Any]] = []
    for g in glossary_entries:
        g_dict = _row_to_dict(g, exclude=("created_at", "updated_at"))
        syn_q = await tenant_db.execute(
            select(GlossarySynonym).where(GlossarySynonym.entry_id == g.id)
        )
        g_dict["synonyms"] = [_row_to_dict(s) for s in syn_q.scalars().all()]
        att_q = await tenant_db.execute(
            select(GlossaryAttachment).where(GlossaryAttachment.entry_id == g.id)
        )
        g_dict["attachments"] = [_row_to_dict(a) for a in att_q.scalars().all()]
        glossary_dicts.append(g_dict)
    snap["glossary_entries"] = glossary_dicts

    # Bug-7959: compute deployment-pinned effective_description for each
    # dimension and measure by merging approved/show glossary definitions
    # at serialisation time.  Consumers (JDBC catalogue, XMLA catalogue)
    # read ``effective_description`` from the snapshot and fall back to the
    # raw ``description`` when the field is absent — so previously-deployed
    # snapshots keep working without a migration or version bump.
    _merge_effective_descriptions(snap)

    # Aggregate lifecycle events (v2)
    lifecycle_q = await tenant_db.execute(
        select(AggregateLifecycleEvent)
        .where(AggregateLifecycleEvent.model_id == model_id)
        .order_by(AggregateLifecycleEvent.occurred_at, AggregateLifecycleEvent.id)
    )
    snap["aggregate_lifecycle_events"] = [
        _row_to_dict(e) for e in lifecycle_q.scalars().all()
    ]

    # Source statistics + column stats + join stats (v2)
    stats_q = await tenant_db.execute(
        select(SourceStatistics)
        .where(SourceStatistics.data_source_id.in_(source_ids))
        .order_by(SourceStatistics.created_at, SourceStatistics.id)
    )
    stats_rows = list(stats_q.scalars().all())
    stats_dicts: list[dict[str, Any]] = []
    for st in stats_rows:
        st_dict = _row_to_dict(
            st,
            exclude=(
                "created_at", "updated_at",
                "last_refreshed_at", "next_refresh_at", "last_error",
            ),
        )
        col_stats_q = await tenant_db.execute(
            select(SourceColumnStatistics)
            .where(SourceColumnStatistics.source_statistics_id == st.id)
        )
        st_dict["columns"] = [
            _row_to_dict(c, exclude=("computed_at",))
            for c in col_stats_q.scalars().all()
        ]
        stats_dicts.append(st_dict)
    snap["source_statistics"] = stats_dicts

    join_stats_q = await tenant_db.execute(
        select(SourceJoinStatistics)
        .where(SourceJoinStatistics.data_source_id.in_(source_ids))
        .order_by(SourceJoinStatistics.id)
    )
    snap["source_join_statistics"] = [
        _row_to_dict(j, exclude=("computed_at",))
        for j in join_stats_q.scalars().all()
    ]
    # ----- end v2 additions ------------------------------------------------

    # ----- v3 additions (F-013-06) -----------------------------------------
    # Six model-scoped configuration families that previously fell out of the
    # snapshot contract, silently lost on export/import and dangling on revert.

    # Model parameters (parameterized filters)
    params_q = await tenant_db.execute(
        select(ModelParameter)
        .where(ModelParameter.model_id == model_id)
        .order_by(ModelParameter.created_at, ModelParameter.id)
    )
    snap["model_parameters"] = [
        _row_to_dict(p, exclude=("created_at", "updated_at"))
        for p in params_q.scalars().all()
    ]

    # Model alias map (single row keyed by model_id; the phrase->attribute
    # map the agent uses for natural-language resolution)
    alias_q = await tenant_db.execute(
        select(ModelAliasMap).where(ModelAliasMap.model_id == model_id)
    )
    alias_row = alias_q.scalar_one_or_none()
    snap["model_alias_map"] = (
        _row_to_dict(alias_row, exclude=("updated_at",)) if alias_row else None
    )

    # Refresh SLA config (single row per model; breach-episode tracking
    # columns are runtime state and are excluded)
    sla_q = await tenant_db.execute(
        select(RefreshSLAConfig).where(RefreshSLAConfig.model_id == model_id)
    )
    sla_row = sla_q.scalar_one_or_none()
    snap["refresh_sla_config"] = (
        _row_to_dict(
            sla_row,
            exclude=(
                "created_at", "updated_at",
                # Bug-8146: all three breach-episode markers are runtime state
                # written by the SLA monitor, never model configuration. They
                # are excluded so an export/import round-trip does not carry
                # one tenant's breach history into another, and — because this
                # normaliser is also what `shared.deployed_definition_drift`
                # compares live rows against — so an episode opening does not
                # register as deployed-definition drift.
                "last_breach_alerted_on",
                "last_breach_episode_opened_on",
                "last_breach_resolved_at",
            ),
        )
        if sla_row else None
    )

    # Data-quality rules (config only; violations are runtime telemetry and
    # are excluded, like refresh runs)
    dq_q = await tenant_db.execute(
        select(DataQualityRule)
        .where(DataQualityRule.model_id == model_id)
        .order_by(DataQualityRule.created_at, DataQualityRule.id)
    )
    snap["data_quality_rules"] = [
        _row_to_dict(
            r,
            exclude=(
                "created_at", "updated_at",
                "last_checked_at", "last_violation_count",
            ),
        )
        for r in dq_q.scalars().all()
    ]

    # Entity translations (per-model localized strings for glossary entries,
    # measures, dimensions, etc.). The translations *feature* is parked
    # (plan D4), but the rows are model-scoped config: walking them keeps
    # export/import/revert lossless. No feature work — pure table walk.
    trans_q = await tenant_db.execute(
        select(EntityTranslation)
        .where(EntityTranslation.model_id == model_id)
        .order_by(EntityTranslation.id)
    )
    snap["entity_translations"] = [
        _row_to_dict(t, exclude=("created_at", "updated_at"))
        for t in trans_q.scalars().all()
    ]
    # ----- end v3 additions ------------------------------------------------

    # AI scheduler config (per-model, single row)
    sched_q = await tenant_db.execute(
        select(ModelAISchedulerConfig)
        .where(ModelAISchedulerConfig.model_id == model_id)
    )
    sched = sched_q.scalar_one_or_none()
    snap["ai_scheduler_config"] = (
        _row_to_dict(sched, exclude=("created_at", "updated_at")) if sched else None
    )

    # Per-model settings
    settings_q = await tenant_db.execute(
        select(ModelSetting).where(ModelSetting.model_id == model_id)
    )
    snap["model_settings"] = {
        s.key: _j(s.value_json) for s in settings_q.scalars().all()
    }

    # Model versions + deployed pointer (project-level export only).
    # Bug-7623 (per-version fidelity — "correct restore from now on"): each
    # exported version now carries its OWN ``snapshot_json`` — the real shape
    # saved for that version — so a NEW-format bundle (PROJECT_BUNDLE_VERSION 2+)
    # can restore version N to version N's actual shape, not today's live shape.
    #
    # This reverses the earlier trim, which excluded ``snapshot_json`` for two
    # reasons: (a) bloat (potentially megabytes per model with many versions),
    # and (b) the importer could not use it anyway because a version's snapshot
    # carries foreign-tenant ids (Bug-5354), so H2 (Bug-6295) honest-degraded
    # every imported version to a non-servable placeholder. Correctness for a
    # restore feature outweighs size, and the importer now rebinds the portable
    # fields (connection ids) on the way in, so the payload IS usable.
    #
    # Versions whose shape is genuinely unrecoverable (already carrying
    # ``snapshot_unavailable=True`` with a ``{}`` placeholder — e.g. history
    # imported BEFORE this fix) export their placeholder verbatim: the
    # ``snapshot_unavailable`` column travels as a plain field, so the importer
    # keeps them honest-degrade. Old (v1) bundles have no per-version snapshots
    # at all; the importer degrades every version for them. Historical shapes
    # from before this fix are NOT reconstructed.
    if include_versions:
        snap["exported_deployed_version_id"] = _j(model.deployed_version_id)
        versions_q = await tenant_db.execute(
            select(ModelVersion)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number)
        )
        snap["model_versions"] = [
            _row_to_dict(v) for v in versions_q.scalars().all()
        ]

    return snap
