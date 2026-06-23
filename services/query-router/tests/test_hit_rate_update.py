"""
Tests for aggregate hit-rate crediting (F-004-07).

The hit-credit moved out of ``find_best_aggregate`` (which only *matches*) and
into ``record_aggregate_hit``, which the router calls ONLY after a route has
actually been committed to the aggregate (validation passed, percentile gate
cleared, rewrite succeeded). This prevents crediting aggregates that match but
are subsequently rejected and routed to source — which inflated hit_count /
estimated_hit_rate and skewed eviction ranking.

Run from tessallite/services/query-router/:
    pytest tests/test_hit_rate_update.py
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.routing.aggregate_matcher import find_best_aggregate, record_aggregate_hit

from conftest import (
    make_measure,
    make_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"


async def test_matcher_does_not_credit_hit_on_match():
    """find_best_aggregate matches but must NOT issue the hit-credit UPDATE
    (F-004-07: the credit is deferred to the router after the route commits)."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)], agg_id="agg-hit-1")
    bq = make_bound_query([make_dimension("country")], [m])

    db = AsyncMock()
    db.execute = AsyncMock(return_value=None)

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, db)

    assert result.aggregate is agg
    db.execute.assert_not_called()


async def test_record_aggregate_hit_issues_update():
    """record_aggregate_hit issues the UPDATE that increments hit_count and
    advances estimated_hit_rate toward 1.0."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)], agg_id="agg-hit-2")

    db = AsyncMock()
    db.execute = AsyncMock(return_value=None)

    await record_aggregate_hit(agg, db)

    db.execute.assert_called_once()
    stmt = db.execute.call_args[0][0]
    compiled = str(stmt.compile())
    assert "aggregate_definitions" in compiled
    assert "hit_count" in compiled
    assert "estimated_hit_rate" in compiled


async def test_no_hit_rate_update_when_no_match():
    """When no aggregate matches, db.execute is NOT called by the matcher."""
    m = make_measure("revenue")
    m_other = make_measure("orders")
    agg = make_aggregate(["country"], [make_agg_col(m_other)], agg_id="agg-miss-1")
    bq = make_bound_query([make_dimension("country")], [m])

    db = AsyncMock()
    db.execute = AsyncMock(return_value=None)

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, db)

    assert result.aggregate is None
    db.execute.assert_not_called()
