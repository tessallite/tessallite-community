"""Schema drift auto-remediation.

Called after SchemaChangeEvent rows are written. Applies automatic corrective
actions based on the change type:

  column_added     — auto-create a hidden ModelColumn (non-breaking discovery)
  column_removed   — mark the column drift_removed=True, invalidate any
                     dimensions/measures backed by it, raise a ModelAlert
  type_changed     — update the stored data_type; if the new type is
                     incompatible with a measure's aggregation, invalidate it
                     and raise a ModelAlert
"""
from __future__ import annotations

import logging
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    Measure,
    ModelAlert,
    ModelColumn,
    ModelTable,
    SchemaChangeEvent,
)

logger = logging.getLogger(__name__)

_NUMERIC_AGG_TYPES = {"sum", "avg", "min", "max", "stddev"}
_NUMERIC_SQL_TYPES = {
    "integer", "int", "bigint", "smallint", "numeric", "decimal",
    "real", "double precision", "float", "float4", "float8",
    "int4", "int8", "int2",
}


def _is_numeric(data_type: str) -> bool:
    return data_type.lower().split("(")[0].strip() in _NUMERIC_SQL_TYPES


async def apply_remediation(
    model_id: object,
    events: Sequence[SchemaChangeEvent],
    db: AsyncSession,
) -> None:
    """Apply remediation for a batch of drift events on one model."""
    if not events:
        return

    for event in events:
        change_type = event.change_type
        detail = event.detail or {}

        if change_type == "column_added":
            await _handle_added(event, detail, db)
        elif change_type == "column_removed":
            await _handle_removed(model_id, event, detail, db)
        elif change_type == "type_changed":
            await _handle_type_changed(model_id, event, detail, db)


async def _handle_added(
    event: SchemaChangeEvent,
    detail: dict,
    db: AsyncSession,
) -> None:
    col_name = detail.get("column_name", "")
    data_type = detail.get("data_type", "text")

    # Find the ModelTable for this event
    if event.source_id is None:
        return

    tables_result = await db.execute(
        select(ModelTable).where(
            ModelTable.model_id == event.model_id,
            ModelTable.physical_name == event.table_name,
        )
    )
    table = tables_result.scalar_one_or_none()
    if table is None:
        return

    # Skip if already catalogued
    existing = await db.execute(
        select(ModelColumn).where(
            ModelColumn.model_table_id == table.id,
            ModelColumn.column_name == col_name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return

    new_col = ModelColumn(
        id=uuid.uuid4(),
        model_table_id=table.id,
        column_name=col_name,
        display_name=col_name,
        data_type=data_type,
        is_hidden=True,
        is_nullable=True,
        drift_removed=False,
    )
    db.add(new_col)
    logger.info(
        "Schema drift: auto-created hidden column %s on table %s (model %s)",
        col_name, event.table_name, event.model_id,
    )


async def _handle_removed(
    model_id: object,
    event: SchemaChangeEvent,
    detail: dict,
    db: AsyncSession,
) -> None:
    col_name = detail.get("column_name", "")

    # Find the ModelTable
    tables_result = await db.execute(
        select(ModelTable).where(
            ModelTable.model_id == model_id,
            ModelTable.physical_name == event.table_name,
        )
    )
    table = tables_result.scalar_one_or_none()
    if table is None:
        return

    # Find the ModelColumn
    col_result = await db.execute(
        select(ModelColumn).where(
            ModelColumn.model_table_id == table.id,
            ModelColumn.column_name == col_name,
        )
    )
    col = col_result.scalar_one_or_none()
    if col is None:
        return

    col.drift_removed = True
    invalid_reason = (
        f"Source column '{col_name}' was removed from table '{event.table_name}'."
    )

    # Invalidate dimensions backed by this column
    dims_result = await db.execute(
        select(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.source_column_id == col.id,
        )
    )
    for dim in dims_result.scalars().all():
        dim.is_invalid = True
        dim.invalid_reason = invalid_reason

    # Invalidate measures backed by this column
    meas_result = await db.execute(
        select(Measure).where(
            Measure.model_id == model_id,
            Measure.source_column_id == col.id,
        )
    )
    for meas in meas_result.scalars().all():
        meas.is_invalid = True
        meas.invalid_reason = invalid_reason

    # Create a ModelAlert (category=schema_drift, severity=error)
    alert = ModelAlert(
        id=uuid.uuid4(),
        model_id=model_id,  # type: ignore[arg-type]
        severity="error",
        category="schema_drift",
        title=f"Column removed: {col_name}",
        detail=invalid_reason,
        related_object_type="schema_change_event",
        related_object_id=event.id,
    )
    db.add(alert)
    logger.warning(
        "Schema drift: column %s removed from %s (model %s) — dimensions/measures invalidated",
        col_name, event.table_name, model_id,
    )


async def _handle_type_changed(
    model_id: object,
    event: SchemaChangeEvent,
    detail: dict,
    db: AsyncSession,
) -> None:
    col_name = detail.get("column_name", "")
    old_type = detail.get("old_data_type", "")
    new_type = detail.get("new_data_type", "")

    # Find the ModelTable
    tables_result = await db.execute(
        select(ModelTable).where(
            ModelTable.model_id == model_id,
            ModelTable.physical_name == event.table_name,
        )
    )
    table = tables_result.scalar_one_or_none()
    if table is None:
        return

    # Find the ModelColumn and update its data_type
    col_result = await db.execute(
        select(ModelColumn).where(
            ModelColumn.model_table_id == table.id,
            ModelColumn.column_name == col_name,
        )
    )
    col = col_result.scalar_one_or_none()
    if col is None:
        return

    col.data_type = new_type

    # Check whether any measures using this column have incompatible aggregation
    severity = "warning"
    meas_result = await db.execute(
        select(Measure).where(
            Measure.model_id == model_id,
            Measure.source_column_id == col.id,
        )
    )
    for meas in meas_result.scalars().all():
        if meas.default_agg in _NUMERIC_AGG_TYPES and not _is_numeric(new_type):
            meas.is_invalid = True
            meas.invalid_reason = (
                f"Column '{col_name}' type changed from '{old_type}' to '{new_type}', "
                f"incompatible with aggregation '{meas.default_agg}'."
            )
            severity = "error"

    alert = ModelAlert(
        id=uuid.uuid4(),
        model_id=model_id,  # type: ignore[arg-type]
        severity=severity,
        category="schema_drift",
        title=f"Column type changed: {col_name} ({old_type} → {new_type})",
        detail=(
            f"Column '{col_name}' in '{event.table_name}' changed type from "
            f"'{old_type}' to '{new_type}'."
        ),
        related_object_type="schema_change_event",
        related_object_id=event.id,
    )
    db.add(alert)
    logger.info(
        "Schema drift: type changed %s.%s %s→%s (model %s)",
        event.table_name, col_name, old_type, new_type, model_id,
    )
