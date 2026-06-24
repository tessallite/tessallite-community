"""Shared reference guard for deleting a single user-defined attribute.

Both UI-reachable single-UDA delete endpoints route through this guard:

  - ``table_attributes.py::delete_table_attribute`` (kind=user_defined) — the
    path the frontend AttributesTab Delete button calls.
  - ``user_defined_attributes.py::delete_user_defined_attribute`` — the direct
    REST endpoint.

Before this guard existed both endpoints checked only ``Dimension`` and
``Measure`` references. A generated UDA that keys a ``HierarchyLevel`` (or is
attached as a ``HierarchyLevelAttribute``) could therefore be hard-deleted from
the table editor, leaving a dangling ``key_attribute_id`` — the same data-loss
class as F-016-07, but on the per-attribute delete path (Bug-1505, F-016-08).

The hierarchy-level / level-attribute reference check here mirrors the
surviving-reference scan in ``hierarchies.py::_delete_unreferenced_generated_udas``
so that a UDA referenced by ANY hierarchy level cannot be deleted via EITHER
endpoint.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from shared.db.models import (
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Measure,
)


async def assert_uda_deletable(db, *, model_id: UUID, attribute_id: UUID) -> None:
    """Raise 409 if any model object still references the given UDA.

    Checks dimensions, measures, hierarchy-level key attributes, and
    hierarchy-level display/filter attributes. Used by both single-UDA delete
    endpoints so the guard cannot be bypassed from either surface.
    """
    dim_result = await db.execute(
        select(Dimension.name).where(
            Dimension.model_id == model_id,
            Dimension.user_defined_attribute_id == attribute_id,
        )
    )
    meas_result = await db.execute(
        select(Measure.name).where(
            Measure.model_id == model_id,
            Measure.user_defined_attribute_id == attribute_id,
        )
    )
    # Hierarchy levels that key on this UDA. Join to the hierarchy/level so the
    # rejection message names the level the modeller would orphan.
    level_key_result = await db.execute(
        select(HierarchyDefinition.name, HierarchyLevel.name)
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .where(
            HierarchyDefinition.model_id == model_id,
            HierarchyLevel.key_attribute_source == "user_defined_attribute",
            HierarchyLevel.key_attribute_id == attribute_id,
        )
    )
    # Hierarchy levels that carry this UDA as a display/filter attribute.
    level_attr_result = await db.execute(
        select(HierarchyDefinition.name, HierarchyLevel.name)
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .join(
            HierarchyLevelAttribute,
            HierarchyLevelAttribute.level_id == HierarchyLevel.id,
        )
        .where(
            HierarchyDefinition.model_id == model_id,
            HierarchyLevelAttribute.attribute_source == "user_defined_attribute",
            HierarchyLevelAttribute.attribute_id == attribute_id,
        )
    )

    dim_refs = [r[0] for r in dim_result.fetchall()]
    meas_refs = [r[0] for r in meas_result.fetchall()]
    level_refs = sorted({f"{h}.{lvl}" for h, lvl in level_key_result.fetchall()})
    level_attr_refs = sorted({f"{h}.{lvl}" for h, lvl in level_attr_result.fetchall()})

    if not (dim_refs or meas_refs or level_refs or level_attr_refs):
        return

    refs: list[str] = []
    if dim_refs:
        refs.append(f"dimensions: {', '.join(dim_refs)}")
    if meas_refs:
        refs.append(f"measures: {', '.join(meas_refs)}")
    if level_refs:
        refs.append(f"hierarchy levels (key): {', '.join(level_refs)}")
    if level_attr_refs:
        refs.append(f"hierarchy levels (attribute): {', '.join(level_attr_refs)}")

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot delete attribute; it is referenced by {'; '.join(refs)}",
    )
