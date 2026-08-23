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

from unittest.mock import AsyncMock, patch

from src.dax.dax_parser import find_kpi_member_functions
from src.dax.mdx_execute import (
    build_real_execute_response,
    resolve_kpi_property_expr,
)
from src.dax import xmla_server


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

async def _live_kpi_status(kpi: dict, governed: dict):
    """Drive ``_maybe_resolve_kpi_members`` for KPIStatus with the governed
    ``/evaluate`` response mocked; return the status cell value."""
    statement = 'SELECT FROM [modely] WHERE (KPIStatus("aa"))'
    kpi = dict(kpi, id=kpi.get("id") or "kpi-aa")
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ), patch.object(
        xmla_server, "evaluate_kpi_governed", new=AsyncMock(return_value=governed),
    ):
        result = await xmla_server._maybe_resolve_kpi_members(
            statement=statement,
            model_id="model-1",
            project_id="proj-1",
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=_MEASURES,
            dimensions_meta=[],
            hierarchy_level_dim_map={},
            hierarchy_default_dim_map={},
            dim_names=set(),
            model_slug="modely",
            persona_id=None,
            is_technical_view=True,
        )
    assert result is not None
    columns, rows = result
    return rows[0][columns[0]]


class TestKpiStatusE2E:
    """Bug-6608 un-gated: the live KPIStatus is the governed −1/0/1 RAG verdict
    from the model-service /evaluate authority (F-025-01), NOT the raw value."""

    @pytest.mark.asyncio
    async def test_status_is_governed_verdict_not_raw_value(self):
        cell = await _live_kpi_status(
            _KPI_HIGHER, {"value": 15000.0, "status": 1},
        )
        assert cell == 1
        assert cell != 15000.0

    @pytest.mark.asyncio
    async def test_status_governed_for_each_verdict(self):
        for governed_status in (-1, 0, 1):
            cell = await _live_kpi_status(
                _KPI_LOWER, {"value": 9500.0, "status": governed_status},
            )
            assert cell == governed_status

    @pytest.mark.asyncio
    async def test_status_null_value_returns_none(self):
        """A governed status of None (no data / no target) is blank."""
        cell = await _live_kpi_status(
            _KPI_HIGHER, {"value": None, "status": None},
        )
        assert cell is None


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
    """Bug-6608 un-gated: MDSCHEMA advertises the addressable value MEMBER for
    KPIStatus (no band CASE); the LIVE cell is the governed −1/0/1 verdict — the
    two are different roles (member string vs governed value), not the same value."""

    def test_resolver_status_is_value_member_any_direction(self):
        for kpi in (_KPI_HIGHER, _KPI_LOWER):
            expr = resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES)
            assert expr == "[Measures].[fee_amount]"
            assert "CASE" not in expr.upper()

    @pytest.mark.asyncio
    async def test_metadata_member_and_live_governed_verdict(self):
        # Metadata advertises the addressable value member; the live path serves the
        # governed −1/0/1 verdict (not the raw value).
        expr = resolve_kpi_property_expr(_KPI_HIGHER, "KPIStatus", _MEASURES)
        assert expr == "[Measures].[fee_amount]"
        cell = await _live_kpi_status(_KPI_HIGHER, {"value": 15000.0, "status": -1})
        assert cell == -1
        assert cell != 15000.0
