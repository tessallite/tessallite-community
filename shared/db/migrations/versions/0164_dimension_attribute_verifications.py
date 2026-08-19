"""Add dimension_attribute_verifications for derived-grain routing (Bug-7359).

Spec: architecture_derived-grain-aggregate-routing.md §5.3 / §7.6. Append-only
complete-data verification evidence for a declared attribute relationship. This
is LIVE OPERATIONAL STATE (tied to a deployed version + physical refresh run),
NOT model content — it is deliberately excluded from model snapshots.

Phase 2 records evidence but authorises NO serving route. The migration is
tenant-schema guarded (skip when the schema has no
``dimension_attribute_relationships`` table) and idempotent (skip when the table
exists), matching the 0159/0162/0163 guard pattern.

Revision ID: 0164
Revises: 0163
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None


_TABLE = "dimension_attribute_verifications"
_PARENT = "dimension_attribute_relationships"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    # Parent table present => tenant meta schema with the Phase-1b relationships.
    if _PARENT not in table_names:
        return
    if _TABLE in table_names:
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("relationship_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("verifier_version", sa.String(length=64), nullable=False),
        sa.Column("declaration_hash", sa.String(length=64), nullable=False),
        sa.Column("deployed_version_id", UUID(as_uuid=True), nullable=True),
        sa.Column("deploy_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "scope_kind", sa.String(length=32), nullable=False,
            server_default="TENANT_GLOBAL",
        ),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("source_data_version", sa.String(length=128), nullable=True),
        sa.Column(
            "artifact_kind", sa.String(length=32), nullable=False,
            server_default="DEPLOY_CHECK",
        ),
        sa.Column("artifact_id", UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_refresh_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "checked_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("violation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["relationship_id"], [f"{_PARENT}.id"], ondelete="CASCADE",
        ),
        # deployed_version_id FK is SET NULL so pruning an old version does not
        # delete the evidence history (which is health/audit).
        sa.ForeignKeyConstraint(
            ["deployed_version_id"], ["model_versions.id"], ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "relationship_id", "artifact_refresh_run_id", "declaration_hash",
            "verifier_version",
            name="uq_dim_attr_verif_run_decl_verifier",
        ),
    )
    op.create_index(
        "ix_dimension_attribute_verifications_relationship_id",
        _TABLE, ["relationship_id"],
    )
    op.create_index(
        "ix_dimension_attribute_verifications_deployed_version_id",
        _TABLE, ["deployed_version_id"],
    )
    op.create_index(
        "ix_dimension_attribute_verifications_artifact_id",
        _TABLE, ["artifact_id"],
    )
    op.create_index(
        "ix_dim_attr_verif_relationship_checked",
        _TABLE, ["relationship_id", "checked_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if _TABLE not in table_names:
        return
    for ix in (
        "ix_dim_attr_verif_relationship_checked",
        "ix_dimension_attribute_verifications_artifact_id",
        "ix_dimension_attribute_verifications_deployed_version_id",
        "ix_dimension_attribute_verifications_relationship_id",
    ):
        op.drop_index(ix, table_name=_TABLE)
    op.drop_table(_TABLE)
