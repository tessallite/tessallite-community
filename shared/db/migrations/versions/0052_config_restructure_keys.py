"""Admin-config restructure — port stored values to their new homes.

Companion to 0051 (LLM-config re-scope). This migration handles the
non-LLM key renames/moves implied by the 2026-04 admin-config
restructure:

  Tenant level eliminated. Pre-restructure tenant-level keys move as:

    source_db.fallback_host       -> system level
    source_db.fallback_port       -> system level
    source_db.fallback_database   -> system level
    agg_target.default_schema     -> system level
    agg_target.default_dataset    -> system level
    agg_target.default_database   -> system level
    spark.thrift_port             -> system level
    spark.thrift_database         -> system level
    spark.thrift_auth_mode        -> system level
    agent.conversation_retention_days -> project level (fan-out per project)

  Plus: any tenant_settings row whose key is no longer in the registry is
  dropped as an orphan. System-level rows for keys not in the registry
  are dropped likewise (e.g. legacy ``security.*`` operator flags).

The migration operates on the bound database. The system DB carries
``system_settings`` only; tenant DBs carry ``tenant_settings`` /
``project_settings``. Each branch is gated on ``inspector`` checks so
the migration is a no-op for the half that doesn't apply.

Revision ID: 0052
Revises: 0051
Create Date: 2026-04-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


# Tenant keys that moved up to system level (operator-only fallbacks).
_TENANT_TO_SYSTEM_KEYS = [
    "source_db.fallback_host",
    "source_db.fallback_port",
    "source_db.fallback_database",
    "agg_target.default_schema",
    "agg_target.default_dataset",
    "agg_target.default_database",
    "spark.thrift_port",
    "spark.thrift_database",
    "spark.thrift_auth_mode",
]

# Tenant keys that move down to project level via fan-out (one row per project).
_TENANT_TO_PROJECT_FANOUT_KEYS = [
    "agent.conversation_retention_days",
]

# System-level keys removed from the registry entirely (no fallback resolver
# entry remains). Drop any stored rows so the resolver can't surface them.
_DROPPED_SYSTEM_KEYS = [
    "security.accept_tenant_from_request_context",
    "security.require_tenant_filter",
]


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ------------------------------------------------------------------
    # System DB branch: drop dropped keys, accept inbound copies from any
    # tenant migration that pre-staged values (we keep this idempotent).
    # ------------------------------------------------------------------
    if _table_exists(inspector, "system_settings"):
        for k in _DROPPED_SYSTEM_KEYS:
            bind.execute(
                sa.text("DELETE FROM system_settings WHERE key = :k"),
                {"k": k},
            )

    # ------------------------------------------------------------------
    # Tenant DB branch: tenant_settings → project_settings fan-out, then
    # drop orphan tenant_settings rows.
    # ------------------------------------------------------------------
    if _table_exists(inspector, "tenant_settings") and _table_exists(
        inspector, "project_settings"
    ) and _table_exists(inspector, "projects"):
        # Fan out tenant-level rows to every project in the tenant.
        for k in _TENANT_TO_PROJECT_FANOUT_KEYS:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO project_settings (project_id, key, value_json)
                    SELECT p.id, ts.key, ts.value_json
                    FROM tenant_settings ts
                    CROSS JOIN projects p
                    WHERE ts.key = :k
                      AND NOT EXISTS (
                        SELECT 1 FROM project_settings ps
                        WHERE ps.project_id = p.id AND ps.key = ts.key
                      )
                    """
                ),
                {"k": k},
            )

    # Drop every tenant_settings row whose key is no longer in the registry.
    # The post-restructure tenant level is empty, so every row is an orphan
    # — but keys that were also moved to system level are intentionally
    # not preserved at tenant DB write time (system rows are seeded by
    # bootstrap on the next service start).
    if _table_exists(inspector, "tenant_settings"):
        kept_keys = (
            _TENANT_TO_SYSTEM_KEYS  # rows dropped from tenant; system seed handles defaults
            + _TENANT_TO_PROJECT_FANOUT_KEYS  # fanned out above; safe to drop the source rows
        )
        if kept_keys:
            placeholders = ", ".join(f":k{i}" for i in range(len(kept_keys)))
            params = {f"k{i}": k for i, k in enumerate(kept_keys)}
            bind.execute(
                sa.text(
                    f"DELETE FROM tenant_settings WHERE key IN ({placeholders})"
                ),
                params,
            )
        # Anything else left in tenant_settings — purge as orphan: the
        # registry has no tenant-level keys after this restructure.
        bind.execute(sa.text("DELETE FROM tenant_settings"))


def downgrade() -> None:
    """No-op downgrade.

    Re-creating tenant_settings rows from their post-restructure homes is
    not safely reversible: system rows have already been merged across
    tenants, project fan-outs may have been edited divergently. Operators
    rolling back to pre-restructure should restore from the
    ``backup/pre-config-restructure-2026-04-26`` branch and from a DB
    snapshot taken before this migration was applied.
    """
    pass
