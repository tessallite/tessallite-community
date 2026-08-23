"""Bug-8615 phase G1 - join population participation flag + deploy-time evidence.

Two tenant-schema changes:

1. ``joins.population_participation`` - the modeller-declared answer to "does
   this join's row-filtering / row-multiplying effect define this model's
   population?" (contract 2 of
   ``docs/architecture/architecture_join-population-governance.md``).

   NOT NULL with server default ``'preserve_base_rows'``. Every pre-existing
   row therefore receives the value that means "may still be elided, exactly as
   before", so this migration changes NO served numbers. The server default is
   permanent, not a backfill-only convenience: the snapshot rehydrate path
   issues a raw ``INSERT`` built from the snapshot dict, and a snapshot saved
   before this column existed simply omits the key.

2. ``join_population_checks`` - one CURRENT deploy-time verdict per join
   (``join_id`` UNIQUE, the same shape ``source_join_statistics`` uses).
   Live operational state bound to a deployed version + deploy epoch, NOT model
   content: it is excluded from model snapshots and cascade-deletes with its
   join and its model.

Warn-only: nothing here blocks a deploy. Block mode is governance plan phase G5.

Revision ID: 0190
Revises: 0188
Create Date: 2026-08-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0190"
down_revision = "0188"
branch_labels = None
depends_on = None

_JOINS = "joins"
_PARTICIPATION = "population_participation"
_DEFAULT_PARTICIPATION = "preserve_base_rows"
_CHECKS = "join_population_checks"


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        c["name"] == column
        for c in sa.inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    if _table_exists(_JOINS) and not _has_column(_JOINS, _PARTICIPATION):
        op.add_column(
            _JOINS,
            sa.Column(
                _PARTICIPATION,
                sa.String(32),
                nullable=False,
                server_default=sa.text(f"'{_DEFAULT_PARTICIPATION}'"),
            ),
        )

    if not _table_exists(_CHECKS):
        op.create_table(
            _CHECKS,
            sa.Column(
                "id", postgresql.UUID(as_uuid=True),
                primary_key=True, nullable=False,
            ),
            sa.Column(
                "join_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("joins.id", ondelete="CASCADE"),
                nullable=False, unique=True,
            ),
            sa.Column(
                "model_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("models.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "deployed_version_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("model_versions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "deploy_epoch", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("classification", sa.String(16), nullable=False),
            sa.Column(_PARTICIPATION, sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column(
                "measured", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("row_loss_ratio", sa.Float(), nullable=True),
            sa.Column("row_mult_ratio", sa.Float(), nullable=True),
            sa.Column("row_effect_ratio", sa.Float(), nullable=True),
            sa.Column("reason", sa.String(64), nullable=True),
            sa.Column("inputs_fingerprint", sa.String(64), nullable=True),
            sa.Column(
                "checked_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
        )
        op.create_index(
            f"ix_{_CHECKS}_model_id", _CHECKS, ["model_id"],
        )


def downgrade() -> None:
    if _table_exists(_CHECKS):
        op.drop_table(_CHECKS)
    if _table_exists(_JOINS) and _has_column(_JOINS, _PARTICIPATION):
        op.drop_column(_JOINS, _PARTICIPATION)
