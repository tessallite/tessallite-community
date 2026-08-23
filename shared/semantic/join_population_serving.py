"""Shared serving contract for population-defining model joins (Bug-8615).

``join_type`` and ``cardinality`` describe how a join behaves.  The
``population_participation`` field describes whether that behaviour is part of
the model's row population.  A population-defining edge therefore belongs in
every deployed serving/build plan even when no selected field comes from its
far table.  This module is deliberately small and dependency-light so source,
aggregate, pocket, scheduler, and optimizer code all use the same closure.

Rows arrive from both deployed snapshots and live ORM queries.  The normalizer
accepts mappings, ORM objects, and namespace-like objects; malformed or
unknown participation is ``undeclared`` and malformed endpoints fail closed.
It never turns an unknown value into a mandatory edge, which prevents a bad
snapshot from silently widening a query plan.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from shared.schemas.domains.aggregates_security import (
    DEFAULT_POPULATION_PARTICIPATION,
    POPULATION_PARTICIPATION_POPULATION_DEFINING,
    coerce_population_participation,
)

__all__ = [
    "JOIN_POPULATION_SERVING_CONTRACT_VERSION",
    "DEFAULT_POPULATION_PARTICIPATION",
    "POPULATION_PARTICIPATION_POPULATION_DEFINING",
    "normalized_population_participation",
    "population_defining_join_rows",
    "population_defining_table_ids",
    "augment_required_table_ids",
]

# Bump when the deployed FROM/JOIN population contract changes.  Existing
# aggregate, pocket, and Named Query artifacts are invalidated by the tenant
# migration that introduces the new value; runtime artifact gates still require
# an exact deployed (version, epoch) binding on every serve.
JOIN_POPULATION_SERVING_CONTRACT_VERSION = 1


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read a join field from a mapping, ORM row, or namespace."""
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def normalized_population_participation(row: Any) -> str:
    """Return the declared participation token for *row*.

    A missing field is an old snapshot row and therefore receives the schema's
    compatibility default.  A present but unknown token is intentionally
    coerced to ``undeclared`` by the existing schema helper.
    """
    raw = _field(row, "population_participation", DEFAULT_POPULATION_PARTICIPATION)
    if raw is None:
        raw = DEFAULT_POPULATION_PARTICIPATION
    return coerce_population_participation(raw)


def population_defining_join_rows(rows: Iterable[Any]) -> tuple[Any, ...] | None:
    """Return valid population-defining rows, or ``None`` when malformed.

    Endpoint validation is centralized here so every consumer makes the same
    fail-closed decision.  Participation values that are unknown are not
    selected, because they are not affirmative declarations.
    """
    selected: list[Any] = []
    for row in rows:
        if normalized_population_participation(row) != POPULATION_PARTICIPATION_POPULATION_DEFINING:
            continue
        left = _field(row, "left_table_id")
        right = _field(row, "right_table_id")
        if not left or not right:
            return None
        selected.append(row)
    return tuple(selected)


def population_defining_table_ids(rows: Iterable[Any]) -> frozenset[str] | None:
    """Return both endpoints of every population-defining edge.

    ``None`` means a mandatory edge was malformed and no caller may proceed as
    if the graph had no mandatory relations.
    """
    selected = population_defining_join_rows(rows)
    if selected is None:
        return None
    table_ids: set[str] = set()
    for row in selected:
        table_ids.add(str(_field(row, "left_table_id")))
        table_ids.add(str(_field(row, "right_table_id")))
    return frozenset(table_ids)


def augment_required_table_ids(
    required_table_ids: Iterable[Any],
    joins: Iterable[Any],
    *,
    table_ids: Iterable[Any] | None = None,
) -> set[Any] | None:
    """Add mandatory population edges to a builder's required table set.

    The returned IDs preserve the native key values used by the caller (UUIDs
    in ORM graphs, strings in deployed snapshots).  If a mandatory endpoint is
    absent from the graph universe, ``None`` is returned so the caller can fail
    closed rather than silently dropping the edge.
    """
    join_rows = tuple(joins)
    mandatory = population_defining_table_ids(join_rows)
    if mandatory is None:
        return None

    result = set(required_table_ids)
    if table_ids is None:
        by_text: dict[str, Any] = {}
        for row in join_rows:
            for key in ("left_table_id", "right_table_id"):
                value = _field(row, key)
                if value is not None:
                    by_text.setdefault(str(value), value)
    else:
        by_text = {str(value): value for value in table_ids if value is not None}

    for table_id in mandatory:
        native = by_text.get(table_id)
        if native is None:
            return None
        result.add(native)
    return result
