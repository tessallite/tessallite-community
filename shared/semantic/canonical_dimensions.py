"""Canonical dimension list builder.

Merges flat Dimension rows and hierarchy-level dimensions into a single
deduplicated list.  Two entries that share the same backing key (UDA ID
or source_column_id) collapse into one canonical dimension.

Used by:
- AI optimizer (masking, validation, dedup)
- Aggregate creator (grain resolution)
- Aggregate matcher (query-grain normalization)
- Telemetry collector (semantic schema dimensions)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
)


@dataclass
class CanonicalDim:
    canonical_name: str
    backing_key: str
    all_names: set[str] = field(default_factory=set)
    source_column_id: Optional[UUID] = None
    user_defined_attribute_id: Optional[UUID] = None
    is_time_dim: bool = False


def _backing_key(
    source_column_id: UUID | None,
    user_defined_attribute_id: UUID | None,
) -> str | None:
    if user_defined_attribute_id is not None:
        return f"uda:{user_defined_attribute_id}"
    if source_column_id is not None:
        return f"col:{source_column_id}"
    return None


async def build_canonical_dimension_list(
    model_id: object,
    db: AsyncSession,
) -> list[CanonicalDim]:
    """Build a deduplicated list of all dimensions for a model.

    Flat Dimension rows and hierarchy-level dimensions are merged.
    Entries sharing the same backing UDA or source column collapse
    into one CanonicalDim whose ``canonical_name`` is the bare name
    when unambiguous, or the qualified ``hierarchy.level`` form
    otherwise.
    """
    dims_result = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    flat_dims = list(dims_result.scalars().all())

    hier_result = await db.execute(
        select(HierarchyDefinition, HierarchyLevel)
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .where(HierarchyDefinition.model_id == model_id)
        .order_by(HierarchyDefinition.name, HierarchyLevel.ordinal)
    )
    hier_rows = hier_result.all()

    by_key: dict[str, CanonicalDim] = {}
    no_key: list[CanonicalDim] = []

    for dim in flat_dims:
        key = _backing_key(dim.source_column_id, dim.user_defined_attribute_id)
        if key is None:
            no_key.append(CanonicalDim(
                canonical_name=dim.name,
                backing_key=f"dim:{dim.id}",
                all_names={dim.name},
                is_time_dim=dim.is_time_dim,
            ))
            continue
        if key in by_key:
            by_key[key].all_names.add(dim.name)
        else:
            by_key[key] = CanonicalDim(
                canonical_name=dim.name,
                backing_key=key,
                all_names={dim.name},
                source_column_id=dim.source_column_id,
                user_defined_attribute_id=dim.user_defined_attribute_id,
                is_time_dim=dim.is_time_dim,
            )

    bare_count: dict[str, int] = {}
    qualified_entries: list[tuple[str, str, str, bool]] = []

    for hierarchy, level in hier_rows:
        level_name = (level.name or "").strip()
        if not level_name:
            continue
        source = (level.key_attribute_source or "").strip()
        if source == "user_defined_attribute":
            key = f"uda:{level.key_attribute_id}"
            uda_id = level.key_attribute_id
            col_id = None
        elif source == "physical_column":
            key = f"col:{level.key_attribute_id}"
            uda_id = None
            col_id = level.key_attribute_id
        else:
            continue

        qualified = f"{hierarchy.name}.{level_name}"
        is_time = (hierarchy.dimension_kind == "time")
        qualified_entries.append((key, qualified, level_name, is_time))
        bare_count[level_name] = bare_count.get(level_name, 0) + 1

        if key in by_key:
            by_key[key].all_names.add(qualified)
            by_key[key].is_time_dim = by_key[key].is_time_dim or is_time
        else:
            by_key[key] = CanonicalDim(
                canonical_name=qualified,
                backing_key=key,
                all_names={qualified},
                source_column_id=col_id,
                user_defined_attribute_id=uda_id,
                is_time_dim=is_time,
            )

    flat_names = {d.name for d in flat_dims}
    for key, qualified, bare, is_time in qualified_entries:
        entry = by_key.get(key)
        if entry is None:
            continue
        if bare_count.get(bare, 0) == 1 and bare not in flat_names:
            entry.all_names.add(bare)
            if entry.canonical_name == qualified:
                entry.canonical_name = bare

    result = list(by_key.values()) + no_key
    result.sort(key=lambda d: d.canonical_name)
    return result


def build_name_to_canonical_map(
    dims: list[CanonicalDim],
) -> dict[str, str]:
    """Return {any_known_name: canonical_name} for all dimensions."""
    mapping: dict[str, str] = {}
    for dim in dims:
        for name in dim.all_names:
            mapping[name] = dim.canonical_name
    return mapping
