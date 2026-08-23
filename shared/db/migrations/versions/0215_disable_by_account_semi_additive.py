"""Wave 2 scope enforcement (#10): by_account semi-additive is not supported.

``by_account`` was removed from ``VALID_SEMI_ADDITIVE_BEHAVIORS`` so the forward
create/update API now rejects it and the rehydrate/import boundary
(``rehydrator._validate_measure_enums``) imports an already-persisted by_account
measure as a DISABLED measure. This migration applies the SAME mechanism to
measures already live in a tenant's ``measures`` table: any row still carrying
``semi_additive_behavior = 'by_account'`` is flagged invalid (``is_invalid`` +
``invalid_reason``) and its behaviour NULLed — exactly what the rehydrate
boundary does for an unresolvable token — so it surfaces in the builder's
invalid-measure chip and the Model Health panel instead of silently persisting
an unsupported behaviour the query rewriter fails loud on.

No rows are deleted. Idempotent: a re-run only touches rows that still carry the
token, and never overwrites an ``invalid_reason`` already set for another cause.

This is a TENANT-chain migration (measures live in ``<slug>_meta``); table names
are unqualified and resolve against the per-tenant ``search_path`` the runner
sets, exactly like 0214. It revises tenant head 0214. The system branch remains
at 0212.

Revision ID: 0215
Revises: 0214
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0215"
down_revision = "0214"
branch_labels = None
depends_on = None

_TABLE = "measures"
_REQUIRED_COLS = {"semi_additive_behavior", "is_invalid", "invalid_reason"}
_REASON = (
    "The 'by_account' semi-additive behaviour is no longer supported. This "
    "measure was imported as disabled; choose a supported semi-additive "
    "behaviour (last_non_empty, first_non_empty, avg_of_children, min, max) "
    "and re-enable it."
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if not _REQUIRED_COLS <= cols:
        # A schema too old to carry the invalid-measure columns cannot surface
        # the flag; nothing to backfill.
        return
    op.get_bind().execute(
        sa.text(
            f"UPDATE {_TABLE} SET "
            "is_invalid = true, "
            "invalid_reason = COALESCE(invalid_reason, :reason), "
            "semi_additive_behavior = NULL "
            "WHERE lower(semi_additive_behavior) = 'by_account'"
        ),
        {"reason": _REASON},
    )


def downgrade() -> None:
    # Irreversible by design: the removed per-account behaviour is not
    # recoverable (the token is gone from the product), so the safe state is to
    # leave the affected measures flagged invalid. No-op.
    pass
