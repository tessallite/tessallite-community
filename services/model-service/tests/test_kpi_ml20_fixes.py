"""ML20 (unit 017) business-outcome tests for the medium/low KPI fixes.

Each test asserts a real numeric / structural outcome of an ML20 fix, not an
implementation detail. Pure-unit only (no live stack); the endpoint-level
behaviours (F-017-08/10/11/12/13/14) are exercised by the integration suite
against the live gateway.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.kpi_compiler import CompilerContext, compile_expression
from src.kpi_composite import ChildScore, evaluate_composite
from src.kpi_business_builder import validate_business_definition
from src.api.kpis import _select_prior_snapshot_value
from shared.semantic.kpi_expression import validate_expression


@dataclass
class _Snap:
    value: float
    snapshot_at: datetime


# --------------------------------------------------------------------------
# F-017-13 — trend prior value is the snapshot nearest (now - trend_period)
# --------------------------------------------------------------------------

class TestPriorSnapshotSelection:
    def _series(self):
        # Oldest -> newest, one per month, value = month index.
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return [
            _Snap(100.0, now - timedelta(days=90)),   # ~3 months ago
            _Snap(110.0, now - timedelta(days=60)),   # ~2 months ago
            _Snap(120.0, now - timedelta(days=30)),   # ~1 month ago
            _Snap(130.0, now),                          # newest
        ]

    def test_month_period_picks_month_ago(self):
        # trend_period "month" -> compare against the snapshot ~1 month before
        # the newest (value 120), not the immediately preceding one.
        val = _select_prior_snapshot_value(self._series(), "month")
        assert val == 120.0

    def test_quarter_period_picks_quarter_ago(self):
        # ~91 days before newest -> the 90-days-ago snapshot (value 100).
        val = _select_prior_snapshot_value(self._series(), "quarter")
        assert val == 100.0

    def test_unknown_period_falls_back_to_newest(self):
        val = _select_prior_snapshot_value(self._series(), None)
        assert val == 130.0

    def test_single_snapshot_returns_it(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        val = _select_prior_snapshot_value([_Snap(42.0, now)], "month")
        assert val == 42.0


# --------------------------------------------------------------------------
# F-017-21 — median outer aggregation + inner-grain DATE_TRUNC
# --------------------------------------------------------------------------

class TestMedianAndGrain:
    def test_median_outer_agg_uses_percentile_cont(self):
        # MEDIAN(...) is invalid PostgreSQL; the compiler must emit the
        # ordered-set PERCENTILE_CONT form.
        ctx = CompilerContext(
            inner_agg="sum", inner_grain="month", outer_agg="median",
            time_column="business_date",
        )
        sql = compile_expression('measure("Revenue")', ctx).sql.upper()
        assert "PERCENTILE_CONT(0.5)" in sql
        assert "MEDIAN(" not in sql

    def test_inner_grain_keyword_date_truncs(self):
        ctx = CompilerContext(
            inner_agg="avg", inner_grain="month", outer_agg="sum",
            time_column="business_date",
        )
        sql = compile_expression('measure("Revenue")', ctx).sql.upper()
        assert "DATE_TRUNC('MONTH'" in sql

    def test_inner_grain_real_column_not_truncated(self):
        # A non-grain-keyword inner_grain is a real column, used verbatim.
        ctx = CompilerContext(
            inner_agg="avg", inner_grain="region_code", outer_agg="sum",
        )
        sql = compile_expression('measure("Revenue")', ctx).sql
        assert '"region_code"' in sql
        assert "DATE_TRUNC" not in sql.upper()


# --------------------------------------------------------------------------
# F-017-24 — row_first / pre_aggregated honour outer_agg
# --------------------------------------------------------------------------

class TestOuterAggModes:
    def test_row_first_avg_per_row(self):
        # Average-per-product margin (spec 5.4.2): outer_agg=avg must produce
        # AVG(...) across rows, not the context default SUM.
        ctx = CompilerContext(calc_agg_mode="row_first", outer_agg="avg")
        expr = compile_expression('measure("Margin")', ctx).select_expr
        assert expr.startswith("AVG(")

    def test_pre_aggregated_aggregates_not_bare(self):
        ctx = CompilerContext(calc_agg_mode="pre_aggregated", outer_agg="avg")
        expr = compile_expression('measure("Margin")', ctx).select_expr
        assert expr == 'AVG("Margin")'


# --------------------------------------------------------------------------
# F-017-22 — share/rank outside grouped CTE fails closed (no degenerate 1)
# --------------------------------------------------------------------------

class TestUngroupedWindow:
    def test_share_of_total_plain_expression_flagged(self):
        ctx = CompilerContext()  # no share_type -> plain path
        compiled = compile_expression('share_of_total(measure("Revenue"))', ctx)
        assert compiled.has_ungrouped_window is True

    def test_rank_over_plain_expression_flagged(self):
        ctx = CompilerContext()
        compiled = compile_expression('rank_over(measure("Revenue"))', ctx)
        assert compiled.has_ungrouped_window is True

    def test_plain_measure_not_flagged(self):
        ctx = CompilerContext()
        compiled = compile_expression('measure("Revenue")', ctx)
        assert compiled.has_ungrouped_window is False


# --------------------------------------------------------------------------
# F-017-09 — composite min_max derives bounds from per-child history
# --------------------------------------------------------------------------

class TestCompositeMinMaxBounds:
    def test_min_max_without_config_uses_child_bounds(self):
        # No composite-level bounds, but the child carries snapshot-derived
        # bounds -> the child scores instead of being excluded (NULL composite).
        child = ChildScore(
            kpi_id="1", kpi_name="Revenue", raw_value=75.0, target=None,
            weight=1.0, direction="higher_is_better",
            bound_min=50.0, bound_max=100.0,
        )
        result = evaluate_composite([child], normalisation_method="min_max")
        # (75-50)/(100-50)*100 = 50
        assert result.composite_score == 50.0
        assert child.excluded is False

    def test_min_max_without_any_bounds_excludes(self):
        child = ChildScore(
            kpi_id="1", kpi_name="Revenue", raw_value=75.0, target=None,
            weight=1.0, direction="higher_is_better",
        )
        result = evaluate_composite([child], normalisation_method="min_max")
        assert result.composite_score is None
        assert child.excluded is True

    def test_config_bounds_override_child_bounds(self):
        child = ChildScore(
            kpi_id="1", kpi_name="Revenue", raw_value=75.0, target=None,
            weight=1.0, direction="higher_is_better",
            bound_min=0.0, bound_max=1000.0,
        )
        # Composite-level bounds take precedence: (75-50)/(100-50)*100 = 50.
        result = evaluate_composite(
            [child], normalisation_method="min_max",
            bound_min=50.0, bound_max=100.0,
        )
        assert result.composite_score == 50.0


# --------------------------------------------------------------------------
# F-017-15 — grain validation is positional (builder & spec argument order)
# --------------------------------------------------------------------------

class TestGrainValidationOrder:
    def test_builder_order_invalid_grain_rejected(self):
        # Wizard/builder order (expr, grain, literal(n)) — invalid grain must
        # still be caught even though it is not the last argument.
        res = validate_expression(
            'moving_avg(measure("Revenue"), "fortnight", literal(3))'
        )
        assert res.valid is False
        assert any(e.code == "INVALID_GRAIN" for e in res.errors)

    def test_builder_order_valid_grain_accepted(self):
        res = validate_expression(
            'moving_avg(measure("Revenue"), "month", literal(3))'
        )
        assert all(e.code != "INVALID_GRAIN" for e in res.errors)


# --------------------------------------------------------------------------
# F-017-19 — custom_range date format validation
# --------------------------------------------------------------------------

class TestCustomRangeValidation:
    _M_IDS = {"m1"}
    _D_IDS = {"d1"}
    _TD_IDS = {"d1"}

    def _bd(self, start, end):
        return {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": "m1",
                "aggregation": "sum",
            },
            "time_window": {
                "dimension_id": "d1",
                "preset": "custom_range",
                "start": start,
                "end": end,
            },
        }

    def test_non_iso_start_rejected(self):
        errors = validate_business_definition(
            self._bd("2024-01-01'; DROP TABLE", "2024-12-31"),
            self._M_IDS, self._D_IDS, self._TD_IDS,
        )
        assert any("custom_range start" in e for e in errors)

    def test_iso_dates_accepted(self):
        errors = validate_business_definition(
            self._bd("2024-01-01", "2024-12-31"),
            self._M_IDS, self._D_IDS, self._TD_IDS,
        )
        assert not any("custom_range" in e for e in errors)
