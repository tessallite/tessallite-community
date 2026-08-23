"""
Programmatic bottom-up cascade delete for models and projects.

Deletes all child records explicitly rather than relying on DB-level
CASCADE, giving per-step error reporting and avoiding FK constraint
collisions on log tables.

This is the single canonical model-deletion path (F-020-06). It lives in
``shared`` so both the model-service delete endpoints (re-exported via
``services/model-service/src/api/_cascade_delete.py``) and the project
import rehydrator use the same hardened delete instead of a raw
``delete(Model)`` that trips the ``ai_optimizer_runs.telemetry_snapshot_id``
NO ACTION FK and stale log-table constraints (Bug-411 class).
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.model_lock import acquire_model_definition_lock

logger = logging.getLogger(__name__)


def _as_uuid(model_id) -> UUID:
    """Coerce a model identifier to ``UUID``.

    ``delete_project_cascade`` and the project rehydrator source their ids from
    a raw ``SELECT id FROM models``. asyncpg returns ``uuid.UUID`` there, but a
    driver or a test double that hands back a string must not silently skip the
    advisory lock (``model_advisory_lock_key`` needs ``.int``), so the coercion
    is explicit and fails loudly on anything that is not a UUID.
    """
    return model_id if isinstance(model_id, UUID) else UUID(str(model_id))


# Tables with a direct model_id FK to models.id, ordered bottom-up.
# Grandchild tables (no model_id) cascade from their parent via DB CASCADE:
#   data_quality_violations    ← cascades from data_quality_rules
#   aggregate_columns/refresh  ← cascades from aggregate_definitions
#   pocket_predicates/refresh  ← cascades from pocket_definitions
#   hierarchy_levels/links     ← cascades from hierarchy_definitions
#   glossary_synonym/attachment← cascades from glossary_entry
#   persona_tag_restrictions   ← cascades from personas / data_tags
#   model_columns              ← cascades from model_tables
#   calendar_tables            ← cascades from data_sources
#
# Cross-table FK that requires explicit ordering:
#   ai_optimizer_runs.telemetry_snapshot_id → model_telemetry_snapshots (NO ACTION)
#
# Log tables (query_logs, query_miss_logs, route_logs) have no FK constraints;
# they are deleted for hygiene only.
_MODEL_DELETE_STEPS: list[tuple[str, str]] = [
    # --- Log tables (no FK constraints) ---
    ("route_logs",
     "DELETE FROM route_logs WHERE query_log_id IN "
     "(SELECT id FROM query_logs WHERE model_id = :mid)"),
    ("query_logs",
     "DELETE FROM query_logs WHERE model_id = :mid"),
    ("query_miss_logs",
     "DELETE FROM query_miss_logs WHERE model_id = :mid"),

    # --- SET NULL reference (not a child, just a pointer) ---
    ("project_agent_configs.primary_model_id",
     "UPDATE project_agent_configs SET primary_model_id = NULL "
     "WHERE primary_model_id = :mid"),

    # --- Leaf tables (no other model children reference them) ---
    ("model_versions",
     "DELETE FROM model_versions WHERE model_id = :mid"),
    ("model_parameters",
     "DELETE FROM model_parameters WHERE model_id = :mid"),
    ("ai_optimizer_runs",
     "DELETE FROM ai_optimizer_runs WHERE model_id = :mid"),
    ("model_telemetry_snapshots",
     "DELETE FROM model_telemetry_snapshots WHERE model_id = :mid"),
    ("model_ai_scheduler_config",
     "DELETE FROM model_ai_scheduler_config WHERE model_id = :mid"),
    ("model_settings",
     "DELETE FROM model_settings WHERE model_id = :mid"),
    ("glossary_share_token",
     "DELETE FROM glossary_share_token WHERE model_id = :mid"),
    ("refresh_sla_configs",
     "DELETE FROM refresh_sla_configs WHERE model_id = :mid"),
    ("project_agent_models",
     "DELETE FROM project_agent_models WHERE model_id = :mid"),
    ("project_agent_model_contexts",
     "DELETE FROM project_agent_model_contexts WHERE model_id = :mid"),
    ("project_persona_model_scopes",
     "DELETE FROM project_persona_model_scopes WHERE model_id = :mid"),
    ("model_alias_maps",
     "DELETE FROM model_alias_maps WHERE model_id = :mid"),
    ("gateway_query_references",
     "DELETE FROM gateway_query_references WHERE model_id = :mid"),
    ("user_access_bindings",
     "DELETE FROM user_access_bindings WHERE model_id = :mid"),
    ("model_alerts",
     "DELETE FROM model_alerts WHERE model_id = :mid"),
    ("schema_change_events",
     "DELETE FROM schema_change_events WHERE model_id = :mid"),

    # --- Tables that cross-reference other model children ---
    ("lineage_mappings",
     "DELETE FROM lineage_mappings WHERE model_id = :mid"),
    ("aggregate_lifecycle_events",
     "DELETE FROM aggregate_lifecycle_events WHERE model_id = :mid"),
    ("ai_aggregate_recommendations",
     "DELETE FROM ai_aggregate_recommendations WHERE model_id = :mid"),
    ("hierarchy_health_issues",
     "DELETE FROM hierarchy_health_issues WHERE model_id = :mid"),
    ("data_quality_rules",
     "DELETE FROM data_quality_rules WHERE model_id = :mid"),
    ("glossary_entry",
     "DELETE FROM glossary_entry WHERE model_id = :mid"),
    ("downstream_assets",
     "DELETE FROM downstream_assets WHERE model_id = :mid"),
    ("data_tags",
     "DELETE FROM data_tags WHERE model_id = :mid"),
    ("row_security_rules",
     "DELETE FROM row_security_rules WHERE model_id = :mid"),

    # --- Entity tables (their own children cascade at DB level) ---
    ("personas",
     "DELETE FROM personas WHERE model_id = :mid"),
    ("aggregate_definitions",
     "DELETE FROM aggregate_definitions WHERE model_id = :mid"),
    ("pocket_definitions",
     "DELETE FROM pocket_definitions WHERE model_id = :mid"),
    ("dimensions",
     "DELETE FROM dimensions WHERE model_id = :mid"),
    ("measures",
     "DELETE FROM measures WHERE model_id = :mid"),
    ("hierarchy_definitions",
     "DELETE FROM hierarchy_definitions WHERE model_id = :mid"),
    ("joins",
     "DELETE FROM joins WHERE model_id = :mid"),
    ("user_defined_attributes",
     "DELETE FROM user_defined_attributes WHERE model_id = :mid"),

    # --- Named Query family (F-013-07 / Bug-9162) -----------------------------
    # ``named_query_artifacts.target_id`` -> ``data_targets`` is NO ACTION, so
    # deleting ``data_targets`` (below) while an artifact still references it
    # FK-fails once a Named Query has been refreshed — a model/project could not
    # be deleted at all. Delete the artifacts (and runs/policies/definitions)
    # FIRST. Artifacts and runs/policies CASCADE from ``named_queries`` at the DB
    # level, but the ordering vs ``data_targets`` is what the explicit steps fix.
    # Ordered artifacts -> runs -> policies -> named_queries so no child outlives
    # a delete of its parent.
    ("named_query_artifacts",
     "DELETE FROM named_query_artifacts WHERE named_query_id IN "
     "(SELECT id FROM named_queries WHERE model_id = :mid)"),
    ("named_query_refresh_runs",
     "DELETE FROM named_query_refresh_runs WHERE named_query_id IN "
     "(SELECT id FROM named_queries WHERE model_id = :mid)"),
    ("named_query_refresh_policies",
     "DELETE FROM named_query_refresh_policies WHERE named_query_id IN "
     "(SELECT id FROM named_queries WHERE model_id = :mid)"),
    ("named_queries",
     "DELETE FROM named_queries WHERE model_id = :mid"),

    # --- Infrastructure tables ---
    ("model_tables",
     "DELETE FROM model_tables WHERE model_id = :mid"),
    ("data_sources",
     "DELETE FROM data_sources WHERE model_id = :mid"),
    ("data_targets",
     "DELETE FROM data_targets WHERE model_id = :mid"),
]


async def _collect_model_physical_tables(
    db: AsyncSession, model_id: UUID
) -> tuple[list, list, list]:
    """Load every aggregate/pocket/named-query artifact before metadata deletion.

    This enumeration is fail-closed.  An incomplete list would let the metadata
    transaction commit without scheduling one of its physical tables, losing
    the cleanup identity and retry owner permanently.

    F-013-07: Named Query artifacts are the third materialised family. Their
    target table must be scheduled for drop too, or a deleted model leaves an
    orphan NQ table on the source-side target.
    """
    from shared.db.models import (
        AggregateDefinition,
        NamedQuery,
        NamedQueryArtifact,
        PocketDefinition,
    )

    agg_rows = list(
        (
            await db.execute(
                select(AggregateDefinition).where(
                    AggregateDefinition.model_id == model_id
                )
            )
        ).scalars().all()
    )
    pocket_rows = list(
        (
            await db.execute(
                select(PocketDefinition).where(
                    PocketDefinition.model_id == model_id
                )
            )
        ).scalars().all()
    )
    nq_artifact_rows = list(
        (
            await db.execute(
                select(NamedQueryArtifact)
                .join(NamedQuery, NamedQueryArtifact.named_query_id == NamedQuery.id)
                .where(NamedQuery.model_id == model_id)
            )
        ).scalars().all()
    )
    return agg_rows, pocket_rows, nq_artifact_rows


async def delete_model_cascade(
    db: AsyncSession,
    model_id: UUID,
    *,
    fail_fast: bool = True,
    cleanup_reason: str = "model_delete",
) -> list[str]:
    """Delete all child records of a model bottom-up, then the model itself.

    When *fail_fast* is True (default), execution stops at the first
    failed step so the caller can rollback cleanly.  When False, all
    steps are attempted and errors are collected.

    Returns metadata/scheduling errors (empty = the metadata transaction is
    ready to commit).  Physical DDL is NEVER issued here.  Complete detached
    cleanup rows are inserted before the metadata deletes, commit atomically
    with them, and are drained by the caller only after commit.  A rollback
    therefore restores metadata without any irreversible target-side effect.

    LOCK ORDER (Bug-7982 class; the ordering contract is set by migration
    ``0194_join_orientation_backfill_deployed_snapshot_repair``)
    ------------------------------------------------------------------------
    ``0194._lock_models`` takes ``pg_advisory_xact_lock`` for every affected
    model FIRST and only then writes ``joins`` rows, and says why in as many
    words: the API's Save/revert path takes the advisory lock and then writes
    ``joins``, so touching the rows before the lock "would invert that order,
    and an ABBA deadlock aborts the whole transaction with an opaque 40P01".

    This cascade used to do exactly that inversion: it reached ``joins`` (and
    every other snapshot-owned table) with NO advisory lock at all and only
    touched the ``models`` row last. Two consequences, both live:

      * against a lock holder that is mid-write on the same model (revert,
        Save, or 0194 itself), the cascade could delete that model's children
        underneath it, and the cycle "holder waits for the cascade's ``joins``
        row lock / cascade waits for the holder's FK KEY SHARE on ``models``"
        is a real deadlock that aborts one side MID-CASCADE;
      * the runtime write guard (``shared/db/model_write_lock_guard.py``) saw
        every one of those writes as an unlocked write to a snapshot-owned
        table and, in the default ``warn`` mode, has been reporting them.

    Acquiring the lock as the FIRST statement fixes both halves at once: the
    order becomes lock-then-rows, which is what 0194 and the API already do.

    Target identity is resolved while this transaction holds the definition
    lock, so a concurrent revert cannot change the victim set between capture
    and deletion.  The lock is released by metadata commit before any target
    DDL begins.
    """
    model_id = _as_uuid(model_id)
    errors: list[str] = []
    params = {"mid": str(model_id)}

    # FIRST statement — see the LOCK ORDER note above. Nothing this function
    # does may read or write model-owned state before this line.
    await acquire_model_definition_lock(db, model_id)

    # Bug-8140: capture + persist complete detached identity before any owning
    # metadata row is deleted. Any failure aborts the metadata transaction; an
    # incomplete outbox is not a successful delete.
    try:
        agg_defs, pocket_defs, nq_artifacts = await _collect_model_physical_tables(
            db, model_id
        )
        from shared.physical_cleanup import schedule_model_physical_cleanup

        await schedule_model_physical_cleanup(
            db,
            model_id=model_id,
            aggregate_definitions=agg_defs,
            pocket_definitions=pocket_defs,
            named_query_artifacts=nq_artifacts,
            requested_by=cleanup_reason,
        )
    except Exception as exc:
        msg = f"physical_cleanup_schedule: {exc}"
        logger.error("Model %s cleanup scheduling failed — %s", model_id, msg)
        return [msg]

    for step_name, sql in _MODEL_DELETE_STEPS:
        try:
            await db.execute(text(sql), params)
        except Exception as exc:
            msg = f"{step_name}: {exc}"
            logger.warning("Model %s delete step failed — %s", model_id, msg)
            errors.append(msg)
            if fail_fast:
                return errors

    try:
        await db.execute(
            text("DELETE FROM models WHERE id = :mid"), params
        )
    except Exception as exc:
        msg = f"models: {exc}"
        logger.error("Model %s final delete failed — %s", model_id, msg)
        errors.append(msg)

    return errors


async def delete_project_cascade(
    db: AsyncSession, project_id: UUID, *, fail_fast: bool = True
) -> list[str]:
    """Delete a project and all its models (bottom-up per model).

    Returns a list of error messages for failed steps (empty = full success).

    The models are visited in a DETERMINISTIC order (sorted by the UUID's
    string form). ``delete_model_cascade`` now takes each model's advisory lock,
    so a project delete holds N of them at once for the life of the
    transaction; two concurrent multi-model lockers that disagree on order
    deadlock with each other. ``str``-sorting is not an arbitrary choice — it is
    byte-identical to ``0194._lock_models``' ``sorted(model_ids, key=str)``, so
    a project delete and a running tenant migration acquire the shared subset in
    the same sequence.
    """
    errors: list[str] = []

    rows = (await db.execute(
        text("SELECT id FROM models WHERE project_id = :pid"),
        {"pid": str(project_id)},
    )).fetchall()
    ordered = sorted((_as_uuid(r[0]) for r in rows), key=str)

    for mid in ordered:
        model_errors = await delete_model_cascade(
            db, mid, fail_fast=fail_fast, cleanup_reason="project_delete"
        )
        errors.extend(model_errors)
        if fail_fast and errors:
            return errors

    try:
        await db.execute(
            text("DELETE FROM projects WHERE id = :pid"),
            {"pid": str(project_id)},
        )
    except Exception as exc:
        msg = f"projects: {exc}"
        logger.error("Project %s final delete failed — %s", project_id, msg)
        errors.append(msg)

    return errors
