"""Pocket refresh policies — separate schedule record per pocket.

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-20

Creates ``pocket_refresh_policies`` (1:1 with ``pocket_definitions``),
mirroring ``aggregate_refresh_policies``.  Pockets only support full
refresh, so no ``refresh_mode`` / ``incremental_*`` columns.

``is_enabled`` lives here rather than on ``pocket_definitions`` so that
the pocket's own lifecycle (fresh / retired / deleted) stays
independent of whether its refresh schedule is active.

Idempotent back-fill: every existing pocket with a non-empty
``refresh_cron`` gets a policy row carrying that cron with
``is_enabled = (refresh_policy = 'schedule')``.  ``ON CONFLICT DO
NOTHING`` so re-running against a partially-migrated schema is a no-op.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pocket_refresh_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "pocket_definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pocket_definitions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cron_expression", sa.String(128)),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.execute(
        """
        INSERT INTO pocket_refresh_policies
            (pocket_definition_id, cron_expression, is_enabled)
        SELECT id,
               refresh_cron,
               (COALESCE(refresh_policy, '') = 'schedule')
          FROM pocket_definitions
         WHERE refresh_cron IS NOT NULL AND refresh_cron <> ''
        ON CONFLICT (pocket_definition_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("pocket_refresh_policies")
