"""ML5 regression tests — aggregate-routing medium/low fixes (F-004-06..16).

Business-outcome tests for the routing-layer corrections:
- F-004-06: matcher/validator/rewriter agree on the exact-grain definition
  (GROUP BY grain), so a filtered non-additive query falls to source up front
  instead of matching-then-being-rejected.
- F-004-07: the hit-credit is deferred to the router (see test_hit_rate_update).
- F-004-09: Redshift quantiles are exact and routable.
- F-004-12: a measure-count column does not satisfy COUNT(*).
- F-004-13: the exactness validator shares the matcher's non-additive
  determination (variant/semi-additive/quantile coverage), and its filter check
  no longer carries the dead measure-physical-name union.

Run from tessallite/services/query-router/:
    pytest tests/test_ml5_routing_fixes.py
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

from src.routing.aggregate_matcher import (
    find_best_aggregate,
    compute_has_non_additive,
    AggregateSkipReason,
)
from src.routing.exactness_validator import validate_aggregate_route
from src.ir.logical_query import LogicalFilter

from conftest import (
    make_measure,
    make_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_INACTIVE = "src.routing.aggregate_matcher.load_inactive_aggregates"


# --------------------------------------------------------------------------- #
# F-004-06 — exact-grain gate compares GROUP BY grain, not GROUP BY ∪ filters  #
# --------------------------------------------------------------------------- #

async def test_non_additive_filter_not_in_group_by_falls_to_source_in_matcher():
    """A non-additive measure grouped by `country`, filtered on `region`,
    against a `(country, region)` aggregate must NOT match: the rewriter would
    group by `country` only and re-aggregate the non-re-aggregatable column at
    coarser grain. The matcher rejects it directly (EXACT_GRAIN_MISMATCH)."""
    # Bug-5892: default_agg="count_distinct" matches the stored stat so the
    # rejection is proven to come from the EXACT-GRAIN rule this test
    # targets, not an incidental stat-type mismatch from the fixture.
    m = make_measure("user_id", default_agg="count_distinct", is_additive=False)
    agg = make_aggregate(
        ["country", "region"],
        [make_agg_col(m, "count_distinct")],
    )
    filters = [LogicalFilter("region", "eq", "EMEA")]
    bq = make_bound_query([make_dimension("country")], [m], filters=filters)
    bq.resolved_dimensions_by_name = {
        "country": make_dimension("country"),
        "region": make_dimension("region"),
    }

    with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_INACTIVE, new_callable=AsyncMock, return_value=[]):
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None
    assert AggregateSkipReason.EXACT_GRAIN_MISMATCH in result.skip_reasons


async def test_non_additive_exact_group_by_still_matches():
    """The legitimate case still accelerates: non-additive measure grouped by
    exactly the aggregate grain (no extra filter dimension)."""
    # Bug-5892: default_agg="count_distinct" matches the stored stat.
    m = make_measure("user_id", default_agg="count_distinct", is_additive=False)
    agg = make_aggregate(["country"], [make_agg_col(m, "count_distinct")])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_INACTIVE, new_callable=AsyncMock, return_value=[]):
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg


# --------------------------------------------------------------------------- #
# F-004-12 — a measure-count column does not satisfy COUNT(*)                  #
# --------------------------------------------------------------------------- #

async def test_measure_count_column_does_not_satisfy_row_count():
    """An aggregate with `revenue__count` (counts non-null revenue, not rows)
    but no `__row_count__count` must NOT match a COUNT(*) query — the rewriter
    hardcodes the row-count column and would reference a missing column."""
    revenue = make_measure("revenue")
    row_count_measure = make_measure("__row_count", default_agg="count")
    # Aggregate stores only a measure-count column, NOT the row-count column.
    agg = make_aggregate(
        ["country"],
        [make_agg_col(revenue, "count")],  # physical revenue__count
    )
    bq = make_bound_query([make_dimension("country")], [row_count_measure])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_INACTIVE, new_callable=AsyncMock, return_value=[]):
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None


async def test_explicit_row_count_column_satisfies_row_count():
    """A real `__row_count__count` column satisfies COUNT(*)."""
    row_count_measure = make_measure("__row_count", default_agg="count")
    row_count_col = types.SimpleNamespace(
        measure=row_count_measure,
        stat_type="count",
        physical_col_name="__row_count__count",
    )
    agg = make_aggregate(["country"], [row_count_col])
    bq = make_bound_query([make_dimension("country")], [row_count_measure])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_INACTIVE, new_callable=AsyncMock, return_value=[]):
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg


# --------------------------------------------------------------------------- #
# F-004-13 — validator shares the matcher's non-additive determination        #
# --------------------------------------------------------------------------- #

def test_validator_rejects_variant_measure_at_coarser_grain():
    """A variant measure is non-re-aggregatable; the validator (now using the
    shared compute_has_non_additive) must reject a coarser-grain aggregate even
    though the old local check looked only at is_additive + default_agg."""
    variant = make_measure("rev_lag", variant_kind="lag")
    agg = make_aggregate(["country", "region"], [make_agg_col(variant)])
    bq = make_bound_query([make_dimension("country")], [variant])
    bq.resolved_dimensions_by_name = {"country": make_dimension("country")}

    valid, reason = validate_aggregate_route(bq, agg)
    assert valid is False
    assert "exact grain" in reason.lower()


def test_compute_has_non_additive_flags_variant_and_semi_additive():
    assert compute_has_non_additive(
        make_bound_query([make_dimension("c")], [make_measure("v", variant_kind="lag")])
    ) is True
    assert compute_has_non_additive(
        make_bound_query(
            [make_dimension("c")],
            [make_measure("s", semi_additive_behavior="last")],
        )
    ) is True
    assert compute_has_non_additive(
        make_bound_query([make_dimension("c")], [make_measure("plain")])
    ) is False


def test_validator_filter_on_grain_dimension_passes():
    """The dead measure-physical-name union was removed; a filter on a real
    grain dimension still validates."""
    m = make_measure("revenue")
    agg = make_aggregate(["country", "region"], [make_agg_col(m)])
    filters = [LogicalFilter("region", "eq", "EMEA")]
    bq = make_bound_query([make_dimension("country")], [m], filters=filters)

    valid, _ = validate_aggregate_route(bq, agg)
    assert valid is True


def test_validator_filter_not_in_grain_rejected():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    filters = [LogicalFilter("region", "eq", "EMEA")]
    bq = make_bound_query([make_dimension("country")], [m], filters=filters)

    valid, reason = validate_aggregate_route(bq, agg)
    assert valid is False
    assert "region" in reason


# --------------------------------------------------------------------------- #
# F-004-09 — Redshift quantiles are exact and routable                        #
# --------------------------------------------------------------------------- #

def test_redshift_quantiles_are_exact():
    from shared.aggregate_quantiles import (
        quantiles_are_exact,
        quantile_materialization_is_exact,
    )

    assert quantiles_are_exact("redshift") is True
    # Same-engine and cross-engine (postgres-family source) stay exact.
    assert quantile_materialization_is_exact("redshift", "redshift") is True
    assert quantile_materialization_is_exact("redshift", "postgres") is True
    assert quantile_materialization_is_exact("postgres", "redshift") is True
    # BigQuery remains approximate.
    assert quantile_materialization_is_exact("bigquery", "bigquery") is False


# --------------------------------------------------------------------------- #
# Bug-7019 — variant at non-exact grain raises AggregateRewriteUnsupported     #
# --------------------------------------------------------------------------- #

def test_bug_7019_variant_at_coarser_grain_raises_unsupported():
    """Bug-7019: a variant measure that somehow reaches the aggregate rewriter
    at non-exact grain must raise AggregateRewriteUnsupported (fail loud, fall
    to source) instead of silently forcing exact-grain semantics (which would
    emit finer-grain rows than the user's GROUP BY asked for).

    The matcher normally gates this, but the rewriter defence-in-depth must
    also fail closed, not force a wrong cardinality.
    """
    import pytest
    from src.rewrite.aggregate import rewrite_for_aggregate, AggregateRewriteUnsupported

    variant = make_measure("rev_lag", variant_kind="lag")
    region = make_dimension("region")
    country = make_dimension("country")

    # Aggregate at grain [country, region]; query at grain [country] only.
    agg = make_aggregate(
        ["country", "region"],
        [make_agg_col(variant)],
    )
    bq = make_bound_query(
        [country], [variant], grain=["country"],
    )

    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_aggregate(bq, agg, target_dialect="postgres")
