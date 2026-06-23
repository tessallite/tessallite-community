"""
Shared helpers for resolving column names to ModelColumn records.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import ModelColumn


async def resolve_column(
    db: AsyncSession,
    table_id: UUID,
    column_name: str,
    data_type: str = "unknown",
) -> ModelColumn:
    """Look up a ModelColumn by name in the given table; create if not found.

    Pass ``data_type`` when the caller knows the real type (e.g. from schema
    profiling) so the record is stored with accurate metadata.  Existing
    records whose data_type is still ``"unknown"`` are upgraded in-place.
    """
    result = await db.execute(
        select(ModelColumn).where(
            ModelColumn.model_table_id == table_id,
            ModelColumn.column_name == column_name,
        )
    )
    col = result.scalar_one_or_none()
    if col is not None:
        # Upgrade stale "unknown" entries when the real type is now available
        if col.data_type == "unknown" and data_type != "unknown":
            col.data_type = data_type
            await db.flush()
        return col
    col = ModelColumn(
        model_table_id=table_id,
        column_name=column_name,
        data_type=data_type,
        is_nullable=True,
    )
    db.add(col)
    await db.flush()
    return col
