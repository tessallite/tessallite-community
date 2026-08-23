"""Bind join-population evidence to the selected deployed snapshot.

Revision 0190 made ``join_population_checks.join_id`` a foreign key to the
mutable ``joins`` table.  That was correct for the original live-only health
reader, but it makes an allowed historical deploy fail when the selected join
has since been deleted from the draft.  G5 evidence is version-bound
operational state, so the join id is retained as a stable snapshot identity
without a live FK.  New rows also retain the selected endpoint labels for
diagnostics when the draft graph no longer contains the join.

The new label columns are nullable because existing evidence predates this
revision.  Every writer after 0222 populates them from the selected graph;
health uses the deployed snapshot as the definition authority and can still
render old rows from that snapshot.

Revision ID: 0222
Revises: 0221
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0222"
down_revision = "0221"
branch_labels = None
depends_on = None

_CHECKS = "join_population_checks"
_LABEL_COLUMNS = {
    "join_label": sa.String(length=1024),
    "left_table_name": sa.String(length=255),
    "right_table_name": sa.String(length=255),
    "left_column_name": sa.String(length=255),
    "right_column_name": sa.String(length=255),
}


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        item["name"] == column
        for item in sa.inspect(op.get_bind()).get_columns(table)
    )


def _join_foreign_keys() -> list[dict]:
    if not _table_exists(_CHECKS):
        return []
    return [
        fk for fk in sa.inspect(op.get_bind()).get_foreign_keys(_CHECKS)
        if fk.get("referred_table") == "joins"
        and set(fk.get("constrained_columns") or ()) == {"join_id"}
    ]


def upgrade() -> None:
    if not _table_exists(_CHECKS):
        return

    # Drop the live-draft FK by the name Alembic/PostgreSQL actually assigned,
    # rather than assuming a generated name.  This also handles tenants that
    # were created with a different naming convention.
    for fk in _join_foreign_keys():
        if fk.get("name"):
            op.drop_constraint(fk["name"], _CHECKS, type_="foreignkey")

    for name, column_type in _LABEL_COLUMNS.items():
        if not _has_column(_CHECKS, name):
            op.add_column(
                _CHECKS,
                sa.Column(name, column_type, nullable=True),
            )


def downgrade() -> None:
    if not _table_exists(_CHECKS):
        return

    # Evidence for a deleted historical join cannot be represented by the
    # pre-0222 schema.  Remove only those orphan rows before restoring its FK;
    # current live evidence remains intact.  The downgrade is therefore
    # structurally reversible while explicitly discarding data the old schema
    # has no way to retain.
    if not _join_foreign_keys():
        op.execute(
            sa.text(
                "DELETE FROM join_population_checks c "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM joins j WHERE j.id = c.join_id"
                ")"
            )
        )
        op.create_foreign_key(
            "join_population_checks_join_id_fkey",
            _CHECKS,
            "joins",
            ["join_id"],
            ["id"],
            ondelete="CASCADE",
        )

    for name in _LABEL_COLUMNS:
        if _has_column(_CHECKS, name):
            op.drop_column(_CHECKS, name)
