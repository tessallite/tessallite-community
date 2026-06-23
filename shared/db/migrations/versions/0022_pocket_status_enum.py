"""Constrain pocket_definitions.status to the spec state machine.

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-19

The spec (``docs/architecture/architecture_pocket-tables.md``) defines the status lifecycle as
``fresh | stale | invalidating``; we also keep ``failed`` for the
operational case where a refresh raised an exception (failure is not
the same as staleness — the row is no longer usable, but TTL-eviction
logic still needs to see it). Retirement is tracked via
``retired_at IS NOT NULL`` instead of a dedicated ``dropped`` status
so eviction is idempotent and the status column carries one meaning.

Data migration:

* ``dropped`` rows → set ``retired_at`` if null, status becomes
  ``stale`` (harmless because ``retired_at IS NOT NULL`` is the
  authoritative "retired" signal and those rows are excluded from all
  active paths).
* Any other unexpected value → coerced to ``stale``.

Then a CHECK constraint pins status to the four allowed values.
"""
from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


ALLOWED = ("fresh", "stale", "invalidating", "failed")


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE pocket_definitions
               SET retired_at = COALESCE(retired_at, now()),
                   status = 'stale'
             WHERE status = 'dropped'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE pocket_definitions
               SET status = 'stale'
             WHERE status NOT IN ('fresh', 'stale', 'invalidating', 'failed')
            """
        )
    )
    op.create_check_constraint(
        "ck_pocket_definitions_status",
        "pocket_definitions",
        "status IN ('fresh', 'stale', 'invalidating', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pocket_definitions_status",
        "pocket_definitions",
        type_="check",
    )
