"""Bug-8768 - persist the aggregate incremental append-only contract.

Existing refresh policies remain fail-closed. A policy may use the windowed
incremental path only after a modeller explicitly selects the append-only mode.
The optional full-rebuild interval periodically corrects drift if that source
contract is later violated.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None

_TABLE = "aggregate_refresh_policies"


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return False
    return any(column["name"] == name for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    if not _has_column("incremental_append_only"):
        op.add_column(
            _TABLE,
            sa.Column(
                "incremental_append_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if not _has_column("full_rebuild_interval_days"):
        op.add_column(
            _TABLE,
            sa.Column("full_rebuild_interval_days", sa.Integer(), nullable=True),
        )
        op.create_check_constraint(
            "ck_aggregate_refresh_policy_full_rebuild_interval_positive",
            _TABLE,
            "full_rebuild_interval_days IS NULL OR full_rebuild_interval_days > 0",
        )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    if _has_column("full_rebuild_interval_days"):
        op.drop_constraint(
            "ck_aggregate_refresh_policy_full_rebuild_interval_positive",
            _TABLE,
            type_="check",
        )
        op.drop_column(_TABLE, "full_rebuild_interval_days")
    if _has_column("incremental_append_only"):
        op.drop_column(_TABLE, "incremental_append_only")
