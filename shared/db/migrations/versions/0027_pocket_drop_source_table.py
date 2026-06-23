"""Pocket identity refactor: drop source_table, add predicate_set_hash.

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-21

Pocket identity becomes query-only: ``(model_id, query_fingerprint,
predicate_set_hash)``. The redundant ``source_table`` column is removed
— the source table list is derivable from the parsed ``defining_sql``.

``predicate_set_hash`` is a SHA-256 hex[:64] computed from the sorted
canonical form of the pocket's predicates (column, operator, value).
Two pockets over the same query shape but different filter values are
distinct rows.

DB is clean of pocket artefacts as of 0026, so no data backfill is
needed. A ``server_default=''`` placeholder keeps the NOT NULL column
safe in the unlikely case a stale row exists; the optimizer / API
paths never insert the empty string going forward.
"""
from alembic import op
import sqlalchemy as sa


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pocket_definitions",
        sa.Column(
            "predicate_set_hash",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
    )
    # Drop the transient server_default so future inserts must supply the
    # hash explicitly — otherwise a buggy insert path would silently take
    # "" and then collide on the UNIQUE constraint with an opaque error.
    op.alter_column(
        "pocket_definitions",
        "predicate_set_hash",
        server_default=None,
    )
    op.create_unique_constraint(
        "uq_pocket_model_fp_predhash",
        "pocket_definitions",
        ["model_id", "query_fingerprint", "predicate_set_hash"],
    )
    op.drop_column("pocket_definitions", "source_table")


def downgrade() -> None:
    op.add_column(
        "pocket_definitions",
        sa.Column(
            "source_table",
            sa.String(512),
            nullable=False,
            server_default="",
        ),
    )
    op.drop_constraint(
        "uq_pocket_model_fp_predhash",
        "pocket_definitions",
        type_="unique",
    )
    op.drop_column("pocket_definitions", "predicate_set_hash")
