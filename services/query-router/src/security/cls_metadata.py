"""Column-level-security (CLS) filtering for metadata surfaces.

Bug-7412 / Bug-6141: metadata surfaces (headless REST, ``$KPIs`` scorecard) must
withhold measures and dimensions whose column closure reaches a persona-CLS
(data-tag) restricted column — not just persona allow-listed objects. A persona
may INCLUDE a measure/dimension whose underlying column is data-tag-restricted;
the base-table CLS gate blocks it at query time, but a naive metadata endpoint
would still disclose the restricted object's name + description.

This module resolves the persona's tag restrictions to restricted model columns
and reuses the SAME column-closure engine the runtime CLS gate uses
(``router._touches_restricted_columns`` / ``router._ClsClosure``) so the metadata
withhold decision matches the base-table gate exactly. Direct, UDA-backed,
variant, and calculated closures are all honoured, and any object whose closure
cannot be resolved fails CLOSED (treated as restricted).

The result is a pair of blocked-id sets — one for measures, one for dimensions —
that a metadata endpoint intersects with its response to withhold restricted rows.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    Measure,
    PersonaTagRestriction,
    UserDefinedAttributeColumnRef,
    data_tag_columns,
)


async def cls_restricted_column_ids(
    db: AsyncSession, persona: Any | None,
) -> list:
    """Model-column ids the persona's data-tag restrictions block (empty = none)."""
    if persona is None:
        return []
    restriction_rows = (
        await db.execute(
            select(PersonaTagRestriction.data_tag_id)
            .where(PersonaTagRestriction.persona_id == persona.id)
        )
    ).scalars().all()
    if not restriction_rows:
        return []
    restricted_col_rows = (
        await db.execute(
            select(data_tag_columns.c.model_column_id)
            .where(data_tag_columns.c.tag_id.in_(restriction_rows))
        )
    ).scalars().all()
    return list(restricted_col_rows)


async def cls_blocked_measure_and_dimension_ids(
    db: AsyncSession,
    model_id: str,
    persona: Any | None,
    *,
    measures: list[Any] | None = None,
    dimensions: list[Any] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(blocked_measure_ids, blocked_dimension_ids)`` for *persona*.

    Empty sets mean no CLS restriction is in force (serve everything the persona
    allow-list permits). Uses the runtime closure engine so the metadata withhold
    matches the base-table CLS gate; fails closed on any unresolvable closure.

    Bug-7418 (GAP 1): callers can pass pre-resolved ``measures`` and
    ``dimensions`` (e.g. from the deployed snapshot) so the CLS closure is
    computed against the SAME objects that execution binds, not live draft
    tables. When omitted, the function queries live tables (back-compat).
    """
    if persona is None:
        return frozenset(), frozenset()

    restricted_col_rows = await cls_restricted_column_ids(db, persona)
    if not restricted_col_rows:
        return frozenset(), frozenset()

    # Import the runtime closure engine lazily to avoid a module-load import cycle
    # (router imports from several api modules). This module only READS these — it
    # does not modify the engine.
    from src.routing.router import (
        _ClsClosure,
        _all_model_physical_names,
        _model_table_identifiers,
        _restricted_physical_names,
        _touches_restricted_columns,
    )

    restricted_ids = {str(c) for c in restricted_col_rows}

    # Bug-7418 (GAP 1): use caller-supplied snapshot objects when provided;
    # fall back to live tables only when not supplied.
    if measures is None:
        measures = list(
            (await db.execute(select(Measure).where(Measure.model_id == model_id)))
            .scalars().all()
        )
    if dimensions is None:
        dimensions = list(
            (await db.execute(select(Dimension).where(Dimension.model_id == model_id)))
            .scalars().all()
        )

    ctx = _ClsClosure()
    for m in measures:
        ctx.measures_by_id[str(m.id)] = m
        ctx.measures_by_name[m.name] = m

    # UDA closure — needed whenever any measure/dimension is UDA-backed OR a
    # calculated measure could reach a UDA-backed reference.
    uda_rows = (
        await db.execute(
            select(UserDefinedAttributeColumnRef.attribute_id)
            .where(UserDefinedAttributeColumnRef.column_id.in_(list(restricted_col_rows)))
        )
    ).scalars().all()
    ctx.restricted_uda_ids = {str(a) for a in uda_rows}

    # Physical-name lookups — needed for calc dimensions (calc_expression). Load
    # them whenever ANY dimension carries a calc_expression so the calc-dimension
    # gate (which fails closed on whole-row/table refs) has both sets in lockstep.
    if any(getattr(d, "calc_expression", None) for d in dimensions):
        ctx.restricted_physical_names = await _restricted_physical_names(
            list(restricted_col_rows), db,
        )
        ctx.known_physical_names = await _all_model_physical_names(model_id, db)
        ctx.table_identifiers = await _model_table_identifiers(model_id, db)
    else:
        # Populate the restricted-name set anyway (cheap) so any calc closure that
        # slips through still has a non-None set to match against.
        ctx.restricted_physical_names = await _restricted_physical_names(
            list(restricted_col_rows), db,
        )

    blocked_measures = frozenset(
        str(m.id)
        for m in measures
        if _touches_restricted_columns(m, restricted_ids, ctx)
    )

    def _dimension_blocked(d: Any) -> bool:
        # Runtime column closure (direct source_column_id, UDA, variant, calc).
        if _touches_restricted_columns(d, restricted_ids, ctx):
            return True
        # Bug-7412 R2: a flat dimension can surface a SEPARATE display column
        # (``display_column_id``, Bug-5434) as the member caption while its key
        # column is clean. The runtime closure only walks ``source_column_id``, so
        # a restricted DISPLAY column would leak the dimension's members/metadata.
        # Withhold the dimension when its display column is restricted too.
        disp = getattr(d, "display_column_id", None)
        if disp is not None and str(disp) in restricted_ids:
            return True
        return False

    blocked_dimensions = frozenset(
        str(d.id) for d in dimensions if _dimension_blocked(d)
    )
    return blocked_measures, blocked_dimensions
