"""Create tess_system.revoked_embed_tokens on the SYSTEM branch (Bug-1033).

Revision ID: 0128
Revises: 0016
Create Date: 2026-06-12

Root cause fixed here: the original migration (0099) sat on the TENANT
branch but its body only executed under ``MIGRATE_MODE=system`` — a
combination no documented migration path ever runs (``system@head``
stopped at 0016, and tenant runs set ``MIGRATE_MODE=tenant``). The table
was therefore never created anywhere, and every embed token carrying a
``jti`` died with HTTP 500 (UndefinedTableError) in the shared auth
middleware's revocation lookup.

This revision puts the table on the system branch, where a system-schema
table belongs, so ``alembic upgrade system@head`` actually creates it.
Migration 0099 is now an explicit no-op. The create is guarded by an
existence check so environments where the table was created out-of-band
upgrade cleanly.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0128"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'tess_system' AND table_name = 'revoked_embed_tokens'"
        )
    ).scalar()
    if exists:
        return
    op.create_table(
        "revoked_embed_tokens",
        sa.Column("jti", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        schema="tess_system",
    )


def downgrade() -> None:
    op.drop_table("revoked_embed_tokens", schema="tess_system")
