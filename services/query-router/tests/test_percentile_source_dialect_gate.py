"""Bug-987: the percentile exactness gate must key on the aggregate's SOURCE
dialect (where the quantile column was computed), not the target table dialect.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

from shared.aggregate_quantiles import (
    quantile_materialization_is_exact,
    quantiles_are_exact,
)


async def _source_dialect_for(connection_type: str | None):
    from src.routing.router import _resolve_aggregate_source_dialect

    agg = types.SimpleNamespace(model_id="model-1")
    db = AsyncMock()
    conn = (
        types.SimpleNamespace(connection_type=connection_type)
        if connection_type is not None
        else None
    )
    with patch(
        "shared.aggregate_connection.resolve_source_connection",
        new=AsyncMock(return_value=conn),
    ):
        return await _resolve_aggregate_source_dialect(agg, db)


async def test_bigquery_source_resolves_to_approximate():
    d = await _source_dialect_for("bigquery")
    assert not quantiles_are_exact(d)  # bigquery quantiles are approximate → gated


async def test_postgres_source_resolves_to_exact():
    d = await _source_dialect_for("postgresql")
    assert quantiles_are_exact(d)  # postgres quantiles are exact → routable


async def test_spark_cross_engine_gate_is_not_exact():
    # Bug-1007: the router percentile gate now uses BOTH source and target. A
    # Spark-source aggregate materialised into a Postgres target (cross-engine)
    # skips pNN materialisation, so the gate must treat it as NOT exact and route
    # the percentile query to source — even though the source dialect alone
    # (quantiles_are_exact("spark")) is True.
    src = await _source_dialect_for("hadoop_spark")
    assert quantiles_are_exact(src)  # source-only check would wrongly allow routing
    assert not quantile_materialization_is_exact(src, "postgresql")  # gate blocks cross-engine
    assert quantile_materialization_is_exact(src, src)  # same-engine Spark stays exact


async def test_unresolvable_source_returns_none():
    # resolve_source_connection raising (e.g. multi-source) → None so the caller
    # falls back to the target dialect rather than crashing the route.
    from src.routing.router import _resolve_aggregate_source_dialect

    agg = types.SimpleNamespace(model_id="model-1")
    with patch(
        "shared.aggregate_connection.resolve_source_connection",
        new=AsyncMock(side_effect=ValueError("multiple sources")),
    ):
        assert await _resolve_aggregate_source_dialect(agg, AsyncMock()) is None


async def test_missing_model_id_returns_none():
    from src.routing.router import _resolve_aggregate_source_dialect

    assert await _resolve_aggregate_source_dialect(types.SimpleNamespace(), AsyncMock()) is None
