"""Target-dialect resolution (DB-backed) for the query rewriter.

Resolves the SQL dialect for a model / bound query from its source
connection(s). ``_resolve_target_dialect`` is the monkeypatch seam used by the
test suite (patched at ``src.rewrite.dialect_resolution._resolve_target_dialect``).

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import inspect
from typing import Any

from src.rewrite.dialects import _dialect_from_connection_type


async def resolve_target_dialect(db: Any, model_id: Any) -> str:
    """Public alias for the target-dialect resolver — used by router.py."""
    return await _resolve_target_dialect(db, model_id)



async def resolve_target_dialect_for_bound(db: Any, bound_query: Any) -> str:
    """Resolve the SQL dialect from the source the bound query actually touches.

    Fixes Bug-899 (multi-source dialect mismatch): the original
    resolve_target_dialect used DataSource.limit(1) which picked an arbitrary
    source for the model.  execute_routed_query resolves the source from the
    columns the query touches, so the two could disagree on multi-source
    models and produce SQL rewritten for source A but executed against source B.

    This function mirrors the touched-source logic in routes._collect_touched_source_ids:
    - Collect source_column_id values from resolved_dimensions + resolved_measures.
    - Map them through ModelColumn → ModelTable → DataSource.
    - If exactly one source is touched, use it.
    - If zero columns resolve (passthrough / SELECT 1), fall back to the
      model's first DataSource (same as _resolve_target_dialect).
    - Multi-source: routes.execute_routed_query will raise CROSS_SOURCE_UNSUPPORTED
      before execution; we pick any source here so rewriting has a dialect.
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import DataSource, Dimension as DimensionORM, Measure as MeasureORM, ModelColumn, ModelTable, ProjectConnection
    from shared.schemas.connection_type import normalize_connection_type

    # Phase 1 — source_column_ids from SELECT/GROUP BY objects.
    column_ids: set = {
        obj.source_column_id
        for obj in (
            list(getattr(bound_query, "resolved_dimensions", []) or [])
            + list(getattr(bound_query, "resolved_measures", []) or [])
        )
        if getattr(obj, "source_column_id", None) is not None
    }

    # Phase 2 — Bug-903: also include filter-only and order-only columns which
    # may live on a different source than the selected columns.
    filter_names = {
        f.dimension_name
        for f in getattr(bound_query, "resolved_filters", []) or []
    }
    order_names = {
        col
        for col, _ in (
            getattr(getattr(bound_query, "logical_query", None), "order_by", None) or []
        )
    }
    extra_names = (filter_names | order_names) - {
        getattr(obj, "name", "") for obj in (
            list(getattr(bound_query, "resolved_dimensions", []) or [])
            + list(getattr(bound_query, "resolved_measures", []) or [])
        )
    }
    if extra_names and getattr(getattr(bound_query, "model", None), "id", None):
        model_id_extra = bound_query.model.id
        dim_r = await db.execute(
            sa_select(DimensionORM.source_column_id)
            .where(
                DimensionORM.model_id == model_id_extra,
                DimensionORM.name.in_(extra_names),
                DimensionORM.source_column_id.isnot(None),
            )
        )
        column_ids |= {row[0] for row in dim_r.all()}
        meas_r = await db.execute(
            sa_select(MeasureORM.source_column_id)
            .where(
                MeasureORM.model_id == model_id_extra,
                MeasureORM.name.in_(extra_names),
                MeasureORM.source_column_id.isnot(None),
            )
        )
        column_ids |= {row[0] for row in meas_r.all()}

    source_id: Any = None
    if column_ids:
        result = await db.execute(
            sa_select(ModelTable.source_id)
            .join(ModelColumn, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelColumn.id.in_(column_ids))
            .distinct()
            .limit(2)
        )
        rows = result.all()
        if rows:
            source_id = rows[0][0]

    source = None
    if source_id is not None:
        source = await db.get(DataSource, source_id)

    if source is None:
        # No touched columns or source not found — fall back to model default.
        result = await db.execute(
            sa_select(DataSource)
            .where(DataSource.model_id == bound_query.model.id)
            .order_by(DataSource.created_at)
            .limit(1)
        )
        source = result.scalar_one_or_none()

    if source is None:
        return "postgres"

    conn = await db.get(ProjectConnection, source.project_connection_id)
    if conn is None:
        return "postgres"

    connector_type = normalize_connection_type((conn.connection_type or "").lower())
    return _dialect_from_connection_type(connector_type)



async def _resolve_target_dialect(db: Any, model_id: Any) -> str:
    """
    Resolve connector dialect from the model's primary source connection.
    Defaults to postgres-compatible SQL.

    Uses the first DataSource ordered by creation time. For queries where
    the correct source is already known, use resolve_target_dialect_for_bound
    instead — it derives the dialect from the columns the query actually touches.
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import DataSource, ProjectConnection
    from shared.schemas.connection_type import normalize_connection_type

    result = await db.execute(
        sa_select(DataSource)
        .where(DataSource.model_id == model_id)
        .order_by(DataSource.created_at)
        .limit(1)
    )
    source = result.scalar_one_or_none()
    if inspect.isawaitable(source):
        source = await source
    if source is None:
        return "postgres"

    conn = await db.get(ProjectConnection, source.project_connection_id)
    if conn is None:
        return "postgres"

    connector_type = normalize_connection_type((conn.connection_type or "").lower())
    return _dialect_from_connection_type(connector_type)

