"""Tests for canonical dimension equivalence in the aggregate matcher.

Covers plan tasks E2:
- Query hierarchy_A.Year, aggregate grain hierarchy_B.Year, same backing -> match
- Same test but different backing -> no match
- Mixed: some dimensions match by string, some by canonical equivalence -> match
- Ordinal coverage still works independently for intra-hierarchy roll-up
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from shared.semantic.canonical_dimensions import CanonicalDim
from src.routing.aggregate_matcher import find_best_aggregate

from conftest import (
    make_measure,
    make_dimension,
    make_hierarchy_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_CANONICAL = "shared.semantic.canonical_dimensions.build_canonical_dimension_list"


# ---------------------------------------------------------------------------
# E2.1 — Cross-hierarchy equivalence: same backing -> match
# ---------------------------------------------------------------------------

async def test_canonical_equivalence_same_backing_matches():
    """Query on hierarchy_A.Year should match an aggregate built on
    hierarchy_B.Year when both share the same backing UDA."""
    m = make_measure("revenue")
    d = make_hierarchy_dimension(
        "hierarchy_A.Year", hierarchy_id="h-A",
        hierarchy_name="hierarchy_A", ordinal=1,
    )
    agg = make_aggregate(["hierarchy_B.Year"], [make_agg_col(m)])
    bq = make_bound_query([d], [m])

    canonical_dims = [
        CanonicalDim(
            canonical_name="year",
            backing_key="uda:shared-uda-1",
            all_names={"hierarchy_A.Year", "hierarchy_B.Year", "year"},
            is_time_dim=True,
        ),
    ]

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[agg]),
        patch(_PATCH_CANONICAL, new_callable=AsyncMock, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is not None
    assert result.aggregate.id == agg.id


# ---------------------------------------------------------------------------
# E2.2 — Different backing -> no match
# ---------------------------------------------------------------------------

async def test_canonical_equivalence_different_backing_no_match():
    """Query on hierarchy_A.Year should NOT match an aggregate built on
    hierarchy_B.Month when they have different backing UDAs."""
    m = make_measure("revenue")
    d = make_hierarchy_dimension(
        "hierarchy_A.Year", hierarchy_id="h-A",
        hierarchy_name="hierarchy_A", ordinal=1,
    )
    agg = make_aggregate(["hierarchy_B.Month"], [make_agg_col(m)])
    bq = make_bound_query([d], [m])

    canonical_dims = [
        CanonicalDim(
            canonical_name="year",
            backing_key="uda:uda-year",
            all_names={"hierarchy_A.Year", "year"},
            is_time_dim=True,
        ),
        CanonicalDim(
            canonical_name="month",
            backing_key="uda:uda-month",
            all_names={"hierarchy_B.Month", "month"},
            is_time_dim=True,
        ),
    ]

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[agg]),
        patch(_PATCH_CANONICAL, new_callable=AsyncMock, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None


# ---------------------------------------------------------------------------
# E2.3 — Mixed: some string match, some canonical match -> match
# ---------------------------------------------------------------------------

async def test_canonical_mixed_string_and_equivalence_match():
    """Grain with one dimension matching by exact string and another
    matching via canonical equivalence should still match."""
    m = make_measure("revenue")
    d_city = make_dimension("city")
    d_year = make_hierarchy_dimension(
        "date_hier.Year", hierarchy_id="h-date",
        hierarchy_name="date_hier", ordinal=1,
    )
    agg = make_aggregate(["city", "fiscal_hier.Year"], [make_agg_col(m)])
    bq = make_bound_query([d_city, d_year], [m])

    canonical_dims = [
        CanonicalDim(
            canonical_name="city",
            backing_key="col:col-city",
            all_names={"city"},
        ),
        CanonicalDim(
            canonical_name="year",
            backing_key="uda:uda-year",
            all_names={"date_hier.Year", "fiscal_hier.Year", "year"},
            is_time_dim=True,
        ),
    ]

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[agg]),
        patch(_PATCH_CANONICAL, new_callable=AsyncMock, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is not None
    assert result.aggregate.id == agg.id


# ---------------------------------------------------------------------------
# E2.4 — Ordinal coverage independent of canonical equivalence
# ---------------------------------------------------------------------------

async def test_ordinal_rollup_without_ancestor_column_falls_to_source():
    """F-004-03: an intra-hierarchy ordinal roll-up (Month aggregate, Year
    query) must NOT match unless the coarser level is physically materialised.

    The aggregate at grain ``["date_hier.Month"]`` has a Month column and no
    Year column; the rewriter cannot derive Year from Month, so the old
    ordinal-coverage match produced ``GROUP BY "Year"`` against a table without
    that column — a runtime error with no fallback. Canonical equivalence is a
    different, still-supported mechanism (the test below); pure ordinal
    roll-up without a materialised ancestor falls to source."""
    m = make_measure("revenue")
    d_year = make_hierarchy_dimension(
        "date_hier.Year", hierarchy_id="h-date",
        hierarchy_name="date_hier", ordinal=1,
    )
    d_month = make_hierarchy_dimension(
        "date_hier.Month", hierarchy_id="h-date",
        hierarchy_name="date_hier", ordinal=2,
    )
    agg = make_aggregate(["date_hier.Month"], [make_agg_col(m)])
    bq = make_bound_query([d_year], [m], all_dimensions=[d_month])

    canonical_dims = [
        CanonicalDim(
            canonical_name="date_hier.Year",
            backing_key="uda:uda-year",
            all_names={"date_hier.Year"},
            is_time_dim=True,
        ),
        CanonicalDim(
            canonical_name="date_hier.Month",
            backing_key="uda:uda-month",
            all_names={"date_hier.Month"},
            is_time_dim=True,
        ),
    ]

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[agg]),
        patch(_PATCH_CANONICAL, new_callable=AsyncMock, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None
