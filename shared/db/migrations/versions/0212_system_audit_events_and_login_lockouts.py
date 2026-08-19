"""Create tess_system.system_audit_events and login_lockouts (system branch).

CP-08 / G-022-01 / G-021-04. Chains onto the SYSTEM head (0207). Does not
merge with the tenant branch. Idempotent create.

Revision ID: 0212
Revises: 0207
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0212"
down_revision = "0207"
branch_labels = None
depends_on = None

_SCHEMA = "tess_system"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": _SCHEMA, "t": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "system_audit_events"):
        op.create_table(
            "system_audit_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("actor_email", sa.Text(), nullable=True),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("target_type", sa.Text(), nullable=True),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("target_name", sa.Text(), nullable=True),
            sa.Column("tenant_slug", sa.String(64), nullable=True),
            sa.Column("severity", sa.Text(), nullable=False),
            sa.Column("detail", postgresql.JSONB(), nullable=True),
            sa.Column("ip_address", sa.Text(), nullable=True),
            schema=_SCHEMA,
        )
        op.create_index(
            "ix_system_audit_events_timestamp",
            "system_audit_events",
            ["timestamp"],
            schema=_SCHEMA,
        )
    if not _has_table(conn, "login_lockouts"):
        op.create_table(
            "login_lockouts",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("scope_key", sa.String(128), nullable=False),
            sa.Column("email_canonical", sa.String(255), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "scope_key",
                "email_canonical",
                name="uq_login_lockout_scope_email",
            ),
            schema=_SCHEMA,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "login_lockouts"):
        op.drop_table("login_lockouts", schema=_SCHEMA)
    if _has_table(conn, "system_audit_events"):
        op.drop_index(
            "ix_system_audit_events_timestamp",
            table_name="system_audit_events",
            schema=_SCHEMA,
        )
        op.drop_table("system_audit_events", schema=_SCHEMA)
