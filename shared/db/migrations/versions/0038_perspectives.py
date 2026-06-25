"""Perspectives — Phase 8.B core table.

Revision ID: 0038
Revises: 0037
Create Date: 2026-04-24

Adds the ``perspectives`` table backing analyst-facing scopes over a
model. Empty include lists mean "no restriction on this object class";
populated lists are allow-lists. ``bypass_row_security`` ships in 8.C.1
as migration 00NN to keep the security-sensitive column separate from
the core table creation.

Plan: ``work/phase-8-action-plan.md`` §8.B.1.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": table},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _table_exists("perspectives"):
        return
    op.create_table(
        "perspectives",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "model_id",
            UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "included_measure_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "included_dimension_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "included_hierarchy_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "audience_roles",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "default_filters",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("model_id", "name", name="uq_perspective_model_name"),
    )
    op.create_index(
        "ix_perspectives_model_id",
        "perspectives",
        ["model_id"],
    )


def downgrade() -> None:
    if not _table_exists("perspectives"):
        return
    op.drop_index("ix_perspectives_model_id", table_name="perspectives")
    op.drop_table("perspectives")
