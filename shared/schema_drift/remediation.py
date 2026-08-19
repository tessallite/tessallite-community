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
    HierarchyDefinition,
    HierarchyLevel,
    Measure,
    ModelAlert,
    ModelColumn,
    ModelTable,
    SchemaChangeEvent,
)
from shared.schema_drift.artifact_invalidation import (
    invalidate_dependent_materialisations,
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
    """Apply remediation for a batch of drift events on one model.

    F-012-01: after invalidating the dependent semantic objects (dimensions /
    measures), also make every physical materialisation that depends on them
    non-routable in the SAME transaction. Otherwise the matcher keeps serving
    old aggregate/pocket rows after a breaking source change — silent
    wrong-number exposure.
    """
    if not events:
        return

    # F-012-01: accumulate the semantic objects invalidated this run so the
    # dependent-materialisation resolver can trip the canonical non-routable
    # flags on aggregates/pockets before the caller commits.
    invalidated_measure_ids: set = set()
    invalidated_dimension_names: set[str] = set()

    for event in events:
        change_type = event.change_type
        detail = event.detail or {}

        if change_type == "column_added":
            await _handle_added(event, detail, db)
        elif change_type == "column_removed":
            await _handle_removed(
                model_id, event, detail, db,
                invalidated_measure_ids=invalidated_measure_ids,
                invalidated_dimension_names=invalidated_dimension_names,
            )
        elif change_type == "type_changed":
            await _handle_type_changed(
                model_id, event, detail, db,
                invalidated_measure_ids=invalidated_measure_ids,
            )

    # Fable R1: use the canonical ``is_breaking`` flag already computed and
    # persisted by ``_diff_schemas`` (schema_drift.py) rather than re-deriving
    # "breaking" locally inside each handler. The canonical flag covers EVERY
    # breaking case (dimension-backed type changes, not only numeric-agg measure
    # incompatibility), and aligns with what the webhook/alert/sweep log report.
    has_breaking_event = any(getattr(e, "is_breaking", False) for e in events)

    # F-012-01: resolve every dependent aggregate/pocket and fail them closed.
    await invalidate_dependent_materialisations(
        model_id, db,
        invalidated_measure_ids=invalidated_measure_ids,
        invalidated_dimension_names=invalidated_dimension_names,
        has_breaking_event=has_breaking_event,
        reason="Source schema drift invalidated a dependent dimension or measure.",
    )


async def _resolve_model_table(model_id: object, event: SchemaChangeEvent, db: AsyncSession):
    """Resolve the ``ModelTable`` a drift event refers to.

    Bug-7886: a multi-connection model can carry the same ``physical_name``
    under more than one source connection, so a ``(model_id, physical_name)``
    lookup is ambiguous and ``scalar_one_or_none`` raised
    ``MultipleResultsFound`` (surfaced as a 500 from the schema-drift trigger).
    Scope by the event's ``source_id`` when present so the tuple is unique, and
    fall back to the first match for legacy events that carry no ``source_id``
    rather than crashing the whole drift run.
    """
    stmt = select(ModelTable).where(
        ModelTable.model_id == model_id,
        ModelTable.physical_name == event.table_name,
    )
    source_id = getattr(event, "source_id", None)
    if source_id is not None:
        stmt = stmt.where(ModelTable.source_id == source_id)
    result = await db.execute(stmt)
    return result.scalars().first()


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

    table = await _resolve_model_table(event.model_id, event, db)
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
    *,
    invalidated_measure_ids: set,
    invalidated_dimension_names: set[str],
) -> None:
    col_name = detail.get("column_name", "")

    # Find the ModelTable (scoped by source — Bug-7886)
    table = await _resolve_model_table(model_id, event, db)
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
        # F-012-01: record the logical dimension name so dependent aggregates
        # (whose grain references it) can be failed closed.
        dim_name = getattr(dim, "name", None)
        if dim_name:
            invalidated_dimension_names.add(dim_name)

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
        # F-012-01: record the measure id so dependent aggregates (whose
        # AggregateColumn references it) can be failed closed.
        invalidated_measure_ids.add(meas.id)

    # Fable R1: hierarchy levels backed by this physical column. A hierarchy
    # level (key_attribute_source="physical_column", key_attribute_id=col.id)
    # resolves to a virtual dimension with the qualified grain name
    # "hierarchy_name.level_name" — the exact token stored in
    # AggregateDefinition.grain. Plain Dimension queries miss these because
    # they are NOT Dimension rows; they live in the hierarchy_levels table.
    hlevel_result = await db.execute(
        select(HierarchyDefinition.name, HierarchyLevel.name)
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .where(
            HierarchyDefinition.model_id == model_id,
            HierarchyLevel.key_attribute_source == "physical_column",
            HierarchyLevel.key_attribute_id == col.id,
        )
    )
    for hierarchy_name, level_name in hlevel_result.fetchall():
        qualified = f"{hierarchy_name}.{level_name}"
        invalidated_dimension_names.add(qualified)
        # Also add the bare level name — the grain resolver may emit it
        # when the level name is unique across hierarchies (hierarchy_resolver.py
        # line ~93).
        if level_name:
            invalidated_dimension_names.add(level_name)

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
    *,
    invalidated_measure_ids: set,
) -> None:
    """Apply type-change remediation."""
    col_name = detail.get("column_name", "")
    old_type = detail.get("old_data_type", "")
    new_type = detail.get("new_data_type", "")

    # Find the ModelTable (scoped by source — Bug-7886)
    table = await _resolve_model_table(model_id, event, db)
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
            # F-012-01: an incompatibly-retyped measure column is a breaking
            # change; record the measure so dependent aggregates fail closed.
            invalidated_measure_ids.add(meas.id)

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
