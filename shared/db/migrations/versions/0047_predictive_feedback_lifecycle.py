"""Phase 9 / F10 + F13 — predictive feedback metadata + aggregate lifecycle log.

F10: ``aggregate_definitions.predictive_validated_at`` is set when the
feedback sweep observes that a predictive aggregate has been hit by
real queries enough times within the evaluation window. The label
stays ``creation_reason='predictive'`` (per Risk-4 in the action plan
— don't clobber explicit modeller approvals); the eviction policy
``validated_survives`` is the consumer of this column.

F13: ``aggregate_lifecycle_events`` is the per-model audit log. One
row per create / validate / retire / refresh-fail event, payload
captured as JSONB so future event types don't require a schema change.

Revision ID: 0047
Revises: 0046
Create Date: 2026-04-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "aggregate_definitions" in inspector.get_table_names():
        existing_cols = {
            c["name"] for c in inspector.get_columns("aggregate_definitions")
        }
        if "predictive_validated_at" not in existing_cols:
            op.add_column(
                "aggregate_definitions",
                sa.Column(
                    "predictive_validated_at",
                    sa.TIMESTAMP(timezone=True),
                    nullable=True,
                ),
            )

    if "aggregate_lifecycle_events" not in inspector.get_table_names():
        op.create_table(
            "aggregate_lifecycle_events",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "model_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "aggregate_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "aggregate_definitions.id", ondelete="SET NULL"
                ),
                nullable=True,
                index=True,
            ),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("reason", sa.String(64), nullable=True),
            sa.Column(
                "payload",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "occurred_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
                index=True,
            ),
        )
        op.create_index(
            "ix_aggregate_lifecycle_events_model_occurred",
            "aggregate_lifecycle_events",
            ["model_id", "occurred_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "aggregate_lifecycle_events" in inspector.get_table_names():
        op.drop_index(
            "ix_aggregate_lifecycle_events_model_occurred",
            table_name="aggregate_lifecycle_events",
        )
        op.drop_table("aggregate_lifecycle_events")

    if "aggregate_definitions" in inspector.get_table_names():
        existing_cols = {
            c["name"] for c in inspector.get_columns("aggregate_definitions")
        }
        if "predictive_validated_at" in existing_cols:
            op.drop_column(
                "aggregate_definitions", "predictive_validated_at"
            )
