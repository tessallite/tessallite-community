"""
Unit tests for src.routing.aggregate_matcher — find_best_aggregate().

Async tests — uses unittest.mock.AsyncMock to patch load_active_aggregates.

Run from tessallite/services/query-router/:
    pytest tests/test_aggregate_matcher.py
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from src.routing.aggregate_matcher import find_best_aggregate
from src.ir.logical_query import LogicalFilter

from conftest import (
    make_measure,
    make_dimension,
    make_hierarchy_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_INACTIVE = "src.routing.aggregate_matcher.load_inactive_aggregates"


from src.routing.aggregate_matcher import AggregateSkipReason


# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------

async def test_exact_grain_match_returns_aggregate():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_superset_grain_accepted():
    m = make_measure("revenue")
    # Aggregate has more grain dims than query — still valid (superset rule)
    agg = make_aggregate(["country", "region"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_missing_grain_returns_none():
    m = make_measure("revenue")
    agg = make_aggregate(["region"], [make_agg_col(m)])           # only "region"
    bq = make_bound_query([make_dimension("country")], [m])       # needs "country"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_missing_measure_returns_none():
    m_query = make_measure("revenue")
    m_agg = make_measure("orders")
    agg = make_aggregate(["country"], [make_agg_col(m_agg)])
    bq = make_bound_query([make_dimension("country")], [m_query])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# F-015-01 (Fable gate C1-1): window variant with an explicit divergent anchor
# must NOT be served from an aggregate (route to source) so it cannot return a
# window ordered by a different date than the source route.
# ---------------------------------------------------------------------------

async def test_window_variant_with_explicit_anchor_skips_aggregate():
    m = make_measure("sales_lag", variant_kind="lag")
    # Modeller selected an explicit ORDER BY date column for this window.
    m.date_dimension_column_id = "col-ship-date"
    agg = make_aggregate(["order_month"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("order_month")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    # Fail closed to source — no aggregate served.
    assert result.aggregate is None
    assert AggregateSkipReason.VARIANT_ANCHOR_UNPROVEN in (result.skip_reasons or [])


async def test_window_variant_without_explicit_anchor_still_eligible():
    m = make_measure("sales_lag", variant_kind="lag")
    # No explicit anchor: orders by the grain time column on both routes.
    agg = make_aggregate(["order_month"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("order_month")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    # No anchor divergence -> the variant is not force-skipped by the anchor gate.
    assert AggregateSkipReason.VARIANT_ANCHOR_UNPROVEN not in (result.skip_reasons or [])


@pytest.mark.parametrize("kind", ["pct_change", "cagr"])
async def test_bug_8293_mismatched_resolved_anchor_refuses_admission(kind):
    """Bug-8293/F-01: legacy mismatched artifacts fail closed at serve time."""
    measure = make_measure(f"sales_{kind}", variant_kind=kind)
    measure.resolved_date_col_id = "ship-date-col"
    measure.date_dimension_column_id = None
    time_dim = make_dimension("order_day")
    time_dim.is_time_dim = True
    time_dim.time_grain = "day"
    time_dim.source_column_id = "order-date-col"
    agg = make_aggregate(["order_day"], [make_agg_col(measure)])
    bound = make_bound_query([time_dim], [measure])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bound, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.VARIANT_ANCHOR_UNPROVEN in result.skip_reasons


@pytest.mark.parametrize("kind", ["pct_change", "cagr"])
async def test_bug_8293_matching_resolved_anchor_keeps_admission(kind):
    """Bug-8293/F-01: a proven matching anchor keeps acceleration enabled."""
    measure = make_measure(f"sales_{kind}", variant_kind=kind)
    measure.resolved_date_col_id = "order-date-col"
    measure.date_dimension_column_id = None
    time_dim = make_dimension("order_day")
    time_dim.is_time_dim = True
    time_dim.time_grain = "day"
    time_dim.source_column_id = "order-date-col"
    agg = make_aggregate(["order_day"], [make_agg_col(measure)])
    bound = make_bound_query([time_dim], [measure])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bound, AsyncMock())
    assert AggregateSkipReason.VARIANT_ANCHOR_UNPROVEN not in result.skip_reasons


@pytest.mark.parametrize("kind", ["pct_change", "cagr"])
async def test_bug_8293_missing_physical_anchor_refuses_admission(kind):
    """Bug-8293/F-01: an unproven anchor refuses the aggregate route.

    Test escape: when the measure's anchor identity and the selected time
    dimension's source column were both absent, ``matches_grain`` treated
    the two Nones as a proven match and the aggregate was admitted even
    though nothing proved its window ordering matches the source route.
    Guard: conservative refusal with VARIANT_ANCHOR_UNPROVEN. Tier: T3.
    """
    measure = make_measure(f"sales_{kind}", variant_kind=kind)
    measure.resolved_date_col_id = None
    measure.date_dimension_column_id = None
    time_dim = make_dimension("order_day")
    time_dim.is_time_dim = True
    time_dim.time_grain = "day"
    time_dim.source_column_id = None  # expression-derived: no physical column
    agg = make_aggregate(["order_day"], [make_agg_col(measure)])
    bound = make_bound_query([time_dim], [measure])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bound, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.VARIANT_ANCHOR_UNPROVEN in result.skip_reasons


# ---------------------------------------------------------------------------
# Freshness checks
# ---------------------------------------------------------------------------

async def test_inactive_aggregate_skipped():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)], status="retired")
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_no_last_refreshed_at_skipped():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    agg.last_refreshed_at = None
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# Non-additive measure (Q8 rule)
# ---------------------------------------------------------------------------

async def test_non_additive_exact_grain_passes():
    # Bug-5892: default_agg must match the aggregate's stored stat_type
    # ("count_distinct") — a realistic COUNT DISTINCT measure has
    # default_agg="count_distinct" (mirrors production: matcher Rule 2 now
    # requires the exact (measure, stat) pair for standard non-additive
    # measures, not just a name-level presence check).
    m = make_measure("user_id", default_agg="count_distinct", is_additive=False)
    agg = make_aggregate(["country"], [make_agg_col(m, "count_distinct")])
    bq = make_bound_query([make_dimension("country")], [m])   # exact match

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_non_additive_wrong_stat_column_falls_back_to_source():
    """Bug-5892 (F-004-02) regression: a candidate aggregate that stores a
    DIFFERENT stat for a non-additive measure (e.g. only a legacy/incorrect
    ``user_id__sum`` column instead of ``user_id__count_distinct``) must be
    REJECTED, not matched by name alone. Before the fix, this matched
    (``STAT_TYPE_MISMATCH`` was never raised) and the rewriter emitted NULL
    for the business metric instead of falling back to source."""
    m = make_measure("user_id", default_agg="count_distinct", is_additive=False)
    # Aggregate has the measure NAME present but the WRONG stat column.
    agg = make_aggregate(["country"], [make_agg_col(m, "sum")])
    bq = make_bound_query([make_dimension("country")], [m])   # exact grain

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.STAT_TYPE_MISMATCH in result.skip_reasons


async def test_non_additive_extra_grain_rejected():
    # Bug-5892: default_agg="count_distinct" matches the stored stat so the
    # rejection below is proven to come from the EXACT-GRAIN rule this test
    # targets, not an incidental stat-type mismatch from the fixture.
    m = make_measure("user_id", default_agg="count_distinct", is_additive=False)
    agg = make_aggregate(["country", "region"], [make_agg_col(m, "count_distinct")])
    bq = make_bound_query([make_dimension("country")], [m])   # agg has extra "region"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.EXACT_GRAIN_MISMATCH in result.skip_reasons


# ---------------------------------------------------------------------------
# Best-fit selection
# ---------------------------------------------------------------------------

async def test_picks_tightest_grain():
    m = make_measure("revenue")
    agg_tight = make_aggregate(["country"], [make_agg_col(m)], agg_id="tight")
    agg_loose = make_aggregate(["country", "region", "city"], [make_agg_col(m)], agg_id="loose")
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_loose, agg_tight]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate.id == "tight"


async def test_tie_picks_most_recent():
    m = make_measure("revenue")
    agg_old = make_aggregate(["country"], [make_agg_col(m)], agg_id="old", age_hours=24)
    agg_new = make_aggregate(["country"], [make_agg_col(m)], agg_id="new", age_hours=1)
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_old, agg_new]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate.id == "new"


async def test_uda_dimension_name_matches_aggregate_grain():
    """
    UDA-backed dimensions behave like first-class attributes in matcher grain checks.
    """
    m = make_measure("revenue")
    agg = make_aggregate(["fx_account_type"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("fx_account_type")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_uda_dimension_without_direct_grain_match_falls_back():
    """
    Conservative behavior: no implicit dependency proof means no aggregate match.
    """
    m = make_measure("revenue")
    agg = make_aggregate(["account_type"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("fx_account_type")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_filter_only_dimension_must_exist_in_aggregate_grain():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    filters = [LogicalFilter("region", "eq", "EMEA")]
    bq = make_bound_query([make_dimension("country")], [m], filters=filters)
    bq.resolved_dimensions_by_name = {
        "country": make_dimension("country"),
        "region": make_dimension("region"),
    }

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_filter_only_dimension_in_aggregate_grain_matches():
    m = make_measure("revenue")
    agg = make_aggregate(["country", "region"], [make_agg_col(m)])
    filters = [LogicalFilter("region", "eq", "EMEA")]
    bq = make_bound_query([make_dimension("country")], [m], filters=filters)
    bq.resolved_dimensions_by_name = {
        "country": make_dimension("country"),
        "region": make_dimension("region"),
    }

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_non_additive_hierarchy_filter_requires_exact_required_grain():
    m = make_measure("user_id", is_additive=False)
    agg = make_aggregate(["region_level"], [make_agg_col(m, "count_distinct")])
    filters = [LogicalFilter("country_level", "eq", "UK")]
    bq = make_bound_query([make_dimension("region_level")], [m], filters=filters)
    bq.resolved_dimensions_by_name = {
        "region_level": make_dimension("region_level"),
        "country_level": make_dimension("country_level"),
    }

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_hierarchy_level_grain_missing_in_aggregate_falls_back():
    m = make_measure("revenue")
    agg = make_aggregate(["country_level"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("region_level")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# Bug-143: SELECT DISTINCT routes to aggregate (no GROUP BY)
# ---------------------------------------------------------------------------

async def test_distinct_no_group_by_routes_to_aggregate():
    """SELECT DISTINCT account_type FROM model should route to an aggregate
    at grain [account_type] — DISTINCT is semantically equivalent to GROUP BY."""
    agg = make_aggregate(["account_type"], [])
    # grain=[] mimics the parser output for SELECT DISTINCT without GROUP BY
    bq = make_bound_query(
        [make_dimension("account_type")],
        [],
        grain=[],
        has_distinct=True,
    )

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_distinct_no_group_by_no_matching_aggregate_returns_none():
    """DISTINCT query with wrong-grain aggregate still falls back to source."""
    agg = make_aggregate(["region"], [])   # aggregate at 'region', query wants 'account_type'
    bq = make_bound_query(
        [make_dimension("account_type")],
        [],
        grain=[],
        has_distinct=True,
    )

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_non_distinct_dimension_only_still_skipped():
    """SELECT account_type (no DISTINCT, no GROUP BY, no measures) must not
    route to an aggregate — that would suppress duplicate rows incorrectly."""
    agg = make_aggregate(["account_type"], [])
    bq = make_bound_query(
        [make_dimension("account_type")],
        [],
        grain=[],
        has_distinct=False,
    )

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# Variant measures require exact grain (Finding 3)
# ---------------------------------------------------------------------------

async def test_variant_measure_exact_grain_matches():
    """A variant measure (e.g. revenue_lag) at exact grain should match."""
    m = make_measure("revenue_lag", variant_kind="lag")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_variant_measure_finer_grain_rejected():
    """A variant measure must NOT match an aggregate with extra grain —
    re-aggregating window function results (LAG, trailing_n) is wrong."""
    m = make_measure("revenue_lag", variant_kind="lag")
    agg = make_aggregate(["country", "region"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# Hierarchy-aware grain matching (D6)
# ---------------------------------------------------------------------------

async def test_finer_aggregate_does_not_cover_coarser_query_without_ancestor_col():
    """F-004-03: a Month aggregate (ordinal=3) must NOT match a Year query
    (ordinal=1) when the aggregate has no Year column.

    Rolling Month detail up to Year is arithmetically valid, but the aggregate
    table only materialises its own grain level (a ``Month`` column, no
    ``Year`` column) and the rewriter has no logic to derive the coarser level.
    The old ordinal-coverage rule matched anyway and the rewriter then emitted
    ``SELECT "Year" ... GROUP BY "Year"`` against a table with no ``Year``
    column — a runtime "column does not exist" error with no source fallback.
    The safe behaviour is to fall to source (no match) unless the coarser level
    is physically present in the aggregate grain (covered by the test below)."""
    m = make_measure("revenue")
    year = make_hierarchy_dimension("Year", ordinal=1)
    month = make_hierarchy_dimension("Month", ordinal=3)
    agg = make_aggregate(["Month"], [make_agg_col(m)])
    bq = make_bound_query([year], [m], all_dimensions=[year, month])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_aggregate_with_ancestor_column_covers_coarser_query():
    """When the coarser queried level IS physically present in the aggregate
    grain (the aggregate materialised both Year and Month), the Year query
    matches via the direct/canonical name match — no ordinal rollup needed."""
    m = make_measure("revenue")
    year = make_hierarchy_dimension("Year", ordinal=1)
    month = make_hierarchy_dimension("Month", ordinal=3)
    agg = make_aggregate(["Year", "Month"], [make_agg_col(m)])
    bq = make_bound_query([year], [m], all_dimensions=[year, month])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_coarser_aggregate_rejects_finer_query():
    """A Year aggregate (ordinal=1) cannot serve a Month query (ordinal=3)
    — the month-level detail is lost and cannot be reconstructed."""
    m = make_measure("revenue")
    year = make_hierarchy_dimension("Year", ordinal=1)
    month = make_hierarchy_dimension("Month", ordinal=3)
    agg = make_aggregate(["Year"], [make_agg_col(m)])
    bq = make_bound_query([month], [m], all_dimensions=[year, month])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_same_hierarchy_level_matches():
    """A Month aggregate matches a Month query — same level, exact fit."""
    m = make_measure("revenue")
    year = make_hierarchy_dimension("Year", ordinal=1)
    month = make_hierarchy_dimension("Month", ordinal=3)
    agg = make_aggregate(["Month"], [make_agg_col(m)])
    bq = make_bound_query([month], [m], all_dimensions=[year, month])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_hierarchy_cross_hierarchy_no_match():
    """Hierarchy levels from different hierarchies should not match."""
    m = make_measure("revenue")
    year = make_hierarchy_dimension("Year", hierarchy_id="h-date", ordinal=1)
    category = make_hierarchy_dimension("Category", hierarchy_id="h-product", ordinal=1)
    agg = make_aggregate(["Year"], [make_agg_col(m)])
    bq = make_bound_query([category], [m], all_dimensions=[year, category])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# F-004-04: HAVING aggregate stat coverage
# ---------------------------------------------------------------------------

async def test_having_max_requires_max_column():
    """A HAVING MAX(x) must NOT match an aggregate that stores only x__sum —
    the rewriter would otherwise filter on the sum as if it were the max."""
    m = make_measure("x")  # additive, default sum
    agg = make_aggregate(["country"], [make_agg_col(m, "sum")])
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING MAX(x) > 5"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.STAT_TYPE_MISMATCH in (result.skip_reasons or [])


async def test_having_max_matches_when_max_column_present():
    """HAVING MAX(x) matches when the aggregate stores x__max."""
    m = make_measure("x")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m, "sum"), make_agg_col(m, "max")],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING MAX(x) > 5"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_having_count_distinct_requires_count_distinct_column():
    """HAVING COUNT(DISTINCT c) needs c__count_distinct (and forces exact
    grain) — a row-count column cannot serve a distinct count."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m, "sum")])
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING COUNT(DISTINCT customer) > 10"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_having_over_expression_falls_to_source():
    """HAVING SUM(a*b) cannot be served by any stat column — fall to source."""
    m = make_measure("a")
    agg = make_aggregate(["country"], [make_agg_col(m, "sum")])
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING SUM(a * b) > 5"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_having_stddev_matches_when_stddev_samp_column_present():
    """Bug-6094: HAVING STDDEV(x) must match an aggregate storing the canonical
    ``x__stddev_samp`` column. sqlglot emits the aggregate key ``stddev`` while
    the stored stat_type is ``stddev_samp``; the matcher must normalize the
    HAVING key (mirroring the SELECT path) so a servable dispersion-stat HAVING
    keeps the aggregate route instead of always losing it."""
    m = make_measure("x")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m, "sum"), make_agg_col(m, "stddev_samp")],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING STDDEV(x) > 5"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_having_variance_matches_when_var_samp_column_present():
    """Bug-6094: HAVING VARIANCE(x) (sqlglot key 'variance') must match the
    stored ``x__var_samp`` column via the same normalization."""
    m = make_measure("x")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m, "sum"), make_agg_col(m, "var_samp")],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING VARIANCE(x) > 5"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_having_stddev_requires_stddev_column():
    """Bug-6094: without the stored stddev_samp column, HAVING STDDEV(x) must
    fall to source — a sum column cannot serve a standard deviation."""
    m = make_measure("x")
    agg = make_aggregate(["country"], [make_agg_col(m, "sum")])
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.having_raw = "HAVING STDDEV(x) > 5"

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# M1: Inactive aggregate produces FRESHNESS skip reason
# ---------------------------------------------------------------------------

async def test_inactive_aggregate_produces_freshness_skip_reason():
    """An inactive aggregate that would otherwise match on grain+measures
    should appear as a FRESHNESS skip reason in diagnostics."""
    m = make_measure("revenue")
    inactive_agg = make_aggregate(
        ["country"], [make_agg_col(m)], status="inactive",
    )
    bq = make_bound_query([make_dimension("country")], [m])

    with (
        patch(_PATCH, new_callable=AsyncMock) as mock_active,
        patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
    ):
        mock_active.return_value = []
        mock_inactive.return_value = [inactive_agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.FRESHNESS in result.skip_reasons


async def test_retired_aggregate_produces_freshness_skip_reason():
    """A retired aggregate that matches on grain+measures → FRESHNESS."""
    m = make_measure("revenue")
    retired_agg = make_aggregate(
        ["country"], [make_agg_col(m)], status="retired",
    )
    bq = make_bound_query([make_dimension("country")], [m])

    with (
        patch(_PATCH, new_callable=AsyncMock) as mock_active,
        patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
    ):
        mock_active.return_value = []
        mock_inactive.return_value = [retired_agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.FRESHNESS in result.skip_reasons


async def test_inactive_aggregate_wrong_grain_no_freshness():
    """An inactive aggregate that doesn't match grain should NOT add FRESHNESS."""
    m = make_measure("revenue")
    inactive_agg = make_aggregate(
        ["region"], [make_agg_col(m)], status="inactive",
    )
    bq = make_bound_query([make_dimension("country")], [m])

    with (
        patch(_PATCH, new_callable=AsyncMock) as mock_active,
        patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
    ):
        mock_active.return_value = []
        mock_inactive.return_value = [inactive_agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.FRESHNESS not in result.skip_reasons


# ---------------------------------------------------------------------------
# F-004-01 — stored avg column satisfies AVG at exact grain only
# ---------------------------------------------------------------------------

async def test_avg_only_aggregate_matches_at_exact_grain():
    """An avg-only (legacy) aggregate still serves the exact-grain query —
    the stored avg value IS the answer there."""
    m = make_measure("score", default_agg="avg")
    agg = make_aggregate(["country"], [make_agg_col(m, "avg")])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_avg_only_aggregate_skipped_at_coarser_grain():
    """F-004-01: a stored avg column is not re-aggregatable. An aggregate
    holding ONLY score__avg must NOT match a coarser query — it falls to
    source instead of serving a SUM/AVG of stored averages."""
    m = make_measure("score", default_agg="avg")
    agg = make_aggregate(["country", "region"], [make_agg_col(m, "avg")])
    bq = make_bound_query([make_dimension("country")], [m])

    with (
        patch(_PATCH, new_callable=AsyncMock) as mock_active,
        patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
    ):
        mock_active.return_value = [agg]
        mock_inactive.return_value = []
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.STAT_TYPE_MISMATCH in result.skip_reasons


async def test_avg_with_sum_count_matches_at_coarser_grain():
    """The multi-stat shape (avg + sum + count) keeps accelerating coarser
    queries — the rewriter decomposes to SUM(sum)/SUM(count)."""
    m = make_measure("score", default_agg="avg")
    agg = make_aggregate(
        ["country", "region"],
        [
            make_agg_col(m, "avg"),
            make_agg_col(m, "sum"),
            make_agg_col(m, "count"),
        ],
    )
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


# ---------------------------------------------------------------------------
# F-004-17 — canonical dimension list cached per deployed model version
# ---------------------------------------------------------------------------

async def test_canonical_dim_list_cached_per_version():
    """The canonical dimension list is built once per (model_id, version) and
    reused on the next routed query -- without changing the routing decision.

    Bug-6977: deployed models now use the snapshot-based builder, so the
    caching assertion counts calls to build_canonical_dimension_list_from_snapshot.
    """
    from src.routing import aggregate_matcher as am

    am.invalidate_canonical_dim_cache()
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    calls = {"n": 0}

    def _fake_snapshot_build(dims, hier_rows):
        calls["n"] += 1
        return []

    with (
        patch(_PATCH, new_callable=AsyncMock) as mock_load,
        patch(
            "shared.semantic.canonical_dimensions.build_canonical_dimension_list_from_snapshot",
            new=_fake_snapshot_build,
        ),
    ):
        mock_load.return_value = [agg]
        r1 = await find_best_aggregate(bq, AsyncMock())
        r2 = await find_best_aggregate(bq, AsyncMock())

    assert r1.aggregate is agg
    assert r2.aggregate is agg          # identical routing decision
    assert calls["n"] == 1              # built once, served from cache second time


async def test_canonical_dim_cache_keyed_by_version():
    """A re-deploy (new deployed_version_id) forces a fresh canonical build.

    Bug-6977: deployed models now use the snapshot-based builder.
    """
    from src.routing import aggregate_matcher as am

    am.invalidate_canonical_dim_cache()
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    calls = {"n": 0}

    def _fake_snapshot_build(dims, hier_rows):
        calls["n"] += 1
        return []

    with (
        patch(_PATCH, new_callable=AsyncMock) as mock_load,
        patch(
            "shared.semantic.canonical_dimensions.build_canonical_dimension_list_from_snapshot",
            new=_fake_snapshot_build,
        ),
    ):
        mock_load.return_value = [agg]
        await find_best_aggregate(bq, AsyncMock())
        bq.model.deployed_version_id = "v2"   # simulate re-deploy
        await find_best_aggregate(bq, AsyncMock())

    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Bug-5181 — a WHERE the IR cannot represent as a LogicalFilter (function /
# expression RHS such as DATE_TRUNC(...), CURRENT_DATE, interval arithmetic,
# or OR/EXISTS/subquery/NOT) leaves resolved_filters empty. The aggregate
# rewriter renders WHERE only from resolved_filters, so the predicate would be
# silently dropped and the matcher's required_grain could not guarantee the
# filtered column is in the aggregate grain. The matcher must bail to source
# (where the raw WHERE is preserved verbatim) so the fail-closed filter_presence
# audit never has to block these queries.
# ---------------------------------------------------------------------------

async def test_unresolvable_where_bails_to_source_even_when_grain_matches():
    """An exact-grain aggregate that would otherwise match must be skipped
    when the query carries an unresolvable WHERE — the function-valued RHS
    predicate cannot be reproduced on the aggregate, so route to source."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query(
        [make_dimension("country")],
        [m],
        raw_sql=(
            "SELECT SUM(revenue) FROM t "
            "WHERE business_date >= DATE_TRUNC('year', CURRENT_DATE) "
            "GROUP BY country"
        ),
    )
    # The parser flags the function-valued RHS as unresolvable; resolved_filters
    # stays empty (the comparison is not extractable as a LogicalFilter).
    bq.logical_query.has_unresolvable_where = True

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None
    assert AggregateSkipReason.UNRESOLVABLE_WHERE in result.skip_reasons


async def test_resolvable_where_still_matches_aggregate():
    """Control: a query WITHOUT an unresolvable WHERE (filters extracted into
    resolved_filters on a grain column) still routes to the aggregate — the
    Bug-5181 bail must not over-fire and disable normal filtered routing."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    filters = [LogicalFilter("country", "eq", "FR")]
    bq = make_bound_query([make_dimension("country")], [m], filters=filters)
    bq.resolved_dimensions_by_name = {"country": make_dimension("country")}
    # has_unresolvable_where defaults to False — the literal-RHS filter is
    # extractable and present in resolved_filters.
    assert getattr(bq.logical_query, "has_unresolvable_where", False) is False

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg


# ---------------------------------------------------------------------------
# Bug-6973: SELECT MIN(x), MAX(x) multi-stat column matching
# ---------------------------------------------------------------------------

async def test_select_min_max_same_measure_matches_with_both_columns():
    """Bug-6973: SELECT MIN(x), MAX(x) -- two analytical select expressions on
    the same measure with different agg_functions.  The aggregate matcher must
    accept an aggregate that stores both x__min and x__max columns."""
    from src.ir.logical_query import SelectExpression

    m = make_measure("x")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m, "sum"), make_agg_col(m, "min"), make_agg_col(m, "max")],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [
        SelectExpression(
            raw_text="MIN(x)", alias=None, classification="analytical",
            agg_function="min", inner_column="x", inner_literal=None,
        ),
        SelectExpression(
            raw_text="MAX(x)", alias=None, classification="analytical",
            agg_function="max", inner_column="x", inner_literal=None,
        ),
    ]

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_select_min_max_same_measure_rejects_without_both_columns():
    """Bug-6973: SELECT MIN(x), MAX(x) must reject an aggregate that stores
    only x__max -- without x__min, the MIN column cannot be served."""
    from src.ir.logical_query import SelectExpression

    m = make_measure("x")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m, "sum"), make_agg_col(m, "max")],  # no x__min
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [
        SelectExpression(
            raw_text="MIN(x)", alias=None, classification="analytical",
            agg_function="min", inner_column="x", inner_literal=None,
        ),
        SelectExpression(
            raw_text="MAX(x)", alias=None, classification="analytical",
            agg_function="max", inner_column="x", inner_literal=None,
        ),
    ]

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.STAT_TYPE_MISMATCH in (result.skip_reasons or [])


# ---------------------------------------------------------------------------
# F-013-02 (Bug-8250) -- artifact-to-version compatibility gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_version_mismatch_skips_aggregate():
    """F-013-02: an aggregate built for a DIFFERENT version/epoch must not serve.

    Test escape: no coverage asserted a fresh aggregate built under a previous
    definition would be refused after deploy.
    Guard: VERSION_MISMATCH skip reason. Tier: T1 (producer/consumer contract).
    """
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)],
                         built_for_version_id="old-version", built_for_epoch=1)
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.VERSION_MISMATCH in (result.skip_reasons or [])


@pytest.mark.asyncio
async def test_null_built_for_skips_aggregate_for_deployed_model():
    """F-013-02: NULL built_for (unmaterialised/import-cleared) must not serve.
    Guard: VERSION_MISMATCH. Tier: T1."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)],
                         built_for_version_id=None, built_for_epoch=None)
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.VERSION_MISMATCH in (result.skip_reasons or [])


@pytest.mark.asyncio
async def test_epoch_only_mismatch_skips_aggregate():
    """F-013-02 / Bug-7140: revert-to-same-version bumps epoch only. An aggregate
    built for the SAME version_id but a DIFFERENT epoch must not serve.
    Guard: VERSION_MISMATCH. Tier: T1."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)],
                         built_for_version_id="v1", built_for_epoch=0)
    bq = make_bound_query([make_dimension("country")], [m])
    # Simulate epoch bump: model now at epoch=1 (revert-to-same-version).
    bq.model.deploy_epoch = 1

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.VERSION_MISMATCH in (result.skip_reasons or [])


@pytest.mark.asyncio
async def test_matching_built_for_serves_aggregate():
    """F-013-02: aggregate built for CURRENT version/epoch must serve.
    Guard: positive-path proof. Tier: T1."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)],
                         built_for_version_id="v1", built_for_epoch=0)
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


@pytest.mark.asyncio
async def test_f003_01_not_in_on_kept_grain_hits_aggregate():
    """F-003-01 / G-004-01 / F-102-04: NOT IN on a stored grain column HITs."""
    m = make_measure("revenue")
    agg = make_aggregate(["region"], [make_agg_col(m)])
    filters = [LogicalFilter("region", "not_in", ["US"])]
    bq = make_bound_query([make_dimension("region")], [m], filters=filters)
    bq.resolved_dimensions_by_name = {"region": make_dimension("region")}

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


@pytest.mark.asyncio
async def test_f004_02_date_trunc_unresolvable_where_is_not_passthrough():
    """F-004-02: DATE_TRUNC + unresolvable WHERE reports UNRESOLVABLE_WHERE."""
    m = make_measure("revenue")
    agg = make_aggregate(["business_date"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("business_date")], [m], grain=[])
    bq.has_passthrough_expressions = True
    bq.logical_query.time_period_grains = [("month", "business_date")]
    bq.logical_query.has_unresolvable_where = True
    bq.logical_query.has_function_grain = True

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.UNRESOLVABLE_WHERE in result.skip_reasons
    assert AggregateSkipReason.PASSTHROUGH not in result.skip_reasons


@pytest.mark.asyncio
async def test_f004_08_invalid_reason_refuses_active_aggregate():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    agg.invalid_reason = "coverage mismatch"
    agg.is_stale = False
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.STALE in result.skip_reasons


@pytest.mark.asyncio
async def test_f004_05_filter_column_missing_is_named():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    filters = [LogicalFilter("channel", "eq", "web")]
    bq = make_bound_query(
        [make_dimension("country")], [m], filters=filters,
        all_dimensions=[make_dimension("country"), make_dimension("channel")],
    )
    bq.resolved_dimensions_by_name = {
        "country": make_dimension("country"),
        "channel": make_dimension("channel"),
    }

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None
    assert AggregateSkipReason.GRAIN_MISSING in result.skip_reasons
    assert "channel" in result.filter_columns_missing


@pytest.mark.asyncio
async def test_f003_08_extract_month_grain_hits_date_aggregate():
    m = make_measure("revenue")
    agg = make_aggregate(["business_date"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("business_date")], [m], grain=[])
    bq.has_passthrough_expressions = True
    bq.logical_query.time_period_grains = [("extract_month", "business_date")]
    bq.logical_query.has_function_grain = True
    bq.resolved_dimensions_by_name = {"business_date": make_dimension("business_date")}

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


@pytest.mark.asyncio
async def test_f004_07_two_hundred_mocked_candidates_matched_in_one_load():
    """F-004-07: N=200 candidates must be matched from a SINGLE candidate load,
    in-memory — never a per-candidate DB round-trip.

    Bug-9240: this used to assert wall-clock ``elapsed < 5.0``, which flaked under
    CPU contention and proved nothing deterministic. The property the gate exists
    to protect is that candidate volume does not multiply the DB work: assert the
    loader is awaited exactly once regardless of N (the deterministic proxy for
    "adding candidates does not blow up") and that the best aggregate is still
    selected."""
    m = make_measure("revenue")
    aggs = [
        make_aggregate(["country"], [make_agg_col(m)], agg_id=f"agg-{i}")
        for i in range(200)
    ]
    bq = make_bound_query([make_dimension("country")], [m])
    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = aggs
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is not None
    mock_load.assert_awaited_once()
