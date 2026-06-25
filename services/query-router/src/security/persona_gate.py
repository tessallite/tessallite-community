"""Persona allow-list gate — Phase 8.B.3.

When a caller binds to a persona catalog (``<model.slug>_<persona.slug>``)
the router must verify that every measure and dimension the bound query
references appears in that persona's include list. An empty include list
means unrestricted (any object of that kind is allowed).

Returns the loaded ``Persona`` ORM row so downstream stages (default
filters merge in 8.B.4) can read its ``default_filters`` without
re-querying.

Raises ``HTTPException(403, error_code=PERSONA_OBJECT_NOT_INCLUDED)``
when the gate denies the request, and ``HTTPException(404)`` when the
persona ID does not resolve to a persona on this model.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import CurrentUser
from shared.db.models import Persona
from shared.security.persona_resolver import resolve_effective_persona

from src.ir.logical_query import BoundQuery, LogicalFilter


# F-008-18: ``resolve_embed_persona`` was dead — the locked-persona path is
# enforced by ``resolve_effective_persona`` (persona_resolver), which all the
# execution/discover routes call via ``resolve_execution_persona`` below.


async def resolve_execution_persona(
    db: AsyncSession,
    *,
    current_user: CurrentUser,
    model_id: str,
    requested_persona_id: str | None,
) -> Persona | None:
    """Resolve the effective persona for an execution request using
    the full audience enforcement matrix.

    Handles embed locked persona, privileged users, technical persona
    holders, single/multi assignment, and unassigned users — same
    rules as model-service metadata endpoints.
    """
    return await resolve_effective_persona(
        db,
        current_user=current_user,
        model_id=uuid.UUID(model_id),
        requested_persona_id=requested_persona_id,
    )


async def load_persona(
    db: AsyncSession, *, model_id: str, persona_id: str
) -> Persona:
    """Load a persona and verify it belongs to the queried model."""
    try:
        pid = uuid.UUID(persona_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "PERSONA_INVALID",
                "message": f"Persona id is not a valid UUID: {persona_id!r}",
            },
        )
    persona = await db.get(Persona, pid)
    if persona is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "PERSONA_NOT_FOUND",
                "message": f"Persona {persona_id} not found.",
            },
        )
    if str(persona.model_id) != str(model_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "PERSONA_NOT_FOUND",
                "message": (
                    f"Persona {persona_id} does not belong to the queried model."
                ),
            },
        )
    return persona


def _ids_as_strings(values: Any) -> set[str]:
    if not values:
        return set()
    return {str(v) for v in values}


async def _get_excluded_level_attribute_ids(
    db: AsyncSession, model_id: str, persona: Persona,
) -> set[str] | None:
    """Compute attribute IDs that hierarchy levels must NOT expose.

    When ``included_dimension_ids`` is populated, any hierarchy level
    whose key attribute backs an excluded dimension is hidden.  Returns
    ``None`` when there is no dimension restriction.
    """
    from sqlalchemy import select
    from shared.db.models import Dimension

    allowed_dim_ids = _ids_as_strings(persona.included_dimension_ids)
    if not allowed_dim_ids:
        return None
    result = await db.execute(
        select(Dimension.id, Dimension.source_column_id, Dimension.user_defined_attribute_id)
        .where(Dimension.model_id == model_id)
    )
    excluded: set[str] = set()
    for dim_id, src_col_id, uda_id in result.all():
        if str(dim_id) in allowed_dim_ids:
            continue
        if src_col_id is not None:
            excluded.add(str(src_col_id))
        if uda_id is not None:
            excluded.add(str(uda_id))
    return excluded


def enforce_persona(
    persona: Persona,
    bound: BoundQuery,
    excluded_level_attrs: set[str] | None = None,
) -> None:
    """Reject or filter the bound query based on the persona's allow list.

    When the query uses ``SELECT *``, disallowed objects are silently
    removed from the resolved lists so BI tools that send ``SELECT *``
    get back only the persona-scoped columns instead of a hard 403.
    Explicitly requested objects that fall outside the allow list still
    raise 403 — the caller asked for something they cannot see.

    Complex SQL fails CLOSED (F-008-03): CTEs/subqueries/window
    functions skip binder column resolution, leaving the resolved lists
    empty, so the allow lists (and persona default filters, which the
    passthrough rewriter cannot inject) cannot be enforced. When the
    persona carries any allow list or default filter, the query is
    rejected instead of executed unrestricted.
    """
    is_star = bound.logical_query.select_star

    measure_allow = _ids_as_strings(persona.included_measure_ids)
    dimension_allow = _ids_as_strings(persona.included_dimension_ids)
    hierarchy_allow = _ids_as_strings(persona.included_hierarchy_ids)

    if getattr(bound.logical_query, "has_complex_sql", False) and (
        measure_allow
        or dimension_allow
        or hierarchy_allow
        or (persona.default_filters or {})
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "PERSONA_COMPLEX_SQL_NOT_ALLOWED",
                "message": (
                    "This query uses advanced SQL constructs (for example "
                    "subqueries, CTEs, set operations, window functions, or "
                    "non-standard aggregates) that cannot be verified against "
                    f"the allow lists or default filters of persona "
                    f"'{persona.name}', so it was rejected. Rewrite the query "
                    "as a plain SELECT over the model."
                ),
                "persona_id": str(persona.id),
            },
        )

    if measure_allow:
        if is_star:
            before = len(bound.resolved_measures)
            bound.resolved_measures = [
                m for m in bound.resolved_measures
                if getattr(m, "id", None) is None
                or str(m.id) in measure_allow
            ]
            if len(bound.resolved_measures) != before:
                bound.persona_narrowed_star = True
        else:
            for m in bound.resolved_measures:
                # Synthetic measures (the binder's __row_count for
                # COUNT(*)) carry no id and reference no modeled measure
                # or physical column — the allow list does not gate them.
                if getattr(m, "id", None) is None:
                    continue
                if str(m.id) not in measure_allow:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "error_code": "PERSONA_OBJECT_NOT_INCLUDED",
                            "message": (
                                f"Measure {m.name!r} is not included in persona "
                                f"{persona.name!r}."
                            ),
                            "object_kind": "measure",
                            "object_name": m.name,
                            "persona_id": str(persona.id),
                        },
                    )

    if dimension_allow:
        def _dim_allowed(d: Any) -> bool:
            if str(d.id) in dimension_allow:
                return True
            hid = getattr(d, "hierarchy_id", None)
            if hid is not None:
                if excluded_level_attrs is not None:
                    src = getattr(d, "source_column_id", None)
                    uda = getattr(d, "user_defined_attribute_id", None)
                    if (src is not None and str(src) in excluded_level_attrs) or \
                       (uda is not None and str(uda) in excluded_level_attrs):
                        return False
                if hierarchy_allow:
                    return str(hid) in hierarchy_allow
                return True
            return False

        if is_star:
            before = len(bound.resolved_dimensions)
            bound.resolved_dimensions = [
                d for d in bound.resolved_dimensions
                if _dim_allowed(d)
            ]
            if len(bound.resolved_dimensions) != before:
                bound.persona_narrowed_star = True
        else:
            for d in bound.resolved_dimensions:
                if not _dim_allowed(d):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "error_code": "PERSONA_OBJECT_NOT_INCLUDED",
                            "message": (
                                f"Dimension {d.name!r} is not included in persona "
                                f"{persona.name!r}."
                            ),
                            "object_kind": "dimension",
                            "object_name": d.name,
                            "persona_id": str(persona.id),
                        },
                    )
    if hierarchy_allow:
        if is_star:
            before = len(bound.resolved_dimensions)
            bound.resolved_dimensions = [
                d for d in bound.resolved_dimensions
                if not (
                    (hid := getattr(d, "hierarchy_id", None)) is not None
                    and str(hid) not in hierarchy_allow
                )
            ]
            if len(bound.resolved_dimensions) != before:
                bound.persona_narrowed_star = True
        else:
            for d in bound.resolved_dimensions:
                hid = getattr(d, "hierarchy_id", None)
                if hid is None:
                    continue
                if str(hid) not in hierarchy_allow:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "error_code": "PERSONA_OBJECT_NOT_INCLUDED",
                            "message": (
                                f"Dimension {d.name!r} belongs to a hierarchy that is not "
                                f"included in persona {persona.name!r}."
                            ),
                            "object_kind": "hierarchy",
                            "object_name": d.name,
                            "persona_id": str(persona.id),
                        },
                    )


async def enforce_persona_gate(
    db: AsyncSession,
    *,
    persona: Persona,
    model_id: str,
    bound: BoundQuery,
) -> None:
    """Apply persona allow-list enforcement with hierarchy-level checks.

    Computes excluded hierarchy-level attribute IDs from the dimension
    allow-list, then delegates to ``enforce_persona``.
    """
    excluded = await _get_excluded_level_attribute_ids(db, model_id, persona)
    enforce_persona(persona, bound, excluded_level_attrs=excluded)


async def apply_persona_gate(
    db: AsyncSession,
    *,
    model_id: str,
    persona_id: Optional[str],
    bound: BoundQuery,
) -> Optional[Persona]:
    """Convenience wrapper used by the route handlers.

    Returns ``None`` when no persona was resolved from the catalog, or the
    loaded Persona ORM row when the gate passed.
    """
    if not persona_id:
        return None
    persona = await load_persona(
        db, model_id=model_id, persona_id=persona_id
    )
    await enforce_persona_gate(db, persona=persona, model_id=model_id, bound=bound)
    return persona


# F-008-16: single source of truth shared with the model-service save-time
# validator, so the operators the gate honours and the operators the editor
# may persist can never drift apart.
from shared.schemas.domains.aggregates_security import (
    PERSONA_FILTER_OPERATORS as _SUPPORTED_OPERATORS,
)


def merge_default_filters(persona: Persona, bound: BoundQuery) -> list[str]:
    """Append the persona's default_filters into the bound query's WHERE.

    Conflict policy: if the caller's query already references a dimension
    by name, the user filter wins and the persona default is skipped.
    Anything else is appended as an additional ``LogicalFilter`` so it
    flows through the existing rewriter ``_render_where`` pipeline.

    Supported value shapes per dimension:
      * scalar  -> eq
      * list    -> in
      * dict    -> first key/value treated as ``{operator: value}``
        (must be one of the supported operators).

    Returns the list of dimension names that were merged, so the caller
    can surface them in trace/audit if useful.
    """
    defaults = persona.default_filters or {}
    if not defaults:
        return []
    user_dim_names = {f.dimension_name for f in bound.resolved_filters}
    merged: list[str] = []
    for dim_name, raw in defaults.items():
        if dim_name in user_dim_names:
            continue
        operator, value = _coerce_filter(raw)
        if operator is None:
            continue
        bound.resolved_filters.append(
            LogicalFilter(dimension_name=dim_name, operator=operator, value=value)
        )
        merged.append(dim_name)
    return merged


def _coerce_filter(raw: Any) -> tuple[Optional[str], Any]:
    """Translate a default_filters value into (operator, value)."""
    if isinstance(raw, dict):
        if not raw:
            return None, None
        op, val = next(iter(raw.items()))
        if op not in _SUPPORTED_OPERATORS:
            return None, None
        return op, val
    if isinstance(raw, list):
        return "in", raw
    return "eq", raw


__all__ = [
    "apply_persona_gate",
    "enforce_persona",
    "enforce_persona_gate",
    "load_persona",
    "merge_default_filters",
    "resolve_execution_persona",
]
