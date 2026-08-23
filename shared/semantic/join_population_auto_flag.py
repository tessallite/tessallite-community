"""Conservative population-participation defaults from source metadata.

The source catalogue can prove only a small, useful subset of model joins:
the modeller's declared LEFT edge must run from the fact table to a dimension,
and the dimension endpoint must be the table's sole verified key.  A join
whose key or orientation is not proven is deliberately left alone.  In
particular, this module never infers a foreign key from a column name.

This is an introspection-time convenience, not a serving or deploy-time
classifier.  It only fills an absent/compatibility-default participation
value whose provenance is ``default``.  Explicit manual decisions are never
rewritten, even when they use the same ``preserve_base_rows`` token.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from shared.schemas.domains.aggregates_security import (
    DEFAULT_POPULATION_PARTICIPATION,
    POPULATION_PARTICIPATION_SOURCE_AUTO,
    POPULATION_PARTICIPATION_SOURCE_DEFAULT,
    coerce_population_participation_source,
)
from shared.semantic.graph_order import is_fact_table
from shared.semantic.join_keyword import normalise_token

_DIMENSION_TYPES = frozenset({"dim_aggregate", "dim_detail"})


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _is_verified_key(column: Any) -> bool:
    """Return whether *column* carries an explicit single-column key proof.

    ``is_primary_key`` is the current producer contract.  ``is_unique`` is
    accepted as a forward-compatible metadata shape, but is not guessed from
    names or cardinality.  Callers still enforce that exactly one such column
    exists on the dimension table and that it is the join endpoint.
    """
    return bool(
        _value(column, "is_primary_key", False)
        or _value(column, "is_unique", False)
    )


def _set_value(row: Any, key: str, value: Any) -> None:
    if isinstance(row, Mapping):
        row[key] = value  # type: ignore[index]
    else:
        setattr(row, key, value)


def auto_flag_safe_population_joins(
    joins: Iterable[Any],
    tables: Iterable[Any],
    columns: Iterable[Any],
    *,
    introspected_table_ids: set[Any] | None = None,
) -> list[Any]:
    """Mark only structurally proven joins as ``preserve_base_rows``.

    A table must participate in the current introspection operation when
    ``introspected_table_ids`` is provided.  This scopes the convenience to
    the newly refreshed metadata and prevents a later source refresh from
    retroactively re-declaring an existing customer's model.  The helper
    returns the mutated join rows so callers can audit/flush them before their
    enclosing transaction commits.
    """
    table_by_id = {_value(table, "id"): table for table in tables}
    columns_by_table: dict[Any, list[Any]] = {}
    for column in columns:
        columns_by_table.setdefault(_value(column, "model_table_id"), []).append(column)

    flagged: list[Any] = []
    for join in joins:
        participation = _value(join, "population_participation")
        source_raw = _value(join, "population_participation_source")
        # Old rows predate the provenance column.  A legacy compatibility
        # default is safe to treat as default-owned; every other concrete
        # value fails closed.  A current row explicitly marked manual is
        # never overwritten, including when its value is preserve_base_rows.
        source = (
            POPULATION_PARTICIPATION_SOURCE_DEFAULT
            if source_raw is None and participation in (None, DEFAULT_POPULATION_PARTICIPATION)
            else coerce_population_participation_source(source_raw)
        )
        if source != POPULATION_PARTICIPATION_SOURCE_DEFAULT:
            continue
        if participation not in (None, DEFAULT_POPULATION_PARTICIPATION):
            continue

        left_table_id = _value(join, "left_table_id")
        right_table_id = _value(join, "right_table_id")
        if introspected_table_ids is not None and not (
            left_table_id in introspected_table_ids
            or right_table_id in introspected_table_ids
        ):
            continue

        left_table = table_by_id.get(left_table_id)
        right_table = table_by_id.get(right_table_id)
        if left_table is None or right_table is None:
            continue
        # Only the canonical orientation is safe to auto-classify.  Legacy
        # cardinality tokens are intentionally not treated as LEFT joins.
        if normalise_token(_value(join, "join_type")) != "left":
            continue
        if not is_fact_table(left_table):
            continue
        if _value(right_table, "table_type") not in _DIMENSION_TYPES:
            continue

        # The declared edge must resolve on BOTH sides of the current model
        # catalogue. A right-side key proof without a fact-side endpoint is
        # incomplete structural evidence (and can arise from stale/imported
        # join metadata), so do not turn it into a default.
        left_column_id = _value(join, "left_column_id")
        left_columns = columns_by_table.get(left_table_id, [])
        if not left_column_id or not any(
            _value(column, "id") == left_column_id for column in left_columns
        ):
            continue

        right_columns = columns_by_table.get(right_table_id, [])
        verified_keys = [column for column in right_columns if _is_verified_key(column)]
        if len(verified_keys) != 1:
            # Composite/ambiguous/missing keys are not a proof of uniqueness.
            continue
        if _value(verified_keys[0], "id") != _value(join, "right_column_id"):
            continue

        _set_value(join, "population_participation", DEFAULT_POPULATION_PARTICIPATION)
        _set_value(join, "population_participation_source", POPULATION_PARTICIPATION_SOURCE_AUTO)
        flagged.append(join)

    return flagged


__all__ = ["auto_flag_safe_population_joins"]
