"""Shared hierarchy-level dimension resolution.

Two entry points:
1. ``load_hierarchy_level_dimensions`` — ORM-based (async, needs DB session).
   Used by the query-router binder.
2. ``resolve_hierarchy_dimension_map`` — dict-based (sync, no DB).
   Used by the XMLA gateway which fetches metadata over HTTP.

Both share the same core resolution logic so hierarchy-level→dimension
mapping stays consistent across the binder and the gateway.
"""
from __future__ import annotations

import types
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import HierarchyDefinition, HierarchyLevel


async def load_hierarchy_level_dimensions(
    model_id: object,
    db: AsyncSession,
) -> list[types.SimpleNamespace]:
    """Load hierarchy levels as virtual dimensions for query-time resolution.

    Returns a list of ``SimpleNamespace`` objects with all fields the
    semantic binder needs: id, name, source_column_id,
    user_defined_attribute_id, hierarchy_id/name, level ordinal, and
    dimension_kind / is_time_dim derived from the parent hierarchy.
    """
    result = await db.execute(
        select(HierarchyDefinition, HierarchyLevel)
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .where(HierarchyDefinition.model_id == model_id)
        .order_by(HierarchyDefinition.name, HierarchyLevel.ordinal)
    )
    raw: list[types.SimpleNamespace] = []
    bare_count: dict[str, int] = {}
    for hierarchy, level in result.all():
        level_name = (level.name or "").strip()
        if not level_name:
            continue
        source = (level.key_attribute_source or "").strip()
        if source == "physical_column":
            source_column_id = level.key_attribute_id
            uda_id = None
        elif source == "user_defined_attribute":
            source_column_id = None
            uda_id = level.key_attribute_id
        else:
            continue
        qualified = f"{hierarchy.name}.{level_name}"
        raw.append(
            types.SimpleNamespace(
                id=f"hlevel-{level.id}",
                name=qualified,
                bare_name=level_name,
                source_column_id=source_column_id,
                user_defined_attribute_id=uda_id,
                hierarchy_id=hierarchy.id,
                hierarchy_name=hierarchy.name,
                hierarchy_level_id=level.id,
                hierarchy_level_ordinal=level.ordinal,
                is_hierarchy_level=True,
                dimension_kind=hierarchy.dimension_kind,
                is_time_dim=(hierarchy.dimension_kind == "time"),
            )
        )
        bare_count[level_name] = bare_count.get(level_name, 0) + 1

    out: list[types.SimpleNamespace] = []
    seen_qualified: set[str] = set()
    for dim in raw:
        if dim.name in seen_qualified:
            continue
        seen_qualified.add(dim.name)
        out.append(dim)
        if bare_count.get(dim.bare_name, 0) == 1 and dim.bare_name not in seen_qualified:
            alias = types.SimpleNamespace(**vars(dim))
            alias.name = dim.bare_name
            out.append(alias)
            seen_qualified.add(dim.bare_name)
    return out


async def load_dimensions_with_hierarchy_levels(
    model_id: object,
    db: AsyncSession,
    dimensions: list[Any],
) -> list[Any]:
    """Return the canonical grain vocabulary used by aggregate builders.

    Explicit dimensions retain precedence over hierarchy aliases with the same
    name. Qualified hierarchy names remain available, while an unambiguous bare
    level alias is included when it does not collide with an explicit dimension.
    """
    combined = list(dimensions)
    names_seen = {dimension.name for dimension in combined}
    for hierarchy_level in await load_hierarchy_level_dimensions(model_id, db):
        if hierarchy_level.name in names_seen:
            continue
        combined.append(
            types.SimpleNamespace(
                id=hierarchy_level.id,
                name=hierarchy_level.name,
                source_column_id=hierarchy_level.source_column_id,
                user_defined_attribute_id=hierarchy_level.user_defined_attribute_id,
                is_time_dim=hierarchy_level.is_time_dim,
            )
        )
        names_seen.add(hierarchy_level.name)
    return combined


async def resolve_aggregate_layout_with_hierarchy_levels(
    model_id: object,
    db: AsyncSession,
    dimensions: list[Any],
    resolver: Any = None,
    **layout_kwargs: Any,
) -> tuple[Any, list[Any]]:
    """Resolve a refresh layout with the creator's hierarchy vocabulary.

    Flat dimensions remain the fast path. Only an unknown grain dimension
    triggers the hierarchy query and retry; unknown measures and every other
    resolution error remain fail-closed without changing their meaning.
    """
    from shared.semantic.grain_resolver import (
        GrainResolutionError,
        resolve_aggregate_layout,
    )
    resolve = resolver or resolve_aggregate_layout

    try:
        return (
            resolve(dimensions=dimensions, **layout_kwargs),
            dimensions,
        )
    except GrainResolutionError as exc:
        if not str(exc).startswith("Unknown dimension:"):
            raise
        combined = await load_dimensions_with_hierarchy_levels(
            model_id, db, dimensions
        )
        return (
            resolve(dimensions=combined, **layout_kwargs),
            combined,
        )


def resolve_hierarchy_dimension_map(
    dimensions_meta: list[dict[str, Any]],
    hierarchy_meta: list[dict[str, Any]],
) -> tuple[set[str], dict[str, dict[str, str]], dict[str, str]]:
    """Build hierarchy→dimension mapping from raw metadata dicts.

    Used by the XMLA gateway which fetches model metadata over HTTP.

    Returns:
        dim_names: set of all known dimension names (including virtual
            hierarchy-level names that have no explicit Dimension object).
        hierarchy_level_dim_map: ``{hierarchy_name_lower: {level_name_lower: dim_name}}``.
        hierarchy_default_dim_map: ``{hierarchy_name_lower: dim_name}``
            for the deepest (leaf) level of each hierarchy.
    """
    dim_names: set[str] = set()
    dim_by_source: dict[tuple[str, str], str] = {}

    for d in dimensions_meta:
        dim_name = str(d.get("name") or "").strip()
        if not dim_name:
            continue
        dim_names.add(dim_name)
        source_col_id = d.get("source_column_id")
        if source_col_id:
            dim_by_source[("physical_column", str(source_col_id))] = dim_name
        uda_id = d.get("user_defined_attribute_id")
        if uda_id:
            dim_by_source[("user_defined_attribute", str(uda_id))] = dim_name

    hierarchy_level_dim_map: dict[str, dict[str, str]] = {}
    hierarchy_default_dim_map: dict[str, str] = {}
    bare_count: dict[str, int] = {}
    virtual_entries: list[tuple[str, str, str, str]] = []

    for h in hierarchy_meta:
        hname = str(h.get("name") or "").strip()
        if not hname:
            continue
        hkey = hname.lower()
        levels = sorted(
            h.get("levels") or [],
            key=lambda item: int(item.get("ordinal", 0)),
        )

        by_level: dict[str, str] = {}
        for lvl in levels:
            lname = str(lvl.get("name") or "").strip()
            if not lname:
                continue

            key_attr = lvl.get("key_attribute") or {}
            source = str(
                key_attr.get("source")
                or lvl.get("key_attribute_source")
                or "physical_column"
            ).strip()
            attr_id = key_attr.get("id") or lvl.get("key_attribute_id")
            source_key = (source, str(attr_id)) if attr_id else None

            dim_name = dim_by_source.get(source_key) if source_key else None
            if dim_name:
                by_level[lname.lower()] = dim_name
                continue
            if lname in dim_names:
                by_level[lname.lower()] = lname
                continue

            qualified = f"{hname}.{lname}"
            dim_names.add(qualified)
            by_level[lname.lower()] = qualified
            bare_count[lname] = bare_count.get(lname, 0) + 1
            virtual_entries.append((hkey, lname.lower(), lname, qualified))

        if by_level:
            hierarchy_level_dim_map[hkey] = by_level

        default_dim = None
        for lvl in reversed(levels):
            lname = str(lvl.get("name") or "").strip().lower()
            if lname in by_level:
                default_dim = by_level[lname]
                break
        if not default_dim and hname in dim_names:
            default_dim = hname
        if default_dim:
            hierarchy_default_dim_map[hkey] = default_dim

    for _hkey, _lkey, bare_name, _qualified in virtual_entries:
        if bare_count.get(bare_name, 0) == 1 and bare_name not in dim_names:
            dim_names.add(bare_name)
            by_level = hierarchy_level_dim_map.get(_hkey)
            if by_level and by_level.get(_lkey) == _qualified:
                by_level[_lkey] = bare_name
            if hierarchy_default_dim_map.get(_hkey) == _qualified:
                hierarchy_default_dim_map[_hkey] = bare_name

    return dim_names, hierarchy_level_dim_map, hierarchy_default_dim_map


__all__ = [
    "load_dimensions_with_hierarchy_levels",
    "load_hierarchy_level_dimensions",
    "resolve_aggregate_layout_with_hierarchy_levels",
    "resolve_hierarchy_dimension_map",
]
