"""Tenant-level role on local_users.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-14

Adds a ``role`` column to ``local_users`` for the Phase D1 admin revamp.
Allowed values: ``member`` (default) and ``tenant_admin``. The new
``tenant_admin`` role grants full admin power within a single tenant
(create/edit/delete users, projects, models, and access bindings) while
leaving ``system_admin`` as the only cross-tenant role. Per-project
``admin/modeler/viewer`` bindings in ``user_access_bindings`` are
unchanged.

The column is backfilled to ``member`` for every existing row via the
server default; no data migration step is required.
"""
from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _column_exists("local_users", "role"):
        return
    op.add_column(
        "local_users",
        sa.Column(
            "role",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
    )


def downgrade() -> None:
    if _column_exists("local_users", "role"):
        op.drop_column("local_users", "role")
