"""Regression tests for Bug-9764 — hierarchy DrilldownLevel resolves to level 1.

``DrilldownLevel({[H].[H].[All]})`` on a genuine multi-level hierarchy must
return the All member plus the first data level (Year, Continent, …), not leaf
days. Option (d): rewrite to ``{[H].[H].[Level1].Members}`` at the XMLA seam
and register a single-level ``SubtotalHierarchy`` for the All row.
"""

from src.dax.subtotal_engine import (
    detect_all_members_hierarchy_rollups,
    detect_drilldown_hierarchy_rollups,
    rewrite_drilldown_level_hierarchy_all,
    rewrite_hierarchy_all_members_to_first_level,
)
from src.dax.xmla_server import _mdx_to_sql, _mdx_axis_expr

CALENDAR_HIER = {
    "name": "business_date Calendar",
    "levels": [
        {"ordinal": 0, "name": "(All)", "key_attribute": {"id": "c-all", "source": "physical_column"}},
        {"ordinal": 1, "name": "Year", "key_attribute": {"id": "c-year", "source": "physical_column"}},
        {"ordinal": 2, "name": "Month", "key_attribute": {"id": "c-month", "source": "physical_column"}},
        {"ordinal": 3, "name": "Day", "key_attribute": {"id": "c-day", "source": "physical_column"}},
    ],
}

CALENDAR_DIMS = [
    {"name": "business_date_calendar_year", "source_column_id": "c-year"},
    {"name": "business_date_calendar_month", "source_column_id": "c-month"},
    {"name": "business_date_calendar_day", "source_column_id": "c-day"},
]

CALENDAR_LEVEL_MAP = {
    "business_date calendar": {
        "year": "business_date_calendar_year",
        "month": "business_date_calendar_month",
        "day": "business_date_calendar_day",
    },
}

DRILL_CAL = (
    "DrilldownLevel({[business_date Calendar].[business_date Calendar].[All]})"
)
DRILL_CAL_WIRE = (
    "DrilldownLevel({[Hierarchies].[business_date Calendar].[All]})"
)
FLAT_DRILL = "DrilldownLevel({[account_type].[account_type].[All]})"


class TestBug9619DrilldownHierarchyDetection:
    def test_detects_single_level_subtotal_hierarchy(self):
        rollups = detect_drilldown_hierarchy_rollups(
            "",
            DRILL_CAL,
            [CALENDAR_HIER],
            CALENDAR_LEVEL_MAP,
            {"account_type"},
        )
        assert len(rollups) == 1
        assert rollups[0].hierarchy_name == "business_date Calendar"
        assert [lvl.name for lvl in rollups[0].levels] == ["Year"]
        assert rollups[0].levels[0].dim_name == "business_date_calendar_year"

    def test_flat_attribute_drilldown_stays_out(self):
        assert detect_drilldown_hierarchy_rollups(
            "", FLAT_DRILL, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        ) == []

    def test_detects_wire_hierarchy_unique_name(self):
        rollups = detect_drilldown_hierarchy_rollups(
            "",
            DRILL_CAL_WIRE,
            [CALENDAR_HIER],
            CALENDAR_LEVEL_MAP,
            {"account_type"},
        )
        assert len(rollups) == 1
        assert rollups[0].levels[0].dim_name == "business_date_calendar_year"


class TestBug9619DrilldownHierarchyRewrite:
    def test_rewrites_to_level_one_members(self):
        mdx = f"SELECT {{[Measures].[base_amount]}} ON COLUMNS, {DRILL_CAL} ON ROWS FROM [modely]"
        out = rewrite_drilldown_level_hierarchy_all(
            mdx, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        )
        assert DRILL_CAL not in out
        assert "{[business_date Calendar].[business_date Calendar].[Year].Members}" in out
        assert FLAT_DRILL not in mdx or FLAT_DRILL in out  # unchanged when not in input

    def test_rewrites_wire_hierarchy_unique_name(self):
        out = rewrite_drilldown_level_hierarchy_all(
            DRILL_CAL_WIRE, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        )
        assert DRILL_CAL_WIRE not in out
        assert "{[Hierarchies].[business_date Calendar].[Year].Members}" in out

    def test_rewrite_is_idempotent(self):
        once = rewrite_drilldown_level_hierarchy_all(
            DRILL_CAL, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        )
        twice = rewrite_drilldown_level_hierarchy_all(
            once, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        )
        assert once == twice


class TestBug9619DrilldownHierarchySql:
    def test_mdx_to_sql_groups_by_year_not_day(self):
        mdx = f"""
        SELECT {{[Measures].[base_amount]}} ON COLUMNS,
               {DRILL_CAL} ON ROWS
        FROM [modely]
        """
        sql, protocol = _mdx_to_sql(
            mdx,
            [{"name": "base_amount", "default_agg": "sum"}],
            CALENDAR_DIMS,
            hierarchy_meta=[CALENDAR_HIER],
            model_slug="modely",
        )
        assert protocol == "jdbc"
        assert 'GROUP BY "business_date_calendar_year"' in sql
        assert "business_date_calendar_day" not in sql

    def test_mdx_to_sql_wire_hierarchy_drilldown_groups_by_year(self):
        mdx = f"""
        SELECT {{[Measures].[base_amount]}} ON COLUMNS,
               {DRILL_CAL_WIRE} ON ROWS
        FROM [modely]
        """
        sql, _ = _mdx_to_sql(
            mdx,
            [{"name": "base_amount", "default_agg": "sum"}],
            CALENDAR_DIMS,
            hierarchy_meta=[CALENDAR_HIER],
            model_slug="modely",
        )
        assert 'GROUP BY "business_date_calendar_year"' in sql
        assert "business_date_calendar_day" not in sql

    def test_geo_hierarchy_drilldown_uses_first_data_level(self):
        mdx = """
        SELECT {[Measures].[Amount]} ON COLUMNS,
               DrilldownLevel({[GeoHierarchy].[GeoHierarchy].[All]}) ON ROWS
        FROM [demo]
        """
        measures_meta = [{"name": "Amount", "default_agg": "sum"}]
        dimensions_meta = [
            {"name": "continent_dim", "source_column_id": "col-continent"},
            {"name": "country_dim", "source_column_id": "col-country"},
            {"name": "city_dim", "source_column_id": "col-city"},
        ]
        hierarchy_meta = [{
            "name": "GeoHierarchy",
            "levels": [
                {"ordinal": 0, "name": "Continent", "key_attribute": {"id": "col-continent", "source": "physical_column"}},
                {"ordinal": 1, "name": "Country", "key_attribute": {"id": "col-country", "source": "physical_column"}},
                {"ordinal": 2, "name": "City", "key_attribute": {"id": "col-city", "source": "physical_column"}},
            ],
        }]
        sql, _ = _mdx_to_sql(
            mdx, measures_meta, dimensions_meta, hierarchy_meta=hierarchy_meta,
        )
        assert 'GROUP BY "continent_dim"' in sql
        assert "city_dim" not in sql


ALL_MEMBERS_CAL = (
    "[business_date Calendar].[business_date Calendar].[(All)].Members"
)
ALL_MEMBERS_CAL_WIRE = (
    "[Hierarchies].[business_date Calendar].[(All)].Members"
)
ALL_MEMBERS_EXCEL = """
SELECT NON EMPTY Hierarchize(AddCalculatedMembers(
  {[business_date Calendar].[business_date Calendar].[(All)].Members}
)) ON COLUMNS FROM [modely] WHERE ([Measures].[base_amount])
"""


class TestBug9619AllMembersHierarchyDetection:
    def test_detects_lone_all_members_on_axis(self):
        rollups = detect_all_members_hierarchy_rollups(
            "",
            ALL_MEMBERS_CAL,
            [CALENDAR_HIER],
            CALENDAR_LEVEL_MAP,
            {"account_type"},
        )
        assert len(rollups) == 1
        assert rollups[0].levels[0].name == "Year"

    def test_skips_crossjoin_subtotal_shape(self):
        cross = (
            "CrossJoin("
            f"{{{ALL_MEMBERS_CAL}}}, "
            "{[account_type].[account_type].[(All)].Members})"
        )
        assert detect_all_members_hierarchy_rollups(
            cross, "", [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        ) == []


class TestBug9619AllMembersHierarchyRewrite:
    def test_rewrites_lone_all_members_to_year(self):
        mdx = f"SELECT {{[Measures].[base_amount]}} ON COLUMNS, {{{ALL_MEMBERS_CAL}}} ON ROWS FROM [modely]"
        col = _mdx_axis_expr(mdx, 0)
        row = _mdx_axis_expr(mdx, 1)
        out = rewrite_hierarchy_all_members_to_first_level(
            mdx, col, row, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        )
        assert "[Year].Members" in out
        assert "[(All)].Members" not in out

    def test_rewrites_wire_all_members(self):
        mdx = f"SELECT {{{ALL_MEMBERS_CAL_WIRE}}} ON ROWS FROM [modely]"
        col = _mdx_axis_expr(mdx, 0)
        row = _mdx_axis_expr(mdx, 1)
        out = rewrite_hierarchy_all_members_to_first_level(
            mdx, col, row, [CALENDAR_HIER], CALENDAR_LEVEL_MAP, {"account_type"},
        )
        assert "[Hierarchies].[business_date Calendar].[Year].Members" in out


class TestBug9619AllMembersHierarchySql:
    def test_mdx_to_sql_groups_by_year_for_all_members(self):
        mdx = f"""
        SELECT {{[Measures].[base_amount]}} ON COLUMNS,
               {{{ALL_MEMBERS_CAL}}} ON ROWS
        FROM [modely]
        """
        sql, _ = _mdx_to_sql(
            mdx,
            [{"name": "base_amount", "default_agg": "sum"}],
            CALENDAR_DIMS,
            hierarchy_meta=[CALENDAR_HIER],
            model_slug="modely",
        )
        assert 'GROUP BY "business_date_calendar_year"' in sql
        assert "business_date_calendar_day" not in sql

    def test_excel_hierarchize_shape_groups_by_year(self):
        sql, _ = _mdx_to_sql(
            ALL_MEMBERS_EXCEL,
            [{"name": "base_amount", "default_agg": "sum"}],
            CALENDAR_DIMS,
            hierarchy_meta=[CALENDAR_HIER],
            model_slug="modely",
        )
        assert "business_date_calendar_day" not in sql
