"""Widen kpi_latest/kpi_snapshots status_label from 64 to 255 chars.

The B9 fail-loud KPI status labels (composite depth limit, time-intelligence
without a time dimension, and TI-decomposition failures carrying arbitrary
router detail) exceed the original ``String(64)`` contract. The batch upsert
(``_upsert_kpi_latest_batch``) writes these verbatim, so an oversized label
raised ``StringDataRightTruncationError`` and 500'd the whole ``/evaluate-batch``
request — starving the scheduler snapshot sweep that calls the same endpoint.

This migration widens both tenant-schema columns to ``String(255)`` to match the
sibling ``kpi_name``/``created_by`` columns. It is guarded so it is a no-op when
the column is already 255, and runs once per tenant ``{slug}_meta`` schema via the
search_path set in ``env.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


_TABLES = ("kpi_snapshots", "kpi_latest")


def _current_length(conn, table_name: str) -> int | None:
    """Return the character_maximum_length of status_label in the current
    schema, or None when the table/column is absent (fresh schema mid-build)."""
    row = conn.execute(
        sa.text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :t AND column_name = 'status_label'"
        ),
        {"t": table_name},
    ).fetchone()
    return row[0] if row is not None else None


def upgrade() -> None:
    conn = op.get_bind()
    for table_name in _TABLES:
        length = _current_length(conn, table_name)
        if length is None:
            # Column not present in this schema yet — nothing to widen.
            continue
        if length is not None and length < 255:
            op.alter_column(
                table_name,
                "status_label",
                existing_type=sa.String(length),
                type_=sa.String(255),
                existing_nullable=True,
            )


def downgrade() -> None:
    conn = op.get_bind()
    for table_name in _TABLES:
        length = _current_length(conn, table_name)
        if length is None or length <= 64:
            continue
        # Truncate any over-long values first so the narrowing cannot fail.
        conn.execute(
            sa.text(
                f'UPDATE "{table_name}" SET status_label = left(status_label, 64) '
                "WHERE status_label IS NOT NULL AND char_length(status_label) > 64"
            )
        )
        op.alter_column(
            table_name,
            "status_label",
            existing_type=sa.String(length),
            type_=sa.String(64),
            existing_nullable=True,
        )
