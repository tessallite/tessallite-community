"""Aggregate matcher — calculated measure rules (Phase 4B.1).

Covers:

* ``calc_agg_mode=per_row_then_aggregate`` measures behave like standard
  additive measures: match by ``(name, default_agg)`` and accept a
  superset grain.
* ``calc_agg_mode=expression_as_written`` measures require exact-grain
  match (no re-aggregation of the stored ratio column) and are matched
  by measure-name presence only.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.routing.aggregate_matcher import find_best_aggregate

from conftest import (
    make_agg_col,
    make_aggregate,
    make_bound_query,
    make_dimension,
    make_measure,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"


async def test_per_row_calc_measure_matches_like_standard_sum():
    """per_row_then_aggregate: stored stat_type matches default_agg — routes fine."""
    m = make_measure(
        "gross",
        default_agg="sum",
        measure_type="calculated",
        calc_agg_mode="per_row_then_aggregate",
        expression='measure("price") * measure("qty")',
    )
    # Aggregate stores the calc column under __sum (per_row convention).
    agg = make_aggregate(["country", "region"], [make_agg_col(m, "sum")])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_expression_as_written_requires_exact_grain():
    """expression_as_written stored values cannot re-aggregate — superset rejected."""
    m = make_measure(
        "ratio",
        default_agg="avg",
        measure_type="calculated",
        calc_agg_mode="expression_as_written",
        expression='safe_div(measure("a"), measure("b"))',
    )
    # Aggregate has one extra grain dim — must be rejected.
    agg = make_aggregate(
        ["country", "region"],
        [make_agg_col(m, "calculated")],
    )
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_expression_as_written_matches_on_exact_grain():
    """At exact grain, the calculated sentinel column resolves via name-only check."""
    m = make_measure(
        "ratio",
        default_agg="avg",
        measure_type="calculated",
        calc_agg_mode="expression_as_written",
        expression='safe_div(measure("a"), measure("b"))',
    )
    agg = make_aggregate(["country"], [make_agg_col(m, "calculated")])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_expression_as_written_missing_name_rejected():
    """Even at exact grain, a calc measure absent from the aggregate is skipped."""
    m = make_measure(
        "ratio",
        default_agg="avg",
        measure_type="calculated",
        calc_agg_mode="expression_as_written",
        expression='safe_div(measure("a"), measure("b"))',
    )
    other = make_measure("other")
    agg = make_aggregate(["country"], [make_agg_col(other)])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
