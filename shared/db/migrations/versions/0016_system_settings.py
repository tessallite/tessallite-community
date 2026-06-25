"""System-level settings + restart-pending registry.

Revision ID: 0016
Revises: 0001
Create Date: 2026-04-18

Phase 0 of the configuration revamp (work/action-plan-config-revamp.md).
Adds two tables to the system DB:

  - ``system_settings`` — generic key/value store for every system-level
    setting that was previously hardcoded in source. Values are stored as
    JSONB so a single column holds ints, floats, strings, lists and dicts
    without per-setting schema changes. Validation is enforced by the
    Python registry in ``shared/config/registry.py``.

  - ``system_restart_pending`` — append-only log of writes to settings
    flagged ``restart_required=true`` in the registry. The System
    Configuration UI surfaces these so the operator knows which knobs are
    awaiting a service restart.

Tenant-level, project-level and model-level settings live in the per-tenant
DB and are added by migration 0017.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0016"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value_json", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(255)),
        schema="tess_system",
    )

    op.create_table(
        "system_restart_pending",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("setting_key", sa.String(128), nullable=False),
        sa.Column(
            "written_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("written_by", sa.String(255)),
        schema="tess_system",
    )

    op.create_index(
        "ix_system_restart_pending_setting_key",
        "system_restart_pending",
        ["setting_key"],
        schema="tess_system",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_system_restart_pending_setting_key",
        table_name="system_restart_pending",
        schema="tess_system",
    )
    op.drop_table("system_restart_pending", schema="tess_system")
    op.drop_table("system_settings", schema="tess_system")
