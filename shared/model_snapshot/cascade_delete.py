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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

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

    # --- Infrastructure tables ---
    ("model_tables",
     "DELETE FROM model_tables WHERE model_id = :mid"),
    ("data_sources",
     "DELETE FROM data_sources WHERE model_id = :mid"),
    ("data_targets",
     "DELETE FROM data_targets WHERE model_id = :mid"),
]


async def delete_model_cascade(
    db: AsyncSession, model_id: UUID, *, fail_fast: bool = True
) -> list[str]:
    """Delete all child records of a model bottom-up, then the model itself.

    When *fail_fast* is True (default), execution stops at the first
    failed step so the caller can rollback cleanly.  When False, all
    steps are attempted and errors are collected.

    Returns a list of error messages for failed steps (empty = full success).
    """
    errors: list[str] = []
    params = {"mid": str(model_id)}

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
    """
    errors: list[str] = []

    rows = (await db.execute(
        text("SELECT id FROM models WHERE project_id = :pid"),
        {"pid": str(project_id)},
    )).fetchall()

    for (mid,) in rows:
        model_errors = await delete_model_cascade(db, mid, fail_fast=fail_fast)
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
