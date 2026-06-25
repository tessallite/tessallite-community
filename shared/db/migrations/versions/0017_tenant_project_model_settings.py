"""Tenant, project, and model-level settings tables.

Revision ID: 0017
Revises: 0015
Create Date: 2026-04-18

Phase 0 of the configuration revamp (work/action-plan-config-revamp.md).
Adds three generic key/value tables to the per-tenant schema. Each row
holds a JSONB value validated by the Python registry at write time.

  - ``tenant_settings`` — one row per (key) for the tenant. The owning
    tenant is implicit because each per-tenant DB schema is dedicated to
    a single tenant; no ``tenant_id`` column is needed.

  - ``project_settings`` — overrides keyed by ``(project_id, key)``.
    Falls back to tenant level when absent.

  - ``model_settings`` — overrides keyed by ``(model_id, key)``. Falls
    back to project then tenant then system level when absent.

The system-level settings table lives in the system DB and is created by
migration 0016.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0017"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value_json", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(255)),
    )

    op.create_table(
        "project_settings",
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value_json", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(255)),
        sa.PrimaryKeyConstraint("project_id", "key"),
    )

    op.create_table(
        "model_settings",
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value_json", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(255)),
        sa.PrimaryKeyConstraint("model_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("model_settings")
    op.drop_table("project_settings")
    op.drop_table("tenant_settings")
