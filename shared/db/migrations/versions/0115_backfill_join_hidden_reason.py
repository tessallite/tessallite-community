"""One-off backfill: hide dimension-side join key columns on existing joins."""
from alembic import op
import sqlalchemy as sa

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find all dimension-side join key columns from existing fact-to-dim joins.
    # Case 1: left table is fact, right table is dim_* → hide right_column_id
    # Case 2: right table is fact, left table is dim_* → hide left_column_id
    rows = conn.execute(sa.text("""
        SELECT
            CASE
                WHEN lt.table_type = 'fact' AND rt.table_type LIKE 'dim_%'
                    THEN j.right_column_id
                WHEN rt.table_type = 'fact' AND lt.table_type LIKE 'dim_%'
                    THEN j.left_column_id
            END AS dim_column_id
        FROM joins j
        JOIN model_tables lt ON lt.id = j.left_table_id
        JOIN model_tables rt ON rt.id = j.right_table_id
        WHERE (lt.table_type = 'fact' AND rt.table_type LIKE 'dim_%')
           OR (rt.table_type = 'fact' AND lt.table_type LIKE 'dim_%')
    """)).fetchall()

    dim_col_ids = {r[0] for r in rows if r[0] is not None}
    if not dim_col_ids:
        return

    # Only update columns that are not already manually hidden by the user.
    # Columns with hidden_reason='user' or already hidden_reason='join' are skipped.
    conn.execute(
        sa.text("""
            UPDATE model_columns
            SET is_hidden = true, hidden_reason = 'join'
            WHERE id = ANY(:ids)
              AND (hidden_reason IS NULL)
        """),
        {"ids": list(dim_col_ids)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Reverse: unhide all columns that were auto-hidden by this migration.
    conn.execute(sa.text("""
        UPDATE model_columns
        SET is_hidden = false, hidden_reason = NULL
        WHERE hidden_reason = 'join'
    """))
