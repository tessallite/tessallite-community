"""Guard test for Bug-7796 / F-006-01: DAX inline agg override HITs the
override stat column on the aggregate route.

The aggregate rewriter reads measure_agg_overrides the same way source_sql
does, so a SUM override of an avg-default measure matches amount__sum.
A case-insensitive override against an avg-only aggregate still misses
because the sum column is absent — not because of a whole-query bail.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_measure, make_dimension, make_agg_col, make_aggregate, make_bound_query
from src.routing.aggregate_matcher import (
    find_best_aggregate,
    compute_has_non_additive,
    AggregateSkipReason,
)
from src.rewrite.query_rewriter import rewrite_for_aggregate

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"

pytestmark = pytest.mark.asyncio


async def test_dax_sum_override_hits_amount_sum():
    """F-006-01 / F-004-04 / F-102-05: SUM override of avg-default HITs
    amount__sum when the aggregate stores that column.
    """
    m = make_measure("amount", default_agg="avg")
    agg = make_aggregate(
        ["country"],
        [
            make_agg_col(m, "avg"),
            make_agg_col(m, "sum"),
            make_agg_col(m, "count"),
        ],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.measure_agg_overrides = {"amount": "sum"}

    with patch(_PATCH, new_callable=AsyncMock, return_value=[agg]):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg
    sql = rewrite_for_aggregate(bq, agg)
    assert "amount__sum" in sql


async def test_dax_sum_override_case_insensitive_misses_when_sum_absent():
    """Override key may differ in case. Missing sum column is STAT_TYPE_MISMATCH,
    not a whole-query override bail.
    """
    m = make_measure("Amount", default_agg="avg")
    agg = make_aggregate(["country"], [make_agg_col(m, "avg")])
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.measure_agg_overrides = {"amount": "sum"}

    with patch(_PATCH, new_callable=AsyncMock, return_value=[agg]):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None
    assert AggregateSkipReason.STAT_TYPE_MISMATCH in result.skip_reasons


async def test_no_override_uses_default_agg():
    """Without an agg override, the measure's default_agg drives the
    required stat (pre-7796 behaviour preserved). Aggregate matches.
    """
    m = make_measure("amount", default_agg="avg")
    agg = make_aggregate(
        ["country"],
        [
            make_agg_col(m, "avg"),
            make_agg_col(m, "sum"),
            make_agg_col(m, "count"),
        ],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    # No override -- default_agg=avg is the required stat.

    with patch(_PATCH, new_callable=AsyncMock, return_value=[agg]):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg


async def test_override_on_unrelated_measure_does_not_affect_match():
    """An override for a measure NOT in the query's resolved_measures
    does not trigger the decline.
    """
    m = make_measure("amount", default_agg="avg")
    agg = make_aggregate(
        ["country"],
        [
            make_agg_col(m, "avg"),
            make_agg_col(m, "sum"),
            make_agg_col(m, "count"),
        ],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    # Override for a different measure -- not in resolved_measures.
    bq.logical_query.measure_agg_overrides = {"other_measure": "sum"}

    with patch(_PATCH, new_callable=AsyncMock, return_value=[agg]):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg


async def test_override_on_quantile_default_skips_quantile_gate():
    """Bug-7796 + Bug-7780 interaction: a DAX SUM override on a p50-default
    measure (is_additive=True by model definition) must be treated as an
    explicit aggregation by _explicitly_aggregated_measure_names so the
    quantile exact-grain gate is NOT falsely triggered. The measure is then
    declined by the override bail (not by the quantile gate).
    """
    m = make_measure("latency", default_agg="p50", is_additive=True)
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.measure_agg_overrides = {"latency": "sum"}

    # The override makes "latency" an explicitly-aggregated name.
    # compute_has_non_additive should NOT force exact grain because the
    # p50 default_agg check is skipped for explicitly-aggregated measures.
    result = compute_has_non_additive(bq)
    assert result is False


# ---------------------------------------------------------------------------
# Derived-exact path (router.py _build_measure_requests)
# ---------------------------------------------------------------------------


async def test_derived_exact_path_applies_override_stat():
    """F-006-01: _build_measure_requests applies the override as requested_stat
    (sum, not None / not a whole-path decline).
    """
    from src.routing.router import _build_measure_requests

    m = make_measure("amount", default_agg="avg")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.measure_agg_overrides = {"amount": "sum"}

    result = _build_measure_requests(bq)

    assert result is not None
    assert len(result) == 1
    assert result[0].measure_name == "amount"
    assert result[0].requested_stat == "sum"


async def test_derived_exact_path_no_override_builds_request():
    """Without an override, _build_measure_requests builds a request
    with the measure's default_agg (pre-7796 behaviour preserved).
    """
    from src.routing.router import _build_measure_requests

    m = make_measure("amount", default_agg="avg")
    bq = make_bound_query([make_dimension("country")], [m])
    # No override.

    result = _build_measure_requests(bq)

    assert result is not None
    assert len(result) == 1
    assert result[0].measure_name == "amount"
    assert result[0].requested_stat == "avg"


async def test_derived_exact_path_unrelated_override_builds_request():
    """An override for a measure NOT in resolved_measures does not
    block _build_measure_requests.
    """
    from src.routing.router import _build_measure_requests

    m = make_measure("amount", default_agg="avg")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.measure_agg_overrides = {"other_measure": "sum"}

    result = _build_measure_requests(bq)

    assert result is not None
    assert len(result) == 1
    assert result[0].requested_stat == "avg"
