"""Bug-8695 — add FK on models.predictive_built_for_version_id → model_versions.id.

``predictive_built_for_version_id`` is per-instance metadata. When a project is
imported, this pointer must be cleared (the target instance has different deployed
versions). Adding ON DELETE SET NULL ensures the column is cleared automatically
when the referenced deployed version row is pruned.

Column already nullable (created nullable in migration 0046).

Revision ID: 0201
Revises: 0200
Create Date: 2026-08-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0201"
down_revision = "0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = inspector.get_table_names()

    if "models" not in table_names or "model_versions" not in table_names:
        return

    # Check if the FK already exists
    existing_fks = {
        fk["name"]
        for fk in inspector.get_foreign_keys("models")
    }
    fk_name = "fk_models_predictive_built_for_version_id_model_versions"
    if fk_name in existing_fks:
        return

    # PRE-FLIGHT: clear pointers whose target version no longer exists.
    #
    # This column has had NO foreign key until now, so nothing has been
    # keeping it honest, and two supported paths orphan it on the DEFAULT
    # configuration:
    #
    #   * ``revert_to_version`` (model-service ``api/versions.py``) deletes
    #     every version NEWER than the target. A model deployed at v5 whose
    #     predictive sweep stamped v5 and is then reverted to v3 is left
    #     pointing at a deleted row.
    #   * ``_prune_old_versions`` retains newest-N plus ``deployed_version_id``
    #     only; ``predictive_built_for_version_id`` is not in the retained set,
    #     so a prune can delete the stamped version.
    #
    # Postgres validates a plain ADD CONSTRAINT immediately against existing
    # rows, so without this cleanup the whole tenant chain aborts with a
    # ForeignKeyViolation and the schema is stranded at 0200. Verified on the
    # seeded acme-demo tenant, where model ``modell`` carries exactly such a
    # dangling pointer.
    #
    # NULLing is also the semantically correct value, not a convenience: a
    # stamp pointing at a destroyed version means "not built", which is
    # precisely what the ON DELETE SET NULL below will write from now on.
    op.execute(
        sa.text(
            "UPDATE models SET predictive_built_for_version_id = NULL "
            "WHERE predictive_built_for_version_id IS NOT NULL "
            "  AND NOT EXISTS ("
            "        SELECT 1 FROM model_versions v "
            "         WHERE v.id = models.predictive_built_for_version_id)"
        )
    )

    op.create_foreign_key(
        fk_name,
        "models",
        "model_versions",
        ["predictive_built_for_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = inspector.get_table_names()

    if "models" not in table_names:
        return

    existing_fks = {
        fk["name"]
        for fk in inspector.get_foreign_keys("models")
    }
    fk_name = "fk_models_predictive_built_for_version_id_model_versions"
    if fk_name in existing_fks:
        op.drop_constraint(fk_name, "models", type_="foreignkey")
