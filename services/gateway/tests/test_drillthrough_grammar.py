"""Grammar and parser tests for MDX DRILLTHROUGH statement.

Verifies that the tree-sitter MDX grammar correctly parses DRILLTHROUGH
statements and that ``ts_mdx_parser.parse_mdx`` extracts the structured
fields: ``is_drillthrough``, ``maxrows``, ``return_columns``.

Non-regression: regular SELECT statements must continue to work and
must NOT set ``is_drillthrough``.
"""
from __future__ import annotations

import pytest

from src.dax.ts_mdx_parser import parse_mdx


# ---------------------------------------------------------------------------
# Non-regression — regular SELECT
# ---------------------------------------------------------------------------

def test_regular_select_not_drillthrough():
    r = parse_mdx("SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]")
    assert r.is_drillthrough is False
    assert r.maxrows is None
    assert r.return_columns == []
    assert r.cube_name == "modely"
    assert len(r.axes) == 1


def test_regular_select_with_where():
    r = parse_mdx(
        "SELECT {[Measures].[Sales]} ON COLUMNS "
        "FROM [Sales] WHERE ([Date].[Year].&[2025])"
    )
    assert r.is_drillthrough is False
    assert len(r.where_members) > 0


def test_regular_select_with_clause():
    r = parse_mdx(
        "WITH MEMBER [Measures].[CalcAmount] AS [Measures].[amount] * 1.1 "
        "SELECT {[Measures].[CalcAmount]} ON COLUMNS FROM [modely]"
    )
    assert r.is_drillthrough is False
    assert len(r.with_members) == 1


# ---------------------------------------------------------------------------
# Bare DRILLTHROUGH (no MAXROWS, no RETURN)
# ---------------------------------------------------------------------------

def test_bare_drillthrough():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    assert r.is_drillthrough is True
    assert r.maxrows is None
    assert r.return_columns == []
    assert r.cube_name == "modely"
    assert len(r.axes) == 1
    assert not r.warnings


def test_bare_drillthrough_two_axes():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS, "
        "{[Date].[Year].&[2025]} ON ROWS FROM [modely]"
    )
    assert r.is_drillthrough is True
    assert len(r.axes) == 2
    assert r.axes[0].axis_name == "COLUMNS"
    assert r.axes[1].axis_name == "ROWS"


def test_drillthrough_case_insensitive():
    r = parse_mdx(
        "drillthrough select {[Measures].[amount]} on columns from [modely]"
    )
    assert r.is_drillthrough is True
    assert r.cube_name == "modely"


# ---------------------------------------------------------------------------
# MAXROWS clause
# ---------------------------------------------------------------------------

def test_drillthrough_maxrows():
    r = parse_mdx(
        "DRILLTHROUGH MAXROWS 100 SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    assert r.is_drillthrough is True
    assert r.maxrows == 100


def test_drillthrough_maxrows_large():
    r = parse_mdx(
        "DRILLTHROUGH MAXROWS 10000 SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales]"
    )
    assert r.maxrows == 10000


def test_drillthrough_maxrows_one():
    r = parse_mdx(
        "DRILLTHROUGH MAXROWS 1 SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales]"
    )
    assert r.maxrows == 1


def test_drillthrough_maxrows_case_insensitive():
    r = parse_mdx(
        "drillthrough maxrows 50 select {[Measures].[amount]} on columns from [modely]"
    )
    assert r.maxrows == 50


# ---------------------------------------------------------------------------
# RETURN clause
# ---------------------------------------------------------------------------

def test_drillthrough_return_single_column():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] RETURN [$Date].[business_date]"
    )
    assert r.is_drillthrough is True
    assert len(r.return_columns) == 1
    assert r.return_columns[0] == ["$Date", "business_date"]


def test_drillthrough_return_multiple_columns():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] RETURN [$Date].[business_date], [$Transactions].[customer_name]"
    )
    assert len(r.return_columns) == 2
    assert r.return_columns[0] == ["$Date", "business_date"]
    assert r.return_columns[1] == ["$Transactions", "customer_name"]


def test_drillthrough_return_three_part_ref():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON COLUMNS "
        "FROM [Sales] RETURN [$Date].[Calendar].[Month]"
    )
    assert len(r.return_columns) == 1
    assert r.return_columns[0] == ["$Date", "Calendar", "Month"]


# ---------------------------------------------------------------------------
# MAXROWS + RETURN together
# ---------------------------------------------------------------------------

def test_drillthrough_maxrows_and_return():
    r = parse_mdx(
        "DRILLTHROUGH MAXROWS 50 "
        "SELECT {[Measures].[amount]} ON COLUMNS, "
        "{[Date].[Year].&[2025]} ON ROWS "
        "FROM [modely] "
        "RETURN [$Date].[business_date], [$Transactions].[customer_name]"
    )
    assert r.is_drillthrough is True
    assert r.maxrows == 50
    assert len(r.return_columns) == 2
    assert r.cube_name == "modely"
    assert len(r.axes) == 2


# ---------------------------------------------------------------------------
# DRILLTHROUGH with WITH clause in inner SELECT
# ---------------------------------------------------------------------------

def test_drillthrough_with_clause():
    r = parse_mdx(
        "DRILLTHROUGH "
        "WITH MEMBER [Measures].[CalcAmount] AS [Measures].[amount] * 1.1 "
        "SELECT {[Measures].[CalcAmount]} ON COLUMNS "
        "FROM [modely]"
    )
    assert r.is_drillthrough is True
    assert len(r.with_members) == 1
    assert r.with_members[0].name == "[Measures].[CalcAmount]"


# ---------------------------------------------------------------------------
# DRILLTHROUGH with WHERE clause in inner SELECT
# ---------------------------------------------------------------------------

def test_drillthrough_where_clause():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON COLUMNS "
        "FROM [Sales] WHERE ([Date].[Year].&[2025])"
    )
    assert r.is_drillthrough is True
    assert len(r.where_members) > 0


def test_drillthrough_where_multiple_filters():
    r = parse_mdx(
        "DRILLTHROUGH MAXROWS 200 "
        "SELECT {[Measures].[Sales]} ON COLUMNS "
        "FROM [Sales] WHERE ([Date].[Year].&[2025], [Region].[Country].&[US])"
    )
    assert r.is_drillthrough is True
    assert r.maxrows == 200
    assert len(r.where_members) == 2


# ---------------------------------------------------------------------------
# DRILLTHROUGH with subselect in inner SELECT
# ---------------------------------------------------------------------------

def test_drillthrough_subselect():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON COLUMNS "
        "FROM (SELECT {[Date].[Year].&[2025]} ON COLUMNS FROM [Sales])"
    )
    assert r.is_drillthrough is True
    assert r.subselect is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_drillthrough_no_axes():
    r = parse_mdx("DRILLTHROUGH SELECT FROM [modely]")
    assert r.is_drillthrough is True
    assert r.cube_name == "modely"
    assert len(r.axes) == 0


def test_drillthrough_non_empty_axis():
    r = parse_mdx(
        "DRILLTHROUGH SELECT NON EMPTY {[Measures].[Sales]} ON COLUMNS "
        "FROM [Sales]"
    )
    assert r.is_drillthrough is True
    assert r.axes[0].non_empty is True


def test_drillthrough_dimension_properties():
    r = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[Sales]} "
        "DIMENSION PROPERTIES [Date].[Calendar] "
        "ON COLUMNS FROM [Sales]"
    )
    assert r.is_drillthrough is True
    assert len(r.axes) == 1
    assert len(r.axes[0].dim_properties) > 0
