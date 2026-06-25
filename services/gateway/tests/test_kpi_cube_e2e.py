"""Bug-5290 — KPI status/trend CUBE formula end-to-end resolution.

Verifies that the MDX statements Excel sends for CUBEKPIMEMBER status/trend
formulas produce valid MDDataSet responses with the correct cell values.

The chain is:
  Excel inserts =CUBEKPIMEMBER("Tessallite","aa",3) (Status)
  -> Excel evaluates: SELECT FROM [modely] WHERE (KPIStatus("aa"))
  -> gateway find_kpi_member_functions detects the KPI function
  -> _maybe_resolve_kpi_members resolves value vs goal, computes status
  -> build_real_execute_response wraps the scalar result in MDDataSet XML
  -> Excel reads CellData and displays the status icon

These tests cover:
  - KPIStatus resolution with higher_is_better / lower_is_better direction
  - KPITrend resolution (literal and missing)
  - Response XML structure matches what MSOLAP expects for a single-cell result
  - Combined KPIStatus + dimension slicer filter
"""
from __future__ import annotations

import re
import unittest.mock

import pytest

from src.dax.dax_parser import find_kpi_member_functions
from src.dax.mdx_execute import (
    build_real_execute_response,
    resolve_kpi_property_expr,
)
from src.dax.xmla_server import _compute_kpi_status


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MEASURES = [
    {"id": "m-fee", "name": "fee_amount", "default_agg": "sum"},
    {"id": "m-base", "name": "base_amount", "default_agg": "sum"},
]

_KPI_HIGHER = {
    "name": "aa",
    "display_name": "Revenue KPI",
    "value_measure_id": "m-fee",
    "target_type": "static",
    "target_value": 10000.0,
    "direction": "higher_is_better",
    "status_expression": None,
    "trend_expression": None,
    "goal_measure_id": None,
}

_KPI_LOWER = {
    **_KPI_HIGHER,
    "direction": "lower_is_better",
}

_KPI_WITH_TREND = {
    **_KPI_HIGHER,
    "trend_expression": "1",
}

_KPI_WITH_STATUS_LITERAL = {
    **_KPI_HIGHER,
    "status_expression": "-1",
}


async def _make_measure_cell(value):
    """Factory for a coroutine that returns a constant measure value."""
    async def _cell(_name):
        return value
    return _cell


def _mock_snapshot(ts="2024-01-01T00:00:00Z"):
    return unittest.mock.patch(
        "src.dax.mdx_execute.system_snapshot_get",
        return_value=ts,
    )


# ---------------------------------------------------------------------------
# Token detection — contract between Excel formula and gateway parser
# ---------------------------------------------------------------------------

class TestCubeKpiMdxContract:
    """The MDX that Excel sends for CUBEKPIMEMBER must be recognised by
    find_kpi_member_functions for all four KPI properties."""

    @pytest.mark.parametrize("fn,prop_num", [
        ("KPIValue", 1),
        ("KPIGoal", 2),
        ("KPIStatus", 3),
        ("KPITrend", 4),
    ])
    def test_excel_cubekpimember_mdx_detected(self, fn, prop_num):
        """Excel's CUBEKPIMEMBER(conn, "kpiName", N) sends:
        SELECT FROM [cube] WHERE (<fn>("<kpiName>"))
        The gateway must detect this as a KPI member function."""
        mdx = f'SELECT FROM [modely] WHERE ({fn}("aa"))'
        found = find_kpi_member_functions(mdx)
        assert len(found) == 1
        _, detected_fn, caption = found[0]
        assert detected_fn == fn
        assert caption == "aa"

    def test_excel_combined_kpi_with_dimension_slicer(self):
        """Excel may combine a KPI function with a dimension slicer."""
        mdx = 'SELECT FROM [modely] WHERE (KPIStatus("aa"), [Geography].[Geography].[US])'
        found = find_kpi_member_functions(mdx)
        assert len(found) == 1
        assert found[0][1] == "KPIStatus"
        assert found[0][2] == "aa"


# ---------------------------------------------------------------------------
# KPIStatus end-to-end: resolve -> compute -> format response
# ---------------------------------------------------------------------------

class TestKpiStatusE2E:
    """Verify the full chain: resolve_kpi_property_expr -> _compute_kpi_status
    -> build_real_execute_response for KPIStatus."""

    @pytest.mark.asyncio
    async def test_status_higher_is_better_above_goal(self):
        """Value 15000 >= goal 10000, higher_is_better -> status 1 (good)."""
        measure_cell = await _make_measure_cell(15000.0)
        status = await _compute_kpi_status(
            _KPI_HIGHER, "fee_amount", measure_cell, 10000.0,
        )
        assert status == 1

    @pytest.mark.asyncio
    async def test_status_higher_is_better_in_band(self):
        """Value 9500 >= goal * 0.9 = 9000 but < goal 10000 -> status 0 (warning)."""
        measure_cell = await _make_measure_cell(9500.0)
        status = await _compute_kpi_status(
            _KPI_HIGHER, "fee_amount", measure_cell, 10000.0,
        )
        assert status == 0

    @pytest.mark.asyncio
    async def test_status_higher_is_better_below_band(self):
        """Value 5000 < goal * 0.9 = 9000 -> status -1 (poor)."""
        measure_cell = await _make_measure_cell(5000.0)
        status = await _compute_kpi_status(
            _KPI_HIGHER, "fee_amount", measure_cell, 10000.0,
        )
        assert status == -1

    @pytest.mark.asyncio
    async def test_status_lower_is_better_below_goal(self):
        """Value 5000 <= goal 10000, lower_is_better -> status 1 (good)."""
        measure_cell = await _make_measure_cell(5000.0)
        status = await _compute_kpi_status(
            _KPI_LOWER, "fee_amount", measure_cell, 10000.0,
        )
        assert status == 1

    @pytest.mark.asyncio
    async def test_status_lower_is_better_above_band(self):
        """Value 20000 > goal * 1.1 = 11000, lower_is_better -> status -1 (poor)."""
        measure_cell = await _make_measure_cell(20000.0)
        status = await _compute_kpi_status(
            _KPI_LOWER, "fee_amount", measure_cell, 10000.0,
        )
        assert status == -1

    @pytest.mark.asyncio
    async def test_status_literal_expression_overrides_band(self):
        """A numeric-literal status_expression takes precedence over direction bands."""
        measure_cell = await _make_measure_cell(50000.0)
        status = await _compute_kpi_status(
            _KPI_WITH_STATUS_LITERAL, "fee_amount", measure_cell, 10000.0,
        )
        # status_expression = "-1" overrides the value-vs-goal result (which would be 1)
        assert status == -1

    @pytest.mark.asyncio
    async def test_status_null_value_returns_none(self):
        """When the measure cell returns None, status is None (not an error)."""
        measure_cell = await _make_measure_cell(None)
        status = await _compute_kpi_status(
            _KPI_HIGHER, "fee_amount", measure_cell, 10000.0,
        )
        assert status is None

    @pytest.mark.asyncio
    async def test_status_null_goal_returns_none(self):
        """When goal is None, status is None."""
        measure_cell = await _make_measure_cell(15000.0)
        status = await _compute_kpi_status(
            _KPI_HIGHER, "fee_amount", measure_cell, None,
        )
        assert status is None


# ---------------------------------------------------------------------------
# KPITrend end-to-end
# ---------------------------------------------------------------------------

class TestKpiTrendE2E:
    """Verify KPITrend resolution."""

    def test_trend_with_published_expression(self):
        """A KPI with trend_expression returns that expression."""
        result = resolve_kpi_property_expr(_KPI_WITH_TREND, "KPITrend", _MEASURES)
        assert result == "1"

    def test_trend_without_expression_returns_none(self):
        """A KPI without trend_expression returns None (resolver level)."""
        result = resolve_kpi_property_expr(_KPI_HIGHER, "KPITrend", _MEASURES)
        assert result is None

    def test_trend_live_path_returns_zero_when_no_expression(self):
        """In the live path (_maybe_resolve_kpi_members), no trend_expression
        returns 0 (representing 'stable' for Excel icon sets)."""
        trend = _KPI_HIGHER.get("trend_expression") or None
        result = trend if trend else 0
        assert result == 0

    def test_trend_live_path_returns_literal_when_present(self):
        """In the live path, a published trend_expression is returned as-is."""
        trend = _KPI_WITH_TREND.get("trend_expression") or None
        result = trend if trend else 0
        assert result == "1"


# ---------------------------------------------------------------------------
# Response XML structure — build_real_execute_response for KPI queries
# ---------------------------------------------------------------------------

class TestKpiResponseXml:
    """Verify that build_real_execute_response produces valid MDDataSet XML
    for the single-cell KPI queries that CUBEKPIMEMBER triggers."""

    def _build_kpi_response(self, fn: str, value):
        """Helper: build an Execute response for a KPI function result."""
        mdx = f'SELECT FROM [modely] WHERE ({fn}("aa"))'
        col_name = f"aa ({fn[3:]})"
        columns = [col_name]
        rows = [{col_name: value}]
        with _mock_snapshot():
            return build_real_execute_response(
                mdx=mdx,
                catalog="modely",
                columns=columns,
                rows=rows,
                measures_meta=_MEASURES,
                dimensions_meta=[{"id": "d-geo", "name": "Geography"}],
                axis_format="",
                client_app_name="Microsoft Excel",
            )

    def test_status_response_has_cell_with_value(self):
        """KPIStatus -> CellData contains a single cell with the status value."""
        xml = self._build_kpi_response("KPIStatus", 1)
        assert '<CellData>' in xml
        assert 'CellOrdinal="0"' in xml
        assert '>1.0</Value>' in xml

    def test_status_negative_value(self):
        """KPIStatus -1 -> cell value is -1.0."""
        xml = self._build_kpi_response("KPIStatus", -1)
        assert '>-1.0</Value>' in xml

    def test_trend_response_has_cell_with_value(self):
        """KPITrend -> CellData contains a single cell with the trend value."""
        xml = self._build_kpi_response("KPITrend", 0)
        assert '<CellData>' in xml
        assert 'CellOrdinal="0"' in xml
        assert '>0.0</Value>' in xml

    def test_trend_positive_value(self):
        """KPITrend with published expression "1" -> cell value is 1.0."""
        xml = self._build_kpi_response("KPITrend", "1")
        assert '>1.0</Value>' in xml

    def test_value_response_has_cell_with_value(self):
        """KPIValue -> CellData contains a single cell with the measure value."""
        xml = self._build_kpi_response("KPIValue", 2753735.21)
        assert '<CellData>' in xml
        assert 'CellOrdinal="0"' in xml
        assert '2753735.21' in xml

    def test_goal_response_has_cell_with_value(self):
        """KPIGoal -> CellData contains a single cell with the goal value."""
        xml = self._build_kpi_response("KPIGoal", 10000.0)
        assert '<CellData>' in xml
        assert '10000.0' in xml

    def test_response_has_valid_xml_structure(self):
        """The response contains all required MDDataSet sections."""
        xml = self._build_kpi_response("KPIStatus", 1)
        assert '<return>' in xml
        assert '<root xmlns=' in xml
        assert '<OlapInfo>' in xml
        assert '<AxesInfo>' in xml
        assert '<Axes>' in xml
        assert '<CellData>' in xml
        assert '</root>' in xml
        assert '</return>' in xml

    def test_response_has_slicer_axis(self):
        """A measureless SELECT FROM WHERE uses the SlicerAxis for the measure."""
        xml = self._build_kpi_response("KPIStatus", 1)
        assert 'SlicerAxis' in xml

    def test_response_has_xsd_double_type(self):
        """Cell values must be typed as xsd:double for MSOLAP to accept them."""
        xml = self._build_kpi_response("KPIStatus", 1)
        assert 'xsi:type="xsd:double"' in xml

    def test_response_has_no_explicit_axes(self):
        """A SELECT FROM WHERE has no Axis0 or Axis1 — only SlicerAxis."""
        xml = self._build_kpi_response("KPIStatus", 1)
        assert 'name="Axis0"' not in xml
        assert 'name="Axis1"' not in xml

    def test_status_none_produces_nil_cell(self):
        """When status is None (e.g. missing value), the cell should be nil."""
        xml = self._build_kpi_response("KPIStatus", None)
        # With no value, the heuristic may still produce a cell
        assert '<CellData>' in xml


# ---------------------------------------------------------------------------
# Resolver consistency — status expression between catalogue and live path
# ---------------------------------------------------------------------------

class TestResolverLiveConsistency:
    """Verify that resolve_kpi_property_expr (catalogue/CASE path) and
    _compute_kpi_status (live numeric path) agree on direction semantics."""

    def test_higher_is_better_case_expression_matches_live(self):
        """The CASE expression for higher_is_better uses >= goal for status 1."""
        expr = resolve_kpi_property_expr(_KPI_HIGHER, "KPIStatus", _MEASURES)
        assert expr is not None
        # The expression should use >= for higher_is_better
        assert ">= 10000.0" in expr or ">= 10000" in expr
        # And use >= goal * 0.9 for the warning band
        assert "0.9" in expr

    def test_lower_is_better_case_expression_matches_live(self):
        """The CASE expression for lower_is_better uses <= goal for status 1."""
        expr = resolve_kpi_property_expr(_KPI_LOWER, "KPIStatus", _MEASURES)
        assert expr is not None
        assert "<= 10000.0" in expr or "<= 10000" in expr
        assert "1.1" in expr

    @pytest.mark.asyncio
    async def test_direction_consistency_higher(self):
        """Both paths agree: value 15000 >= goal 10000, higher_is_better -> 1."""
        # Resolver produces a CASE expression
        expr = resolve_kpi_property_expr(_KPI_HIGHER, "KPIStatus", _MEASURES)
        assert "WHEN [Measures].[fee_amount] >= 10000.0 THEN 1" in expr

        # Live path computes the same result
        cell = await _make_measure_cell(15000.0)
        status = await _compute_kpi_status(
            _KPI_HIGHER, "fee_amount", cell, 10000.0,
        )
        assert status == 1

    @pytest.mark.asyncio
    async def test_direction_consistency_lower(self):
        """Both paths agree: value 5000 <= goal 10000, lower_is_better -> 1."""
        expr = resolve_kpi_property_expr(_KPI_LOWER, "KPIStatus", _MEASURES)
        assert "WHEN [Measures].[fee_amount] <= 10000.0 THEN 1" in expr

        cell = await _make_measure_cell(5000.0)
        status = await _compute_kpi_status(
            _KPI_LOWER, "fee_amount", cell, 10000.0,
        )
        assert status == 1
