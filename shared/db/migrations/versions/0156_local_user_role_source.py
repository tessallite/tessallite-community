"""Add role_source (provenance) to local_users (Bug-6597 SSO admin reconcile).

An SSO/IdP group could elevate a user to ``tenant_admin``, but that role was
indistinguishable from a manually-promoted admin. Removing the user from the
IdP admin group therefore never revoked their tenant-admin power. This adds a
``role_source`` column so ``jit.jit_adopt_user`` can reconcile an SSO-elevated
admin DOWN when its admin group disappears, while leaving a manually-promoted
admin (``role_source='manual'``) untouched.

The column is NOT NULL with ``server_default='manual'`` so every existing row
is treated as an operator-set (manual) role and is never auto-downgraded
(fail-closed).

``local_users`` is a TENANT-scoped table (``TenantBase``), living in each
``{slug}_meta`` schema. It does not exist in ``tess_system``; on that DB this
migration is a no-op, mirroring the table-existence guard used by 0155 / 0154.
This revision extends the single tenant head (0155); it does not create a
second head or merge the system/tenant branches.

Revision ID: 0156
Revises: 0155
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only table; no-op on the tess_system DB.
    if "local_users" not in table_names:
        return

    # Idempotency guard: skip if the column already exists (re-run safety).
    existing_cols = {c["name"] for c in inspector.get_columns("local_users")}
    if "role_source" in existing_cols:
        return

    op.add_column(
        "local_users",
        sa.Column(
            "role_source",
            sa.String(length=16),
            nullable=False,
            server_default="manual",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "local_users" not in table_names:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("local_users")}
    if "role_source" not in existing_cols:
        return

    op.drop_column("local_users", "role_source")
