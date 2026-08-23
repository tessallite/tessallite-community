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
from src.routing.aggregate_matcher import AggregateSkipReason, find_best_aggregate
from src.routing.exactness_validator import validate_aggregate_route
from src.rewrite.query_rewriter import rewrite_for_aggregate

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
# Bug-6977: deployed models now use the snapshot-based builder. Tests that
# inject specific canonical dims must also patch this path.
_PATCH_CANONICAL_SNAPSHOT = "shared.semantic.canonical_dimensions.build_canonical_dimension_list_from_snapshot"


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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is not None
    assert result.aggregate.id == agg.id
    assert result.logical_to_aggregate_grain == {
        "hierarchy_A.Year": "hierarchy_B.Year",
    }

    rewritten = rewrite_for_aggregate(
        bq,
        agg,
        logical_to_aggregate_grain=result.logical_to_aggregate_grain,
    )
    assert '"hierarchy_B.Year" AS "hierarchy_A.Year"' in rewritten
    assert 'SELECT "hierarchy_A.Year"' not in rewritten


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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None


async def test_coarser_hierarchy_query_falls_to_source_with_grain_missing():
    """F-016-05 decision guard: hierarchy rollup is SOURCE-ONLY.

    A finer-grain aggregate (grain ``["date_hier.Month"]``) must NEVER serve a
    coarser hierarchy query (``GROUP BY date_hier.Year``). There is no
    hierarchy-rollup rewrite; the coarser query falls back to source. This pins
    both halves of the decision:

    - ``result.aggregate is None`` — the finer-grain aggregate is refused, so the
      query routes to source (the always-correct path).
    - the refusal carries a stable skip reason (``grain_missing``) that the
      route log / ``/explain`` surface can report.

    The Wave-C product decision names this outcome
    ``hierarchy_rollup_source_only``. The matcher currently surfaces the refusal
    through the shared ``GRAIN_MISSING`` skip reason (which also covers a plain
    missing flat dimension); minting a distinct ``hierarchy_rollup_source_only``
    label would require adding hierarchy-vs-flat discrimination logic inside the
    sensitive aggregate matcher and is therefore gated on explicit approval per
    the sensitive-component guard. This test asserts the reason the code actually
    emits so it fails loudly if either the source-fallback behaviour or the
    stable reason token regresses.
    """
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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None
    assert AggregateSkipReason.GRAIN_MISSING in result.skip_reasons


def test_non_additive_exactness_accepts_canonical_equivalent_grain():
    """A non-additive stored value is still exact when the query and aggregate
    grains are canonical aliases for the same backing column."""
    m = make_measure("customer_id", default_agg="count_distinct", is_additive=False)
    d = make_dimension("hierarchy_A.Year")
    agg = make_aggregate(["hierarchy_B.Year"], [make_agg_col(m, "count_distinct")])
    bq = make_bound_query([d], [m])

    valid, reason = validate_aggregate_route(
        bq,
        agg,
        name_to_canonical={
            "hierarchy_A.Year": "year",
            "hierarchy_B.Year": "year",
        },
    )

    assert valid, reason


# ---------------------------------------------------------------------------
# Bug-6092 (F-004-01) — canonical-equivalent exact-grain match with
# non-re-aggregatable measure must route correctly, never silent NULL.
# ---------------------------------------------------------------------------

async def test_canonical_equivalent_non_additive_rewrites_stored_column():
    """Bug-6092 (F-004-01): when the query grain and aggregate grain are
    canonical equivalents but have DIFFERENT raw names, and the query has a
    non-re-aggregatable measure (count_distinct), the matcher must carry the
    logical-to-aggregate grain mapping so the rewriter reads the stored column
    instead of emitting NULL or falling back to the source."""
    m = make_measure("customer_id", default_agg="count_distinct", is_additive=False)
    d = make_hierarchy_dimension(
        "hierarchy_A.Year", hierarchy_id="h-A",
        hierarchy_name="hierarchy_A", ordinal=1,
    )
    agg = make_aggregate(
        ["hierarchy_B.Year"],
        [make_agg_col(m, "count_distinct")],
    )
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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is not None
    assert result.aggregate.id == agg.id
    assert result.logical_to_aggregate_grain == (
        {"hierarchy_A.Year": "hierarchy_B.Year"}
    )
    rewritten = rewrite_for_aggregate(
        bq,
        result.aggregate,
        logical_to_aggregate_grain=result.logical_to_aggregate_grain,
    )
    assert '"customer_id__count_distinct" AS "customer_id"' in rewritten
    assert '"hierarchy_B.Year" AS "hierarchy_A.Year"' in rewritten
    assert "NULL" not in rewritten.upper()


async def test_duplicate_canonical_grain_non_additive_refused_to_source():
    """F-1 (Fable sensitive-worktree review): when the AGGREGATE grain holds TWO
    canonical-equivalent columns (hierarchy_A.Year + hierarchy_B.Year, one
    backing UDA) and a non-re-aggregatable query groups by only one of them, the
    matcher MUST refuse the aggregate (EXACT_GRAIN_MISMATCH -> source).

    Before the fix the matcher compared CANONICAL sets: it collapsed the 2-column
    aggregate grain to a single canonical ``year`` == the query's canonical
    ``year`` and ACCEPTED — but ``rewrite/aggregate.py`` maps the requested grain
    through ``logical_to_aggregate_grain`` and compares RAW sets, so it saw a
    size-1 mapped grain != the size-2 aggregate grain, set
    ``is_exact_grain=False``, and re-aggregated the stored count_distinct column
    (SUM/NULL over per-group distinct counts) = silently wrong numbers. The
    matcher now uses the rewriter-identical raw-set verdict, so the two layers
    agree and this shape falls to source (correct data)."""
    m = make_measure("customer_id", default_agg="count_distinct", is_additive=False)
    d = make_hierarchy_dimension(
        "hierarchy_A.Year", hierarchy_id="h-A",
        hierarchy_name="hierarchy_A", ordinal=1,
    )
    # Aggregate grain carries BOTH canonical-equivalent columns.
    agg = make_aggregate(
        ["hierarchy_A.Year", "hierarchy_B.Year"],
        [make_agg_col(m, "count_distinct")],
    )
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
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    # Matcher and rewriter now agree: this is NOT an exact-grain match, so the
    # non-additive query is refused and routed to source rather than served a
    # SUM/NULL over the stored distinct-count column.
    assert result.aggregate is None
    from src.routing.aggregate_matcher import AggregateSkipReason
    assert AggregateSkipReason.EXACT_GRAIN_MISMATCH in result.skip_reasons


def test_duplicate_canonical_grain_validator_uses_mapped_raw_exactness():
    """Bug-6857: validator must not re-open a route the matcher/rewriter treat
    as non-exact. Canonical equality collapses the aggregate's two raw grain
    columns to one ``year``; mapped raw equality keeps the duplicate visible."""
    m = make_measure("customer_id", default_agg="count_distinct", is_additive=False)
    d = make_dimension("hierarchy_A.Year")
    agg = make_aggregate(
        ["hierarchy_A.Year", "hierarchy_B.Year"],
        [make_agg_col(m, "count_distinct")],
    )
    bq = make_bound_query([d], [m])

    valid, reason = validate_aggregate_route(
        bq,
        agg,
        name_to_canonical={
            "hierarchy_A.Year": "year",
            "hierarchy_B.Year": "year",
        },
        logical_to_aggregate_grain={
            "hierarchy_A.Year": "hierarchy_A.Year",
        },
    )

    assert not valid
    assert "exact grain" in reason


async def test_canonical_equivalent_non_additive_same_raw_name_matches():
    """When raw grain names match (same name, not just canonical-equivalent),
    the exact-grain gate passes and the rewriter sees is_exact_grain=True,
    so count_distinct is served correctly from the stored column."""
    m = make_measure("customer_id", default_agg="count_distinct", is_additive=False)
    d = make_dimension("year")
    agg = make_aggregate(
        ["year"],
        [make_agg_col(m, "count_distinct")],
    )
    bq = make_bound_query([d], [m])

    canonical_dims = [
        CanonicalDim(
            canonical_name="year",
            backing_key="uda:shared-uda-1",
            all_names={"year"},
            is_time_dim=True,
        ),
    ]

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[agg]),
        patch(_PATCH_CANONICAL, new_callable=AsyncMock, return_value=canonical_dims),
        patch(_PATCH_CANONICAL_SNAPSHOT, return_value=canonical_dims),
    ):
        result = await find_best_aggregate(bq, AsyncMock())

    # Same raw names → match succeeds, rewriter sees is_exact_grain=True.
    assert result.aggregate is not None
    assert result.aggregate.id == agg.id
