"""Physical table resolution for the source-path SQL builder.

Two helpers carved out of ``_build_source_sql`` (Phase 3 internal
decomposition; behaviour-identical, extracted byte-for-byte):

- ``_load_model_graph`` — load (and cache) the model's tables, joins,
  columns, and UDAs for a query.
- ``_resolve_required_and_base_tables`` — map the query's dimensions,
  measures, filters, and ORDER BY references to the physical tables that
  must appear in the FROM/JOIN graph, then pick the base (fact/dimension)
  table.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.ir.logical_query import BoundQuery, SemanticBindingError
from src.rewrite.join_graph_cache import _get_join_graph, _put_join_graph

if TYPE_CHECKING:
    # F-006-10: the local helpers below are annotated ``-> ModelTable | None``.
    # ModelTable is imported lazily inside ``_load_model_graph`` (to keep the
    # heavy shared.db.models import out of module load), so it was never bound
    # at module scope — harmless under ``from __future__ import annotations``
    # but it broke ``typing.get_type_hints`` and misled readers. Bind it for
    # type-checkers only.
    from shared.db.models import ModelTable


async def _load_model_graph(bound_query: BoundQuery, db: Any, uda_ids: set) -> tuple:
    """Load (and cache) the model graph: tables, joins, columns, UDAs.

    Returns ``(tables_by_id, joins, columns_by_id, uda_by_id)``.
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import Join, ModelColumn, ModelTable, UserDefinedAttribute

    _cached_graph = _get_join_graph(bound_query.model.id)
    if _cached_graph is not None:
        tables_by_id = dict(_cached_graph.tables_by_id)
        joins = _cached_graph.joins
        columns_by_id = dict(_cached_graph.columns_by_id)
        uda_by_id = dict(_cached_graph.uda_by_id)
        # Bug-894: refill UDAs not present in the cache (built before fix, or
        # models that gained UDAs between the cache-build and now).
        _missing_udas = uda_ids - set(uda_by_id)
        if _missing_udas and db is not None:
            _refill = await db.execute(
                sa_select(UserDefinedAttribute).where(
                    UserDefinedAttribute.id.in_(_missing_udas)
                )
            )
            for _ua in _refill.scalars().all():
                uda_by_id[_ua.id] = _ua
    else:
        # Load all columns for the model (avoids per-query targeted loads)
        result = await db.execute(
            sa_select(ModelColumn).where(ModelColumn.model_table_id.in_(
                sa_select(ModelTable.id).where(ModelTable.model_id == bound_query.model.id)
            ))
        )
        columns_by_id = {c.id: c for c in result.scalars().all()}

        # Load ALL UDAs for the model so the cache is complete for every
        # query, not just the first one that populates it (Bug-894).
        result = await db.execute(
            sa_select(UserDefinedAttribute).where(
                UserDefinedAttribute.model_id == bound_query.model.id
            )
        )
        uda_by_id = {a.id: a for a in result.scalars().all()}

        # Load all tables for the model
        result = await db.execute(
            sa_select(ModelTable).where(ModelTable.model_id == bound_query.model.id)
        )
        tables_by_id = {t.id: t for t in result.scalars().all()}

        # Load all joins for the model
        result = await db.execute(
            sa_select(Join).where(Join.model_id == bound_query.model.id)
        )
        joins = list(result.scalars().all())

        # Cache the model graph
        _put_join_graph(
            bound_query.model.id, tables_by_id, joins, columns_by_id, uda_by_id,
        )
    return tables_by_id, joins, columns_by_id, uda_by_id


def _resolve_required_and_base_tables(
    bound_query: BoundQuery,
    columns_by_id: dict,
    tables_by_id: dict,
    uda_by_id: dict,
    dimensions_by_name: dict,
    filter_dim_names: set,
    order_col_names: set,
    _order_measures: list,
    calc_ref_measures_by_name: dict,
    _sa_finest_time_col_id: Any,
    _sa_has_time_in_grain: bool,
) -> tuple:
    """Resolve the required physical tables and the base table.

    Returns ``(required_table_ids, base_table)``; raises
    ``SemanticBindingError`` when no physical table can be mapped.
    """
    def _table_for_column(column_id: Any) -> ModelTable | None:
        col = columns_by_id.get(column_id)
        return tables_by_id.get(col.model_table_id) if col else None

    def _table_for_dimension(dim: Any) -> ModelTable | None:
        if not dim:
            return None
        source_col_id = getattr(dim, "source_column_id", None)
        if source_col_id:
            return _table_for_column(source_col_id)
        uda_id = getattr(dim, "user_defined_attribute_id", None)
        if uda_id:
            uda = uda_by_id.get(uda_id)
            return tables_by_id.get(uda.table_id) if uda else None
        return None

    def _table_for_measure(measure: Any) -> ModelTable | None:
        if not measure:
            return None
        source_col_id = getattr(measure, "source_column_id", None)
        if source_col_id:
            return _table_for_column(source_col_id)
        uda_id = getattr(measure, "user_defined_attribute_id", None)
        if uda_id:
            uda = uda_by_id.get(uda_id)
            return tables_by_id.get(uda.table_id) if uda else None
        return None

    required_table_ids: set[Any] = set()
    for dim in bound_query.resolved_dimensions:
        tbl = _table_for_dimension(dim)
        if tbl:
            required_table_ids.add(tbl.id)
    for meas in bound_query.resolved_measures:
        tbl = _table_for_measure(meas)
        if tbl:
            required_table_ids.add(tbl.id)
    # Phase 4A — pull in tables referenced by calculated measures' base
    # measures so the JOIN planner sees them.
    for _ref_meas in calc_ref_measures_by_name.values():
        tbl = _table_for_measure(_ref_meas)
        if tbl:
            required_table_ids.add(tbl.id)
    for dim_name in filter_dim_names:
        dim = dimensions_by_name.get(dim_name)
        tbl = _table_for_dimension(dim)
        if tbl:
            required_table_ids.add(tbl.id)
    # ORDER BY columns may reference tables not in SELECT — include them.
    for dim_name in order_col_names:
        dim = dimensions_by_name.get(dim_name)
        tbl = _table_for_dimension(dim) if dim else None
        if tbl:
            required_table_ids.add(tbl.id)
    for meas in _order_measures:
        tbl = _table_for_measure(meas)
        if tbl:
            required_table_ids.add(tbl.id)

    # Semi-additive: include the finest time dimension's table in the JOIN
    # so the ordering column is accessible.
    if _sa_finest_time_col_id and _sa_has_time_in_grain:
        _sa_col = columns_by_id.get(_sa_finest_time_col_id)
        if _sa_col:
            required_table_ids.add(_sa_col.model_table_id)

    if not required_table_ids:
        # Phase 2 fail-loud (Finding 3): the query references columns,
        # measures, or dimensions but none could be mapped to a physical
        # source table.  Returning the raw (semantic) query would skip
        # physical-name substitution and leak semantic names to the source.
        raise SemanticBindingError(
            "Cannot rewrite query to source SQL: the query references "
            "columns or measures but none could be mapped to a physical "
            "source table."
        )

    base_table = None
    for meas in bound_query.resolved_measures:
        base_table = _table_for_measure(meas)
        if base_table:
            break
    # Calculated measures have no source_column_id themselves; fall back to
    # any referenced base measure so queries against only calculated
    # measures (no dims, no regular measures) still pick a fact table.
    if base_table is None:
        for ref_meas in calc_ref_measures_by_name.values():
            base_table = _table_for_measure(ref_meas)
            if base_table:
                break
    # __row_count has no source_column_id so the measure loop yields nothing.
    # Pick the first fact table from tables_by_id to avoid falling through to
    # a dimension table (which counts dimension rows, not fact rows). (Bug-119)
    if base_table is None:
        if any(m.name == "__row_count" for m in bound_query.resolved_measures):
            for _tbl in tables_by_id.values():
                if getattr(_tbl, "table_type", None) == "fact":
                    base_table = _tbl
                    break
    if base_table is None:
        for dim in bound_query.resolved_dimensions:
            base_table = _table_for_dimension(dim)
            if base_table:
                break
    if base_table is None:
        for dim_name in filter_dim_names:
            dim = dimensions_by_name.get(dim_name)
            base_table = _table_for_dimension(dim)
            if base_table:
                break
    # ORDER BY-only measures may be the only table reference.
    if base_table is None:
        for meas in _order_measures:
            base_table = _table_for_measure(meas)
            if base_table:
                break
    if base_table is None:
        for dim_name in order_col_names:
            dim = dimensions_by_name.get(dim_name)
            if dim:
                base_table = _table_for_dimension(dim)
                if base_table:
                    break
    if base_table is None:
        # Phase 2 fail-loud (Finding 3): no base (fact/dimension) table
        # could be resolved for the requested measures and dimensions.
        raise SemanticBindingError(
            "Cannot rewrite query to source SQL: no base (fact or "
            "dimension) table could be resolved for the requested "
            "measures and dimensions."
        )

    return required_table_ids, base_table
