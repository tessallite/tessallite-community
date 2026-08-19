"""Bug-8411 — per-project agent webhook event subscriptions.

Adds ``project_agent_configs.webhook_event_filters`` (JSONB) so a project can
choose which agent events its webhook receiver gets, instead of receiving all
five unconditionally. Values are validated by the API against
``shared/webhooks/agent_event_types.py``; ``["*"]`` means everything.

Upgrade posture — deliberately NULLABLE with a server default:

* New rows get ``["*"]`` from the server default, i.e. exactly the behaviour
  every project had before filters existed.
* Existing rows are NOT backfilled. ``agent_event_subscribed`` reads NULL as
  "deliver everything", so an already-configured receiver cannot silently
  stop receiving events because of this upgrade — and a NULL is
  distinguishable from a deliberate ``["*"]``, which a backfill would erase.
  Backfilling would also rewrite every row of a table with no benefit.

The mirror-image mistake on the platform-wide sibling is worth restating
because this column must never repeat it: Bug-7330 coerced an EMPTY
``event_filters`` list to match-all, so a subscriber who deselected every
event kept receiving all of them. Here the API rejects an empty list at
write time and ``agent_event_subscribed`` honours an empty list as
"nothing", never as "everything".

Tenant-schema guarded (skip when the schema has no ``project_agent_configs``
table), idempotent, reversible.

Revision ID: 0185
Revises: 0184
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0185"
down_revision = "0184"
branch_labels = None
depends_on = None

_TABLE = "project_agent_configs"
_COLUMN = "webhook_event_filters"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _has_column(_TABLE, _COLUMN):
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("'[\"*\"]'::jsonb"),
        ),
    )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
