"""Add dimension_attribute_relationships for derived-grain routing (Bug-7359).

Spec: architecture_derived-grain-aggregate-routing.md §5.3. A modeller may declare
several key-to-detail relationships per dimension (name, ISO code, phone key), each
with a BIJECTION or FUNCTIONAL_N_TO_1 cardinality. This table persists the
DECLARATION only — Phase 1b. Verification evidence and active-run pointers are
NOT here; they are live operational state added by a later phase (§5.3).

This entity is unused at serving time in this phase, so it changes no query path.
The migration is tenant-schema guarded (skip when the schema has no ``dimensions``
table, i.e. it is not a tenant meta schema) and idempotent (skip when the table
already exists), matching the 0159/0162 guard pattern.

Revision ID: 0163
Revises: 0162
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None


_TABLE = "dimension_attribute_relationships"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    # ``dimensions`` present => this is a tenant meta schema. System / non-meta
    # schemas have no dimensions table; skip so the migration is a no-op there.
    if "dimensions" not in table_names:
        return
    if _TABLE in table_names:
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), nullable=False),
        sa.Column("dimension_id", UUID(as_uuid=True), nullable=False),
        # key/detail columns are SET NULL on physical-column delete so the
        # declaration survives as a broken edge rather than disappearing.
        sa.Column("key_column_id", UUID(as_uuid=True), nullable=True),
        sa.Column("detail_column_id", UUID(as_uuid=True), nullable=True),
        sa.Column("cardinality", sa.String(length=32), nullable=False),
        sa.Column(
            "null_policy", sa.String(length=32), nullable=False,
            server_default="REJECT_NULL",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column("declaration_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dimension_id"], ["dimensions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["key_column_id"], ["model_columns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["detail_column_id"], ["model_columns.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "dimension_id", "detail_column_id", "cardinality",
            name="uq_dim_attr_rel_dimension_detail_cardinality",
        ),
    )
    op.create_index(
        "ix_dimension_attribute_relationships_model_id", _TABLE, ["model_id"],
    )
    op.create_index(
        "ix_dimension_attribute_relationships_dimension_id", _TABLE, ["dimension_id"],
    )
    op.create_index(
        "ix_dimension_attribute_relationships_key_column_id", _TABLE, ["key_column_id"],
    )
    op.create_index(
        "ix_dimension_attribute_relationships_detail_column_id", _TABLE, ["detail_column_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if _TABLE not in table_names:
        return
    for ix in (
        "ix_dimension_attribute_relationships_detail_column_id",
        "ix_dimension_attribute_relationships_key_column_id",
        "ix_dimension_attribute_relationships_dimension_id",
        "ix_dimension_attribute_relationships_model_id",
    ):
        op.drop_index(ix, table_name=_TABLE)
    op.drop_table(_TABLE)
