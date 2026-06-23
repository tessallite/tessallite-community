"""Drop pocket_definitions.refresh_event_name; strip event-driven path.

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-19

The event-driven refresh policy in the original spec was never
implemented end-to-end — the scheduler's ``refresh_due_pockets`` only
honours ``schedule`` and ``manual``.  We keep only those two modes and
drop the column so we're not carrying dead storage + confusing the UI.

A belt-and-braces UPDATE first coerces any ``event`` rows to ``manual``
(they couldn't be refreshed anyway).
"""
from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE pocket_definitions SET refresh_policy = 'manual' "
            "WHERE refresh_policy = 'event'"
        )
    )
    op.drop_column("pocket_definitions", "refresh_event_name")


def downgrade() -> None:
    op.add_column(
        "pocket_definitions",
        sa.Column("refresh_event_name", sa.String(length=128), nullable=True),
    )
