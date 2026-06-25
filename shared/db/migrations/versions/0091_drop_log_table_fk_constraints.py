"""Drop FK constraints from log tables (query_logs, query_miss_logs, route_logs).

Log tables should not enforce referential integrity — SET NULL on model
deletion caused unique-constraint collisions (Bug-411).
"""
from alembic import op
import sqlalchemy as sa

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def _fk_exists(table: str, constraint: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.table_constraints"
            "  WHERE constraint_name = :c AND table_name = :t"
            "    AND table_schema = current_schema()"
            "    AND constraint_type = 'FOREIGN KEY'"
            ")"
        ),
        {"c": constraint, "t": table},
    )
    return result.scalar()


_FK_DROPS = [
    ("query_logs", "query_logs_model_id_fkey"),
    ("query_logs", "query_logs_aggregate_id_fkey"),
    ("query_logs", "query_logs_pocket_id_fkey"),
    ("query_logs", "query_logs_persona_id_fkey"),
    ("query_miss_logs", "query_miss_logs_model_id_fkey"),
    ("query_miss_logs", "query_miss_logs_candidate_aggregate_id_fkey"),
    ("query_miss_logs", "query_miss_logs_persona_id_fkey"),
    ("route_logs", "route_logs_query_log_id_fkey"),
]

_FK_RESTORE = [
    ("query_logs", "query_logs_model_id_fkey",
     "models", ["model_id"], ["id"], "SET NULL"),
    ("query_logs", "query_logs_aggregate_id_fkey",
     "aggregate_definitions", ["aggregate_id"], ["id"], "SET NULL"),
    ("query_logs", "query_logs_pocket_id_fkey",
     "pocket_definitions", ["pocket_id"], ["id"], "SET NULL"),
    ("query_logs", "query_logs_persona_id_fkey",
     "personas", ["persona_id"], ["id"], "SET NULL"),
    ("query_miss_logs", "query_miss_logs_model_id_fkey",
     "models", ["model_id"], ["id"], "SET NULL"),
    ("query_miss_logs", "query_miss_logs_candidate_aggregate_id_fkey",
     "aggregate_definitions", ["candidate_aggregate_id"], ["id"], "SET NULL"),
    ("query_miss_logs", "query_miss_logs_persona_id_fkey",
     "personas", ["persona_id"], ["id"], "SET NULL"),
    ("route_logs", "route_logs_query_log_id_fkey",
     "query_logs", ["query_log_id"], ["id"], "CASCADE"),
]


def upgrade() -> None:
    for table, constraint in _FK_DROPS:
        if _fk_exists(table, constraint):
            op.drop_constraint(constraint, table, type_="foreignkey")


def downgrade() -> None:
    for table, constraint, ref_table, local_cols, remote_cols, ondelete in _FK_RESTORE:
        if not _fk_exists(table, constraint):
            op.create_foreign_key(
                constraint, table, ref_table,
                local_cols, remote_cols,
                ondelete=ondelete,
            )
