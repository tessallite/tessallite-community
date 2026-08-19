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

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.ir.logical_query import (
    BoundQuery,
    DeployedSnapshotUnavailableError,
    SemanticBindingError,
)
from src.rewrite.join_graph_cache import _get_join_graph, _put_join_graph

if TYPE_CHECKING:
    # F-006-10: the local helpers below are annotated ``-> ModelTable | None``.
    # ModelTable is imported lazily inside ``_load_model_graph`` (to keep the
    # heavy shared.db.models import out of module load), so it was never bound
    # at module scope — harmless under ``from __future__ import annotations``
    # but it broke ``typing.get_type_hints`` and misled readers. Bind it for
    # type-checkers only.
    from shared.db.models import ModelTable


def _coerce_scalar(value: Any) -> Any:
    """Coerce a serialised snapshot scalar back to a native Python type.

    Same coercion as ``snapshot_resolver._coerce``: 36-char dashed strings to
    UUID, ISO datetimes to datetime. Everything else passes through. Duplicated
    here to avoid a cross-module import from the hot path.
    """
    if isinstance(value, str):
        if len(value) == 36 and value.count("-") == 4:
            try:
                return uuid.UUID(value)
            except ValueError:
                pass
        if len(value) >= 19 and value[4] == "-" and value[7] == "-" and value[10] in ("T", " "):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
    return value


def _hydrate_orm(model_cls: type, row: dict[str, Any], model_id: Any) -> Any:
    """Build a transient (un-persisted) ORM instance from a snapshot row.

    Only keys that map to real ORM columns are passed, so an extra serialised
    key does not blow up construction. ``model_id`` is re-stamped so every
    object is anchored to the model even though it is detached.
    """
    valid_cols = {c.name for c in model_cls.__table__.columns}
    kwargs: dict[str, Any] = {}
    for k, v in row.items():
        if k not in valid_cols:
            continue
        kwargs[k] = _coerce_scalar(v)
    if "model_id" in valid_cols:
        kwargs["model_id"] = model_id
    return model_cls(**kwargs)


def _build_graph_from_snapshot(
    snapshot: dict[str, Any], model_id: Any,
) -> tuple[dict, list, dict, dict] | None:
    """Build a (tables_by_id, joins, columns_by_id, uda_by_id) graph from
    the deployed snapshot's serialised table/column/join/UDA families.

    F-013-01: source SQL must be assembled from the DEPLOYED snapshot's
    physical graph, not from live ORM tables. A draft join/table/column/UDA
    change must not alter production SQL before the next deploy.

    Returns None if the snapshot does not carry enough graph data to hydrate.
    """
    from shared.db.models import Join, ModelColumn, ModelTable, UserDefinedAttribute

    snap_tables = snapshot.get("tables") or []
    snap_columns = snapshot.get("columns") or []
    snap_joins = snapshot.get("joins") or []
    snap_udas = snapshot.get("user_defined_attributes") or []

    # Must have at least tables and columns to form a valid graph.
    if not snap_tables:
        return None

    # Bug-8605 round-3 review: the stored list order of a snapshot written
    # before the serialiser emitted canonical order is NOT canonical, and a
    # version snapshot rehydrated from an imported bundle carries whatever
    # order that bundle had, permanently. Normalise on the way in so the
    # deployed graph is a pure function of the rows, exactly as the live
    # branch below already is.
    from shared.semantic.graph_order import (
        canonical_join_order,
        canonical_table_order,
    )

    tables_by_id = {}
    for t in canonical_table_order(
        _hydrate_orm(ModelTable, t, model_id) for t in snap_tables
    ):
        tables_by_id[t.id] = t

    columns_by_id = {}
    for c in snap_columns:
        obj = _hydrate_orm(ModelColumn, c, model_id)
        columns_by_id[obj.id] = obj

    joins = canonical_join_order(
        _hydrate_orm(Join, j, model_id) for j in snap_joins
    )

    uda_by_id = {}
    for u in snap_udas:
        obj = _hydrate_orm(UserDefinedAttribute, u, model_id)
        uda_by_id[obj.id] = obj

    return tables_by_id, joins, columns_by_id, uda_by_id


async def _load_model_graph(bound_query: BoundQuery, db: Any, uda_ids: set) -> tuple:
    """Load (and cache) the model graph: tables, joins, columns, UDAs.

    F-013-01 (Bug-7979 fail-closed): when the model is deployed, the graph is
    built from the DEPLOYED snapshot (tables/columns/joins/UDAs serialised at
    save+deploy time). Draft changes to the physical graph do not affect
    production source SQL until the next deploy. The cache is keyed by
    ``(model_id, deployed_version_id, deploy_epoch)`` so a deploy/revert
    naturally invalidates the cache entry.

    For undeployed models (no deployed_version_id), the live ORM tables are
    the authority (original behaviour).

    The two branches are mutually exclusive and there is NO path from the
    deployed branch to the live branch (Bug-7981 round 3). A deployed model
    either builds its graph from its pinned snapshot or raises
    ``DeployedSnapshotUnavailableError`` (-> HTTP 503); it never reads live
    rows and never writes live rows under a deployed cache key.

    Returns ``(tables_by_id, joins, columns_by_id, uda_by_id)``.
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import ModelColumn, ModelTable, UserDefinedAttribute

    model = bound_query.model
    deployed_version_id = getattr(model, "deployed_version_id", None)
    deploy_epoch = getattr(model, "deploy_epoch", 0) or 0

    # Cache key includes version/epoch so the graph is pinned per deployment
    # and self-invalidates on deploy/revert. For undeployed models, fallback
    # to the bare model_id key with epoch 0 / version None (original scheme).
    cache_key = (str(model.id), str(deployed_version_id), deploy_epoch)
    _cached_graph = _get_join_graph(cache_key)
    if _cached_graph is not None:
        tables_by_id = dict(_cached_graph.tables_by_id)
        joins = _cached_graph.joins
        columns_by_id = dict(_cached_graph.columns_by_id)
        uda_by_id = dict(_cached_graph.uda_by_id)
        # Bug-894: refill UDAs not present in the cache (built before fix, or
        # models that gained UDAs between the cache-build and now).
        _missing_udas = uda_ids - set(uda_by_id)
        if _missing_udas and db is not None:
            if deployed_version_id is not None:
                # Deployed model: UDAs should come from the snapshot. If they're
                # missing from the snapshot-built cache, the snapshot was built
                # before they were added — fail closed (leave unresolved).
                pass
            else:
                _refill = await db.execute(
                    sa_select(UserDefinedAttribute).where(
                        UserDefinedAttribute.id.in_(_missing_udas)
                    )
                )
                for _ua in _refill.scalars().all():
                    uda_by_id[_ua.id] = _ua
    elif deployed_version_id is not None:
        # ------------------------------------------------------------------
        # DEPLOYED model: the pinned snapshot is the ONLY physical-graph
        # authority. There is deliberately no live-ORM fallback on this branch.
        # ------------------------------------------------------------------
        # Bug-7981: consume the SAME request-selected deployed snapshot the
        # binder used. A backward revert deletes every newer ModelVersion at
        # commit; independently re-resolving the version row here let an
        # already-bound request observe ``None`` after commit and fall through
        # to the rehydrated LIVE graph, mixing old semantic objects with new
        # tables/columns/joins.
        #
        # Two independent guarantees now make that impossible:
        #  1. ``BoundQuery.deployed_shape`` carries the binder's exact shape.
        #  2. ``resolve_deployed_shape`` is request-pinned (see the contract in
        #     ``semantic/snapshot_resolver.py``), so ANY consumer -- including
        #     one that does not thread the shape -- gets the identical object
        #     for the request's deployment identity even after the process
        #     cache is evicted and the version row is deleted.
        from src.semantic.snapshot_resolver import resolve_deployed_shape

        deployed_shape = getattr(bound_query, "deployed_shape", None)
        if deployed_shape is None:
            deployed_shape = await resolve_deployed_shape(model, db)
        if deployed_shape is None:
            # A deployed identity must never populate its versioned cache key
            # from mutable live ORM rows.
            raise DeployedSnapshotUnavailableError(
                "The deployed snapshot selected for this query is no longer "
                "available; the query was blocked to prevent mixed-version "
                "source SQL. Retry against the current deployment."
            )
        snapshot = {
            "tables": list(
                (getattr(deployed_shape, "tables_by_id", {}) or {}).values()
            ),
            "columns": list(
                (getattr(deployed_shape, "columns_by_id", {}) or {}).values()
            ),
            "joins": list(
                getattr(deployed_shape, "join_rows", []) or []
            ),
            "user_defined_attributes": list(
                getattr(
                    deployed_shape,
                    "user_defined_attribute_rows",
                    [],
                )
                or []
            ),
        }
        graph = _build_graph_from_snapshot(snapshot, model.id)
        if graph is None:
            # Bug-7981 round 3 (CRITICAL): the pinned shape carries semantic
            # members but no physical tables. The previous code fell through to
            # the live ORM here and then cached those mutable draft rows UNDER
            # THE DEPLOYED VERSION/EPOCH KEY -- both a direct F-013-01 breach
            # (unpublished table/join/column edits reach production SQL) and a
            # poisoned deployed cache entry served to every later request.
            #
            # There is no correct live fallback for a deployed model: the live
            # graph is by definition a different authority from the one the
            # request's semantic objects came from. Fail closed, exactly as
            # Bug-8306 already decided for the columns-without-tables shape.
            # Nothing is written to the cache on this path.
            raise DeployedSnapshotUnavailableError(
                "The deployed snapshot for this model carries no physical "
                "table graph, so source SQL cannot be generated from the "
                "deployed contract. Re-deploy the model to repair it; the "
                "query was blocked rather than served from unpublished draft "
                "tables."
            )
        tables_by_id, joins, columns_by_id, uda_by_id = graph
        _put_join_graph(
            cache_key, tables_by_id, joins, columns_by_id, uda_by_id,
        )
    else:
        # ------------------------------------------------------------------
        # UNDEPLOYED model: the live ORM IS the authority (authoring paths).
        # ------------------------------------------------------------------
        # Bug-8605: canonically ordered like every other model-graph read on
        # this path, so a future positional consumer cannot inherit an
        # unordered one. (The dict below is order-insensitive today.)
        from shared.semantic.graph_order import order_model_columns

        result = await db.execute(
            order_model_columns(
                sa_select(ModelColumn).where(ModelColumn.model_table_id.in_(
                    sa_select(ModelTable.id).where(ModelTable.model_id == model.id)
                ))
            )
        )
        columns_by_id = {c.id: c for c in result.scalars().all()}

        result = await db.execute(
            sa_select(UserDefinedAttribute).where(
                UserDefinedAttribute.model_id == model.id
            )
        )
        uda_by_id = {a.id: a for a in result.scalars().all()}

        # Bug-8605: canonical ``id`` order, the same order
        # ``_build_graph_from_snapshot`` normalises the deployed graph to, so
        # the live authoring path and the deployed path enumerate identically.
        # Table order feeds positional base-table selection; join order feeds
        # the JOIN planner's tie-breaking between equally short paths, which
        # decides which INTERMEDIATE tables reach the FROM clause.
        from shared.semantic.graph_order import (
            canonical_join_order,
            canonical_table_order,
            select_model_joins,
            select_model_tables,
        )

        result = await db.execute(select_model_tables(model.id))
        tables_by_id = {
            t.id: t for t in canonical_table_order(result.scalars().all())
        }

        result = await db.execute(select_model_joins(model.id))
        joins = canonical_join_order(result.scalars().all())

        _put_join_graph(
            cache_key, tables_by_id, joins, columns_by_id, uda_by_id,
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
    #
    # Bug-7192: the table is needed at ALL grains, not only when time is in
    # the GROUP BY.  A grand total (no GROUP BY) or a non-time grain
    # (GROUP BY region) must still return LAST/FIRST non-empty balance
    # across the time domain, and the ordering column lives on the time
    # dimension's table.  Without this join, _sa_finest_time_col_id
    # resolves to None in source_sql.py and the measure silently degrades
    # to SUM.
    if _sa_finest_time_col_id:
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
            # Bug-8605: the shared fact test, so COUNT(*) cannot disagree with
            # the anchor rule about which row is the fact table.
            from shared.semantic.graph_order import is_fact_table

            for _tbl in tables_by_id.values():
                if is_fact_table(_tbl):
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
