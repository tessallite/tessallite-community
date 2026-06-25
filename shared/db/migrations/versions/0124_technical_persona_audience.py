"""Gate seeded Technical personas behind the model_technical audience role.

F-008-04: migration 0042 seeded one ``slug='technical'`` persona per model
with ``audience_roles='[]'``. The persona resolver treats an empty audience
list as "available to everyone", and a hidden-columns persona resolves with
priority — so every non-privileged user was force-locked to the technical
(hidden-columns) view and every business-persona pick returned 403.

The resolver now requires an explicit audience-role match for
hidden-columns personas; this migration gives the seeded rows that
explicit gate (``model_technical``) so genuine technical users keep the
auto-resolve behaviour by being granted that role.

Revision ID: 0124
Revises: 0123
Create Date: 2026-06-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _table_exists("personas"):
        return
    op.get_bind().execute(
        sa.text(
            """
            UPDATE personas
            SET audience_roles = '["model_technical"]'::jsonb
            WHERE includes_hidden_columns = true
              AND slug = 'technical'
              AND audience_roles = '[]'::jsonb
            """
        )
    )


def downgrade() -> None:
    if not _table_exists("personas"):
        return
    op.get_bind().execute(
        sa.text(
            """
            UPDATE personas
            SET audience_roles = '[]'::jsonb
            WHERE includes_hidden_columns = true
              AND slug = 'technical'
              AND audience_roles = '["model_technical"]'::jsonb
            """
        )
    )
