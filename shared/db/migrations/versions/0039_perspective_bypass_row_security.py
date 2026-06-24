"""Perspective bypass_row_security flag — Phase 8.C.1.

Revision ID: 0039
Revises: 0038
Create Date: 2026-04-24

Adds ``perspectives.bypass_row_security BOOLEAN DEFAULT FALSE``.
When true, the Query Router skips the Phase 5.1 row-security wrap for
any execution bound to this perspective. Every other security control
(perspective allow list, audience-role gating) still applies.

No dedicated role (per Q-X2.1=c): any model-owner may toggle the flag.
No dedicated audit table (per Q-X2.2=c): the router tags bypassed
executions with a ``perspective_bypass_row_security=true`` structured
log field so operators can filter existing request logs for audit.

Plan: ``work/phase-8-action-plan.md`` §8.C.1.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0039"
down_revision = "0038"
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
    if _column_exists("perspectives", "bypass_row_security"):
        return
    op.add_column(
        "perspectives",
        sa.Column(
            "bypass_row_security",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    if not _column_exists("perspectives", "bypass_row_security"):
        return
    op.drop_column("perspectives", "bypass_row_security")
