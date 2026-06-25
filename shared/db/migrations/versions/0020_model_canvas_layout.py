"""Persist Model Builder canvas layout on the models row.

Revision ID: 0020
Revises: 0018
Create Date: 2026-04-18

Phase 2 of the deploy/versioning bundle (work/action-plan-deploy-versioning.md).
Adds a single ``canvas_layout`` JSONB column to ``models`` so table
positions, viewport zoom and pan, and any future per-edge styling
travel with the model. Default is an empty object so existing rows
read as "no positions saved yet" and the SPA falls back to its
auto-layout.

Shape (validated by the API, not the DB):

    {
      "tables": {
        "<table_id>": {"x": 120, "y": 80, "w": 200, "h": 240}
      },
      "viewport": {"x": 0, "y": 0, "zoom": 1.0}
    }
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0020"
down_revision = "0018"
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
    if _column_exists("models", "canvas_layout"):
        return
    op.add_column(
        "models",
        sa.Column(
            "canvas_layout",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    if _column_exists("models", "canvas_layout"):
        op.drop_column("models", "canvas_layout")
