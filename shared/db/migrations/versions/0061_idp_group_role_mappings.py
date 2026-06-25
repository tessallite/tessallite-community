"""Create idp_group_role_mappings table in per-tenant meta schema.

Revision ID: 0061
Revises: 0060
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def _table_exists(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "idp_group_role_mappings"):
        op.create_table(
            "idp_group_role_mappings",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("idp_group_name", sa.Text(), nullable=False),
            sa.Column("project_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("idp_group_name", "project_id", name="uq_idp_group_project"),
        )
        op.create_index(
            "ix_idp_group_role_mappings_group",
            "idp_group_role_mappings",
            ["idp_group_name"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "idp_group_role_mappings"):
        op.drop_table("idp_group_role_mappings")
