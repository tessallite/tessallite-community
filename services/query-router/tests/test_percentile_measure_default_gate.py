"""Phase 0 guards — measure-level quantile requests must not re-aggregate or
serve an approximate column in exact mode (Bug-7780 CRITICAL, Bug-7779 HIGH).

Spec: docs/strategy/strategy_percentile-aggregate-routing.md
(Gap G, invariant I1, §12.3 Reviewer adversarial cases).

Root causes:
- Bug-7780: ``compute_has_non_additive`` classified a measure whose
  ``default_agg`` is a quantile stat (p01..p99) as re-aggregatable because
  ``is_additive`` defaults True and the function registry has no pNN key. A
  superset-grain aggregate then matched and the bare-measure rewrite emitted
  ``SUM(stored_p50)`` at coarser grain — sum of group medians served as the
  median. The fix makes the measure loop treat any quantile ``default_agg`` as
  exact-grain non-additive.
- Bug-7779: ``_query_uses_percentile`` inspected only SELECT ``agg_function``,
  so a quantile arriving via ``default_agg`` or calc expansion skipped the
  dialect-exactness gate — a BigQuery-source aggregate served its
  ``APPROX_QUANTILES`` column in exact mode. The fix makes the inventory
  semantic (resolved measures + calc-expanded base stats).

Known-value assertions (numbers, not just non-empty rows):
- SUM-of-city-medians = 17.5 (2.5 + 15) must NEVER be served; the true
  combined-group median of the fixture is 7.
- BigQuery APPROX boundary 100 differs from the exact p90 of [1,100] = 90.1;
  the exact-mode query must route to source so the engine computes 90.1.

Run from tessallite/services/query-router/:
    pytest tests/test_percentile_measure_default_gate.py
"""
from __future__ import annotations

from statistics import median
from unittest.mock import AsyncMock, patch

import pytest

from src.routing.aggregate_matcher import (
    find_best_aggregate,
    compute_has_non_additive,
    AggregateSkipReason,
)
from src.routing.exactness_validator import validate_aggregate_route

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
# Bug-7780 — measure with quantile default_agg is exact-grain non-additive     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pnn", ["p50", "p25", "p90", "p95", "p99", "p01"])
def test_quantile_default_agg_is_non_additive(pnn):
    """A bare measure whose ``default_agg`` is any pNN stat is non-additive at
    the query grain — no re-aggregation is ever valid, even though
    ``is_additive`` is left at its True default."""
    m = make_measure("latency_p", default_agg=pnn)  # is_additive defaults True
    bq = make_bound_query([make_dimension("country")], [m])
    assert compute_has_non_additive(bq) is True


def test_plain_additive_measure_still_additive():
    """Guard the additive path is untouched: a SUM measure is still additive."""
    m = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [m])
    assert compute_has_non_additive(bq) is False


def test_legacy_median_default_agg_is_non_additive():
    """A measure carrying the legacy ``median`` token (un-coerced to p50) must
    still force exact grain — is_quantile_agg_token catches it where the pNN-only
    is_quantile_stat_type would not (Codex R2 finding 1)."""
    m = make_measure("med_x", default_agg="median")
    bq = make_bound_query([make_dimension("country")], [m])
    assert compute_has_non_additive(bq) is True


def test_having_median_forces_exact_grain():
    """Bug-7782 / Codex R2 finding 3: a HAVING percentile alongside a
    re-aggregatable SELECT measure must force exact grain, so the rewriter never
    computes a percentile over stored per-group medians at a coarser grain
    (median-of-medians, wrong numbers)."""
    revenue = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [revenue])
    bq.logical_query.having_raw = "HAVING MEDIAN(latency) > 5"
    assert compute_has_non_additive(bq) is True


def test_having_sum_does_not_force_exact_grain():
    """Control: a HAVING over an additive function does not force exact grain."""
    revenue = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [revenue])
    bq.logical_query.having_raw = "HAVING SUM(revenue) > 100"
    assert compute_has_non_additive(bq) is False


def test_explicit_sum_over_quantile_default_not_forced_exact_grain():
    """Codex R3 finding 2: an explicit SUM(latency) overrides a p50 default_agg.
    The additive path must stay byte-identical — this must NOT be forced to
    exact grain (the explicit SUM is a re-aggregatable additive request)."""
    m = make_measure("latency", default_agg="p50")
    bq = make_bound_query([make_dimension("country")], [m])
    # Explicit SUM(latency) on the select expression overrides default_agg.
    import types as _t
    bq.logical_query.select_expressions = [
        _t.SimpleNamespace(
            classification="analytical", agg_function="sum",
            inner_column="latency", composable=False, inner_aggregates=[],
        ),
    ]
    assert compute_has_non_additive(bq) is False


def test_explicit_median_over_sum_default_forces_exact_grain():
    """Symmetric control: an explicit MEDIAN(x) over a SUM-default measure DOES
    force exact grain (the explicit quantile function wins)."""
    m = make_measure("latency", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [m])
    import types as _t
    bq.logical_query.select_expressions = [
        _t.SimpleNamespace(
            classification="analytical", agg_function="p50",
            inner_column="latency", composable=False, inner_aggregates=[],
        ),
    ]
    assert compute_has_non_additive(bq) is True


async def test_quantile_measure_coarser_grain_routes_to_source():
    """The wrong-numbers repro: measure ``med_x`` default_agg=p50, aggregate at
    grain (country, city) with a p50 column, query GROUP BY country only.

    Before the fix the matcher matched the (country, city) aggregate and the
    rewriter emitted ``SUM(med_x__p50)`` at country grain — a sum of the two
    city medians (2.5 + 15 = 17.5) served as the country median. The true
    combined-group median of the fixture is 7. The matcher must instead reject
    the coarser aggregate (EXACT_GRAIN_MISMATCH) so the query falls to source.
    """
    m = make_measure("med_x", default_agg="p50")  # is_additive default True
    agg = make_aggregate(
        ["country", "city"],
        [make_agg_col(m, "p50")],
    )
    bq = make_bound_query([make_dimension("country")], [m])
    bq.resolved_dimensions_by_name = {
        "country": make_dimension("country"),
        "city": make_dimension("city"),
    }

    with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_INACTIVE, new_callable=AsyncMock, return_value=[]):
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is None
    assert AggregateSkipReason.EXACT_GRAIN_MISMATCH in result.skip_reasons


def test_sum_of_medians_is_not_the_median_sanity():
    """Independent fixture math proving the wrong-number the gate prevents:
    SUM of per-city medians (17.5) is not the true combined median (7)."""
    cairo = [1, 2, 3, 4]  # NULLs ignored by percentile semantics
    alexandria = [10, 10, 20, 100]
    combined = cairo + alexandria
    sum_of_city_medians = median(cairo) + median(alexandria)
    assert sum_of_city_medians == 17.5
    assert median(combined) == 7.0
    assert sum_of_city_medians != median(combined)


async def test_quantile_measure_exact_grain_still_matches():
    """The legitimate case still accelerates: measure default_agg=p50 grouped
    by exactly the aggregate grain (country) matches (no re-aggregation)."""
    m = make_measure("med_x", default_agg="p50")
    agg = make_aggregate(["country"], [make_agg_col(m, "p50")])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_INACTIVE, new_callable=AsyncMock, return_value=[]):
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())

    assert result.aggregate is agg


def test_validator_rejects_quantile_measure_at_coarser_grain():
    """The exactness validator shares compute_has_non_additive, so it also
    rejects a coarser-grain aggregate for a quantile-default measure."""
    m = make_measure("med_x", default_agg="p50")
    agg = make_aggregate(["country", "city"], [make_agg_col(m, "p50")])
    bq = make_bound_query([make_dimension("country")], [m])
    bq.resolved_dimensions_by_name = {"country": make_dimension("country")}

    valid, reason = validate_aggregate_route(bq, agg)
    assert valid is False
    assert "exact grain" in reason.lower()
