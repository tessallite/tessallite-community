"""Add derived-grain artifact manifest fields to aggregates + pockets (Bug-7359).

Spec: architecture_derived-grain-aggregate-routing.md §5.2/§5.3/§7.6.3 (Phase 3).
Immutable build metadata describing what each artifact actually materialised:

  aggregate_definitions:
    grain_keys            JSONB   ordered MaterializedGrainKey list
    attribute_edges       JSONB   MaterializedAttributeEdge list (carried edges)
    passenger_columns     JSONB   detail passengers carried beside the key
    active_refresh_run_id UUID    live pointer to the run the manifest describes
                                  (FK -> aggregate_refresh_runs, SET NULL)
  pocket_definitions:
    row_manifest          JSONB   versioned row-population manifest
    active_refresh_run_id UUID    live pointer (FK -> pocket_refresh_runs, SET NULL)

Serving stays SHADOW-ONLY through Phase 4: no route reads these yet. The
active_refresh_run_id pointer is live state — snapshot-excluded and cleared on
import/clone — so a rehydrated definition re-earns trust (§5.3, I8).

The migration is tenant-schema guarded (skip when the schema has no
``aggregate_definitions`` table) and idempotent (skip a column that already
exists), matching the 0160/0164 add-column guard pattern. Fully reversible.

Revision ID: 0165
Revises: 0164
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0165"
down_revision = "0164"
branch_labels = None
depends_on = None


_AGG = "aggregate_definitions"
_AGG_RUN = "aggregate_refresh_runs"
_POCKET = "pocket_definitions"
_POCKET_RUN = "pocket_refresh_runs"

# New columns keyed by table. active_refresh_run_id is added separately because
# it carries a named FK constraint.
_AGG_JSON_COLS = ("grain_keys", "attribute_edges", "passenger_columns")
_POCKET_JSON_COLS = ("row_manifest",)

_AGG_FK = "fk_aggregate_definitions_active_refresh_run"
_POCKET_FK = "fk_pocket_definitions_active_refresh_run"


def _existing_columns(inspector: sa.engine.reflection.Inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    # Guard: only tenant meta schemas that carry the aggregate/pocket tables.
    if _AGG not in table_names:
        return

    agg_cols = _existing_columns(inspector, _AGG)
    for col in _AGG_JSON_COLS:
        if col not in agg_cols:
            op.add_column(_AGG, sa.Column(col, JSONB(), nullable=True))
    if "active_refresh_run_id" not in agg_cols:
        op.add_column(
            _AGG, sa.Column("active_refresh_run_id", UUID(as_uuid=True), nullable=True)
        )
        if _AGG_RUN in table_names:
            op.create_foreign_key(
                _AGG_FK, _AGG, _AGG_RUN,
                ["active_refresh_run_id"], ["id"], ondelete="SET NULL",
            )

    if _POCKET in table_names:
        pocket_cols = _existing_columns(inspector, _POCKET)
        for col in _POCKET_JSON_COLS:
            if col not in pocket_cols:
                op.add_column(_POCKET, sa.Column(col, JSONB(), nullable=True))
        if "active_refresh_run_id" not in pocket_cols:
            op.add_column(
                _POCKET,
                sa.Column("active_refresh_run_id", UUID(as_uuid=True), nullable=True),
            )
            if _POCKET_RUN in table_names:
                op.create_foreign_key(
                    _POCKET_FK, _POCKET, _POCKET_RUN,
                    ["active_refresh_run_id"], ["id"], ondelete="SET NULL",
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if _AGG not in table_names:
        return

    agg_cols = _existing_columns(inspector, _AGG)
    agg_fks = {fk["name"] for fk in inspector.get_foreign_keys(_AGG)}
    if "active_refresh_run_id" in agg_cols:
        if _AGG_FK in agg_fks:
            op.drop_constraint(_AGG_FK, _AGG, type_="foreignkey")
        op.drop_column(_AGG, "active_refresh_run_id")
    for col in _AGG_JSON_COLS:
        if col in agg_cols:
            op.drop_column(_AGG, col)

    if _POCKET in table_names:
        pocket_cols = _existing_columns(inspector, _POCKET)
        pocket_fks = {fk["name"] for fk in inspector.get_foreign_keys(_POCKET)}
        if "active_refresh_run_id" in pocket_cols:
            if _POCKET_FK in pocket_fks:
                op.drop_constraint(_POCKET_FK, _POCKET, type_="foreignkey")
            op.drop_column(_POCKET, "active_refresh_run_id")
        for col in _POCKET_JSON_COLS:
            if col in pocket_cols:
                op.drop_column(_POCKET, col)
