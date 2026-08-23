"""Add FK constraints to kpis.target_measure_id and kpis.time_dimension_id.

Bug-6673: these columns reference measures.id and dimensions.id respectively
but had no foreign key constraint. Deleting the referenced measure or dimension
left a dangling UUID, breaking the KPI silently at read time. Adding
``ondelete=SET NULL`` FKs ensures the DB NULLs the column automatically on
cascade-delete, matching the ``parent_kpi_id`` / ``replacement_id`` columns on
the same table.

Both ``kpis`` and the referenced tables (``measures``, ``dimensions``) are
tenant-scoped (``TenantBase``), so this migration is a no-op on the
``tess_system`` DB. The migration is safe for existing data: any rows whose
target_measure_id or time_dimension_id already reference a deleted entity
are NULLed out before the FK is created to avoid constraint violations.

Revision ID: 0157
Revises: 0156
Create Date: 2026-07-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0157"
down_revision = "0156"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only tables; no-op on the tess_system DB.
    if "kpis" not in table_names:
        return

    existing_fks = {
        fk["name"] for fk in inspector.get_foreign_keys("kpis") if fk.get("name")
    }

    # --- target_measure_id -> measures.id ---
    fk_target = "fk_kpis_target_measure_id_measures"
    if fk_target not in existing_fks:
        # Backfill: NULL out any dangling references before creating the FK.
        bind.execute(
            sa.text(
                "UPDATE kpis SET target_measure_id = NULL "
                "WHERE target_measure_id IS NOT NULL "
                "AND target_measure_id NOT IN (SELECT id FROM measures)"
            )
        )
        op.create_foreign_key(
            fk_target,
            "kpis",
            "measures",
            ["target_measure_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # --- time_dimension_id -> dimensions.id ---
    fk_time = "fk_kpis_time_dimension_id_dimensions"
    if fk_time not in existing_fks:
        # Backfill: NULL out any dangling references before creating the FK.
        bind.execute(
            sa.text(
                "UPDATE kpis SET time_dimension_id = NULL "
                "WHERE time_dimension_id IS NOT NULL "
                "AND time_dimension_id NOT IN (SELECT id FROM dimensions)"
            )
        )
        op.create_foreign_key(
            fk_time,
            "kpis",
            "dimensions",
            ["time_dimension_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "kpis" not in table_names:
        return

    existing_fks = {
        fk["name"] for fk in inspector.get_foreign_keys("kpis") if fk.get("name")
    }

    fk_target = "fk_kpis_target_measure_id_measures"
    if fk_target in existing_fks:
        op.drop_constraint(fk_target, "kpis", type_="foreignkey")

    fk_time = "fk_kpis_time_dimension_id_dimensions"
    if fk_time in existing_fks:
        op.drop_constraint(fk_time, "kpis", type_="foreignkey")
