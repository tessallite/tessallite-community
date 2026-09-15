"""Build-side aggregate SQL compiled by the query-router model path.

Aggregate materialisation must use the same semantic relation closure as a
model query.  This module owns only the small semantic query envelope; the
query-router owns binding, security, join planning, dialect rendering, and the
returned source SQL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from shared.aggregate_quantiles import (
    ROUTABLE_QUANTILE_PERCENTILES,
    is_quantile_stat_type,
    quantile_suffix,
    quantile_suffix_to_fraction,
)
from shared.aggregate_stats import STAT_SQL_TEMPLATES, STAT_TYPES, is_stat_type
from shared.connector_qualify import quote_identifier
from shared.db.models import Model
from shared.semantic.calculated_columns import (
    is_calculated,
    render_calculated_column_sql,
)
from shared.semantic.grain_resolver import ResolvedAggregateLayout, bound_ident
from shared.semantic.numeric_scale import cast_for_agg
from shared.semantic.variant_columns import is_variant
from shared.source_pool import session_tenant_id

logger = logging.getLogger(__name__)

_PG = "postgresql"
_AGGREGATES = frozenset({"sum", "avg", "min", "max", "count"})


def _mint_service_token(tenant_id: str) -> str:
    """Load the Named Query token authority only when a build is executed."""
    from shared.named_query.refresh import _mint_service_token as mint

    return mint(tenant_id)


async def _get_rewritten_sql(
    model_id: object, definition_sql: str, bearer_token: str,
) -> str:
    """Use the exact Named Query ``/explain`` source-compile seam."""
    from shared.named_query.refresh import _get_rewritten_sql as compile_sql

    return await compile_sql(model_id, definition_sql, bearer_token)


@dataclass(frozen=True)
class CompiledAggregateModelQuery:
    """The semantic build request and the exact SQL returned by the router."""

    raw_query: str
    sql: str
    output_columns: tuple[str, ...]


def _measure_by_id(measures: Sequence[Any]) -> dict[Any, Any]:
    return {
        getattr(measure, "id", None): measure
        for measure in measures
        if getattr(measure, "id", None) is not None
    }


def _measure_by_name(measures: Sequence[Any]) -> dict[str, Any]:
    return {
        str(getattr(measure, "name")): measure
        for measure in measures
        if getattr(measure, "name", None)
    }


def _measure_for(column: Any, by_id: Mapping[Any, Any], by_name: Mapping[str, Any]) -> Any:
    measure = by_id.get(getattr(column, "measure_id", None))
    return measure or by_name.get(str(getattr(column, "measure_name", "")))


def _semantic_ref(name: str) -> str:
    return quote_identifier(_PG, str(name))


def _source_ref(
    column: Any,
    measure: Any,
    source_column_names: Mapping[Any, str] | None,
) -> str:
    """Return the physical source column represented by one model measure.

    A model measure name is a semantic alias and is not necessarily the source
    column name.  In particular, putting a quoted measure alias inside
    ``AVG(...)`` prevents the query-router from applying its measure binder and
    makes PostgreSQL look for that alias in the source table.  The resolved
    layout is the authoritative source-column mapping for the materialised
    measure; the producer map covers calculated-measure references that are not
    themselves layout outputs.
    """
    source_name = getattr(column, "source_column_name", None)
    if not source_name and measure is not None and source_column_names:
        source_name = source_column_names.get(getattr(measure, "id", None))
    if not source_name:
        source_name = getattr(measure, "source_column_name", None)
    if not source_name:
        source_name = getattr(column, "measure_name", "") if column is not None else ""
    return _semantic_ref(source_name)


def _plain_measure_expression(
    column: Any,
    measure: Any,
    source_numeric_types: Mapping[Any, str] | None,
    source_column_names: Mapping[Any, str] | None,
    measure_catalog: Mapping[str, Any],
) -> str:
    """Return a model-surface expression, never a physical-table expression."""
    if measure is not None and is_variant(measure):
        # Variants are semantic expressions owned by the query-router.  Keep
        # the bare model reference so the router can expand the window/calendar
        # expression before it chooses the source relation closure.
        return _semantic_ref(getattr(measure, "name", column.measure_name))
    if measure is not None and is_calculated(measure):
        return render_calculated_column_sql(
            measure,
            dialect=_PG,
            ref_measures_by_name=dict(measure_catalog),
            source_column_names=dict(source_column_names or {}),
        )

    ref = _source_ref(column, measure, source_column_names)

    aggregate = str(
        getattr(column, "aggregation_function", None)
        or getattr(column, "stat_type", None)
        or "sum"
    ).lower()
    if aggregate == "count_distinct":
        return f"COUNT(DISTINCT {ref})"
    if aggregate in _AGGREGATES:
        expression = f"{aggregate.upper()}({ref})"
        if measure is not None:
            expression = cast_for_agg(
                expression,
                aggregate,
                (source_numeric_types or {}).get(getattr(measure, "id", None)),
            )
        return expression
    # The layout resolver normally prevents this branch.  Keeping the fallback
    # deterministic is safer than silently changing a requested column into a
    # physical-source reference.
    return f"SUM({ref})"


def _passenger_parts(
    passenger_draft: Any | None,
    semantic_name_by_column_id: Mapping[str, str] | None,
) -> tuple[list[str], list[str]]:
    if passenger_draft is None:
        return [], []
    names: list[str] = []
    parts: list[str] = []
    semantic_names = semantic_name_by_column_id or {}
    for plan in getattr(passenger_draft, "plans", []) or []:
        detail_name = semantic_names.get(str(getattr(plan, "detail_column_id", "")))
        if not detail_name:
            raise ValueError(
                "Bug-8637 aggregate passenger detail column is not exposed by "
                "the model query surface; refusing an independent physical ref"
            )
        detail_ref = _semantic_ref(detail_name)
        passenger_name = str(plan.passenger_column)
        distinct_name = str(plan.detail_ndistinct_column)
        null_name = str(plan.detail_nullcount_column)
        parts.extend(
            [
                f"MIN({detail_ref}) AS {_semantic_ref(passenger_name)}",
                f"COUNT(DISTINCT {detail_ref}) AS {_semantic_ref(distinct_name)}",
                f"SUM(CASE WHEN {detail_ref} IS NULL THEN 1 ELSE 0 END) AS {_semantic_ref(null_name)}",
            ]
        )
        names.extend([passenger_name, distinct_name, null_name])
    return parts, names


def _quantile_columns(
    layout: ResolvedAggregateLayout,
    measures: Sequence[Any],
    include_quantiles: bool,
    source_column_names: Mapping[Any, str] | None,
) -> list[tuple[str, str]]:
    if not include_quantiles:
        return []

    by_name = _measure_by_name(measures)
    requested: list[tuple[str, str]] = []
    for column in layout.measure_cols:
        stat_type = str(getattr(column, "stat_type", ""))
        if is_quantile_stat_type(stat_type):
            measure = by_name.get(str(column.measure_name))
            requested.append(
                (
                    _source_ref(column, measure, source_column_names),
                    str(column.physical_col_name),
                )
            )
    if requested:
        columns = requested
    else:
        columns = []
        for measure in measures:
            if is_variant(measure) or is_calculated(measure):
                continue
            if str(getattr(measure, "default_agg", "") or "").lower() in ("sum", "avg"):
                for percentile in ROUTABLE_QUANTILE_PERCENTILES:
                    suffix = quantile_suffix(percentile)
                    columns.append(
                        (
                            _source_ref(None, measure, source_column_names),
                            bound_ident(f"{measure.name}__{suffix}"),
                        )
                    )

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for ref, output_name in columns:
        if output_name in seen:
            continue
        seen.add(output_name)
        suffix = output_name.rsplit("__", 1)[-1]
        fraction = quantile_suffix_to_fraction(suffix)
        if fraction is None:
            raise ValueError(f"Unsupported aggregate quantile column {output_name!r}")
        out.append(
            (
                f"PERCENTILE_CONT({fraction:g}) WITHIN GROUP (ORDER BY {ref})",
                output_name,
            )
        )
    return out


def _stat_columns(
    layout: ResolvedAggregateLayout,
    measures: Sequence[Any],
    include_stats: bool,
    source_column_names: Mapping[Any, str] | None,
) -> list[tuple[str, str]]:
    if not include_stats:
        return []
    by_name = _measure_by_name(measures)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for column in layout.measure_cols:
        stat_type = str(getattr(column, "stat_type", ""))
        if is_quantile_stat_type(stat_type) or is_stat_type(stat_type):
            continue
        measure_name = str(getattr(column, "measure_name", ""))
        measure = by_name.get(measure_name)
        if measure is None or is_variant(measure) or is_calculated(measure):
            continue
        aggregate = str(
            getattr(column, "aggregation_function", None)
            or getattr(column, "stat_type", None)
            or "sum"
        ).lower()
        if aggregate not in ("sum", "avg") or measure_name in seen:
            continue
        seen.add(measure_name)
        out.extend(
            (
                STAT_SQL_TEMPLATES[stat].format(
                    ref=_source_ref(column, measure, source_column_names),
                ),
                bound_ident(f"{measure_name}__{stat}"),
            )
            for stat in STAT_TYPES
        )
    return out


def build_aggregate_model_query(
    *,
    model_slug: str,
    layout: ResolvedAggregateLayout,
    measures: Sequence[Any],
    include_quantiles: bool = False,
    include_stats: bool = False,
    where_sql: str | None = None,
    passenger_draft: Any | None = None,
    semantic_name_by_column_id: Mapping[str, str] | None = None,
    source_numeric_types: Mapping[Any, str] | None = None,
    source_column_names: Mapping[Any, str] | None = None,
    measure_catalog: Sequence[Any] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Build the model-surface SQL submitted to ``/api/v1/explain``."""
    measure_by_id = _measure_by_id(measures)
    measure_by_name = _measure_by_name(measures)
    measure_catalog_by_name = _measure_by_name(measure_catalog or measures)
    select_parts: list[str] = []
    output_columns: list[str] = []

    group_by = [_semantic_ref(column.logical_name) for column in layout.grain_cols]
    for column in layout.grain_cols:
        output_name = str(column.physical_col_name)
        semantic_ref = _semantic_ref(column.logical_name)
        if output_name == str(column.logical_name):
            # A redundant alias makes the query-router treat the expression as
            # a physical projection and can suppress the dimension binder.  A
            # model-surface reference already has the required output name.
            select_parts.append(semantic_ref)
        else:
            select_parts.append(f"{semantic_ref} AS {_semantic_ref(output_name)}")
        output_columns.append(output_name)

    passenger_parts, passenger_names = _passenger_parts(
        passenger_draft, semantic_name_by_column_id,
    )
    select_parts.extend(passenger_parts)
    output_columns.extend(passenger_names)

    for column in layout.measure_cols:
        stat_type = str(getattr(column, "stat_type", ""))
        if is_quantile_stat_type(stat_type) or is_stat_type(stat_type):
            continue
        measure = _measure_for(column, measure_by_id, measure_by_name)
        output_name = str(column.physical_col_name)
        select_parts.append(
            f"{_plain_measure_expression(
                column,
                measure,
                source_numeric_types,
                source_column_names,
                measure_catalog_by_name,
            )} "
            f"AS {_semantic_ref(output_name)}"
        )
        output_columns.append(output_name)

    row_count_name = "__row_count__count"
    select_parts.append(f"COUNT(*) AS {_semantic_ref(row_count_name)}")
    output_columns.append(row_count_name)

    for expression, output_name in _quantile_columns(
        layout, measures, include_quantiles, source_column_names,
    ):
        select_parts.append(f"{expression} AS {_semantic_ref(output_name)}")
        output_columns.append(output_name)
    for expression, output_name in _stat_columns(
        layout, measures, include_stats, source_column_names,
    ):
        select_parts.append(
            f"{expression} AS {_semantic_ref(output_name)}"
        )
        output_columns.append(output_name)

    query = (
        "SELECT\n  "
        + ",\n  ".join(select_parts)
        + f"\nFROM {_semantic_ref(model_slug)}"
    )
    if where_sql:
        query += f"\nWHERE {where_sql}"
    if group_by:
        query += "\nGROUP BY " + ", ".join(group_by)
    return query, tuple(output_columns)


async def compile_aggregate_model_query(
    *,
    db: Any,
    model_id: object,
    layout: ResolvedAggregateLayout,
    measures: Sequence[Any],
    model_slug: str | None = None,
    tenant_id: str | None = None,
    include_quantiles: bool = False,
    include_stats: bool = False,
    where_sql: str | None = None,
    passenger_draft: Any | None = None,
    semantic_name_by_column_id: Mapping[str, str] | None = None,
    source_numeric_types: Mapping[Any, str] | None = None,
    source_column_names: Mapping[Any, str] | None = None,
    measure_catalog: Sequence[Any] | None = None,
) -> CompiledAggregateModelQuery:
    """Compile one aggregate build through the router source dispatcher."""
    if not model_slug:
        result = await db.execute(select(Model.slug).where(Model.id == model_id))
        model_slug = result.scalar_one_or_none()
    if not model_slug:
        raise ValueError(f"Model {model_id} has no usable slug for aggregate build")

    resolved_tenant_id = tenant_id or session_tenant_id(db)
    if not resolved_tenant_id:
        raise ValueError(
            "Bug-8637 aggregate build requires a tenant-bound session for the "
            "query-router service token"
        )
    raw_query, output_columns = build_aggregate_model_query(
        model_slug=model_slug,
        layout=layout,
        measures=measures,
        include_quantiles=include_quantiles,
        include_stats=include_stats,
        where_sql=where_sql,
        passenger_draft=passenger_draft,
        semantic_name_by_column_id=semantic_name_by_column_id,
        source_numeric_types=source_numeric_types,
        source_column_names=source_column_names,
        measure_catalog=measure_catalog,
    )
    rewritten_query = await _get_rewritten_sql(
        model_id, raw_query, _mint_service_token(resolved_tenant_id),
    )
    if not isinstance(rewritten_query, str) or not rewritten_query.strip():
        raise ValueError("Query router returned empty aggregate build SQL")
    logger.info(
        "[SOURCE_AUDIT] aggregate build model compile model_id=%s "
        "dispatcher=query-router endpoint=/api/v1/explain force_route=source",
        model_id,
    )
    return CompiledAggregateModelQuery(
        raw_query=raw_query,
        sql=rewritten_query,
        output_columns=output_columns,
    )
