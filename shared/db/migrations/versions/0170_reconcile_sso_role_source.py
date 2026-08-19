"""Reconcile stale role_source for pre-0156 SSO tenant_admins (Bug-6639).

Migration 0156 added the ``role_source`` column to ``local_users`` with a
``server_default='manual'``.  This was correct fail-closed behaviour at the
time: every existing row was assumed to be an operator-set (manual) role.

However, SSO users who were elevated to ``tenant_admin`` by an IdP admin-group
mapping BEFORE 0156 deployed received ``role_source='manual'`` retroactively.
The JIT reconciliation path (_reconcile_sso_tenant_role, Case 2) refuses to
auto-demote a ``role_source='manual'`` admin, so those pre-deploy SSO admins
keep their admin power forever -- even after the IdP removes them from the
admin group.  This is a security gap: a stale admin role persists after IdP
deprovisioning.

Fix (fail-closed for security): re-stamp ``role_source`` from ``'manual'`` to
``'sso'`` for every ``local_users`` row where:
  - ``role = 'tenant_admin'``
  - ``role_source = 'manual'``  (the stale server-default)
  - ``auth_source`` is an external IdP backend (not 'local' and not NULL/empty)

This makes them eligible for the standard SSO reconciliation path.  If any of
these users were intentionally manually-promoted (an operator promoted an SSO
user via the admin UI before 0156), the IdP reconcile will now govern them --
the operator can re-promote via the admin API (which stamps
``role_source='manual'``) if the demotion was unintended.  This is strictly
safer than leaving an un-demotable admin.

Tenant-scoped: ``local_users`` lives in each ``{slug}_meta`` schema.  No-op on
the ``tess_system`` DB (no ``local_users`` table).  Idempotent: the WHERE
clause only matches un-reconciled rows.

Revision ID: 0170
Revises: 0169
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0170"
down_revision = "0169"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only table; no-op on the tess_system DB.
    if "local_users" not in table_names:
        return

    # Idempotency: only rows still carrying the stale default are updated.
    existing_cols = {c["name"] for c in inspector.get_columns("local_users")}
    if "role_source" not in existing_cols or "auth_source" not in existing_cols:
        return

    # Re-stamp SSO-origin tenant_admins from the blanket 'manual' default
    # to 'sso' so the JIT reconcile path can govern them.
    bind.execute(
        sa.text(
            "UPDATE local_users "
            "SET role_source = 'sso' "
            "WHERE role = 'tenant_admin' "
            "  AND role_source = 'manual' "
            "  AND auth_source IS NOT NULL "
            "  AND auth_source NOT IN ('local', '')"
        )
    )


def downgrade() -> None:
    # Reverting re-stamps reconciled rows back to 'manual'.  This restores the
    # pre-migration state but re-introduces the grandfathering gap.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "local_users" not in table_names:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("local_users")}
    if "role_source" not in existing_cols or "auth_source" not in existing_cols:
        return

    bind.execute(
        sa.text(
            "UPDATE local_users "
            "SET role_source = 'manual' "
            "WHERE role = 'tenant_admin' "
            "  AND role_source = 'sso' "
            "  AND auth_source IS NOT NULL "
            "  AND auth_source NOT IN ('local', '')"
        )
    )
