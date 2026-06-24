"""Tests for MDX Timeline slicer support (Block C)."""
import pytest

from src.dax.xmla_server import _mdx_to_sql
from src.dax.mdschema import _rows_levels, _rows_hierarchies, _time_level_type


# ---------------------------------------------------------------------------
# C.1 — Timeline MDX pattern recognition (range operator)
# ---------------------------------------------------------------------------

def test_timeline_month_range_3_months():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Product].[Product].Members} ON ROWS
    FROM (
        SELECT {[Date].[Calendar].[Month].&[202501]:[Date].[Calendar].[Month].&[202503]} ON COLUMNS
        FROM [demo]
    )
    """
    sql, protocol = _mdx_to_sql(
        mdx,
        [{"name": "Amount", "default_agg": "sum"}],
        [{"name": "Product"}, {"name": "Month", "source_column_id": "col-month"}],
        hierarchy_meta=[{
            "name": "Calendar",
            "levels": [{
                "ordinal": 0,
                "name": "Month",
                "key_attribute": {"id": "col-month", "source": "physical_column"},
            }],
        }],
    )
    assert protocol == "jdbc"
    assert "BETWEEN '202501' AND '202503'" in sql


def test_timeline_year_range():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM (
        SELECT {[Date].[Calendar].[Year].&[2020]:[Date].[Calendar].[Year].&[2024]} ON COLUMNS
        FROM [demo]
    )
    """
    sql, protocol = _mdx_to_sql(
        mdx,
        [{"name": "Amount", "default_agg": "sum"}],
        [{"name": "Year", "source_column_id": "col-year"}],
        hierarchy_meta=[{
            "name": "Calendar",
            "levels": [{
                "ordinal": 0,
                "name": "Year",
                "key_attribute": {"id": "col-year", "source": "physical_column"},
            }],
        }],
    )
    assert protocol == "jdbc"
    assert "BETWEEN '2020' AND '2024'" in sql


def test_timeline_day_range():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM (
        SELECT {[Date].[Calendar].[Day].&[20250101]:[Date].[Calendar].[Day].&[20250115]} ON COLUMNS
        FROM [demo]
    )
    """
    sql, protocol = _mdx_to_sql(
        mdx,
        [{"name": "Amount", "default_agg": "sum"}],
        [{"name": "Day", "source_column_id": "col-day"}],
        hierarchy_meta=[{
            "name": "Calendar",
            "levels": [{
                "ordinal": 0,
                "name": "Day",
                "key_attribute": {"id": "col-day", "source": "physical_column"},
            }],
        }],
    )
    assert protocol == "jdbc"
    assert "BETWEEN '20250101' AND '20250115'" in sql


def test_timeline_combined_with_regular_slicer():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Product].[Product].Members} ON ROWS
    FROM (
        SELECT {[Date].[Calendar].[Month].&[202501]:[Date].[Calendar].[Month].&[202503]} ON COLUMNS
        FROM [demo]
    )
    WHERE ([Region].[Region].[Europe])
    """
    sql, protocol = _mdx_to_sql(
        mdx,
        [{"name": "Amount", "default_agg": "sum"}],
        [
            {"name": "Product"},
            {"name": "Region"},
            {"name": "Month", "source_column_id": "col-month"},
        ],
        hierarchy_meta=[{
            "name": "Calendar",
            "levels": [{
                "ordinal": 0,
                "name": "Month",
                "key_attribute": {"id": "col-month", "source": "physical_column"},
            }],
        }],
    )
    assert protocol == "jdbc"
    assert "BETWEEN '202501' AND '202503'" in sql
    assert """"Region" = 'Europe'""" in sql


def test_timeline_combined_with_where_report_filter():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM (
        SELECT {[Date].[Calendar].[Month].&[202506]:[Date].[Calendar].[Month].&[202512]} ON COLUMNS
        FROM [demo]
    )
    WHERE ([Product].[Product].[Bikes])
    """
    sql, protocol = _mdx_to_sql(
        mdx,
        [{"name": "Amount", "default_agg": "sum"}],
        [
            {"name": "Product"},
            {"name": "Month", "source_column_id": "col-month"},
        ],
        hierarchy_meta=[{
            "name": "Calendar",
            "levels": [{
                "ordinal": 0,
                "name": "Month",
                "key_attribute": {"id": "col-month", "source": "physical_column"},
            }],
        }],
    )
    assert protocol == "jdbc"
    assert "BETWEEN '202506' AND '202512'" in sql
    assert """"Product" = 'Bikes'""" in sql


def test_timeline_single_member_range():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM (
        SELECT {[Date].[Calendar].[Month].&[202503]:[Date].[Calendar].[Month].&[202503]} ON COLUMNS
        FROM [demo]
    )
    """
    sql, protocol = _mdx_to_sql(
        mdx,
        [{"name": "Amount", "default_agg": "sum"}],
        [{"name": "Month", "source_column_id": "col-month"}],
        hierarchy_meta=[{
            "name": "Calendar",
            "levels": [{
                "ordinal": 0,
                "name": "Month",
                "key_attribute": {"id": "col-month", "source": "physical_column"},
            }],
        }],
    )
    assert protocol == "jdbc"
    assert "BETWEEN '202503' AND '202503'" in sql


# ---------------------------------------------------------------------------
# C.3 — MDSCHEMA metadata for Timelines
# ---------------------------------------------------------------------------

def test_time_level_type_year():
    assert _time_level_type("Year") == "20"


def test_time_level_type_quarter():
    assert _time_level_type("Quarter") == "68"


def test_time_level_type_month():
    assert _time_level_type("Month") == "132"


def test_time_level_type_day():
    assert _time_level_type("Day") == "1028"


def test_time_level_type_regular():
    assert _time_level_type("Region") == "0"


def test_mdschema_levels_time_dimension():
    dims = [{"name": "DateDim", "is_time_dim": True}]
    member_data = {"DateDim": {"members": [
        {"name": "2024", "level": 0},
        {"name": "Q1", "level": 1},
    ]}}
    rows = _rows_levels("demo", dims, member_data)
    data_level = [r for r in rows if r.get("LEVEL_NAME") == "DateDim"]
    assert len(data_level) == 1
    assert data_level[0]["LEVEL_TYPE"] == "0"


def test_mdschema_hierarchies_time_dimension_type():
    dims = [{"name": "DateDim", "is_time_dim": True}]
    rows = _rows_hierarchies("demo", dims)
    dim_rows = [r for r in rows if r.get("HIERARCHY_NAME") == "DateDim"]
    assert len(dim_rows) == 1
    assert dim_rows[0]["DIMENSION_TYPE"] == "1"


def test_mdschema_hierarchies_regular_dimension_type():
    dims = [{"name": "Region", "is_time_dim": False}]
    rows = _rows_hierarchies("demo", dims)
    dim_rows = [r for r in rows if r.get("HIERARCHY_NAME") == "Region"]
    assert len(dim_rows) == 1
    assert dim_rows[0]["DIMENSION_TYPE"] == "3"
