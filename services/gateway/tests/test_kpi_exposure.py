"""Tests for KPI exposure via JDBC virtual table and XMLA MDSCHEMA_KPIS.

Tests the gateway's ability to expose KPIs to BI clients through:
1. JDBC: $KPIs virtual table with KPI columns
2. JDBC: Inline KPI columns (when expose_kpis_inline=true)
3. XMLA: MDSCHEMA_KPIS rowset with v2 expression and composite support
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import mdschema
from src.router_client import (
    KPI_VIRTUAL_TABLE_COLUMNS,
    build_kpi_virtual_table_columns,
)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

CATALOG = "sales_model"

MEASURES = [
    {"id": "m1", "name": "Revenue", "default_agg": "sum", "display_name": "Revenue"},
    {"id": "m2", "name": "Cost", "default_agg": "sum", "display_name": "Cost"},
    {"id": "m3", "name": "Units", "default_agg": "sum", "display_name": "Units Sold"},
    {"id": "m4", "name": "Target_AOV", "default_agg": "avg", "display_name": "Target AOV"},
]

LEGACY_KPIS = [
    {
        "id": "k1",
        "name": "margin_pct",
        "display_name": "Margin %",
        "description": "Gross margin percentage",
        "display_folder": "Finance",
        "value_measure_id": "m1",
        "goal_measure_id": "m2",
        "status_expression": "IIF(KpiValue > KpiGoal, 1, -1)",
        "trend_expression": "",
        "weight": None,
        "parent_kpi_id": None,
        "presentation_type": None,
    },
]

V2_KPIS = [
    {
        "id": "k2",
        "name": "conversion_rate",
        "display_name": "Conversion Rate",
        "description": "Orders / Visits",
        "display_folder": "Marketing",
        "expression": 'safe_div(measure("Orders"), measure("Visits"))',
        "target_type": "static",
        "target_value": 0.05,
        "target_expression": None,
        "status_expression": "",
        "trend_expression": "",
        "weight": 0.4,
        "parent_kpi_id": None,
        "presentation_type": "gauge",
        "value_measure_id": None,
        "goal_measure_id": None,
    },
    {
        "id": "k3",
        "name": "avg_order_value",
        "display_name": "Avg Order Value",
        "description": "Revenue per order",
        "display_folder": "Sales",
        "expression": 'safe_div(measure("Revenue"), measure("Orders"))',
        "target_type": "measure",
        "target_value": None,
        # A measure target is identified by target_measure_id (a measure ref),
        # NOT a DSL string. The gateway resolves it to executable MDX.
        "target_measure_id": "m4",
        "target_expression": 'measure("Target_AOV")',
        "status_expression": "",
        "trend_expression": "",
        "weight": 0.6,
        "parent_kpi_id": None,
        "presentation_type": "bullet_chart",
        "value_measure_id": None,
        "goal_measure_id": None,
    },
]

COMPOSITE_KPIS = [
    {
        "id": "k10",
        "name": "overall_health",
        "display_name": "Overall Health",
        "description": "Composite health score",
        "display_folder": "",
        "expression": "",
        "target_type": None,
        "target_value": None,
        "target_expression": None,
        "status_expression": "",
        "trend_expression": "",
        "weight": None,
        "parent_kpi_id": None,
        "presentation_type": "gauge",
        "value_measure_id": None,
        "goal_measure_id": None,
        "kpi_type": "composite",
    },
    {
        "id": "k11",
        "name": "child_revenue",
        "display_name": "Revenue KPI",
        "description": "",
        "display_folder": "",
        "expression": 'measure("Revenue")',
        "target_type": "static",
        "target_value": 1000,
        "target_expression": None,
        "status_expression": "",
        "trend_expression": "",
        "weight": 0.7,
        "parent_kpi_id": "k10",
        "presentation_type": None,
        "value_measure_id": None,
        "goal_measure_id": None,
    },
    {
        "id": "k12",
        "name": "child_satisfaction",
        "display_name": "Satisfaction KPI",
        "description": "",
        "display_folder": "",
        "expression": 'measure("CSAT")',
        "target_type": "static",
        "target_value": 90,
        "target_expression": None,
        "status_expression": "",
        "trend_expression": "",
        "weight": 0.3,
        "parent_kpi_id": "k10",
        "presentation_type": None,
        "value_measure_id": None,
        "goal_measure_id": None,
    },
]


# ---------------------------------------------------------------------------
# JDBC $KPIs virtual table schema (long / scorecard)
# ---------------------------------------------------------------------------


class TestKpiVirtualTableSchema:
    """The $KPIs virtual table is advertised in the long/scorecard shape and
    must match the query-router's _handle_kpi_table_query output columns."""

    # The exact columns _handle_kpi_table_query returns (one row per KPI).
    ROUTER_OUTPUT_COLUMNS = [
        "kpi_name", "value", "target", "status",
        "status_label", "trend_pct", "formatted_value", "evaluated_at",
    ]

    def test_columns_match_router_output(self):
        names = [name for name, _type in KPI_VIRTUAL_TABLE_COLUMNS]
        assert names == self.ROUTER_OUTPUT_COLUMNS, (
            "Advertised $KPIs columns must match the query-router's "
            "_handle_kpi_table_query output so metadata and data agree."
        )

    def test_schema_is_fixed_not_per_kpi(self):
        # Regression: previously one float8 column per KPI (wide). The schema
        # must be a single fixed set regardless of how many KPIs exist.
        cols = build_kpi_virtual_table_columns()
        assert len(cols) == len(self.ROUTER_OUTPUT_COLUMNS)
        assert [c["name"] for c in cols] == self.ROUTER_OUTPUT_COLUMNS

    def test_kpi_name_is_text_key_and_value_is_numeric(self):
        by_name = {c["name"]: c for c in build_kpi_virtual_table_columns()}
        assert by_name["kpi_name"]["data_type"] == "text"
        assert by_name["kpi_name"]["is_primary_key"] is True
        assert by_name["kpi_name"]["is_nullable"] is False
        assert by_name["value"]["data_type"] == "float8"
        assert by_name["value"]["kind"] == "measure"

    def test_ordinal_positions_are_sequential(self):
        cols = build_kpi_virtual_table_columns()
        assert [c["ordinal_position"] for c in cols] == list(range(1, len(cols) + 1))


# ---------------------------------------------------------------------------
# XMLA MDSCHEMA_KPIS tests
# ---------------------------------------------------------------------------


class TestRowsKpisLegacy:
    """Tests for legacy KPIs (value_measure_id / goal_measure_id)."""

    def test_legacy_kpi_value_references_measure(self):
        rows = mdschema._rows_kpis(CATALOG, LEGACY_KPIS, MEASURES)
        assert len(rows) == 1
        row = rows[0]
        assert row["KPI_VALUE"] == "[Measures].[Revenue]"
        assert row["KPI_GOAL"] == "[Measures].[Cost]"

    def test_legacy_kpi_metadata(self):
        rows = mdschema._rows_kpis(CATALOG, LEGACY_KPIS, MEASURES)
        row = rows[0]
        assert row["KPI_NAME"] == "margin_pct"
        assert row["KPI_CAPTION"] == "Margin %"
        assert row["KPI_DESCRIPTION"] == "Gross margin percentage"
        assert row["KPI_DISPLAY_FOLDER"] == "Finance"
        assert row["CATALOG_NAME"] == CATALOG
        assert row["CUBE_NAME"] == CATALOG

    def test_legacy_kpi_status_expression(self):
        rows = mdschema._rows_kpis(CATALOG, LEGACY_KPIS, MEASURES)
        assert rows[0]["KPI_STATUS"] == "IIF(KpiValue > KpiGoal, 1, -1)"


class TestRowsKpisV2:
    """Tests for v2 expression-based KPIs."""

    def test_v2_kpi_uses_mdx_member_as_value(self):
        # Bug-6702: KPI_VALUE must be an EXECUTABLE measure member the XMLA Execute
        # path can resolve. The old F-017-23 synthetic [Measures].[[KPI] <name>]
        # inline column is never present in the XMLA measure set, so Execute
        # refused it. A COMPOSITE expression (safe_div(measure("Orders"),
        # measure("Visits"))) has no single executable measure member, so KPI_VALUE
        # is "" (undefined) rather than a member Execute cannot run — catalogue and
        # Execute agree by construction.
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        assert len(rows) == 2
        assert rows[0]["KPI_VALUE"] == ""

    def test_v2_single_measure_expression_value_is_executable_member(self):
        # Bug-6702: a v2 KPI whose value expression is a single bare measure
        # (measure("Revenue")) resolves to that measure's executable XMLA member.
        kpi = {
            **V2_KPIS[0],
            "id": "k2s",
            "name": "revenue_kpi",
            "expression": 'measure("Revenue")',
        }
        rows = mdschema._rows_kpis(CATALOG, [kpi], MEASURES)
        assert rows[0]["KPI_VALUE"] == "[Measures].[Revenue]"

    def test_v2_kpi_status_is_addressable_member_not_case(self):
        # Bug-6608 (un-gated): MDSCHEMA_KPIS advertises the addressable status
        # MEMBER (never a band CASE). The live −1/0/1 verdict is governed by the
        # model-service authority, not this metadata string. Bug-6702: a composite
        # expression with no single executable member has "" (== KPI_VALUE), never a
        # synthetic inline column.
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        status = rows[0]["KPI_STATUS"]
        assert status == ""
        assert status == rows[0]["KPI_VALUE"]
        assert not status.upper().startswith("CASE")

    def test_v2_static_target(self):
        # Bug-6888: a static target is advertised as the synthetic goal support
        # MEMBER (a bare scalar is not addable from Excel's KPI field list);
        # the Execute path resolves the member to the 0.05 constant.
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        assert rows[0]["KPI_GOAL"] == "[Measures].[Conversion Rate Goal]"

    def test_v2_measure_target(self):
        # Bug-6259: a measure target resolves to an EXECUTABLE MDX member
        # reference (via target_measure_id), never the raw Tessallite DSL string.
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        assert rows[1]["KPI_GOAL"] == "[Measures].[Target_AOV]"

    def test_v2_weight(self):
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        assert rows[0]["KPI_WEIGHT"] == "0.4"
        assert rows[1]["KPI_WEIGHT"] == "0.6"

    def test_v2_status_graphic_suppressed_without_authored_status(self):
        # Fable R1 finding 1: MDSCHEMA KPI_STATUS is the value member, which a
        # native pivot binds directly (no KPIStatus() interception) -> graphic
        # suppressed to avoid clamping a raw value onto -1/0/1 icon domain. Only
        # authored status_expression KPIs keep the graphic. (Both V2_KPIS lack
        # an authored status_expression.)
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        assert rows[0]["KPI_STATUS_GRAPHIC"] == ""
        assert rows[1]["KPI_STATUS_GRAPHIC"] == ""

    def test_authored_status_expression_keeps_graphic(self):
        # A modeller-authored status expression IS a verdict, so its graphic stays.
        rows = mdschema._rows_kpis(CATALOG, LEGACY_KPIS, MEASURES)
        # LEGACY_KPIS[0] has presentation_type None -> default "Traffic Light".
        assert rows[0]["KPI_STATUS_GRAPHIC"] == "Traffic Light"

    def test_v2_display_folder(self):
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS, MEASURES)
        assert rows[0]["KPI_DISPLAY_FOLDER"] == "Marketing"
        assert rows[1]["KPI_DISPLAY_FOLDER"] == "Sales"


class TestRowsKpisComposite:
    """Tests for composite KPI parent/child relationships."""

    def test_composite_parent_has_no_parent_name(self):
        rows = mdschema._rows_kpis(CATALOG, COMPOSITE_KPIS, MEASURES)
        parent_row = [r for r in rows if r["KPI_NAME"] == "overall_health"][0]
        assert parent_row["KPI_PARENT_KPI_NAME"] == ""

    def test_composite_children_reference_parent(self):
        rows = mdschema._rows_kpis(CATALOG, COMPOSITE_KPIS, MEASURES)
        child_rows = [r for r in rows if r["KPI_PARENT_KPI_NAME"] == "overall_health"]
        assert len(child_rows) == 2
        names = {r["KPI_NAME"] for r in child_rows}
        assert names == {"child_revenue", "child_satisfaction"}

    def test_composite_children_have_weights(self):
        rows = mdschema._rows_kpis(CATALOG, COMPOSITE_KPIS, MEASURES)
        rev_row = [r for r in rows if r["KPI_NAME"] == "child_revenue"][0]
        sat_row = [r for r in rows if r["KPI_NAME"] == "child_satisfaction"][0]
        assert rev_row["KPI_WEIGHT"] == "0.7"
        assert sat_row["KPI_WEIGHT"] == "0.3"


class TestRowsKpisEdgeCases:
    """Edge cases for MDSCHEMA_KPIS."""

    def test_empty_kpis_list(self):
        rows = mdschema._rows_kpis(CATALOG, [], MEASURES)
        assert rows == []

    def test_kpi_with_no_measures(self):
        rows = mdschema._rows_kpis(CATALOG, LEGACY_KPIS, [])
        assert len(rows) == 1
        assert rows[0]["KPI_VALUE"] == ""
        assert rows[0]["KPI_GOAL"] == ""

    def test_kpi_with_no_expression_and_no_measure(self):
        kpi = {
            "id": "k99",
            "name": "empty_kpi",
            "display_name": "Empty",
            "description": "",
            "display_folder": "",
            "expression": "",
            "value_measure_id": None,
            "goal_measure_id": None,
            "status_expression": None,
            "trend_expression": None,
            "weight": None,
            "parent_kpi_id": None,
            "presentation_type": None,
            "target_type": None,
            "target_value": None,
            "target_expression": None,
        }
        rows = mdschema._rows_kpis(CATALOG, [kpi], MEASURES)
        assert len(rows) == 1
        assert rows[0]["KPI_VALUE"] == ""
        assert rows[0]["KPI_GOAL"] == ""

    def test_kpi_weight_none_emits_empty_string(self):
        rows = mdschema._rows_kpis(CATALOG, LEGACY_KPIS, MEASURES)
        assert rows[0]["KPI_WEIGHT"] == ""

    def test_all_required_xmla_columns_present(self):
        """Every KPI row must have all columns expected by XMLA clients."""
        required_keys = {
            "CATALOG_NAME", "SCHEMA_NAME", "CUBE_NAME",
            "MEASUREGROUP_NAME", "KPI_NAME", "KPI_CAPTION",
            "KPI_DESCRIPTION", "KPI_DISPLAY_FOLDER", "KPI_VALUE",
            "KPI_GOAL", "KPI_STATUS", "KPI_TREND",
            "KPI_STATUS_GRAPHIC", "KPI_TREND_GRAPHIC", "KPI_WEIGHT",
            "KPI_CURRENT_TIME_MEMBER", "KPI_PARENT_KPI_NAME",
            "ANNOTATIONS",
        }
        rows = mdschema._rows_kpis(CATALOG, V2_KPIS + COMPOSITE_KPIS, MEASURES)
        for row in rows:
            missing = required_keys - set(row.keys())
            assert not missing, f"Missing XMLA columns: {missing}"
