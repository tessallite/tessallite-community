"""Initial system schema — creates tess_system schema and tenants table.

Revision ID: 0001
Revises:
Create Date: 2026-03-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0001"
down_revision = None
branch_labels = ("system",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS tess_system")
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("encrypted_db_url", sa.LargeBinary, nullable=False),
        sa.Column("db_schema_prefix", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        schema="tess_system",
    )


def downgrade() -> None:
    op.drop_table("tenants", schema="tess_system")
    op.execute("DROP SCHEMA IF EXISTS tess_system")
