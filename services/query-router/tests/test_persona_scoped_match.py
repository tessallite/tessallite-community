"""Phase 8.C.2 — aggregate and pocket matcher persona scoping.

Invariants under test:

  * An aggregate with ``persona_id=X`` is only considered for
    queries bound to persona X.
  * When the same query matches both a persona-scoped and a global
    aggregate, the persona-scoped one wins.
  * Queries with no persona bound still see global aggregates and
    never see a persona-scoped one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.routing.aggregate_matcher import find_best_aggregate

from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"

pytestmark = pytest.mark.integration


def _q():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    return _bind(sql, [m], [d]), m


async def test_persona_scoped_aggregate_wins_over_global():
    bq, m = _q()
    agg_global = make_aggregate(
        ["region_code"], [make_agg_col(m)], agg_id="agg-global"
    )
    agg_scoped = make_aggregate(
        ["region_code"],
        [make_agg_col(m)],
        agg_id="agg-scoped",
        persona_id="pers-1",
    )

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_global, agg_scoped]
        chosen = await find_best_aggregate(bq, AsyncMock(), persona_id="pers-1")

    assert chosen.aggregate is agg_scoped


async def test_persona_scoped_aggregate_is_invisible_to_other_personas():
    bq, m = _q()
    agg_scoped = make_aggregate(
        ["region_code"],
        [make_agg_col(m)],
        agg_id="agg-scoped",
        persona_id="pers-1",
    )

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_scoped]
        chosen = await find_best_aggregate(bq, AsyncMock(), persona_id="pers-2")

    assert chosen.aggregate is None


async def test_global_aggregate_still_serves_persona_queries():
    bq, m = _q()
    agg_global = make_aggregate(
        ["region_code"], [make_agg_col(m)], agg_id="agg-global"
    )

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_global]
        chosen = await find_best_aggregate(bq, AsyncMock(), persona_id="pers-1")

    assert chosen.aggregate is agg_global


async def test_no_persona_query_does_not_pick_up_scoped_aggregate():
    bq, m = _q()
    agg_global = make_aggregate(
        ["region_code"], [make_agg_col(m)], agg_id="agg-global"
    )
    agg_scoped = make_aggregate(
        ["region_code"],
        [make_agg_col(m)],
        agg_id="agg-scoped",
        persona_id="pers-1",
    )

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_global, agg_scoped]
        chosen = await find_best_aggregate(bq, AsyncMock())

    assert chosen.aggregate is agg_global
