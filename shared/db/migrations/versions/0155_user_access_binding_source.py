"""Add source (provenance) to user_access_bindings (Bug-6303 SSO revocation).

SSO group-driven privilege could never be revoked on IdP de-provisioning
because a group-materialised binding was indistinguishable from a manually
granted one. This adds a ``source`` column so ``jit._sync_group_bindings`` can
reconcile and revoke stale ``sso_group`` bindings while leaving ``manual``
grants untouched.

The column is NOT NULL with ``server_default='manual'`` so every existing row
is treated as a manual grant and is never auto-revoked (fail-closed).

``user_access_bindings`` is a TENANT-scoped table (``TenantBase``), living in
each ``{slug}_meta`` schema. It does not exist in ``tess_system``; on that DB
this migration is a no-op, mirroring the table-existence guard used by 0154 /
0148 / 0149. This revision branches the single tenant head (0154); it does not
create a second head or merge the system/tenant branches.

Revision ID: 0155
Revises: 0154
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only table; no-op on the tess_system DB.
    if "user_access_bindings" not in table_names:
        return

    # Idempotency guard: skip if the column already exists (re-run safety).
    existing_cols = {c["name"] for c in inspector.get_columns("user_access_bindings")}
    if "source" in existing_cols:
        return

    op.add_column(
        "user_access_bindings",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="manual",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "user_access_bindings" not in table_names:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("user_access_bindings")}
    if "source" not in existing_cols:
        return

    op.drop_column("user_access_bindings", "source")
