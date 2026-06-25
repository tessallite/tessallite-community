"""Phase 9 / F6 — dual-axis storage-budget caps + F7 approval flag.

Plan REV-1 / Q2 + Q9:

- ``models.predictive_storage_budget_bytes`` (BIGINT NULL) — flat byte
  ceiling. NULL means "no byte cap on this axis".
- ``models.predictive_storage_budget_count`` (INTEGER NULL) — count
  ceiling. NULL means "no count cap".
- Both nullable, independently optional. The build orchestrator stops
  whenever either binds first.
- ``models.predictive_requires_approval`` (BOOLEAN DEFAULT FALSE) —
  Q9 = A: auto-approve unless the modeller toggles this on.

Revision ID: 0045
Revises: 0044
Create Date: 2026-04-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "models" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("models")}

    if "predictive_storage_budget_bytes" not in existing_cols:
        op.add_column(
            "models",
            sa.Column(
                "predictive_storage_budget_bytes",
                sa.BigInteger(),
                nullable=True,
            ),
        )
    if "predictive_storage_budget_count" not in existing_cols:
        op.add_column(
            "models",
            sa.Column(
                "predictive_storage_budget_count",
                sa.Integer(),
                nullable=True,
            ),
        )
    if "predictive_requires_approval" not in existing_cols:
        op.add_column(
            "models",
            sa.Column(
                "predictive_requires_approval",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "models" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("models")}
    for col in (
        "predictive_requires_approval",
        "predictive_storage_budget_count",
        "predictive_storage_budget_bytes",
    ):
        if col in existing_cols:
            op.drop_column("models", col)
