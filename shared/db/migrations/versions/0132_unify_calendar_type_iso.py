"""Unify calendar_type vocabulary: migrate hierarchy 'iso' -> 'iso_week'.

F-016-04: before H9 the hierarchy enum used the legacy token ``iso`` while
calendar tables used ``iso_week``. A hierarchy typed ``iso`` never aligned
with an ``iso_week`` calendar table. The application now normalises ``iso`` to
``iso_week`` on write; this migration converges any rows persisted before the
fix so the two sides share one vocabulary.

Revision ID: 0132
Revises: 0131
Create Date: 2026-06-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE hierarchy_definitions SET calendar_type = 'iso_week' "
            "WHERE calendar_type = 'iso'"
        )
    )


def downgrade() -> None:
    # Best-effort reversal: map the unified token back to the legacy hierarchy
    # spelling. Calendar tables already used 'iso_week' independently, so this
    # only touches hierarchy rows.
    op.execute(
        sa.text(
            "UPDATE hierarchy_definitions SET calendar_type = 'iso' "
            "WHERE calendar_type = 'iso_week'"
        )
    )
