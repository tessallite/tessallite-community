"""Consolidate configuration keys — pocket split-brain + dead keys.

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-19

P0.1: pocket split-brain consolidation. The three concepts below used to
live under two key names each (one system-level, one model-level). We
now declare them at both levels under a single name so the resolver can
walk naturally.

  pocket.default_ttl_days   -> pocket.ttl_days       (system)
  pocket.model_ttl_days     -> pocket.ttl_days       (model)
  pocket.model_max_rows     -> pocket.max_rows       (model; system kept)
  pocket.model_min_hits_24h -> pocket.min_hits_24h   (model; system kept)

P1.3: drop rows for registry keys that were never read by any consumer
so the UI doesn't surface dead knobs.

Idempotent: runs DELETEs gated on current_schema() presence checks, so
applying against a DB where the rows were already purged is a no-op.
"""
from alembic import op
import sqlalchemy as sa


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


_RENAMES_SYSTEM = [
    ("pocket.default_ttl_days", "pocket.ttl_days"),
]

_RENAMES_MODEL = [
    ("pocket.model_ttl_days", "pocket.ttl_days"),
    ("pocket.model_max_rows", "pocket.max_rows"),
    ("pocket.model_min_hits_24h", "pocket.min_hits_24h"),
]

_DEAD_SYSTEM_KEYS = [
    "optimizer.min_queries_for_candidate",
    "optimizer.min_estimated_saving_ms",
    "optimizer.max_candidates_per_run",
    "optimizer.candidate_ttl_hours",
    "optimizer.large_source_threshold",
    "optimizer.agg_max_row_threshold",
    "optimizer.cardinality_ratio_threshold",
    "optimizer.miss_threshold_daily",
    "optimizer.miss_threshold_weekly",
    "optimizer.max_aggregates_per_model_default",
    "schema_drift.check_interval_hours",
    "frontend.dev_server_port",
    "frontend.dev_proxy_target",
]

_DEAD_TENANT_KEYS = [
    "tenant.default_aggregate_cron",
    "tenant.query_timeout_seconds",
    "tenant.schema_sync_cron",
    "tenant.session_timeout_minutes",
]

_DEAD_PROJECT_KEYS = [
    "tenant.default_aggregate_cron",
]

_DEAD_MODEL_KEYS = [
    "optimizer.max_aggregates",
]


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).first()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    if _table_exists(conn, "system_settings"):
        for old, new in _RENAMES_SYSTEM:
            conn.execute(
                sa.text(
                    "UPDATE system_settings SET key = :new "
                    "WHERE key = :old AND NOT EXISTS ("
                    "  SELECT 1 FROM system_settings s2 WHERE s2.key = :new"
                    ")"
                ),
                {"old": old, "new": new},
            )
            conn.execute(
                sa.text("DELETE FROM system_settings WHERE key = :old"),
                {"old": old},
            )
        for k in _DEAD_SYSTEM_KEYS:
            conn.execute(
                sa.text("DELETE FROM system_settings WHERE key = :k"), {"k": k}
            )

    if _table_exists(conn, "model_settings"):
        for old, new in _RENAMES_MODEL:
            conn.execute(
                sa.text(
                    "UPDATE model_settings SET key = :new "
                    "WHERE key = :old AND NOT EXISTS ("
                    "  SELECT 1 FROM model_settings s2 "
                    "  WHERE s2.model_id = model_settings.model_id "
                    "    AND s2.key = :new"
                    ")"
                ),
                {"old": old, "new": new},
            )
            conn.execute(
                sa.text("DELETE FROM model_settings WHERE key = :old"),
                {"old": old},
            )

    if _table_exists(conn, "tenant_settings"):
        for k in _DEAD_TENANT_KEYS:
            conn.execute(
                sa.text("DELETE FROM tenant_settings WHERE key = :k"), {"k": k}
            )

    if _table_exists(conn, "project_settings"):
        for k in _DEAD_PROJECT_KEYS:
            conn.execute(
                sa.text("DELETE FROM project_settings WHERE key = :k"), {"k": k}
            )

    if _table_exists(conn, "model_settings"):
        for k in _DEAD_MODEL_KEYS:
            conn.execute(
                sa.text("DELETE FROM model_settings WHERE key = :k"), {"k": k}
            )


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, "system_settings"):
        for old, new in _RENAMES_SYSTEM:
            conn.execute(
                sa.text("UPDATE system_settings SET key = :old WHERE key = :new"),
                {"old": old, "new": new},
            )
    if _table_exists(conn, "model_settings"):
        for old, new in _RENAMES_MODEL:
            conn.execute(
                sa.text("UPDATE model_settings SET key = :old WHERE key = :new"),
                {"old": old, "new": new},
            )
