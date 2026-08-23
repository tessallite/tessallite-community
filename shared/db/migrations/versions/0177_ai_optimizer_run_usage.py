"""Add provider-reported token usage to ai_optimizer_runs (F-011-04).

The AI optimiser calls a paid LLM provider per run. Without a persisted
per-run usage record, an operator cannot attribute or cap spend per model,
run, provider, or schedule, and the Diagnostics panel cannot show what a run
cost. This adds nullable input_tokens / output_tokens columns populated from
the adapter's provider-reported usage after the call (NULL when the run failed
before the call or the provider reported no usage).

Revision ID: 0177
Revises: 0176
Create Date: 2026-07-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0177"
down_revision = "0176"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "ai_optimizer_runs" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("ai_optimizer_runs")}
    if "input_tokens" not in columns:
        op.add_column(
            "ai_optimizer_runs",
            sa.Column("input_tokens", sa.Integer(), nullable=True),
        )
    if "output_tokens" not in columns:
        op.add_column(
            "ai_optimizer_runs",
            sa.Column("output_tokens", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "ai_optimizer_runs" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("ai_optimizer_runs")}
    if "output_tokens" in columns:
        op.drop_column("ai_optimizer_runs", "output_tokens")
    if "input_tokens" in columns:
        op.drop_column("ai_optimizer_runs", "input_tokens")
