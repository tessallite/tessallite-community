"""Add Model.dependency_revision for impact-analysis revision safety (Bug-7787).

Spec: strategy_model-impact-analysis.md §6.3. A monotonically increasing
BIGINT bumped by the shared ``dependency_mutation`` helper on every mutation that
can add, remove, rename, or rebind a dependency edge. The what-if preview returns
it as an optimistic token; the destructive request sends the expected value and
the server recomputes under lock, returning IMPACT_REVISION_STALE on mismatch.

It is DRAFT control metadata, not the deployed runtime version. Save/rehydrate/
import sets it to the imported value or zero and increments once after the
transaction (§6.3).

Tenant-schema guarded (skip when the schema has no ``models`` table) and
idempotent (skip if the column already exists), matching the 0165 add-column
guard pattern. Fully reversible.

Revision ID: 0166
Revises: 0165
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0166"
down_revision = "0165"
branch_labels = None
depends_on = None

_MODELS = "models"
_COL = "dependency_revision"


def _existing_columns(inspector: sa.engine.reflection.Inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _MODELS not in set(inspector.get_table_names()):
        return
    if _COL in _existing_columns(inspector, _MODELS):
        return
    op.add_column(
        _MODELS,
        sa.Column(
            _COL,
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _MODELS not in set(inspector.get_table_names()):
        return
    if _COL in _existing_columns(inspector, _MODELS):
        op.drop_column(_MODELS, _COL)
