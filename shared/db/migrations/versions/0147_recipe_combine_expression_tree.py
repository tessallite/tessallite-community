"""Cross-model recipe combine: formula string -> semantic expression tree.

Bug-5346 — the combine expression is now a typed semantic tree carried as
JSONB data, never a formula string parsed as code. Existing string values are
not valid expression trees and are not migrated (per decision: stored recipes
are cleared); the column is dropped and re-added as nullable JSONB.

Revision ID: 0147
Revises: 0146
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop + re-add clears legacy formula strings (no data migration); the new
    # column holds an ExprNode tree or NULL (recipe has no combine step).
    op.drop_column("project_cross_model_recipes", "combine")
    op.add_column(
        "project_cross_model_recipes",
        sa.Column("combine", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_cross_model_recipes", "combine")
    op.add_column(
        "project_cross_model_recipes",
        sa.Column(
            "combine", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
    )
