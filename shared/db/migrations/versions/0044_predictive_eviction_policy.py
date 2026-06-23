"""Phase 9 / F9 — creation_reason taxonomy + per-model eviction policy.

Plan REV-1 / Q6 + Q8:

- ``aggregate_definitions.creation_reason`` retains its plain ``String(32)``
  storage but the **valid value set** changes from {auto, manual} to
  {predictive, demand, workload, manual}. Per Q8 (drop and recreate, dev /
  demo data is regenerable), every existing row resets to ``'manual'``
  rather than splitting into the new buckets via heuristics.

- ``models.predictive_eviction_policy`` is added as a ``String(32) NOT NULL
  DEFAULT 'predicted_first'`` column. Values: ``predicted_first``, ``lru``,
  ``validated_survives``, ``never_evict``. F9 retirement code reads this
  column to decide who dies first when ``max_aggregates`` is exceeded.

Revision ID: 0044
Revises: 0043
Create Date: 2026-04-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "aggregate_definitions" in inspector.get_table_names():
        op.execute(
            sa.text(
                "UPDATE aggregate_definitions "
                "SET creation_reason = 'manual' "
                "WHERE creation_reason NOT IN "
                "  ('predictive','demand','workload','manual')"
            )
        )
        op.alter_column(
            "aggregate_definitions",
            "creation_reason",
            existing_type=sa.String(length=32),
            server_default=sa.text("'manual'"),
            existing_nullable=False,
        )

    if "models" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("models")}
        if "predictive_eviction_policy" not in existing_cols:
            op.add_column(
                "models",
                sa.Column(
                    "predictive_eviction_policy",
                    sa.String(length=32),
                    nullable=False,
                    server_default=sa.text("'predicted_first'"),
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "models" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("models")}
        if "predictive_eviction_policy" in existing_cols:
            op.drop_column("models", "predictive_eviction_policy")

    if "aggregate_definitions" in inspector.get_table_names():
        op.alter_column(
            "aggregate_definitions",
            "creation_reason",
            existing_type=sa.String(length=32),
            server_default=sa.text("'auto'"),
            existing_nullable=False,
        )
