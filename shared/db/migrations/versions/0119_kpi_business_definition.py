"""Add business_definition JSONB column to kpis table.

Revision ID: 0119
Revises: 0118
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kpis", sa.Column("business_definition", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("kpis", "business_definition")
