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

import logging
import secrets
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.model_write_lock_guard import model_write_lock_exempt
from shared.deploy_resolver_core import (
    SEMANTIC_SHAPE_FAMILIES,
    malformed_snapshot_families,
)
from shared.model_snapshot.slug_utils import validate_bi_safe_slug
from shared.schemas.domains.governance_advanced import _DQ_TARGET_TYPES
from shared.security.predicate_compiler import (
    RowSecurityCompileError,
    _compile_dsl_expression,
)

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
    ModelAlert,
    ModelAliasMap,
    ModelColumn,
    ModelParameter,
    ModelSetting,
    ModelTable,
    NamedQuery,
    NamedQueryArtifact,
    NamedQueryRefreshPolicy,
    NamedQueryRefreshRun,
    NamedSet,
    Persona,
    PersonaTagRestriction,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshPolicy,
    PocketRefreshRun,
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
from shared.model_snapshot.serialiser import SNAPSHOT_SCHEMA_VERSION
from shared.semantic.graph_order import fact_anchor_violation
from shared.schemas.domains.aggregates_security import persona_filter_value_is_valid
from shared.schemas.domains.governance_advanced import CertificationStatus
from shared.security.persona_resolver import (
    PersonaAudienceNarrowingError,
    reject_empty_audience_narrowing,
)
from shared.security.row_security_audit import ROW_SECURITY_VALID_SOURCES

logger = logging.getLogger(__name__)

# Bug-6622 — defense-in-depth for certification_status at the rehydrate
# boundary. The model-service Create/Update API already gates this to the enum
# (Bug-6264) and the schema Literal was tightened to these 4 values (SH-1), but
# a hand-authored or tampered import bundle bypasses that sanctioned entry point
# and pours rows straight into the live tables via the rehydrator. Any object
# that carries certification_status (KPI + Named Set today) is clamped to the
# enum here: an unknown value is coerced to the safe default "draft"
# (least-privileged — never over-claims certification) and recorded, without
# failing the whole import. The valid set is derived from the single-source
# Literal in governance_advanced so it stays in lock-step with the API gate.
_VALID_CERTIFICATION_STATUSES: frozenset[str] = frozenset(get_args(CertificationStatus))
_DEFAULT_CERTIFICATION_STATUS: str = "draft"

# Bug-7148 [CORRECTNESS]: KPI governance/lifecycle fields that a REVERT must
# NOT roll back. Revert is contractually "definition only -- governance status
# is never reverted" (solution-details-due-diligence.md, the same contract that
# preserves personas / data tags / row-security and named-set certification).
# A revert rebuilds the KPI DEFINITION from the snapshot but its live governance
# state (certification, ownership, deployment) must survive — reverting a
# model's shape must not silently un-certify or un-deploy a governed KPI. These
# are captured by KPI id before the truncate and re-applied after the snapshot
# KPIs are reinserted (on the revert path only; deploy/import fully restore).
# A KPI id absent from the reverted-to snapshot has no surviving row to carry
# governance, so it is simply dropped with the KPI.
_KPI_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "certification_status",
    "replacement_id",
    "owner_user_id",
    "is_deployed",
    "deployed_at",
)

# Bug-7982 (F-013-08) [CORRECTNESS]: named-set governance/lifecycle fields that a
# REVERT must NOT roll back. A named set carries BOTH definition fields
# (expression, dimensions, builder_definition, scope, list_type, display
# metadata) and governance fields (certification_status, replacement_id,
# owner_user_id) in one row. The prior revert preserved the WHOLE row wholesale,
# so the DEFINITION was never restored to the
# reverted-to version — MDX clients kept serving the newer set expression while
# revert also silently mutated certification. Revert must restore the named-set
# DEFINITION from the snapshot (like KPIs/measures) while preserving only these
# live governance fields. Captured by named-set id before the truncate and
# re-applied after the snapshot sets are reinserted (revert path only;
# deploy/import fully restore from the snapshot).
_NAMED_SET_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "certification_status",
    "replacement_id",
    "owner_user_id",
)

# Bug-8950 / L9-F3: which snapshot collection holds the rows a data-quality
# rule's polymorphic ``target_id`` may name, per ``target_type``. The VOCABULARY
# itself is owned by ``_DQ_TARGET_TYPES`` in
# ``shared/schemas/domains/governance_advanced.py`` — this map only says where to
# look for each declared type, and is pinned equal to that domain by
# ``model-service/tests/test_body_fk_scope_p3c.py``. A declared type missing here
# validates against an EMPTY set, i.e. its rules are skipped (fail closed), never
# inserted unvalidated.
_DQ_TARGET_SNAP_TYPE_KEYS: dict[str, str] = {
    "column": "columns",
    "dimension": "dimensions",
    "measure": "measures",
}


def _clamp_certification_status(
    row: dict[str, Any], *, object_kind: str, object_id: Any
) -> None:
    """Coerce an out-of-enum ``certification_status`` to the safe default.

    Bug-6622 (non-blocking defense-in-depth). Mutates ``row`` in place. Rows
    whose value is absent/None (stripped by ``_strip_pk_and_uuids``) fall back
    to the NOT-NULL DB default "draft" and are left untouched. A recognised
    value passes through unchanged; anything else is coerced to "draft" and a
    warning is logged so the coercion is visible to operators.
    """
    if "certification_status" not in row:
        return
    value = row["certification_status"]
    # A legitimate value is always one of the enum strings. Anything else — an
    # unknown string, or a non-string shape (int/bool/UUID from the strip step,
    # or an unhashable list/dict smuggled by a tampered bundle) — is invalid and
    # is coerced. The isinstance guard keeps the membership test from raising
    # ``TypeError: unhashable type`` on a list/dict, preserving the non-blocking
    # contract (a malformed shape coerces, it never fails the whole import).
    if isinstance(value, str) and value in _VALID_CERTIFICATION_STATUSES:
        return
    logger.warning(
        "Bug-6622: %s %s carried out-of-enum certification_status %r on "
        "rehydrate; coercing to safe default %r (bundle bypassed the API gate).",
        object_kind,
        object_id,
        value,
        _DEFAULT_CERTIFICATION_STATUS,
    )
    row["certification_status"] = _DEFAULT_CERTIFICATION_STATUS


class SnapshotSchemaError(ValueError):
    """Raised when the snapshot is missing required keys or fields."""


class SnapshotVersionError(ValueError):
    """Raised when the snapshot's schema_version is newer than this build."""


class OneFactViolationError(SnapshotSchemaError):
    """Raised when a snapshot violates the model fact-anchor contract.

    A subclass of ``SnapshotSchemaError`` so every existing
    ``except SnapshotSchemaError`` caller already maps it to a clean 4xx; callers
    that must distinguish it (e.g. revert -> 409) catch this type first.
    """


def _validate_imported_append_only_policy(row: dict[str, Any]) -> None:
    """Keep snapshot refresh authority strict before ORM Boolean binding.

    Snapshot bundles bypass the Pydantic API models, so SQLAlchemy would accept
    values such as ``1`` or ``"true"`` for the Boolean column and turn them into
    append-only authority. Legacy snapshots omit the field and retain the
    historical fail-closed default of ``False``.
    """
    value = row.get("incremental_append_only", False)
    if type(value) is not bool:
        raise SnapshotSchemaError(
            "aggregate refresh policy incremental_append_only must be a JSON "
            f"boolean, got {value!r}"
        )


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
#   - predictive_built_for_version_id / predictive_built_for_epoch — optimizer
#     artifact stamps, cleared on every rehydrate so import/revert cannot claim
#     a deployment was already built from a different definition
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
        # Bug-8708: predictive build stamps are runtime artifact state, not
        # semantic model definition. They are excluded symmetrically with the
        # serializer and explicitly reset below for both import and restore.
        "predictive_built_for_version_id",
        "predictive_built_for_epoch",
        # opus5 completion-round R2 (finding 3.5): ``deploy_epoch`` must NEVER be
        # rehydrated from a snapshot — it is a MONOTONIC counter (only ever
        # bumped by +1 in ``api/versions.py`` deploy/undeploy/revert), and the
        # KPILatest epoch-monotonicity guard (Bug-7982;
        # ``kpi_latest.py``/``sweep.py`` ``on_conflict_do_update(where=...)``)
        # depends structurally on that monotonicity: if a revert ever restored
        # an OLDER snapshot value here, the epoch would move BACKWARDS and the
        # guard's ``existing.evaluated_for_epoch <= eval_epoch`` condition would
        # then suppress EVERY subsequent kpi_latest write for that model
        # permanently (worse than pre-guard behaviour, which self-healed on the
        # next write) — see ``test_deploy_epoch_is_excluded_from_rehydration``
        # in this module's test suite, which pins this exclusion.
        "deploy_epoch",
        # Bug-7982 R7 (review round 3, B3): ``data_epoch`` has the IDENTICAL
        # contract to ``deploy_epoch`` above — ``shared/model_refresh_epoch.py``
        # and the column comment both document it as monotonically increasing,
        # and it is folded into the KPI evaluation cache key
        # (``model-service/src/kpi_cache.py``). It was excluded on NEITHER side,
        # so a revert restored the snapshot's older value and the counter moved
        # BACKWARDS: live-measured 9 -> 3 on a model whose epoch had advanced
        # through six refreshes since the snapshot. The next scorecard
        # evaluation then re-formed a cache key it had already used and served
        # the PRE-REFRESH value — a deterministic wrong number on every revert
        # of a refreshed model, no race required. Excluded on both sides now,
        # symmetric with deploy_epoch, and pinned by
        # ``test_a_revert_never_regresses_models_data_epoch``.
        "data_epoch",
        # Bug-7787 §6.3: dependency_revision is draft control metadata, not model
        # shape. Rehydrate/import must NOT clobber the destination's live counter
        # with a snapshot value (that would race the mutation lock); the
        # dependency_mutation helper bumps it once after the rehydrate/import
        # transaction instead. The serialiser also excludes it, keeping the
        # contract symmetric (mirrors deploy_epoch on both sides).
        "dependency_revision",
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


def _calendar_physical_key(table_name: str) -> str:
    parts = str(table_name or "").split(".")
    if len(parts) >= 3:
        return ".".join(parts[-2:])
    return str(table_name or "")


def _calendar_column_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("calendar_type"),
        row.get("date_column"),
        row.get("year_column"),
        row.get("half_column"),
        row.get("quarter_column"),
        row.get("month_column"),
        row.get("week_column"),
        row.get("day_column"),
    )


def _guard_conflicting_calendar_tables(snapshot: dict[str, Any]) -> None:
    """Reject snapshots with conflicting calendar maps for one source table.

    BigQuery project-qualified and dataset-qualified names can refer to the
    same source table. A snapshot that stores both with different calendar
    column meanings is internally corrupt: role-playing date aliases may point
    at generated columns that the physical source table does not have.
    """
    seen: dict[tuple[str, str], tuple[Any, ...]] = {}
    for row in snapshot.get("calendar_tables", []) or []:
        source_id = str(row.get("data_source_id") or "")
        table_key = _calendar_physical_key(row.get("table_name") or "")
        if not source_id or not table_key:
            continue
        key = (source_id, table_key)
        signature = _calendar_column_signature(row)
        previous = seen.get(key)
        if previous is not None and previous != signature:
            raise SnapshotSchemaError(
                "snapshot has conflicting calendar mappings for "
                f"source table {table_key!r}; re-export after fixing the "
                "calendar registration"
            )
        seen[key] = signature


class RehydrationMode(str, Enum):
    """Why a snapshot is being rehydrated — the safety decision an import caller
    must never omit (Bug-8768, B8768-R1-05).

    ``IMPORT`` — the snapshot is being imported or cloned into a model that may
    point at a DIFFERENT data source (bundle/YAML/dbt/AtScale/Cube/catalog
    import, project import, demo re-seed). An operational correctness assertion
    like ``incremental_append_only`` describes ONE specific source and must NOT
    transfer: it is reset to false, disabling incremental until the target
    re-confirms the source is append-only.

    ``RESTORE`` — an in-place version restore/revert of the SAME model against
    the SAME source, so a still-valid source assertion is preserved.

    The default is ``IMPORT`` — the fail-SAFE direction. A caller that forgets to
    classify a genuine import still resets (safe); only the version-restore path
    must explicitly opt into preservation, and it is the single such caller.
    """

    IMPORT = "import"
    RESTORE = "restore"


async def rehydrate_into_live(
    model_id: UUID,
    snapshot: dict[str, Any],
    tenant_db: AsyncSession,
    *,
    mode: RehydrationMode = RehydrationMode.IMPORT,
    drop_orphan_aggregates: bool = True,  # deprecated, use preserve_aggregates
    actor: str = "rehydrator",
    connection_id_remap: dict[str, str] | None = None,
    llm_id_remap: dict[str, str] | None = None,
    force_aggregate_pending: bool = False,
    force_pocket_stale: bool = False,
    preserve_aggregates: bool = False,
    preserve_pockets: bool = False,
    preserve_destination_seed: bool = False,
    restore_governance: bool = True,
) -> None:
    """Replace the live state of ``model_id`` with the snapshot's contents.

    ``mode`` (Bug-8768) selects whether an imported ``incremental_append_only``
    declaration is reset (``IMPORT``, the default and fail-safe) or preserved
    (``RESTORE``, the in-place version restore path). See :class:`RehydrationMode`.


    Wraps the work in a single transaction (the caller's session); commit
    is the caller's responsibility.

    When ``preserve_aggregates`` is True, existing aggregates are left
    untouched — they are neither retired nor deleted. This allows
    aggregates to retire naturally via the scheduler when they are no
    longer used, rather than being force-retired on revert.

    When ``preserve_pockets`` is True, existing pocket tables are left
    untouched for the same reason.

    Named sets and KPIs are UPSERTED IN PLACE (Bug-7982 Codex residual 3): a
    surviving parent is UPDATEd with only its DEFINITION columns on a revert, so
    its live governance (certification/owner/replacement) is preserved by omission
    and its ondelete=CASCADE children are never deleted. See
    ``_upsert_definition_rows``.

    When ``restore_governance`` is False (Bug-6205 — the revert path), the
    model's live GOVERNANCE state is left in place instead of being replaced
    from the snapshot: personas, data tags (+ their column assignments and
    persona tag restrictions), and row-security rules. Revert is contractually
    "definition only -- governance status is never reverted"
    (solution-details-due-diligence.md). Deploy and import keep the default
    True (full restore). Because definition tables (model_tables / columns)
    are always rebuilt, two FK couplings are handled so the preserved
    governance survives the rebuild by id:
      * ``row_security_rules.mapping_table_id`` is RESTRICT to model_tables;
        it is detached before the table delete and re-pointed afterwards to
        the same table id if it still exists in the reverted-to definition.
      * ``data_tag_columns.model_column_id`` CASCADE-deletes on the column
        rebuild; the assignments are captured and re-inserted afterwards for
        columns that still exist.
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

    # Bug-8614 / F-013-09 / F-020-12 (Bug-9120): the fact-anchor contract is
    # checked HERE so every caller inherits it (revert,
    # catalogue/dbt/cube/atscale/YAML import), not only the two consumers —
    # project import and model JSON import — that also preflight it. Without
    # this, an invalid snapshot reaches the per-row INSERT below and trips the
    # partial unique index
    # ``uq_model_tables_one_fact_per_model`` as a raw IntegrityError / 500
    # instead of a clean, typed fact-anchor error. The already-guarded callers
    # preflight and raise before reaching here, so a valid bundle is unaffected
    # and the check never double-fires.
    anchor_error = fact_anchor_violation(snapshot.get("tables") or [])
    if anchor_error:
        raise OneFactViolationError(f"snapshot violates fact-anchor contract: {anchor_error}")

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

    # Capture source bindings before the truncate/upsert. A borrowed source can
    # serve artifacts in another model, even when this model has none to keep.
    snapshot_source_ids = [
        source_id
        for source in snapshot.get("data_sources") or []
        if (source_id := _coerce_uuid(source.get("id"))) is not None
    ]
    pre_revert_source_connection_ids: dict[str, str] = {}
    if snapshot_source_ids:
        source_rows = (
            await tenant_db.execute(
                select(DataSource.id, DataSource.project_connection_id).where(
                    DataSource.id.in_(snapshot_source_ids)
                )
            )
        ).all()
        pre_revert_source_connection_ids = {
            str(source_id): str(connection_id)
            for source_id, connection_id in source_rows
        }

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
    _guard_conflicting_calendar_tables(snapshot)

    # Bug-6205: when governance is preserved (revert) the governance rows are
    # NOT deleted/reinserted, but the definition tables/columns they reference
    # ARE rebuilt. Capture the two FK couplings before truncate so they can be
    # restored by id after the rebuild:
    #   * row_security_rules.mapping_table_id -> model_tables (RESTRICT): must
    #     be detached now or the table delete in truncate 500s.
    #   * data_tag_columns.model_column_id -> model_columns (CASCADE): the
    #     assignment rows vanish when columns are rebuilt; capture to re-add.
    preserved_rs_mapping: dict[UUID, UUID] = {}
    preserved_data_tag_columns: list[tuple[UUID, UUID]] = []
    # Bug-7982 (Codex residual 3): named-set and KPI governance + their
    # CASCADE children are NO LONGER captured here. Those parents are upserted
    # IN PLACE (``_upsert_definition_rows``), so their governance is preserved by
    # omission (the revert UPDATE writes only definition columns) and their
    # children (version history, usage, KPI snapshot/latest) are never deleted —
    # nothing to capture or reinsert. Only the row-security / data-tag FK
    # couplings still need capture, because model_tables / model_columns ARE
    # rebuilt below.
    if not restore_governance:
        rs_map_q = await tenant_db.execute(
            select(RowSecurityRule.id, RowSecurityRule.mapping_table_id).where(
                RowSecurityRule.model_id == model_id,
                RowSecurityRule.mapping_table_id.isnot(None),
            )
        )
        preserved_rs_mapping = {r[0]: r[1] for r in rs_map_q.all()}
        if preserved_rs_mapping:
            await tenant_db.execute(
                update(RowSecurityRule)
                .where(RowSecurityRule.id.in_(list(preserved_rs_mapping)))
                .values(mapping_table_id=None)
            )
        dtc_q = await tenant_db.execute(
            select(
                data_tag_columns.c.tag_id,
                data_tag_columns.c.model_column_id,
            )
            .select_from(data_tag_columns)
            .join(DataTag, DataTag.id == data_tag_columns.c.tag_id)
            .where(DataTag.model_id == model_id)
        )
        preserved_data_tag_columns = [(r[0], r[1]) for r in dtc_q.all()]

    # F-013-02: on a revert, a Named Query REMOVED in the reverted-to version is
    # about to be truncated below. Schedule a DROP of its orphan physical table
    # FIRST (while target/connection metadata still exists) so reverting away a
    # NQ does not leak its materialised table. Surviving NQs keep their table
    # (same physical_table_name is re-inserted) and rebuild via the stale gate.
    if mode == RehydrationMode.RESTORE:
        await _schedule_removed_named_query_cleanup(
            model_id, snapshot, tenant_db, requested_by=actor,
        )

    # 2. Truncate every per-model child table for this model.
    await _truncate_model_children(
        model_id, tenant_db,
        preserve_aggregates=preserve_aggregates,
        preserve_pockets=preserve_pockets,
        preserve_targets=preserve_targets,
        restore_governance=restore_governance,
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

    # Bug-6205: re-attach the preserved-governance FK couplings now that the
    # definition tables/columns are rebuilt (with their original ids, since
    # _strip_pk_and_uuids keeps the snapshot id). Anything whose referenced
    # table/column was dropped by the reverted-to definition is left detached
    # rather than crashing — that mirrors the definition change.
    if not restore_governance:
        detached_rs_rules: list[UUID] = []
        detached_tag_links: list[tuple[UUID, UUID]] = []
        if preserved_rs_mapping:
            live_table_ids = {
                r[0] for r in (
                    await tenant_db.execute(
                        select(ModelTable.id).where(
                            ModelTable.model_id == model_id
                        )
                    )
                ).all()
            }
            for rule_id, table_id in preserved_rs_mapping.items():
                if table_id in live_table_ids:
                    await tenant_db.execute(
                        update(RowSecurityRule)
                        .where(RowSecurityRule.id == rule_id)
                        .values(mapping_table_id=table_id)
                    )
                else:
                    # The reverted-to definition dropped the mapping table this
                    # rule pointed at. Fail-closed: leave mapping_table_id NULL
                    # rather than re-point it at a wrong table. At query time the
                    # rule compiles via _load_mapping_table(None), which raises
                    # RowSecurityCompileError -> 422 for every affected principal
                    # (a query OUTAGE, never a row-exposure). The operator must be
                    # told to re-point it (below).
                    detached_rs_rules.append(rule_id)
        if preserved_data_tag_columns:
            live_col_ids = {
                r[0] for r in (
                    await tenant_db.execute(
                        select(ModelColumn.id)
                        .join(
                            ModelTable,
                            ModelColumn.model_table_id == ModelTable.id,
                        )
                        .where(ModelTable.model_id == model_id)
                    )
                ).all()
            }
            live_tag_ids = {
                r[0] for r in (
                    await tenant_db.execute(
                        select(DataTag.id).where(DataTag.model_id == model_id)
                    )
                ).all()
            }
            for tag_id, col_id in preserved_data_tag_columns:
                if tag_id in live_tag_ids and col_id in live_col_ids:
                    await tenant_db.execute(
                        insert(data_tag_columns).values(
                            tag_id=tag_id, model_column_id=col_id,
                        )
                    )
                else:
                    # The tag is preserved on revert, so this fires when the
                    # reverted-to definition DROPPED the tagged column: the CLS
                    # link has no column to re-attach to and is lost along with
                    # the column (nothing to expose). Surface it (below).
                    detached_tag_links.append((tag_id, col_id))

        # Bug-6591 (reopened) — operator-visible signal. A revert whose
        # reverted-to definition dropped a table/column that a PRESERVED
        # governance rule referenced leaves that FK link detached. This is
        # fail-closed, NOT a data-exposure: a detached row-security mapping makes
        # the rule fail-closed at query time (_load_mapping_table(None) ->
        # RowSecurityCompileError -> 422 for affected principals), i.e. a query
        # OUTAGE until re-pointed — row filtering is never silently widened. A
        # detached CLS tag link only arises when the tagged column itself was
        # dropped by the revert, so there is nothing left to expose either. The
        # operator still needs to know so they can restore intended access, so we
        # raise a log warning and a persisted ModelAlert visible in the UI.
        if detached_rs_rules or detached_tag_links:
            # Bug-6855: include specific rule/tag ids in the log and alert so
            # the operator can identify and fix the exact detached governance
            # objects, not just a count.
            rs_ids_str = ", ".join(str(rid) for rid in detached_rs_rules)
            tag_links_str = ", ".join(
                f"tag={tid}/col={cid}"
                for tid, cid in detached_tag_links
            )
            logger.warning(
                "Bug-6205 revert on model %s left governance links detached: "
                "%d row-security mapping(s) [%s], %d CLS tag link(s) [%s] "
                "referenced a table/column dropped by the reverted-to "
                "definition",
                model_id,
                len(detached_rs_rules), rs_ids_str,
                len(detached_tag_links), tag_links_str,
            )
            now = datetime.now(timezone.utc)
            # Bug-6855: build a detail that lists the affected rule ids for
            # operator action, not just a count.
            detail_parts = []
            if detached_rs_rules:
                detail_parts.append(
                    f"{len(detached_rs_rules)} row-security rule(s) "
                    f"(ids: {rs_ids_str}) lost their mapping table reference"
                )
            if detached_tag_links:
                detail_parts.append(
                    f"{len(detached_tag_links)} column-level security tag "
                    f"assignment(s) could not be re-attached"
                )
            detail_text = (
                "; ".join(detail_parts) + ". "
                "These could not be re-attached because the reverted-to model "
                "definition no longer contains the table or column they "
                "referenced. This fails CLOSED, not open: a detached "
                "row-security rule causes affected users' queries on this "
                "model to be DENIED (error) until it is re-pointed to a "
                "current table — row filtering is never silently widened; a "
                "detached tag assignment means its column was dropped by the "
                "revert. Review and re-point the rule (or re-tag a current "
                "column) to restore access."
            )
            tenant_db.add(
                ModelAlert(
                    model_id=model_id,
                    severity="warning",
                    category="governance_revert",
                    title=(
                        f"Governance links detached after revert "
                        f"({len(detached_rs_rules)} rules, "
                        f"{len(detached_tag_links)} tags)"
                    ),
                    detail=detail_text,
                    related_object_type="governance_revert",
                    # Fresh id per revert so the partial-unique dedup index
                    # never collides across repeated reverts.
                    related_object_id=uuid.uuid4(),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )

    await _insert_udas(model_id, snapshot, tenant_db)
    await _insert_joins(model_id, snapshot, tenant_db)
    await _insert_hierarchies(model_id, snapshot, tenant_db)
    await _insert_dimensions(model_id, snapshot, tenant_db)
    await _synthesize_missing_hierarchy_dimensions(model_id, snapshot, tenant_db)
    # Attribute relationships FK dimensions + model_columns; insert after both.
    await _insert_attribute_relationships(model_id, snapshot, tenant_db)
    # Back-fill dimension provenance FKs now that relationships exist.
    await _backfill_dimension_provenance(model_id, snapshot, tenant_db)
    await _insert_measures(model_id, snapshot, tenant_db)

    # F-013-02 follow-on (Bug-1093): restore preserved aggregate-column measure
    # links the measure-delete cascade nulled, but only to measures that still
    # exist after reinsert. A measure dropped in the reverted-to version stays
    # NULL; Bug-7146: _validate_preserved_aggregates now stale-marks any
    # surviving aggregate whose measure links or definitions changed, and
    # marks invalid those whose grain dimensions no longer exist.
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

    # Bug-7982 (Codex residual 3): named sets and KPIs are UPSERTED IN PLACE.
    # On a revert (restore_governance False) the upsert writes only DEFINITION
    # columns for a surviving row, so its live governance (certification, owner,
    # replacement) is preserved by omission, and its CASCADE children (version
    # history, usage, KPI snapshot/latest) are never deleted/reinserted — no
    # separate governance-restore or child-reinsert step is needed.
    await _insert_named_sets(
        model_id, snapshot, tenant_db, restore_governance=restore_governance,
    )
    await _insert_kpis(
        model_id, snapshot, tenant_db, restore_governance=restore_governance,
    )
    await _insert_named_queries(
        model_id, snapshot, tenant_db, mode=mode, reseed=destination_seed,
    )
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
            # Bug-8768 (B8768-R1-05): the append-only reset is keyed on the
            # rehydration MODE, not on force_pending — an import caller cannot
            # transfer a source assertion by omitting a physical-rebind flag.
            reset_incremental_authority=(mode == RehydrationMode.IMPORT),
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
        # Bug-6205: personas are governance. On revert (restore_governance
        # False) they are left in place. On the revert path preserve_pockets
        # is also True, so this branch is skipped entirely and personas survive
        # anyway; the guard keeps the flag honest for any other caller combo.
        if restore_governance:
            await _insert_personas(model_id, snapshot, tenant_db)
        await _insert_pockets(
            model_id, snapshot, tenant_db,
            force_stale=force_pocket_stale,
            reseed=destination_seed if force_pocket_stale else None,
        )
    # Bug-6205: data tags (+ persona tag restrictions) and row-security rules
    # are governance — skipped on revert so live governance is not clobbered.
    if restore_governance:
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
    #    deployed_version_id, last_deployed_at and optimizer artifact stamps —
    #    those are stable / managed elsewhere. The predictive stamps are
    #    explicitly cleared because a restore updates an existing model row;
    #    omission alone would retain the pre-restore artifact claim.
    snap_model = snapshot.get("model") or {}
    update_kwargs: dict[str, Any] = {}
    uuid_fields = _model_uuid_fields()
    # Bug-8950: ``models.target_id`` is a tenant-schema-wide FK to a DataTarget
    # and is restored by the generic model-scalar copy below. A hand-edited or
    # cross-project bundle can re-point the model's DEFAULT materialisation
    # destination — and thus the project_connection_id credentials future
    # aggregates/pockets inherit — at another project's DataTarget (the API
    # guard refuses this on the write path). Only THIS snapshot's targets are
    # (re)inserted, so any target_id outside snap["data_targets"] is dangling
    # and is dropped to NULL (a nullable FK).
    valid_target_ids = {
        str(t["id"]) for t in (snapshot.get("data_targets") or []) if t.get("id")
    }
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
            if (
                field == "target_id"
                and val is not None
                and str(val) not in valid_target_ids
            ):
                logger.warning(
                    "Bug-8950: dropping models.target_id %s on rehydrate of "
                    "model %s: absent from the snapshot's data targets "
                    "(cross-project or hand-edited bundle).",
                    val, model_id,
                )
                val = None
            update_kwargs[field] = val
    update_kwargs.update({
        "predictive_built_for_version_id": None,
        "predictive_built_for_epoch": None,
    })
    if update_kwargs:
        await tenant_db.execute(
            update(Model).where(Model.id == model_id).values(**update_kwargs)
        )

    # 6. A revert can re-point a shared source. Invalidate every model that
    # reads it before its artifact can serve rows built from the former source.
    await _invalidate_borrowing_models_on_source_repoint(
        snapshot,
        tenant_db,
        pre_revert_source_connection_ids,
    )

    # 7. Validate preserved materializations and mark invalid ones.
    # Bug-7146: pass the snapshot so the validator can stale-mark
    # aggregates whose materialised content may not match the reverted-to
    # definitions.
    if preserve_aggregates:
        await _validate_preserved_aggregates(
            model_id, tenant_db, snapshot=snapshot,
        )
    if preserve_pockets:
        await _validate_preserved_pockets(model_id, tenant_db)


# ---------------------------------------------------------------------------
# Validation for preserved materializations
# ---------------------------------------------------------------------------

async def _validate_preserved_aggregates(
    model_id: UUID,
    db: AsyncSession,
    *,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Mark aggregates as invalid/stale after revert/rehydration.

    Two checks:
    1. Grain validity: mark ``invalid`` any aggregate whose grain lists
       dimensions that no longer exist in the reverted-to model.
    2. Bug-7146: definition drift — mark ``is_stale=True`` any surviving
       ``active`` aggregate so its materialised content is rebuilt against
       the current (reverted-to) measure/dimension definitions. The
       aggregate's physical table was built against the *pre-revert*
       definitions; the serving contract is the *deployed snapshot*, and
       nothing else detects that the two have diverged. Stale-marking
       triggers the scheduler refresh sweep to rebuild the aggregate from
       the now-current definitions, closing the draft-leak-via-refresh
       and stale-content-after-revert windows documented in the finding.

       When ``snapshot`` is provided (the revert/import path), the
       validator compares each aggregate column's linked measure
       definition against the snapshot's measure definition. If ANY
       value-defining field differs (expression, default_agg,
       source_column_id, semi_additive_behavior), the aggregate is
       stale. When ``snapshot`` is None (legacy callers), all non-
       invalid/non-retired aggregates are stale-marked conservatively.
    """
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
    aggregates = list(agg_q.scalars().all())

    # Build a lookup of snapshot measure definitions by id for drift
    # comparison. The value-defining fields are those that change the
    # materialised aggregate content: expression, default_agg,
    # source_column_id, and semi_additive_behavior.
    _DEFINITION_FIELDS = (
        "expression", "default_agg", "source_column_id",
        "semi_additive_behavior",
    )
    snap_measures_by_id: dict[str, dict[str, Any]] = {}
    if snapshot:
        for m in snapshot.get("measures") or []:
            mid = m.get("id")
            if mid:
                snap_measures_by_id[str(mid)] = m

    for agg in aggregates:
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
            continue

        # Bug-7146: stale-mark surviving aggregates whose measure
        # definitions may have drifted from what the aggregate's
        # physical table contains.
        if agg.is_stale:
            continue  # already stale

        should_stale = False
        if snap_measures_by_id:
            # Compare each aggregate column's linked measure against
            # the snapshot's definition of that measure.
            agg_cols_q = await db.execute(
                select(AggregateColumn).where(
                    AggregateColumn.aggregate_definition_id == agg.id
                )
            )
            for ac in agg_cols_q.scalars().all():
                if ac.measure_id is None:
                    # Measure was dropped — already NULL from cascade;
                    # the aggregate should be invalidated but that is
                    # handled by the grain check or the NULL-measure path.
                    should_stale = True
                    break
                snap_m = snap_measures_by_id.get(str(ac.measure_id))
                if snap_m is None:
                    # Measure id not present in snapshot — it was
                    # dropped in the reverted-to version.
                    should_stale = True
                    break
                # Compare live measure (just rehydrated from snapshot)
                # against the snapshot. If definitions match, the
                # aggregate MIGHT still be valid — but it was built
                # against pre-revert definitions which are unknown.
                # Conservative: always stale-mark on revert.
                should_stale = True
                break
        else:
            # No snapshot provided — conservative stale-mark.
            should_stale = True

        if should_stale:
            await db.execute(
                update(AggregateDefinition)
                .where(AggregateDefinition.id == agg.id)
                .values(is_stale=True)
            )


async def _validate_preserved_pockets(model_id: UUID, db: AsyncSession) -> None:
    """Mark preserved pockets non-serving after a revert/rehydration.

    Two reasons, and the second is unconditional:

    1. a predicate dimension the pocket filters on no longer exists in the
       reverted-to model;
    2. Bug-8602: the revert UPSERTS every column of ``data_sources`` from the
       snapshot, including ``project_connection_id``. Reverting past a source
       re-point therefore silently changes WHICH database the model reads,
       while the preserved pocket still holds rows materialised from the other
       one. Nothing else catches that for a pocket: the control-plane
       invalidator is not on this path (the rehydrator writes the column
       directly), and a pocket carries no source build binding for the
       serve-time guard to refuse (Bug-8780).

    Reason 2 is deliberately not narrowed to "the connection actually changed".
    A revert can also move measures, dimensions and joins under a preserved
    pocket, and ``_validate_preserved_aggregates`` already stale-marks every
    surviving aggregate for exactly that reason (Bug-7146). Matching that
    policy keeps one rule for both artifact kinds; over-invalidation costs a
    refresh, under-invalidation serves rows from the wrong database.
    """
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
        # Bug-7299 (cont.): PocketPredicate stores the dimension column
        # name in ``column_name``, not ``dimension_name`` (which doesn't
        # exist on the model).
        pred_q = await db.execute(
            select(PocketPredicate.column_name).where(
                PocketPredicate.pocket_definition_id == pocket.id
            )
        )
        pred_dims = {r[0] for r in pred_q.all()}
        missing = pred_dims - valid_dims
        # Bug-7299: PocketDefinition has no ``is_stale`` column — the freshness
        # field is ``status`` (matcher keys on status=="fresh"). Writing
        # ``is_stale=True`` crashed with "Unconsumed column names", breaking
        # every revert where a preserved pocket referenced a dropped dimension.
        # Aligned with the aggregate validator's pattern: mark the pocket
        # ``stale`` so the matcher ignores it until it is rebuilt.
        #
        # Bug-8602: applied to EVERY preserved pocket, not only one whose
        # predicate dimension vanished — see this function's docstring. A
        # retired pocket is left alone, matching the invalidators elsewhere.
        if pocket.retired_at is not None:
            continue
        # Rehydration changes the model definition/source regardless of the
        # prior population verdict. Always withdraw the physical generation
        # and clear proof/trust; preserving an old ineligible label would let a
        # later matcher proof reactivate rows built for the reverted definition.
        await db.execute(
            update(PocketDefinition)
            .where(PocketDefinition.id == pocket.id)
            .values(
                status="stale",
                failure_reason=None,
                population_eligibility="unknown",
                population_eligibility_reason=None,
                population_proof_fingerprint=None,
                row_manifest=None,
                active_refresh_run_id=None,
                built_for_version_id=None,
                built_for_epoch=None,
            )
        )


# ---------------------------------------------------------------------------
# Bug-8836: source repoint widening for revert
# ---------------------------------------------------------------------------


async def _invalidate_borrowing_models_on_source_repoint(
    snapshot: dict[str, Any],
    db: AsyncSession,
    pre_revert_connection_ids: dict[str, str],
) -> None:
    """Invalidate artifacts on models borrowing DataSources changed by revert.

    Bug-8836: a revert upserts every column of ``data_sources`` from the
    snapshot, including ``project_connection_id``. Reverting past a source
    re-point silently changes which database the reverted model reads, while
    another model borrowing the SAME DataSource continues serving rows
    materialised from the previous database.

    ``update_source`` already widens its invalidation via
    ``model_ids_reading_source``. The revert path does not — the rehydrator
    writes ``project_connection_id`` directly with no invalidator at all.

    This function compares every snapshot binding with the value captured
    before the rehydrator changed it, and for each changed source calls
    ``invalidate_artifacts_for_model`` on EVERY
    model whose tables read through it (via ``model_ids_reading_source``),
    not only the reverted model.
    """
    from shared.aggregate_connection import model_ids_reading_source
    from shared.artifact_target_binding import invalidate_artifacts_for_model

    snapshot_sources = snapshot.get("data_sources") or []
    if not snapshot_sources or not pre_revert_connection_ids:
        return

    changed_models: dict[UUID, list[str]] = {}
    for s in snapshot_sources:
        sid = _coerce_uuid(s.get("id"))
        if sid is None:
            continue
        sid_str = str(sid)
        snap_conn_id = str(s.get("project_connection_id", ""))
        live_conn_id = pre_revert_connection_ids.get(sid_str, "")
        if snap_conn_id and live_conn_id and snap_conn_id != live_conn_id:
            models = await model_ids_reading_source(sid, db)
            for mid in models:
                if mid not in changed_models:
                    changed_models[mid] = []
                changed_models[mid].append(sid_str)

    for mid, source_refs in changed_models.items():
        await invalidate_artifacts_for_model(
            db, mid,
            reason=(
                f"Model revert changed project_connection_id on "
                f"DataSource(s) {', '.join(source_refs)}, which this "
                f"model's tables read from."
            ),
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
    preserve_targets: bool = False,
    restore_governance: bool = True,
) -> None:
    """Delete every per-model child row in dependency-safe order.

    When ``preserve_aggregates`` is True, aggregate definitions and their
    children (columns, policies, runs) are left untouched. They will be
    validated after rehydration and marked invalid if dependencies are
    missing.

    When ``preserve_pockets`` is True, pocket definitions and their
    children are left untouched.

    When ``restore_governance`` is False (revert), the governance tables
    (data_tags, personas, row_security_rules) are left untouched so the
    caller can preserve live governance across a definition-only rebuild
    (Bug-6205). Their FK links into the rebuilt tables/columns are re-attached
    by the caller after reinsert.
    """

    # Aggregate columns + refresh policies + refresh runs are children of
    # aggregate_definitions. Skip if preserving aggregates.
    if not preserve_aggregates:
        agg_ids_q = await db.execute(
            select(AggregateDefinition.id).where(AggregateDefinition.model_id == model_id)
        )
        agg_ids = [r[0] for r in agg_ids_q.all()]
        if agg_ids:
            # Bug-7852: delete QuantileCoverage rows before their parent
            # AggregateColumn rows (defence-in-depth; CASCADE would also
            # handle this, but explicit delete is deterministic across DBs).
            await db.execute(
                delete(QuantileCoverage).where(
                    QuantileCoverage.aggregate_definition_id.in_(agg_ids)
                )
            )
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

    # Named sets + KPIs are NOT truncated here. Bug-7982 (Codex residual 3): they
    # are UPSERTED IN PLACE by ``_upsert_definition_rows`` so their
    # ondelete=CASCADE children (version history, usage, KPI snapshot/latest) are
    # never cascade-deleted — which is what races a concurrent lock-less child
    # writer. A KPI's measure/dimension self-references are ``ondelete=SET NULL``,
    # so the measure/dimension deletes below simply NULL them and the KPI upsert
    # (after measures/dimensions are reinserted) restores them from the snapshot.

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
    # Bug-6205: on revert governance is preserved — keep the tag rows; their
    # data_tag_columns links (CASCADE off the column rebuild) are re-attached
    # by the caller.
    if restore_governance:
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
    # Named Query children + definitions are always rebuilt from the snapshot.
    # Unlike pockets, ``preserve_pockets=True`` is not a Named Query preserve
    # flag: RESTORE must reinsert the surviving definition/artifact with the
    # same ids and physical name while clearing live run/manifest state. Delete
    # order matters because the artifact points at refresh runs (SET NULL), so
    # artifacts go first. This also keeps IMPORT and RESTORE on one funnel.
    nq_ids_q = await db.execute(
        select(NamedQuery.id).where(NamedQuery.model_id == model_id)
    )
    nq_ids = [r[0] for r in nq_ids_q.all()]
    if nq_ids:
        await db.execute(
            delete(NamedQueryArtifact).where(
                NamedQueryArtifact.named_query_id.in_(nq_ids)
            )
        )
        await db.execute(
            delete(NamedQueryRefreshPolicy).where(
                NamedQueryRefreshPolicy.named_query_id.in_(nq_ids)
            )
        )
        await db.execute(
            delete(NamedQueryRefreshRun).where(
                NamedQueryRefreshRun.named_query_id.in_(nq_ids)
            )
        )
    await db.execute(
        delete(NamedQuery).where(NamedQuery.model_id == model_id)
    )
    if not preserve_pockets:
        # Bug-6205: personas are governance — keep them on revert.
        if restore_governance:
            await db.execute(delete(Persona).where(Persona.model_id == model_id))

    # Row-security rules: FK to model_tables.mapping_table_id is RESTRICT, so
    # this MUST come before model_tables are deleted.
    # Bug-6205: on revert governance is preserved — keep the rules. The caller
    # detached mapping_table_id before this truncate so the RESTRICT FK does
    # not block the model_tables delete, then re-points it after the rebuild.
    if restore_governance:
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

    Bug-7147 [CORRECTNESS]: the upsert path previously only INSERTED/UPDATED the
    snapshot's rows — it never removed live sources/targets that the
    reverted-to snapshot did not contain. A model that gained a data source or
    target AFTER the reverted-to version therefore kept those "ghost" rows on
    revert, so the live model no longer matched the version it claimed to be.
    A revert must reconcile the live set to EXACTLY the snapshot's set. After
    the upserts, live rows whose id is absent from the snapshot are deleted,
    FK-safely: a target still referenced by a PRESERVED aggregate/pocket is a
    RESTRICT FK (no ondelete) and is skipped rather than deleted (the same FK
    chain that motivated preserve_targets in the first place) — that is not a
    ghost, it is an in-use target the revert cannot drop without also dropping
    the preserved materialisation. A source's children (model_tables etc.) were
    already truncated and are reinserted from the snapshot below, so a ghost
    source carries no live dependant and deletes cleanly.
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

    snapshot_source_ids: set[UUID] = set()
    snapshot_target_ids: set[UUID] = set()

    # Bug-8602: TARGETS BEFORE SOURCES, and the order is load-bearing — do not
    # swap these two loops back.
    #
    # A revert holds both tables' row locks in ONE transaction (the upserts
    # below, then the artifact staling, then a single commit). An aggregate
    # build's finalisation ALSO takes both, via
    # ``shared/artifact_target_binding.lock_finalization_rows``, which fixes the
    # canonical order as ``project_connections -> data_targets -> data_sources``.
    # Upserting sources first made this the one writer that takes two of those
    # families in the opposite order, so a revert concurrent with a refresh or
    # an optimizer create on the same model deadlocked — reproduced against real
    # PostgreSQL. Whichever side Postgres aborts is bad: a 500 on a user-facing
    # revert, or a poisoned build session that cannot clean up its orphaned
    # physical table.
    #
    # Nothing else constrains the order (DataSource and DataTarget are siblings
    # under Model with no FK between them), and ``_reconcile_sources_and_targets``
    # below already deletes targets before sources, so this also makes the whole
    # revert transaction internally consistent.
    for t in snap.get("data_targets", []):
        row = _strip_pk_and_uuids(t)
        row["model_id"] = model_id
        if connection_id_remap:
            old_cid = str(row.get("project_connection_id", ""))
            if old_cid in connection_id_remap:
                row["project_connection_id"] = UUID(connection_id_remap[old_cid])
        # Bug-8790: strip the deprecated config.project_id from rehydrated
        # targets — the connection's project is authoritative.
        tconfig = row.get("config", {})
        if isinstance(tconfig, dict) and "project_id" in tconfig:
            tconfig.pop("project_id")
        tid = row.get("id")
        if isinstance(tid, UUID):
            snapshot_target_ids.add(tid)
        await _write(DataTarget, row)
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
        sid = row.get("id")
        if isinstance(sid, UUID):
            snapshot_source_ids.add(sid)
        await _write(DataSource, row)

    if not upsert:
        # Non-upsert callers truncate sources/targets wholesale before this
        # inserter runs, so there is nothing to reconcile — the live set is
        # exactly the snapshot's set already.
        return

    await _reconcile_sources_and_targets(
        model_id, db,
        snapshot_source_ids=snapshot_source_ids,
        snapshot_target_ids=snapshot_target_ids,
    )


async def _reconcile_sources_and_targets(
    model_id: UUID,
    db: AsyncSession,
    *,
    snapshot_source_ids: set[UUID],
    snapshot_target_ids: set[UUID],
) -> None:
    """Bug-7147: delete live data_sources/data_targets absent from the snapshot.

    Only runs on the preserve/upsert (revert) path, where sources/targets were
    NOT truncated. Reconciles the live set to exactly the snapshot's set so no
    ghost row survives a revert. A target still referenced by a preserved
    aggregate or pocket (RESTRICT FK) is retained: deleting it would violate the
    FK and 500 the revert, and it is genuinely in use by a surviving
    materialisation, so it is not a ghost the revert may drop.
    """
    # Live targets for this model.
    live_target_ids = {
        r[0] for r in (
            await db.execute(
                select(DataTarget.id).where(DataTarget.model_id == model_id)
            )
        ).all()
    }
    ghost_target_ids = live_target_ids - snapshot_target_ids
    if ghost_target_ids:
        # Targets referenced by a preserved aggregate/pocket are protected —
        # their RESTRICT FK forbids deletion and they are still serving.
        referenced_target_ids: set[UUID] = set()
        referenced_target_ids.update(
            r[0] for r in (
                await db.execute(
                    select(AggregateDefinition.target_id).where(
                        AggregateDefinition.model_id == model_id,
                        AggregateDefinition.target_id.in_(list(ghost_target_ids)),
                    )
                )
            ).all()
        )
        referenced_target_ids.update(
            r[0] for r in (
                await db.execute(
                    select(PocketDefinition.target_id).where(
                        PocketDefinition.model_id == model_id,
                        PocketDefinition.target_id.in_(list(ghost_target_ids)),
                    )
                )
            ).all()
        )
        deletable_targets = ghost_target_ids - referenced_target_ids
        if deletable_targets:
            await db.execute(
                delete(DataTarget).where(DataTarget.id.in_(list(deletable_targets)))
            )
        if referenced_target_ids:
            logger.warning(
                "Bug-7147 revert on model %s: %d ghost data_target(s) retained "
                "because a preserved aggregate/pocket still references them "
                "[%s]; they are not in the reverted-to snapshot but cannot be "
                "dropped without dropping the surviving materialisation",
                model_id,
                len(referenced_target_ids),
                ", ".join(str(t) for t in sorted(referenced_target_ids, key=str)),
            )

    # Live sources for this model. A ghost source's children (model_tables,
    # calendar_tables, source_statistics) were already truncated and are
    # reinserted from the snapshot below, so a source absent from the snapshot
    # has no live dependant and deletes cleanly (ModelTable is CASCADE anyway).
    live_source_ids = {
        r[0] for r in (
            await db.execute(
                select(DataSource.id).where(DataSource.model_id == model_id)
            )
        ).all()
    }
    ghost_source_ids = live_source_ids - snapshot_source_ids
    if ghost_source_ids:
        await db.execute(
            delete(DataSource).where(DataSource.id.in_(list(ghost_source_ids)))
        )


async def _insert_tables_and_columns(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    # Bug-8932: ``model_tables.calendar_table_id`` is a tenant-schema-wide FK,
    # so a hand-edited or cross-project bundle can point it at another model's
    # calendar table and the DB FK constraint does not stop it — reinstating
    # exactly the binding the API guard (Bug-8878) refuses on the write path.
    # Only THIS snapshot's calendar tables are (re)inserted (_insert_calendar_
    # tables), so the valid post-rehydrate set is exactly snap["calendar_tables"];
    # any id outside it is dangling and is dropped to NULL (a nullable FK),
    # mirroring the dangling-drop the revert path already does elsewhere.
    valid_calendar_ids = {
        str(c["id"]) for c in (snap.get("calendar_tables") or []) if c.get("id")
    }
    for t in snap.get("tables", []):
        row = _strip_pk_and_uuids(t)
        row["model_id"] = model_id
        cal_id = row.get("calendar_table_id")
        if cal_id is not None and str(cal_id) not in valid_calendar_ids:
            logger.warning(
                "Bug-8932: dropping model_tables.calendar_table_id %s on "
                "rehydrate of model %s (table '%s'): absent from the snapshot's "
                "calendar tables (cross-model or hand-edited bundle).",
                cal_id, model_id, t.get("display_name", t.get("id", "?")),
            )
            row.pop("calendar_table_id", None)
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
    from shared.schemas.domains.aggregates_security import (
        POPULATION_PARTICIPATION_VALUES,
        coerce_population_participation,
        coerce_population_participation_source,
        POPULATION_PARTICIPATION_SOURCE_VALUES,
    )

    for j in snap.get("joins", []):
        row = _strip_pk_and_uuids(j)
        row["model_id"] = model_id
        # Bug-8615 phase G1. A snapshot saved before this field existed simply
        # omits the key, and the column's server default supplies
        # ``preserve_base_rows`` — the historical, elidable behaviour, so an old
        # snapshot rehydrates to exactly the joins it always did. A key that IS
        # present but carries a value outside the vocabulary (a hand-edited or
        # tampered bundle, which bypasses the API's Literal validation) is
        # coerced rather than persisted, the same non-blocking treatment
        # Bug-6622 established for ``certification_status``. Coercion lands on
        # ``undeclared``, not on the default: "we cannot tell what was meant"
        # must surface as a validator warning, not masquerade as an affirmative
        # modeller declaration. Both states are equally elidable, so the
        # coercion never changes served numbers.
        raw_participation = row.get("population_participation")
        if raw_participation is not None and (
            raw_participation not in POPULATION_PARTICIPATION_VALUES
        ):
            coerced = coerce_population_participation(raw_participation)
            logger.warning(
                "Bug-8615: join %s carried out-of-enum population_participation "
                "%r on rehydrate; coercing to %r (bundle bypassed the API gate).",
                j.get("id"), raw_participation, coerced,
            )
            row["population_participation"] = coerced
        raw_source = row.get("population_participation_source")
        if raw_source is not None and raw_source not in POPULATION_PARTICIPATION_SOURCE_VALUES:
            coerced_source = coerce_population_participation_source(raw_source)
            logger.warning(
                "G4: join %s carried out-of-vocabulary population participation "
                "provenance %r on rehydrate; coercing to %r",
                j.get("id"), raw_source, coerced_source,
            )
            row["population_participation_source"] = coerced_source
        await db.execute(insert(Join).values(**row))


def _build_attribute_id_sets(snap: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (column_ids, uda_ids) from the snapshot for referential checks."""
    col_ids = {str(c["id"]) for c in snap.get("columns", []) if "id" in c}
    uda_ids = {str(u["id"]) for u in snap.get("user_defined_attributes", []) if "id" in u}
    return col_ids, uda_ids


def _hierarchy_has_dangling_attribute(
    hierarchy: dict[str, Any],
    col_ids: set[str],
    uda_ids: set[str],
) -> str | None:
    """Check if any level in a hierarchy references a nonexistent attribute.

    Bug-6725: an import/rehydrate must not leave a hierarchy level pointing
    at a UDA or column id that is absent from the snapshot. Returns a
    diagnostic string describing the first dangling reference found, or
    None when all references resolve.
    """
    for lvl in hierarchy.get("levels", []):
        attr_id = str(lvl.get("key_attribute_id", ""))
        source = lvl.get("key_attribute_source", "")
        if not attr_id:
            continue
        if source == "user_defined_attribute" and attr_id not in uda_ids:
            return (
                f"level '{lvl.get('name', '?')}' references UDA "
                f"{attr_id} which is absent from the snapshot"
            )
        if source == "physical_column" and attr_id not in col_ids:
            return (
                f"level '{lvl.get('name', '?')}' references column "
                f"{attr_id} which is absent from the snapshot"
            )
    return None


async def _insert_hierarchies(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Insert hierarchies, levels, and level-attributes.

    Bug-6725: validates that every hierarchy level's key_attribute_id
    resolves to an existing UDA or column in the snapshot. Hierarchies
    with dangling attribute references are skipped (logged) rather than
    inserted with broken pointers that would 404 on the detail endpoint.
    """
    _HIER_NESTED = {"levels"}
    _LEVEL_NESTED = {"attributes"}

    col_ids, uda_ids = _build_attribute_id_sets(snap)

    for h in snap.get("hierarchies", []):
        dangling = _hierarchy_has_dangling_attribute(h, col_ids, uda_ids)
        if dangling:
            logger.warning(
                "Bug-6725: skipping hierarchy '%s' (id=%s) during rehydrate: %s",
                h.get("name", "?"), h.get("id", "?"), dangling,
            )
            continue

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
    # Strip provenance FK fields on initial insert because they reference
    # dimension_attribute_relationships rows that are not yet inserted.
    # They are back-filled by _backfill_dimension_provenance after the
    # relationship rows exist.
    for d in snap.get("dimensions", []):
        row = _strip_pk_and_uuids(d)
        row["model_id"] = model_id
        row.pop("detail_of_relationship_id", None)
        row.pop("detail_of_dimension_id", None)
        # Snapshot-only catalogue projection (Bug-7959/Bug-8502), not a live
        # Dimension column. Glossary rows are restored separately and the next
        # deployment snapshot recomputes this pinned effective value.
        row.pop("effective_description", None)
        await db.execute(insert(Dimension).values(**row))


async def _insert_attribute_relationships(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Restore declared dimension attribute relationships (derived-grain §5.3).

    Must run AFTER dimensions and model_columns are inserted (it FKs both). Only
    the PINNED declaration travels; verification evidence is live state, absent
    from the snapshot, so a rehydrated relationship is unverified until the
    Phase-2 verifier re-checks it. PKs travel like every other model entity, so
    the dimension/column id references stay valid.
    """
    for r in snap.get("attribute_relationships", []):
        row = _strip_pk_and_uuids(r)
        row["model_id"] = model_id
        await db.execute(insert(DimensionAttributeRelationship).values(**row))


async def _backfill_dimension_provenance(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    """Back-fill detail_of_relationship_id / detail_of_dimension_id on Dimension
    rows after both dimensions and attribute_relationships have been inserted.

    Must run AFTER _insert_dimensions and _insert_attribute_relationships.
    """
    for d in snap.get("dimensions", []):
        rel_id = d.get("detail_of_relationship_id")
        dim_id = d.get("detail_of_dimension_id")
        if rel_id is None and dim_id is None:
            continue
        dim_pk = d.get("id")
        if dim_pk is None:
            continue
        updates: dict[str, Any] = {}
        if rel_id is not None:
            updates["detail_of_relationship_id"] = rel_id
        if dim_id is not None:
            updates["detail_of_dimension_id"] = dim_id
        if updates:
            await db.execute(
                update(Dimension)
                .where(Dimension.id == dim_pk, Dimension.model_id == model_id)
                .values(**updates)
            )


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


# Bug-6591 (reopened): legacy snapshots persisted BEFORE the AtScale/Cube/dbt
# mapper normalisation carry non-canonical ``default_agg`` tokens. The mappers
# now emit canonical tokens (average->avg, median->p50), but those legacy VALUES
# still live inside every saved model version and exported bundle written before
# the fix. Map the KNOWN legacy tokens to their canonical form on the
# rehydrate/import boundary so those snapshots revert/import cleanly. Mirrors the
# read-coercion the mappers use (atscale_mapper._CALC_METHOD_MAP,
# dbt_mapper/cube_mapper) — kept in lock-step with them.
#   * "average" -> "avg"   (AVG SQL function)
#   * "median"  -> "p50"   (the exact 50th-percentile quantile stat)
# A bare "percentile" carries no fraction so it cannot resolve to a specific
# pNN; it (and any other unknown token) falls to the disabled-measure path
# rather than guessing, mirroring atscale_mapper._resolve_calc_method.
# Bug-6613 (sibling of Bug-6591): legacy snapshots persisted BEFORE the
# F-020-09 mapper normalisation carry non-canonical ``semi_additive_behavior``
# tokens. The old AtScale mapper emitted f"{position}_value" (e.g. "last_value",
# "max_value") — invalid enums the rewriter could not interpret; other importers
# emitted free-form strings ("max_over_order_date"). Those legacy VALUES still
# live inside every saved model version and exported bundle written before the
# fix. Map the KNOWN legacy tokens (the deterministic f"{position}_value" forms)
# to their canonical enum on the rehydrate/import boundary so those snapshots
# revert/import cleanly. Mirrors atscale_mapper._SEMI_ADDITIVE_POSITION_MAP
# (position "average" -> "avg_of_children") — kept in lock-step with it.
# Free-form tokens (e.g. "max_over_order_date", which embed a column name) have
# no fixed synonym and fall to the disabled-measure path rather than guessing.
_LEGACY_SEMI_ADDITIVE_COERCE: dict[str, str] = {
    "last_value": "last_non_empty",
    "first_value": "first_non_empty",
    "min_value": "min",
    "max_value": "max",
    "average_value": "avg_of_children",
}


def _validate_measure_enums(row: dict[str, Any]) -> None:
    """Normalise/coerce measure enums on the rehydrate/import boundary.

    The rehydrator inserts measure rows directly, so the Pydantic API
    validators never run. The FORWARD create/update API surface stays strict
    (schema ``_validate_default_agg`` and the ``MeasureCreate`` semi-additive
    validator reject any non-canonical value); this REHYDRATE/IMPORT boundary is
    instead tolerant-with-coercion so a legacy saved version or exported bundle
    is never bricked by an enum tightening. Both enums follow the SAME contract:
      * a canonical value is accepted and case-normalised in place;
      * a KNOWN legacy token is coerced to its canonical form;
      * an unresolvable/free-form token is imported as a DISABLED measure (the
        field reset to a safe value + ``is_invalid`` set + a reason) rather than
        crashing the whole snapshot — the operator gets a warning and re-enables
        the measure manually.

    Bug-6591 (reopened) — ``default_agg``: legacy snapshots carry
    "average"/"median"/"percentile" (the mappers now emit canonical avg/p50). A
    hard reject made every such saved version and exported bundle un-revertable
    and un-importable. Known tokens coerce (average->avg, median->p50); an
    unresolvable token (bare "percentile", free-form) resets to the NOT-NULL
    "sum" column default + is_invalid. Mirrors atscale_mapper._resolve_calc_method.

    Bug-6613 (sibling) — ``semi_additive_behavior``: legacy snapshots carry the
    old AtScale f"{position}_value" tokens ("last_value", "max_value", ...) and
    other importer free-form strings ("max_over_order_date"). The prior HARD
    reject here was the exact Bug-6591 failure mode on a different field. Known
    tokens coerce (last_value->last_non_empty, ...); an unresolvable token resets
    to NULL (fully additive — the mapper's unknown-position fallback) + is_invalid.
    NULLing rather than guessing avoids WRONG NUMBERS: a balance whose intended
    last-non-empty behaviour we cannot recover must not silently SUM across time.
    NOTE: is_invalid does NOT make the query path skip the measure — the router
    still source-renders an invalid measure and returns a query_fallback alert;
    the invalid flag + reason + operator warning are the signal for manual repair.
    """
    from shared.schemas.domains.dimensions_measures import (
        LEGACY_DEFAULT_AGG_SYNONYMS,
        VALID_DEFAULT_AGGS,
        VALID_SEMI_ADDITIVE_BEHAVIORS,
    )

    # #10: ``by_account`` was removed from VALID_SEMI_ADDITIVE_BEHAVIORS. It is
    # not in the legacy-coerce map either, so a persisted/imported by_account
    # measure falls through to the disabled-measure path below (NULLed behaviour
    # + is_invalid + reason) — the same tolerant mechanism used for any other
    # unresolvable token. No special-casing is needed and none is added.
    behavior = row.get("semi_additive_behavior")
    if behavior is not None:
        canonical_beh = str(behavior).strip().lower()
        if canonical_beh in VALID_SEMI_ADDITIVE_BEHAVIORS:
            row["semi_additive_behavior"] = canonical_beh
        else:
            coerced_beh = _LEGACY_SEMI_ADDITIVE_COERCE.get(canonical_beh)
            if coerced_beh is not None:
                logger.warning(
                    "Bug-6613 rehydrate: measure %r legacy semi_additive_behavior "
                    "%r coerced to %r",
                    row.get("name", row.get("id")), behavior, coerced_beh,
                )
                row["semi_additive_behavior"] = coerced_beh
            else:
                # Unresolvable/free-form token: import as a disabled measure so
                # the snapshot still reverts/imports. semi_additive_behavior is
                # NULLABLE, so reset to NULL (fully additive — the mapper's
                # unknown-position fallback) and flag the measure invalid.
                reason = (
                    f"legacy semi_additive_behavior {behavior!r} is not a "
                    f"canonical value; imported as a disabled measure. Set the "
                    f"semi-additive behaviour and re-enable it."
                )
                logger.warning(
                    "Bug-6613 rehydrate: measure %r %s",
                    row.get("name", row.get("id")), reason,
                )
                row["semi_additive_behavior"] = None
                row["is_invalid"] = True
                if not row.get("invalid_reason"):
                    row["invalid_reason"] = reason

    default_agg = row.get("default_agg")
    if default_agg is None:
        return
    canonical = str(default_agg).strip().lower()
    if canonical in VALID_DEFAULT_AGGS:
        row["default_agg"] = canonical
        return
    coerced = LEGACY_DEFAULT_AGG_SYNONYMS.get(canonical)
    if coerced is not None:
        logger.warning(
            "Bug-6591 rehydrate: measure %r legacy default_agg %r coerced to %r",
            row.get("name", row.get("id")), default_agg, coerced,
        )
        row["default_agg"] = coerced
        return
    # Unresolvable legacy/unknown token: import as a disabled measure so the
    # snapshot still reverts/imports. default_agg is NOT NULL (column default
    # "sum"), so reset it to "sum" and flag the measure invalid + record a
    # reason. This mirrors EXACTLY what the live importers already persist for
    # an unrepresentable aggregate (atscale_mapper._resolve_calc_method returns
    # ("sum", reason) with is_invalid=True), so the rehydrate boundary and the
    # mapper boundary agree. NOTE: is_invalid does NOT make the query path skip
    # the measure — the router still source-renders an invalid measure (skipping
    # only the aggregate matcher) and returns a query_fallback alert. So a user
    # who queries this measure before an operator re-enables it sees a SUM under
    # a query_fallback signal, not an error. The invalid flag + reason + operator
    # warning are the signal that the measure needs manual attention; changing
    # the router to refuse invalid measures is a separate, cross-cutting concern.
    reason = (
        f"legacy default_agg {default_agg!r} is not a canonical aggregate; "
        f"imported as a disabled measure. Set the aggregate and re-enable it."
    )
    logger.warning(
        "Bug-6591 rehydrate: measure %r %s",
        row.get("name", row.get("id")), reason,
    )
    row["default_agg"] = "sum"
    row["is_invalid"] = True
    if not row.get("invalid_reason"):
        row["invalid_reason"] = reason


def _coerce_measure_additivity(row: dict[str, Any]) -> None:
    """Bug-8257: derive ``is_additive`` on the rehydrate/import boundary.

    Called AFTER ``_validate_measure_enums`` so ``default_agg`` is already
    canonicalised. The rehydrator inserts measure rows directly, so the
    ``MeasureCreate`` coercion never runs here — a saved version, an exported
    bundle, or an ecosystem import (dbt / Cube / AtScale, all of which funnel
    through this rehydrator) would otherwise restore the untrustworthy
    ``is_additive=True`` default onto avg/min/max/count_distinct/quantile
    measures, time-variants and calculated measures.

    Uses the same shared precedence as every other producer, so a revert can
    never reintroduce a flag state the create path would have refused. A
    declared ``False`` is preserved; only an unprovable ``True`` is corrected.
    """
    from shared.schemas.domains.dimensions_measures import derive_is_additive

    declared = row.get("is_additive")
    row["is_additive"] = derive_is_additive(
        default_agg=row.get("default_agg"),
        measure_type=row.get("measure_type") or "standard",
        variant_kind=row.get("variant_kind"),
        semi_additive_behavior=row.get("semi_additive_behavior"),
        declared=None if declared is None else bool(declared),
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
            # Snapshot-only catalogue projection; see _insert_dimensions.
            row.pop("effective_description", None)
            _validate_measure_enums(row)
            _coerce_measure_additivity(row)
            await db.execute(insert(Measure).values(**row))
            inserted_ids.add(str(m["id"]))
        pending = [m for m in pending if str(m.get("id")) not in inserted_ids]


async def _upsert_definition_rows(
    model: Any,
    rows: list[dict[str, Any]],
    model_id: UUID,
    db: AsyncSession,
    *,
    self_fk_definition_cols: tuple[str, ...] = (),
    self_fk_governance_cols: tuple[str, ...] = (),
    governance_cols: tuple[str, ...] = (),
    clamp_kind: str | None = None,
    restore_governance: bool = True,
) -> None:
    """In-place UPSERT of a definition parent (NamedSet / KPI) — Bug-7982 Codex
    re-gate residual 3.

    The parent is NEVER truncate-deleted, so its ``ondelete=CASCADE`` children
    (version history, usage telemetry, KPI snapshot history + the KPILatest
    ``$KPIs`` cache) are never cascaded away and then reinserted — which closes the
    lost-update race where a concurrent, lock-less child writer (evaluate-batch,
    scheduler sweep, usage report) commits a value between the old capture and the
    reinsert. Instead:

    - live parents ABSENT from the snapshot are DELETEd (their children correctly
      cascade — the parent is gone);
    - a snapshot parent that exists live is UPDATEd in place (on a revert only its
      DEFINITION columns, so live GOVERNANCE is preserved by omission);
    - a snapshot parent with no live row is INSERTed.

    Self-FKs are wired in a SECOND pass so valid cyclic/self graphs survive
    (the Codex-verified two-pass logic). A DEFINITION self-FK (``parent_kpi_id``)
    is always restored from the snapshot; a GOVERNANCE self-FK (``replacement_id``)
    is restored only on import or for a newly-inserted row — a revert leaves a
    surviving row's live replacement pointer untouched.
    """
    live_ids = {
        r[0] for r in (
            await db.execute(select(model.id).where(model.model_id == model_id))
        ).all()
    }
    all_self_fks = (*self_fk_definition_cols, *self_fk_governance_cols)

    def _strip_row(r: dict[str, Any]) -> dict[str, Any]:
        row = _strip_pk_and_uuids(r)
        row["model_id"] = model_id
        if clamp_kind is not None:
            _clamp_certification_status(row, object_kind=clamp_kind, object_id=r.get("id"))
        for c in all_self_fks:
            row.pop(c, None)
        return row

    snap_by_id: dict[UUID, dict[str, Any]] = {}
    for r in rows:
        rid = _coerce_uuid(r.get("id"))
        if rid is None:
            # An id-less snapshot row cannot be matched; insert it fresh.
            await db.execute(insert(model).values(**_strip_row(r)))
            continue
        snap_by_id[rid] = r
    valid_ids = set(snap_by_id)

    absent = live_ids - valid_ids
    if absent:
        await db.execute(delete(model).where(model.id.in_(absent)))

    # Columns to write on an UPDATE. opus5 R3 finding 1: build the UPDATE from the
    # FULL definition column set — not the None-stripped insert dict — so a column
    # that is NULL in the reverted-to snapshot is set back to NULL. (Under the old
    # delete+reinsert the row was recreated, so a v2-added field naturally
    # vanished on revert; the in-place UPDATE must clear it explicitly or the
    # reverted KPI/named set keeps the newer field — a wrong number / stale
    # governed value on $KPIs.)
    # Server-managed timestamps are excluded from snapshots and must never be
    # forced to NULL by the full-column UPDATE.
    _SERVER_MANAGED = {"created_at", "updated_at"}
    _model_cols = {c.name for c in model.__table__.columns}
    _update_cols = [
        c for c in _model_cols
        if c not in ("id", "model_id")
        and c not in _SERVER_MANAGED
        and c not in all_self_fks  # self-FKs are wired in pass 2
        and not (not restore_governance and c in governance_cols)  # preserve live gov
    ]

    _cols_by_name = {c.name: c for c in model.__table__.columns}

    # Pass 1: update existing / insert new (self-FKs detached).
    for rid, r in snap_by_id.items():
        row = _strip_row(r)
        if rid in live_ids:
            # Build the UPDATE across every definition column so a value that is
            # NULL in the reverted-to snapshot is restored to NULL (finding 1).
            # A column PRESENT in the (None-stripped) row is written; a column
            # ABSENT is set NULL only if it is nullable — a NOT-NULL column absent
            # from the snapshot is left as-is (a complete serialiser snapshot
            # always carries it; skipping avoids a spurious NOT-NULL violation on
            # a partial/hand-built snapshot).
            values: dict[str, Any] = {}
            for col in _update_cols:
                if col in row:
                    values[col] = row[col]
                elif _cols_by_name[col].nullable:
                    values[col] = None
            if values:
                await db.execute(update(model).where(model.id == rid).values(**values))
        else:
            await db.execute(insert(model).values(**row))

    # Pass 2: wire self-FKs (cyclic-safe).
    for rid, r in snap_by_id.items():
        was_new = rid not in live_ids
        values: dict[str, Any] = {}

        def _resolve(col: str) -> Any:
            ref = _coerce_uuid(r.get(col))
            if ref is not None and ref not in valid_ids:
                logger.warning(
                    "%s %s: %s references a row absent from the snapshot "
                    "(dangling/cross-model); detaching.", model.__name__, rid, col,
                )
                return None
            return ref

        for col in self_fk_definition_cols:
            ref = _resolve(col)
            # A surviving row must have its DEFINITION self-FK restored even to
            # NULL (a revert can drop a parent link). A freshly-inserted row was
            # already created with the column detached (NULL), so only wire a
            # real link — skip a redundant None UPDATE.
            if ref is not None or rid in live_ids:
                values[col] = ref
        if restore_governance or was_new:
            for col in self_fk_governance_cols:
                ref = _resolve(col)
                # Governance self-FK: only wire a real link. A None was already
                # left NULL by the insert; a surviving revert row keeps its live
                # value (this branch is skipped on a revert of an existing row).
                if ref is not None:
                    values[col] = ref
        if values:
            await db.execute(update(model).where(model.id == rid).values(**values))


async def _insert_named_sets(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    restore_governance: bool = True,
) -> None:
    await _upsert_definition_rows(
        NamedSet, list(snap.get("named_sets", []) or []), model_id, db,
        self_fk_governance_cols=("replacement_id",),
        governance_cols=_NAMED_SET_GOVERNANCE_FIELDS,
        clamp_kind="named_set", restore_governance=restore_governance,
    )


async def _insert_kpis(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    restore_governance: bool = True,
) -> None:
    # Bug-7850/Bug-7982: KPI has TWO self-FKs (parent_kpi_id = DEFINITION
    # composition; replacement_id = GOVERNANCE). In-place upsert keeps KPI
    # identity so KPIVersion/Usage/Snapshot/Latest children are never cascaded;
    # the two-pass wiring preserves valid cyclic/self graphs.
    #
    # Bug-8950: a KPI carries FOUR non-self FKs into tenant-schema-wide tables —
    # ``time_dimension_id`` -> dimensions.id and ``value_measure_id`` /
    # ``goal_measure_id`` / ``target_measure_id`` -> measures.id. All four are
    # nullable, all four are guarded on the CRUD write path (api/kpis.py refuses
    # "does not belong to this model"), and all four travel verbatim through a
    # hand-edited or cross-project bundle, where the DB FK constraint is
    # satisfied by ANY tenant row and so catches nothing. Only THIS snapshot's
    # dimensions/measures are (re)inserted, so a reference outside those sets is
    # dangling: drop it to NULL before the upsert, mirroring how
    # ``revert_kpi_version`` and the self-FK ``_resolve`` in
    # ``_upsert_definition_rows`` already detach dangling references.
    # _upsert_definition_rows clears a nullable column absent from the
    # (None-stripped) row, so a None here is applied on both INSERT and the
    # in-place UPDATE path.
    #
    # Enumerate every FK, never just the one a bug was reported against: the
    # three measure FKs are the SAME defect on the SAME rows as the dimension
    # FK, and none of them is inert. ``target_measure_id`` drives KPI target
    # evaluation (model-service ``kpi_evaluator.py``), and the "legacy"
    # value/goal pair are still walked as real references by dependency
    # resolution (``dependencies/loader.py``), the lineage graph
    # (``api/lineage_derive.py``) and governance export — so a foreign one
    # pulls another model's measure into this model's dependency and lineage
    # answers.
    valid_dim_ids = {
        str(d["id"]) for d in (snap.get("dimensions") or []) if d.get("id")
    }
    valid_measure_ids = {
        str(m["id"]) for m in (snap.get("measures") or []) if m.get("id")
    }
    foreign_fk_specs: tuple[tuple[str, str, set[str]], ...] = (
        ("time_dimension_id", "dimensions", valid_dim_ids),
        ("value_measure_id", "measures", valid_measure_ids),
        ("goal_measure_id", "measures", valid_measure_ids),
        ("target_measure_id", "measures", valid_measure_ids),
    )
    kpi_rows: list[dict[str, Any]] = []
    for k in (snap.get("kpis", []) or []):
        for field, collection, valid_ids in foreign_fk_specs:
            fk_value = k.get(field)
            if fk_value is not None and str(fk_value) not in valid_ids:
                logger.warning(
                    "Bug-8950: dropping kpis.%s %s on rehydrate of model %s "
                    "(KPI '%s'): absent from the snapshot's %s (cross-model or "
                    "hand-edited bundle).",
                    field, fk_value, model_id,
                    k.get("name", k.get("id", "?")), collection,
                )
                k = {**k, field: None}
        kpi_rows.append(k)
    await _upsert_definition_rows(
        KPI, kpi_rows, model_id, db,
        self_fk_definition_cols=("parent_kpi_id",),
        self_fk_governance_cols=("replacement_id",),
        governance_cols=_KPI_GOVERNANCE_FIELDS,
        clamp_kind="kpi", restore_governance=restore_governance,
    )


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
    safe.

    Bug-7298: names that don't match the expected 3-part shape (e.g.
    optimizer-created bare hex names from ``secrets.token_hex(6)``) MUST NOT
    pass through unchanged — an unmodified name still points at the SOURCE
    model's live aggregate table, so a post-import rebuild would DROP and
    CTAS the source's table (DATA-LOSS).  Such names are now replaced with a
    freshly generated ``<prefix>_<new_seed>_<short_suffix>`` name so they are
    scoped to the destination model and will never collide with the source.
    """
    import secrets as _secrets

    if not new_seed or not isinstance(name, str):
        return name
    parts = name.split("_")
    if len(parts) == 3 and parts[0] == prefix:
        # Standard 3-part shape — rebind the seed.
        return f"{prefix}_{new_seed}_{parts[2]}"
    # Non-standard shape (bare hex, single-part, 4+-part, wrong prefix):
    # generate a destination-namespaced name to prevent source collision.
    suffix = _secrets.token_hex(4)
    return f"{prefix}_{new_seed}_{suffix}"


async def _insert_aggregates(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession,
    force_pending: bool = False,
    reseed: str | None = None,
    reset_incremental_authority: bool = False,
) -> list[tuple[UUID, str]]:
    """Insert aggregate definitions from a snapshot.

    Returns the list of ``(aggregate_id, prior_status)`` for aggregates that
    were FORCED from a healthy status (active/disabled) to ``pending`` by
    ``force_pending`` — i.e. the ones that "disappear" from the active set on
    import (F-013-05) and must be rebuilt. The caller uses this to emit a
    lifecycle event + alert so the transition is observable, not silent.
    """
    _AGG_NESTED = {"columns", "refresh_policy", "quantile_coverage"}
    forced_pending: list[tuple[UUID, str]] = []
    # Bug-8768 (Adjustment 4): count imported policies whose append-only
    # declaration we reset to false so one explanatory alert can be raised.
    _append_only_reset_count = 0
    # Validate every nested policy before the first aggregate statement is
    # emitted, so a malformed authority cannot leave partial import writes in
    # the caller's transaction even when a bundle contains multiple aggregates.
    for aggregate in snap.get("aggregates", []):
        policy = aggregate.get("refresh_policy")
        if policy:
            _validate_imported_append_only_policy(policy)

    for a in snap.get("aggregates", []):
        cols = a.get("columns", [])
        policy = a.get("refresh_policy", None)
        qc_rows = a.get("quantile_coverage", [])
        a_flat = {k: v for k, v in a.items() if k not in _AGG_NESTED}
        row = _strip_pk_and_uuids(a_flat)
        row["model_id"] = model_id
        row.pop("retired_at", None)
        # Derived-grain (Bug-7359, §5.3, I8): a rehydrated definition has no
        # physical run, so any active-run trust pointer MUST be cleared — the
        # manifest may travel as descriptive metadata but cannot be trusted until
        # a rebuild re-earns it. Defense-in-depth (the serialiser already excludes
        # this); a hand-edited/older bundle could still carry it.
        row["active_refresh_run_id"] = None
        # F-013-02 (Bug-8250): a rehydrated aggregate has no physical build for the
        # current deployed version, so its artifact-to-version binding MUST be
        # cleared — it re-earns the binding on its first rebuild. Defense in depth
        # (the serialiser already excludes these); a hand-edited/older bundle could
        # carry them. NULL built_for fails the matcher gate closed.
        row["built_for_version_id"] = None
        row["built_for_epoch"] = None
        # Bug-8481: never trust a bundle-supplied physical storage identity. The
        # imported aggregate has no built table and re-earns this binding only
        # when its first target build completes safely.
        row["built_for_storage_binding"] = None
        # Bug-8602: likewise never trust a bundle-supplied SOURCE identity. The
        # imported aggregate holds no rows read from that database and re-earns
        # this binding only when its first build completes safely.
        row["built_for_source_binding"] = None
        # Bug-7903 (Fable R2 #5): refresh_prior_status is LIVE refresh state the
        # rehydrator OWNS — never trust a bundle-supplied value. Strip any inbound
        # key first (defence against a tampered/hand-edited bundle), then compute
        # the EFFECTIVE settled status: a bundle exported mid-refresh by a pre-fix
        # build can carry the transient status "pending"/"invalid" while its true
        # settled status lives in the (now-stripped) refresh_prior_status; recover
        # it so an export→import chain never launders a "disabled" aggregate into
        # "active" (nor a mid-refresh "active" into a stuck pending with no prior).
        _inbound_prior = str(a.get("refresh_prior_status") or "")
        row.pop("refresh_prior_status", None)
        _raw_status = str(a.get("status") or "")
        if _raw_status in ("pending", "invalid") and _inbound_prior in ("active", "disabled"):
            prior_status = _inbound_prior
        else:
            prior_status = _raw_status
        if force_pending:
            # A DISABLED aggregate is NON-SERVING with or without a physical table,
            # so keep it DISABLED rather than forcing it to pending. Forcing it to
            # pending would (a) leave it stuck "pending" forever when its imported
            # policy is disabled (never swept) — a UI lie — and (b) risk an
            # export→import chain laundering it to active. When the user later
            # re-enables it, the enable path marks it stale and schedules its first
            # build. Every OTHER status is forced to pending as before (F-013-05):
            # the materialised table does not exist post-import, so the aggregate
            # must not serve until a rebuild. An ACTIVE aggregate also records a
            # durable prior snapshot so the sweep restores it to ACTIVE after the
            # rebuild.
            if prior_status == "disabled":
                row["status"] = "disabled"
            else:
                row["status"] = "pending"
                if prior_status == "active":
                    row["refresh_prior_status"] = "active"
                    agg_id = row.get("id")
                    if isinstance(agg_id, UUID):
                        forced_pending.append((agg_id, prior_status))
        else:
            # Not force_pending: still recover the effective settled status so a
            # mid-refresh bundle does not import as a transient "pending"/"invalid".
            row["status"] = prior_status or _raw_status
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
            # Older bundles predate the append-only declaration and periodic
            # correction cadence. Missing declarations must fail closed.
            p_row.setdefault("incremental_append_only", False)
            p_row.setdefault("full_rebuild_interval_days", None)
            # Bug-8768 (Adjustment 4): ``incremental_append_only=true`` is an
            # assertion about how one specific data SOURCE behaves — existing
            # rows never change, no late rows beyond the lookback. A bundle
            # imported or cloned into another model/tenant/environment may point
            # at data with different update and deletion behaviour, so carrying
            # the assertion across would silently authorise a windowed
            # DELETE+INSERT against an unvetted source (a wrong number until a
            # full rebuild). On import/clone (RehydrationMode.IMPORT) the
            # declaration is therefore RESET to false regardless of the bundle
            # value; the target re-enables it deliberately after confirming the
            # source is append-only. A same-model version restore/revert
            # (RehydrationMode.RESTORE) points at the SAME source and preserves it.
            #
            # B8768-R1-05: keyed on ``reset_incremental_authority`` (derived from
            # the rehydration MODE by the caller), NOT on ``force_pending``. A
            # source-authority reset must not depend on a physical-rebind flag
            # that an import caller could omit — the demo re-seed import did
            # exactly that and transferred a true declaration onto a new source.
            if reset_incremental_authority and p_row.get("incremental_append_only"):
                p_row["incremental_append_only"] = False
                _append_only_reset_count += 1
            await db.execute(insert(AggregateRefreshPolicy).values(**p_row))
        # Bug-7852 / Bug-6969: rehydrate QuantileCoverage rows so the
        # consumer's proof gate can find coverage after import/restore.
        # Fail-closed-by-default: force exactness='unknown' on every
        # rehydrated row so an old/imported snapshot carrying exactness=
        # 'exact' cannot re-enable exact serving (wrong pNN risk until
        # Bug-7901 guarantees physical/coverage atomicity).
        for qc in qc_rows:
            qc_row = _strip_pk_and_uuids(qc)
            qc_row["exactness"] = "unknown"
            await db.execute(insert(QuantileCoverage).values(**qc_row))
    # Bug-8768 (Adjustment 4): if any imported append-only declaration was reset,
    # tell the operator so the disabling is visible, not silent. The scheduler
    # will full-rebuild these aggregates until the declaration is re-enabled from
    # the aggregate's refresh policy against the confirmed target source.
    if _append_only_reset_count:
        _now = datetime.now(timezone.utc)
        db.add(
            ModelAlert(
                model_id=model_id,
                severity="info",
                category="aggregate_lifecycle",
                title=(
                    f"Incremental (append-only) refresh disabled on "
                    f"{_append_only_reset_count} aggregate(s) after import"
                ),
                detail=(
                    f"{_append_only_reset_count} imported aggregate(s) declared "
                    "their source append-only for 'Only new rows' incremental "
                    "refresh. That declaration describes one specific data "
                    "source; the source this model now points at may update or "
                    "delete existing rows. Incremental refresh has been disabled "
                    "pending review — the Scheduler runs a full rebuild for these "
                    "aggregates until you re-enable 'Only new rows' from the "
                    "aggregate's refresh policy after confirming this source is "
                    "append-only."
                ),
                related_object_type="aggregate_import",
                # Fresh id per import so the partial-unique dedup index never
                # collides across repeated imports/reseeds (same rule as the
                # forced-pending import alert above).
                related_object_id=uuid.uuid4(),
                first_seen_at=_now,
                last_seen_at=_now,
            )
        )
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


def _restricted_tag_ids_for_persona(
    snap: dict[str, Any], persona_id: Any,
) -> list[Any]:
    """Bug-9268: CLS tag restrictions live in a sibling snapshot array.

    ``_validate_imported_persona`` must see them the same way REST CRUD
    passes ``restricted_tag_ids``, or a tag-only empty-audience persona
    imports and the restriction never applies.
    """
    if persona_id is None or persona_id == "":
        return []
    want = str(persona_id)
    out: list[Any] = []
    for r in snap.get("persona_tag_restrictions") or []:
        if str(r.get("persona_id") or "") != want:
            continue
        tid = r.get("data_tag_id")
        if tid is not None and tid != "":
            out.append(tid)
    return out


async def _insert_personas(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    for p in snap.get("personas", []) or []:
        row = _strip_pk_and_uuids(p)
        row["model_id"] = model_id
        # Bug-6291: validate persona slugs at the insertion chokepoint
        # so that YAML, JSON bundle, and project import paths all
        # inherit the BI-safe contract.  Hyphenated persona slugs break
        # JDBC/XMLA catalogue names (model_slug + persona_slug).
        # Always validate, including empty/missing slugs.
        validate_bi_safe_slug(
            row.get("slug") or "", label="Persona slug"
        )
        await _validate_imported_persona(
            db, model_id, row,
            restricted_tag_ids=_restricted_tag_ids_for_persona(
                snap, p.get("id") or row.get("id"),
            ),
        )
        await db.execute(insert(Persona).values(**row))


async def _validate_imported_persona(
    db: AsyncSession, model_id: UUID, row: dict[str, Any],
    *,
    restricted_tag_ids=None,
) -> None:
    """Bug-9266 / Bug-9268: YAML/snapshot import must run the same
    audience-narrowing and scope checks as REST create/update, including
    CLS tag restrictions. Filter-only empty everything (no allow-lists,
    no default filters, no tag restrictions) with empty audience still
    passes.
    """
    label = row.get("slug") or row.get("name") or "<unnamed persona>"
    try:
        reject_empty_audience_narrowing(
            row.get("audience_roles") or [],
            included_measure_ids=row.get("included_measure_ids"),
            included_dimension_ids=row.get("included_dimension_ids"),
            included_hierarchy_ids=row.get("included_hierarchy_ids"),
            default_filters=row.get("default_filters"),
            restricted_tag_ids=restricted_tag_ids or [],
        )
    except PersonaAudienceNarrowingError as exc:
        raise SnapshotSchemaError(f"Persona {label!r}: {exc}") from exc

    default_filters = row.get("default_filters") or {}
    if default_filters:
        if not isinstance(default_filters, dict):
            raise SnapshotSchemaError(
                f"Persona {label!r}: default_filters must be a mapping"
            )
        bad_vals = [
            k for k, v in default_filters.items()
            if not persona_filter_value_is_valid(v)
        ]
        if bad_vals:
            raise SnapshotSchemaError(
                f"Persona {label!r}: invalid default-filter operators or "
                f"shapes for: {', '.join(str(k) for k in bad_vals)}"
            )
        dim_names = set(
            (
                await db.execute(
                    select(Dimension.name).where(Dimension.model_id == model_id)
                )
            ).scalars().all()
        )
        bad_keys = [k for k in default_filters if k not in dim_names]
        if bad_keys:
            raise SnapshotSchemaError(
                f"Persona {label!r}: these default-filter keys are not "
                f"dimensions on this model: {', '.join(str(k) for k in bad_keys)}"
            )

    for ids, table, kind in (
        (row.get("included_measure_ids"), Measure, "measure"),
        (row.get("included_dimension_ids"), Dimension, "dimension"),
        (row.get("included_hierarchy_ids"), HierarchyDefinition, "hierarchy"),
    ):
        wanted = [_coerce_uuid(i) for i in (ids or [])]
        wanted = [u for u in wanted if u is not None]
        if not wanted:
            continue
        found = {
            r for r in (
                await db.execute(
                    select(table.id).where(
                        table.model_id == model_id, table.id.in_(wanted)
                    )
                )
            ).scalars().all()
        }
        missing = [str(u) for u in wanted if u not in found]
        if missing:
            raise SnapshotSchemaError(
                f"Persona {label!r}: these {kind} ids do not belong to this "
                f"model: {', '.join(missing)}"
            )


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
        # Derived-grain (Bug-7359, §5.3, I8): clear the live active-run pointer on
        # rehydrate — a rehydrated pocket has no physical run and must re-earn
        # trust via a refresh. A travelled row_manifest is security-load-bearing
        # at serve time (Bug-8018/Bug-8393) but is UNTRUSTED without this pointer:
        # the query-router requires manifest.build_refresh_run_id ==
        # active_refresh_run_id, so clearing it here is what stops an imported
        # manifest from admitting a pocket under row security. Do not preserve it.
        row["active_refresh_run_id"] = None
        # F-013-03 (Bug-8250): a rehydrated pocket has no build for the current
        # version, so clear its artifact-to-version binding (defense in depth; the
        # serialiser excludes it). NULL built_for fails the matcher gate closed.
        row["built_for_version_id"] = None
        row["built_for_epoch"] = None
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


async def _schedule_removed_named_query_cleanup(
    model_id: UUID,
    snapshot: dict[str, Any],
    db: AsyncSession,
    *,
    requested_by: str,
) -> None:
    """F-013-02: schedule a DROP for NQ artifacts whose Named Query is absent
    from the reverted-to snapshot.

    Called on RESTORE (revert) BEFORE the truncate deletes the live NQ rows, so
    a Named Query removed by reverting to an older version does not leak its
    materialised target table. A never-refreshed artifact (no physical name) or
    a cross-project/missing target resolves to no cleanup identity and is skipped
    fail-closed by ``schedule_model_physical_cleanup``.
    """
    snapshot_nq_ids = {
        str(nq.get("id"))
        for nq in (snapshot.get("named_queries") or [])
        if nq.get("id") is not None
    }
    live = (
        await db.execute(
            select(NamedQueryArtifact, NamedQuery.id)
            .join(NamedQuery, NamedQueryArtifact.named_query_id == NamedQuery.id)
            .where(NamedQuery.model_id == model_id)
        )
    ).all()
    removed = [art for (art, nq_id) in live if str(nq_id) not in snapshot_nq_ids]
    if not removed:
        return
    from shared.physical_cleanup import schedule_model_physical_cleanup

    await schedule_model_physical_cleanup(
        db,
        model_id=model_id,
        aggregate_definitions=(),
        pocket_definitions=(),
        named_query_artifacts=removed,
        requested_by=requested_by,
    )


async def _insert_named_queries(
    model_id: UUID,
    snap: dict[str, Any],
    db: AsyncSession,
    *,
    mode: "RehydrationMode" = RehydrationMode.IMPORT,
    reseed: str | None = None,
) -> None:
    """Rehydrate Named Query definitions, artifacts and refresh policies.

    The DEFINITION + refresh policy travel (governed model content). The
    artifact is re-created with ``status='stale'`` and its row manifest and
    liveness pointer CLEARED — a rehydrated artifact has no physical table on
    this database and must re-earn trust via a refresh. A travelled manifest
    is security-load-bearing at serve time, and clearing the pointer (the
    query-router requires ``manifest.build_refresh_run_id ==
    active_refresh_run_id``) is what stops an imported manifest from ever
    admitting a rehydrated artifact under row security. The version binding
    is cleared too (NULL built_for fails the version gate closed). On IMPORT
    the physical table name is always rebound to a destination-scoped identity
    before the first refresh; RESTORE preserves the historical identity
    because it belongs to this model.

    F-013-02: identity handling depends on ``mode``.

    * ``RESTORE`` (revert of the SAME model): PRESERVE the Named Query and
      artifact IDs from the snapshot — they ARE this model's own historical
      ids. Minting fresh UUIDs here churned every NQ id on revert, breaking
      saved views / health chips / any stored reference. ``_strip_pk_and_uuids``
      already keeps the snapshot ``id``, so simply not overriding it preserves
      identity, exactly as measures/dimensions/named-sets already do on revert.
    * ``IMPORT`` (clone / cross-model import): MINT fresh ids. The importer
      rewrites nested Named Query ids for normal bundles, while this boundary
      remains defensive for catalog/YAML/direct rehydration callers that hand
      us a raw snapshot; source rows may still hold those ids in the same
      tenant (a collision).
    """
    preserve_identity = mode == RehydrationMode.RESTORE
    _NQ_NESTED = {"artifact", "refresh_policy"}
    for nq in snap.get("named_queries", []) or []:
        artifact = nq.get("artifact")
        policy = nq.get("refresh_policy")
        nq_flat = {k: v for k, v in nq.items() if k not in _NQ_NESTED}
        row = _strip_pk_and_uuids(nq_flat)
        row["model_id"] = model_id
        if preserve_identity and row.get("id") is not None:
            nq_id = row["id"]  # own historical id (kept by _strip_pk_and_uuids)
        else:
            nq_id = uuid.uuid4()
            row["id"] = nq_id
        await db.execute(insert(NamedQuery).values(**row))
        if isinstance(artifact, dict):
            art_row = _strip_pk_and_uuids(artifact)
            if not (preserve_identity and art_row.get("id") is not None):
                art_row["id"] = uuid.uuid4()
            # F-020-01 / Bug-9222 (CRITICAL): on IMPORT (clone / cross-model), the artifact
            # still carries the SOURCE model's physical table name
            # (nq_<srcseed>_<sfx>). A same-tenant clone shares the tenant
            # aggregates schema, so the first refresh of the clone's NQ would
            # DROP/CTAS the SOURCE model's physical table — data loss on a
            # production path. Rebind the seed to the destination model, exactly
            # as aggregates/pockets already do. On RESTORE (revert of the same
            # model) the name is preserved — it IS this model's own table.
            if mode == RehydrationMode.IMPORT:
                # A direct rehydrator caller may omit ``reseed``. Passing the
                # source name through in that case is still a data-loss path:
                # the first refresh would DROP/CTAS the source table. Use a
                # destination-only fallback so every IMPORT funnel is isolated
                # even when it bypasses ``prepare_snapshot_for_import``.
                import_seed = reseed or secrets.token_hex(6)
                art_row["physical_table_name"] = _reseed_physical_table_name(
                    art_row.get("physical_table_name"), "nq", import_seed,
                )
            art_row["named_query_id"] = nq_id
            art_row["status"] = "stale"
            art_row["row_manifest"] = None
            art_row["active_refresh_run_id"] = None
            art_row["built_for_version_id"] = None
            art_row["built_for_epoch"] = None
            art_row["row_count"] = None
            art_row["last_refresh_at"] = None
            art_row["retired_at"] = None
            art_row["failure_reason"] = None
            await db.execute(insert(NamedQueryArtifact).values(**art_row))
        if isinstance(policy, dict):
            pol_row = _strip_pk_and_uuids(policy)
            # Policy rows are model-owned identity too. ``_collect_pks`` now
            # rewrites them for normal import bundles, but mint here as a
            # second boundary guarantee for catalog/YAML/direct rehydration
            # callers that hand us a raw snapshot (Bug-9222).
            if mode == RehydrationMode.IMPORT or pol_row.get("id") is None:
                pol_row["id"] = uuid.uuid4()
            pol_row["named_query_id"] = nq_id
            await db.execute(insert(NamedQueryRefreshPolicy).values(**pol_row))


# Consolidated onto the single canonical definition in
# ``shared.security.row_security_audit`` (Bug-6034 follow-up: valid-source
# consolidation). This alias is the SAME frozenset object the CRUD schema and
# the backfill normaliser use, so the three guards can no longer drift.
_ROW_SECURITY_VALID_SOURCES = ROW_SECURITY_VALID_SOURCES


def _validate_row_security_rule_on_import(
    row: dict[str, Any], *, rule_label: str = "?",
) -> list[str]:
    """Bug-6033: validate a row-security rule dict before insertion.

    Mirrors the Bug-5904 / Bug-5905 CRUD-level shape-consistency guards
    that the snapshot rehydrator previously bypassed.  Invalid rules are
    auto-corrected **in place** (disabled or normalised) so the import
    succeeds without silently persisting an inert security rule.

    Returns a (possibly empty) list of human-readable warnings.
    """
    warnings: list[str] = []
    attr_source = row.get("attribute_source", "jwt_role")
    claim_name = row.get("attribute_claim_name")
    rule_type = row.get("rule_type", "")

    # Bug-6033: guard against non-string claim names from corrupt or
    # hand-edited snapshots. The Pydantic schema guarantees Optional[str]
    # on the CRUD path, but raw snapshot dicts have no type guarantee.
    # Clear the junk value but do NOT disable here: attribute_claim_name is
    # only consumed at runtime for saml_claim/oidc_scope role predicates,
    # and for those the blank-claim check below disables the rule after the
    # clear. For every other shape (jwt_role/idp_group predicates,
    # user_mapping) the field is unused and the rule stays functional —
    # disabling it would silently REMOVE a working security restriction
    # on import (fail-open).
    if claim_name is not None and not isinstance(claim_name, str):
        warnings.append(
            f"rule {rule_label!r}: attribute_claim_name has non-string "
            f"type {type(claim_name).__name__}; cleared"
        )
        row["attribute_claim_name"] = None
        claim_name = None

    # Bug-5904 hardening: trim claim name on import so padded values
    # (e.g. "department ") do not silently fail the exact-key lookup in
    # predicate_compiler._resolve_principal_attribute at query time.
    if isinstance(claim_name, str):
        trimmed = claim_name.strip()
        if trimmed != claim_name:
            row["attribute_claim_name"] = trimmed
            claim_name = trimmed

    # 1. Unknown attribute_source -- normalise to the safe default and
    #    disable so it does not silently fail to match at runtime.
    if attr_source not in _ROW_SECURITY_VALID_SOURCES:
        warnings.append(
            f"rule {rule_label!r}: invalid attribute_source={attr_source!r}; "
            f"normalised to 'jwt_role' and disabled"
        )
        row["attribute_source"] = "jwt_role"
        row["is_enabled"] = False
        return warnings  # further checks are moot after normalisation

    if rule_type == "role_predicate":
        # 2. Bug-5904: claim/scope-sourced rules MUST have a non-empty
        #    claim name or they will never resolve a subject at match time.
        if attr_source in ("saml_claim", "oidc_scope") and not (
            claim_name or ""
        ).strip():
            warnings.append(
                f"rule {rule_label!r}: attribute_source={attr_source!r} but "
                f"attribute_claim_name is blank; disabled on import "
                f"(claim-sourced rule without a claim name never matches)"
            )
            row["is_enabled"] = False

    elif rule_type == "user_mapping":
        # 3. Bug-5905: user_mapping always keys by user_identity --
        #    attribute_source / attribute_claim_name are not consumed.
        if attr_source != "jwt_role":
            warnings.append(
                f"rule {rule_label!r}: user_mapping has "
                f"attribute_source={attr_source!r}; normalised to 'jwt_role'"
            )
            row["attribute_source"] = "jwt_role"
        if claim_name is not None:
            warnings.append(
                f"rule {rule_label!r}: user_mapping has "
                f"attribute_claim_name set; cleared"
            )
            row["attribute_claim_name"] = None

    return warnings


async def _insert_row_security(
    model_id: UUID, snap: dict[str, Any], db: AsyncSession
) -> None:
    _logger = logging.getLogger(__name__)
    for r in snap.get("row_security_rules", []) or []:
        row = _strip_pk_and_uuids(r)
        row["model_id"] = model_id

        # Bug-6033: validate claim-sourced rules on import, mirroring the
        # Bug-5904/5905 CRUD-level guards that the rehydrator bypassed.
        rule_label = row.get("name", row.get("id", "?"))
        for w in _validate_row_security_rule_on_import(
            row, rule_label=str(rule_label)
        ):
            _logger.warning("Bug-6033 import validation: %s", w)

        # Bug-6132: compile-validate role_predicate DSL at seed/import time.
        # The API validates at save time (F-007-04), but the rehydrator
        # bypassed it, allowing unsupported DSL (e.g. dimension_in) to be
        # silently inserted as inert rules that hard-block every matched
        # caller.  Fail loud here so bad DSL never reaches the database.
        # Empty/None predicates are also rejected: a role_predicate with no
        # expression will crash at runtime (_compile_dsl_expression raises
        # on empty strings), so catch it at import rather than at query time.
        if row.get("rule_type") == "role_predicate":
            pred_expr = row.get("predicate_expression") or ""
            if not pred_expr.strip():
                raise SnapshotSchemaError(
                    f"row-security rule {rule_label!r} is a role_predicate "
                    f"but has an empty predicate_expression. Every "
                    f"role_predicate rule must have a valid DSL expression."
                )
            try:
                _compile_dsl_expression(pred_expr)
            except RowSecurityCompileError as exc:
                raise SnapshotSchemaError(
                    f"row-security rule {rule_label!r} has an "
                    f"uncompilable predicate expression: {exc}. "
                    f"Use the restricted DSL (dimension_equals, in, "
                    f"and, or, not) with single-quoted string values."
                ) from exc

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
    # Bug-8950: ``(target_type, target_id)`` is a polymorphic tenant-schema-wide
    # FK. ``shared/data_quality/validator.py::_resolve_column_ref`` dereferences
    # target_id with a bare ``db.get(ModelColumn, ...)`` and introspects that
    # column's physical table on a system_admin service token, so a cross-model
    # target_id turns a rehydrated rule into a read of ANOTHER project's source
    # data (the exact exposure the API guard ``ensure_target_in_model`` closes on
    # the write path). target_id is NOT nullable, so a rule whose target is
    # absent from the snapshot has nothing valid to point at and is SKIPPED
    # (mirroring the dangling-hierarchy skip, Bug-6725) rather than inserted with
    # a dangling/foreign pointer. An unrecognised target_type fails closed (skip).
    #
    # The vocabulary is READ from the canonical domain
    # (``shared/schemas/domains/governance_advanced.py::_DQ_TARGET_TYPES``, the
    # same set the CRUD route's ``_DQ_RULE_TARGETS`` is pinned to) rather than
    # restated here: a re-typed copy is exactly how a fourth target type would
    # be accepted by the API and then silently DROPPED by import, with the
    # membership guard reporting a clean skip.
    # A target type the domain declares but this map has no collection for
    # yields an EMPTY valid set, so every rule of that type is skipped with the
    # warning below (fail closed) instead of being inserted unvalidated. The
    # wiring gap itself is pinned by
    # ``model-service/tests/test_body_fk_scope_p3c.py::
    # test_dq_rule_target_map_covers_exactly_the_schemas_vocabulary``.
    valid_by_type: dict[str, set[str]] = {
        ttype: {
            str(row["id"])
            for row in (snap.get(_DQ_TARGET_SNAP_TYPE_KEYS.get(ttype, "")) or [])
            if row.get("id")
        }
        for ttype in _DQ_TARGET_TYPES
    }
    for r in snap.get("data_quality_rules", []) or []:
        ttype = r.get("target_type")
        tid = r.get("target_id")
        valid_ids = valid_by_type.get(ttype or "")
        if valid_ids is None or tid is None or str(tid) not in valid_ids:
            logger.warning(
                "Bug-8950: skipping data_quality_rule '%s' on rehydrate of model "
                "%s: target (%s, %s) is absent from the snapshot (cross-model, "
                "hand-edited bundle, or unknown target_type).",
                r.get("name", r.get("id", "?")), model_id, ttype, tid,
            )
            continue
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

def _rebind_scheduler_llm_configs(
    snapshot: dict[str, Any], llm_id_remap: dict[str, str],
) -> None:
    """Bake the LLM-config remap into ``ai_scheduler_config`` FKs in place.

    A version snapshot is restored by ``revert_to_version`` -> ``rehydrate_into_live``
    WITHOUT an ``llm_id_remap`` (versions.py passes none), so any LLM-config FK
    left at its SOURCE value would dangle (cross-tenant FK violation) or silently
    cross-link to another project's config (same-tenant duplicate). The live
    import path fixes this in ``_insert_ai_scheduler`` using ``llm_id_remap``;
    for the stored version snapshot we must bake the same decision in now:
    remap a known source config to its imported id, else clear the FK (the user
    re-selects a config after restore). Mirrors ``_insert_ai_scheduler`` exactly.
    """
    sched = snapshot.get("ai_scheduler_config")
    if not isinstance(sched, dict):
        return
    for fk in ("llm_config_id", "glossary_llm_config_id"):
        old = sched.get(fk)
        if old is None:
            continue
        old_str = str(old)
        sched[fk] = llm_id_remap.get(old_str)  # remapped id, or None to clear


def _make_version_snapshot_portable(
    snapshot: dict[str, Any],
    *,
    new_model_id: UUID,
    connection_mapping: dict[str, str] | None,
    llm_id_remap: dict[str, str] | None,
    model_slug: str | None,
    shared_pk_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Rewrite an imported version's own ``snapshot_json`` so a later revert can
    rehydrate it in the TARGET tenant (Bug-7623).

    A version snapshot is a full model snapshot carrying SOURCE-tenant identity.
    A revert rehydrates it verbatim (no remaps), so it must be made fully
    target-portable NOW, exactly as the live model shape is on import. This
    routes it through the SAME vetted machinery ``prepare_snapshot_for_import``
    (re-keys every internal PK so it cannot collide with the source model's
    still-live rows in a same-tenant duplicate; rebinds ``project_connection_id``
    via the connection map; strips cross-tenant-only refs — ``model.llm_config_id``
    and ``glossary_entries[].created_by``), then additionally:
      * bakes ``llm_id_remap`` into the scheduler LLM FKs (revert has no remap),
      * stamps the imported model's final ``slug`` (the live path rewrites the
        model slug after ``prepare_snapshot_for_import``; a version snapshot that
        kept the source slug would rename the model — or collide with a sibling
        slug — on revert),
      * drops ``model.seed`` so a revert keeps the DESTINATION model's seed
        (F-013-05): the imported model was given a fresh seed and its
        aggregate/pocket physical names rebound to it, so re-imposing the source
        seed would mint future ``agg_<seed>_*`` names in the source namespace.

    ``shared_pk_map`` (Bug-7623 R2, SECURITY): the SAME map used to re-key the
    live imported model shape, passed so this version re-keys IDENTICALLY. Id
    continuity with the live model is what lets a revert (which preserves live
    governance and aggregates and re-attaches them BY ID) keep CLS tag links
    (else they drop → masked columns become visible — fail-open), RLS rules, KPI
    governance, and preserved aggregates/targets. Ids that appear only in a
    historical version extend the shared map with a fresh, self-consistent id.

    Returns the portable snapshot, or ``None`` when a source/target references a
    connection with no mapping entry: that version cannot be rebound onto a
    target connection, so the caller honest-degrades it to non-restorable rather
    than persist a snapshot whose revert would fail-closed on an unresolved FK.
    """
    from shared.model_snapshot.importer import prepare_snapshot_for_import

    portable, missing = prepare_snapshot_for_import(
        snapshot,
        new_model_id=new_model_id,
        connection_mapping=connection_mapping or {},
        shared_pk_map=shared_pk_map,
    )
    if missing:
        return None
    _rebind_scheduler_llm_configs(portable, llm_id_remap or {})
    if isinstance(portable.get("model"), dict):
        if model_slug:
            portable["model"]["slug"] = model_slug
        # Keep the destination seed on revert (F-013-05): absent seed -> the
        # rehydrator skips the seed scalar and leaves the live value in place.
        portable["model"].pop("seed", None)
    return portable


async def insert_model_versions(
    model_id: UUID,
    versions: list[dict[str, Any]],
    db: AsyncSession,
    *,
    bundle_carries_version_snapshots: bool = False,
    connection_mapping: dict[str, str] | None = None,
    llm_id_remap: dict[str, str] | None = None,
    model_slug: str | None = None,
    shared_pk_map: dict[str, str] | None = None,
) -> dict[str, UUID]:
    """Insert imported ModelVersion history rows, returning {old_id_str: new_id}.

    Two formats (Bug-7623, "correct restore from now on"):

    NEW-format bundle (``bundle_carries_version_snapshots=True``, bundle
    schema_version >= 2): each version carries its OWN ``snapshot_json`` — the
    real shape saved for that version. Persist it (rebound to the target tenant
    via ``_make_version_snapshot_portable``) with ``snapshot_unavailable=False``,
    so a revert to that version reproduces version N's real shape, not today's.
    A version that still cannot be rebound (its connection is gone, or it was
    already a ``snapshot_unavailable`` / ``{}`` placeholder — e.g. history
    imported before this fix) honest-degrades below.

    OLD-format bundle (default, schema_version 1): no per-version snapshot ever
    arrives, so every version honest-degrades. This preserves the H2 (Bug-6295)
    contract, which supersedes the earlier Bug-5354 shortcut: the previous code
    stamped the CURRENT live shape onto EVERY imported version (a silent-wrong
    "restore of version 1 serves today's shape under an old label"). Honest
    degrade never fabricates: the row is persisted with placeholder
    ``snapshot_json = {}`` and ``snapshot_unavailable = True``. Metadata
    (version number, summary, author, timestamp) is preserved so the timeline
    stays intact, but the shape is explicitly marked unrecoverable so:
      * ``revert_to_version`` refuses a snapshot-unavailable version, and
      * deploy-latest skips it, and ``get_version`` surfaces the flag to the UI.
    Serving is unaffected either way: import appends ONE authentic deployed
    version from the live shape (``append_authentic_import_version``).
    """
    # Bug-7982 R7 (review round 5, F1): this helper is part of a WHOLESALE
    # REBUILD of snapshot-owned state into a model created in the caller's own
    # transaction, so it is a DELIBERATE non-holder of the per-model
    # definition lock. The exemption is declared HERE, not at each call site:
    # round 5 found 13 call sites where the caller exempted
    # rehydrate_into_live and then wrote personas/model_versions unexempt two
    # lines later, flooding the runtime write guard's report and muting it for
    # real violations.
    async with model_write_lock_exempt(
        db, "import: version history rebuilt for a model created in this transaction"
    ):
        from shared.db.models import ModelVersion

        conn_map = connection_mapping or {}
        remap: dict[str, UUID] = {}
        for v in versions:
            row = _strip_pk_and_uuids(v)
            # snapshot_json / snapshot_unavailable are decided below, never trusted
            # verbatim from the bundle row.
            row.pop("snapshot_json", None)
            row.pop("snapshot_unavailable", None)
            row["model_id"] = model_id
            old_id = v.get("id", "")
            new_id = uuid.uuid4()
            row["id"] = new_id

            portable: dict[str, Any] | None = None
            raw_snapshot = v.get("snapshot_json")
            already_unavailable = bool(v.get("snapshot_unavailable"))
            # Bug-8032 (sol C1 -> B3): bundle validation is SHALLOW — it checks
            # that a version row carries a snapshot, not that each entity family
            # inside it is a list of records. A history snapshot with, say,
            # ``dimensions: {}`` therefore persisted as genuinely restorable, and
            # ``revert_to_version`` would install it as the DEPLOY POINTER. Every
            # downstream reader then treats the falsey mapping as "no dimensions
            # deployed" rather than "malformed", which is a silent wrong shape on
            # a serving path. Honest-degrade instead: the same answer this
            # function already gives a snapshot it cannot rebind. The timeline row
            # survives; only the claim that it can be restored is withdrawn.
            #
            # Checked on the RAW snapshot, BEFORE the portability rewrite: that
            # rewrite iterates each family expecting record dicts, so a malformed
            # one reaches it as an unhandled AttributeError rather than a decision.
            malformed = (
                malformed_snapshot_families(raw_snapshot, SEMANTIC_SHAPE_FAMILIES)
                if isinstance(raw_snapshot, dict)
                else []
            )
            if malformed:
                logger.warning(
                    "Imported model version %s carries malformed snapshot "
                    "families %s; marking it snapshot-unavailable rather than "
                    "restorable (Bug-8032).",
                    old_id, ", ".join(malformed),
                )
            if (
                bundle_carries_version_snapshots
                and isinstance(raw_snapshot, dict)
                and raw_snapshot
                and not already_unavailable
                and not malformed
            ):
                portable = _make_version_snapshot_portable(
                    raw_snapshot,
                    new_model_id=model_id,
                    connection_mapping=conn_map,
                    llm_id_remap=llm_id_remap or {},
                    model_slug=model_slug,
                    shared_pk_map=shared_pk_map,
                )

            if portable is not None:
                # NEW-format, rebindable: the version is genuinely restorable.
                row["snapshot_json"] = portable
                row["snapshot_unavailable"] = False
            else:
                # OLD-format, placeholder, or non-rebindable: honest-degrade.
                row["snapshot_json"] = {}
                row["snapshot_unavailable"] = True
            await db.execute(insert(ModelVersion).values(**row))
            remap[str(old_id)] = new_id
        return remap


async def append_authentic_import_version(
    model_id: UUID,
    db: AsyncSession,
    *,
    summary: str = "Imported model (current shape)",
    created_by: str = "import",
) -> "UUID":
    """Create ONE authentic, servable ModelVersion from the just-rehydrated LIVE
    model, numbered above any imported history rows, and return its id.

    Bug-6295 root-cause completion: imported history rows are placeholders
    (``snapshot_unavailable=True``, ``snapshot_json={}``) — correct, because
    their historical shapes are unrecoverable. But "deploy latest saved version"
    resolves the HIGHEST ``version_number``; if the highest row is an imported
    placeholder, deploy-latest hits ``{}`` and ``_validate_snapshot_for_deploy``
    rejects it (400/409), leaving the imported model undeployable. The importer
    must therefore append a real, deployable version of the live shape ABOVE the
    imported history (mirroring the single-model importer's ``deploy_immediately``
    v1 and the native Save path), so deploy-latest resolves a valid snapshot and
    the placeholders remain pure, non-servable history beneath it.

    Returns the new version's id so the caller can set it as the deploy pointer.
    """
    # Bug-7982 R7 (review round 5, F1): this helper is part of a WHOLESALE
    # REBUILD of snapshot-owned state into a model created in the caller's own
    # transaction, so it is a DELIBERATE non-holder of the per-model
    # definition lock. The exemption is declared HERE, not at each call site:
    # round 5 found 13 call sites where the caller exempted
    # rehydrate_into_live and then wrote personas/model_versions unexempt two
    # lines later, flooding the runtime write guard's report and muting it for
    # real violations.
    async with model_write_lock_exempt(
        db, "import: authentic-import version for a model created in this transaction"
    ):
        from shared.db.models import ModelVersion
        from shared.model_snapshot.serialiser import snapshot_model

        snap = await snapshot_model(model_id, db)
        last_q = await db.execute(
            select(ModelVersion.version_number)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
            .limit(1)
        )
        next_n = (last_q.scalar_one_or_none() or 0) + 1
        new_id = uuid.uuid4()
        await db.execute(
            insert(ModelVersion).values(
                id=new_id,
                model_id=model_id,
                version_number=next_n,
                snapshot_json=snap,
                snapshot_unavailable=False,
                summary=summary,
                created_by=created_by,
            )
        )
        return new_id
