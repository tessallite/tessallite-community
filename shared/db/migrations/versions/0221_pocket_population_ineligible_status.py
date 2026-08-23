"""Allow pockets to park on an unproven join population.

Revision ID: 0221
Revises: 0220
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0221"
down_revision = "0220"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # G4 keeps population proof separate from the physical generation state.
    # Add the columns nullable during upgrade so existing tenants can be
    # backfilled before the NOT NULL/default contract is enforced.
    op.add_column(
        "joins",
        sa.Column(
            "population_participation_source", sa.String(length=16),
            nullable=True, server_default=sa.text("'default'"),
        ),
    )
    op.execute(
        "UPDATE joins SET population_participation_source = 'default' "
        "WHERE population_participation_source IS NULL"
    )
    op.alter_column(
        "joins", "population_participation_source",
        existing_type=sa.String(length=16), nullable=False,
        server_default=sa.text("'default'"),
    )

    for _, column in (
        (
            "population_eligibility",
            sa.Column(
                "population_eligibility", sa.String(length=16),
                nullable=True, server_default=sa.text("'unknown'"),
            ),
        ),
        (
            "population_eligibility_reason",
            sa.Column("population_eligibility_reason", sa.Text(), nullable=True),
        ),
        (
            "population_proof_fingerprint",
            sa.Column("population_proof_fingerprint", sa.String(length=64), nullable=True),
        ),
    ):
        op.add_column("pocket_definitions", column)
    op.execute(
        "UPDATE pocket_definitions SET population_eligibility = 'unknown' "
        "WHERE population_eligibility IS NULL"
    )
    op.alter_column(
        "pocket_definitions", "population_eligibility",
        existing_type=sa.String(length=16), nullable=False,
        server_default=sa.text("'unknown'"),
    )

    # The status check was introduced by 0022. Use IF EXISTS so a fresh or
    # partially provisioned tenant schema remains migratable, then recreate it
    # with the one additional non-serving lifecycle state.
    op.execute(
        "ALTER TABLE pocket_definitions "
        "DROP CONSTRAINT IF EXISTS ck_pocket_definitions_status"
    )
    # Legacy G4 rows used the status token itself. Convert them before the new
    # check is recreated; the physical row remains fresh/stale according to
    # its generation evidence, while the mismatch reason survives durably.
    op.execute(
        "UPDATE pocket_definitions SET population_eligibility = 'ineligible', "
        "population_eligibility_reason = 'Ineligible: population mismatch', "
        "status = 'fresh' WHERE status = 'ineligible'"
    )
    op.create_check_constraint(
        "ck_pocket_definitions_status",
        "pocket_definitions",
        "status IN ('fresh', 'stale', 'invalidating', 'failed', 'ineligible')",
    )
    op.create_check_constraint(
        "ck_pocket_definitions_population_eligibility",
        "pocket_definitions",
        "population_eligibility IN ('unknown', 'eligible', 'ineligible')",
    )
    op.create_check_constraint(
        "ck_joins_population_participation_source",
        "joins",
        "population_participation_source IN ('default', 'manual', 'auto')",
    )


def downgrade() -> None:
    # Do not leave rows violating the previous enum if a development database
    # is downgraded. Ineligible pockets are non-serving, so stale is the safe
    # older representation and the next sweep can rebuild them.
    op.execute(
        "UPDATE pocket_definitions SET status = 'stale' "
        "WHERE status = 'ineligible' "
        "OR population_eligibility = 'ineligible'"
    )
    op.execute(
        "ALTER TABLE pocket_definitions "
        "DROP CONSTRAINT IF EXISTS ck_pocket_definitions_status"
    )
    op.create_check_constraint(
        "ck_pocket_definitions_status",
        "pocket_definitions",
        "status IN ('fresh', 'stale', 'invalidating', 'failed')",
    )
    op.execute(
        "ALTER TABLE pocket_definitions "
        "DROP CONSTRAINT IF EXISTS ck_pocket_definitions_population_eligibility"
    )
    op.execute(
        "ALTER TABLE joins "
        "DROP CONSTRAINT IF EXISTS ck_joins_population_participation_source"
    )
    op.drop_column("pocket_definitions", "population_proof_fingerprint")
    op.drop_column("pocket_definitions", "population_eligibility_reason")
    op.drop_column("pocket_definitions", "population_eligibility")
    op.drop_column("joins", "population_participation_source")
