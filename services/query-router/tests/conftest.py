"""
Shared fixtures for query-router tests.

sys.path is configured via [tool.pytest.ini_options] pythonpath in pyproject.toml:
  - "."  → tessallite/services/query-router/   (enables "from src.xxx import")
  - "../../" → tessallite/                     (enables "from shared.xxx import")
"""
from __future__ import annotations
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Lightweight mock factories (use SimpleNamespace — no SQLAlchemy needed)
# ---------------------------------------------------------------------------

def make_measure(
    name: str,
    default_agg: str = "sum",
    is_additive: bool = True,
    *,
    measure_type: str = "standard",
    expression: str | None = None,
    calc_agg_mode: str | None = None,
    semi_additive_behavior: str | None = None,
    variant_kind: str | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"m-{name}",
        name=name,
        default_agg=default_agg,
        is_additive=is_additive,
        measure_type=measure_type,
        expression=expression,
        calc_agg_mode=calc_agg_mode,
        semi_additive_behavior=semi_additive_behavior,
        variant_kind=variant_kind,
    )


def make_dimension(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=f"d-{name}", name=name)


def make_hierarchy_dimension(
    name: str,
    *,
    hierarchy_id: str = "h-1",
    hierarchy_name: str = "Date",
    ordinal: int = 1,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"hlevel-{name}",
        name=name,
        is_hierarchy_level=True,
        hierarchy_id=hierarchy_id,
        hierarchy_name=hierarchy_name,
        hierarchy_level_id=f"lvl-{name}",
        hierarchy_level_ordinal=ordinal,
        dimension_kind="time",
        is_time_dim=True,
    )


def make_agg_col(
    measure: types.SimpleNamespace,
    stat_type: str | None = None,
) -> types.SimpleNamespace:
    st = stat_type or measure.default_agg or "sum"
    return types.SimpleNamespace(
        measure=measure,
        stat_type=st,
        physical_col_name=f"{measure.name}__{st}",
    )


def make_aggregate(
    grain: list[str],
    columns: list,
    *,
    status: str = "active",
    age_hours: int = 0,
    agg_id: str = "agg-1",
    physical_table_name: str = "agg_table",
    target_schema: str = "aggregates",
    persona_id: str | None = None,
) -> types.SimpleNamespace:
    last_refreshed = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return types.SimpleNamespace(
        id=agg_id,
        grain=grain,
        columns=columns,
        status=status,
        last_refreshed_at=last_refreshed,
        physical_table_name=physical_table_name,
        target_schema=target_schema,
        persona_id=persona_id,
    )


def make_bound_query(
    dimensions: list,
    measures: list,
    *,
    filters=None,
    raw_sql: str = "SELECT 1",
    order_by=None,
    limit=None,
    offset=None,
    grain=None,
    select_star: bool = False,
    has_distinct: bool = False,
    all_dimensions: list | None = None,
):
    from src.ir.logical_query import LogicalQuery, BoundQuery

    model = types.SimpleNamespace(id="model-1", slug="test_model", deployed_version_id="v1")
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query=raw_sql,
        requested_measures=[m.name for m in measures],
        requested_dimensions=[d.name for d in dimensions],
        filters=filters or [],
        grain=grain if grain is not None else [d.name for d in dimensions],
        order_by=order_by or [],
        limit=limit,
        offset=offset,
        query_fingerprint="test_fingerprint_abc123",
        select_star=select_star,
        has_distinct=has_distinct,
    )
    dim_map = {d.name: d for d in dimensions}
    if all_dimensions:
        for d in all_dimensions:
            dim_map.setdefault(d.name, d)
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=filters or [],
        resolved_dimensions_by_name=dim_map,
    )


@pytest.fixture(autouse=True)
def _patch_inactive_aggregates():
    # Bug-874: clear module-level join graph cache between tests to prevent
    # stale column references from polluting subsequent test runs.
    from src.rewrite.query_rewriter import invalidate_join_graph_cache
    invalidate_join_graph_cache()
    # F-003-14: clear the versioned deployed-shape and live-metadata caches so a
    # prior test's mocked metadata (same (model_id, deployed_version_id) key)
    # cannot leak into the next. These caches are keyed by the deploy version, so
    # in production they self-invalidate on re-deploy; only tests reuse a key with
    # different fixture data, so the clear is a test-isolation concern only.
    from src.semantic.snapshot_resolver import (
        invalidate as _invalidate_snapshot_cache,
        invalidate_live_metadata as _invalidate_live_metadata_cache,
    )
    _invalidate_snapshot_cache()
    _invalidate_live_metadata_cache()
    # F-004-17: clear the per-version canonical dimension cache so a prior test's
    # patched/empty list cannot leak into the next test under the same key.
    from src.routing.aggregate_matcher import invalidate_canonical_dim_cache
    invalidate_canonical_dim_cache()

    with (
        patch(
            "src.routing.aggregate_matcher.load_inactive_aggregates",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "shared.semantic.canonical_dimensions.build_canonical_dimension_list",
            new_callable=AsyncMock,
            return_value=[],
        ),
        # F-004-14: ``router.resolve_target_dialect`` was an unused import,
        # removed from router.py; only ``resolve_target_dialect_for_bound`` is
        # actually called. The stale patch on the removed symbol is dropped.
        patch(
            "src.routing.router.resolve_target_dialect_for_bound",
            new_callable=AsyncMock,
            return_value="postgres",
        ),
        patch(
            "src.rewrite.source_sql._resolve_target_dialect",
            new_callable=AsyncMock,
            return_value="postgres",
        ),
        patch(
            "src.routing.pocket_matcher.get_setting",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        yield
