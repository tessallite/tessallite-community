"""Bug-6698: real Excel MDX ``DIMENSION PROPERTIES`` tokens must be treated as
axis metadata (properties to RETURN), never as member selections.

Excel (real MSOLAP) decorates every axis set with a comma-separated property
list, e.g.::

    DIMENSION PROPERTIES MEMBER_KEY, MEMBER_VALUE, MEMBER_NAME,
        MEMBER_UNIQUE_NAME, MEMBER_CAPTION, LEVEL_UNIQUE_NAME, LEVEL_NUMBER

and frequently LEVEL-QUALIFIED bracketed refs such as
``[Dim].[Hier].[Level].[MEMBER_KEY]`` which are lexically identical to a member
selection ``[Dim].[Hier].[Level].[Member]``.

Before the fix, the axis cleaner stripped only the FIRST property (up to the
first comma), so the remaining property tokens survived on the axis expression.
The bracketed level-qualified refs then parsed as caption members, producing::

    SELECT "account_type", SUM("base_amount") FROM "modely"
    WHERE "account_type" IN ('MEMBER_KEY', 'MEMBER_VALUE', 'MEMBER_NAME', ...)

which returns 0 rows (empty Excel pivot), or on a date-typed column fails loud
with ``invalid input syntax for type date: "MEMBER_KEY"``.

These tests replay Excel-shaped pivot MDX (NON EMPTY set + DIMENSION PROPERTIES
on both axes + CELL PROPERTIES trailer) and assert the property tokens NEVER
reach the translated SQL as member filters.
"""
from __future__ import annotations

from src.dax.xmla_server import _mdx_to_sql

MEASURES = [{"name": "base_amount", "default_agg": "sum"}]
DIMENSIONS = [{"name": "account_type"}]

# The intrinsic member/level property names Excel emits; none may ever appear as
# a member value in the translated SQL.
_PROPERTY_TOKENS = (
    "MEMBER_KEY",
    "MEMBER_VALUE",
    "MEMBER_NAME",
    "MEMBER_UNIQUE_NAME",
    "MEMBER_CAPTION",
    "LEVEL_UNIQUE_NAME",
    "LEVEL_NUMBER",
    "PARENT_UNIQUE_NAME",
    "HIERARCHY_UNIQUE_NAME",
)


def _assert_no_property_tokens(sql: str) -> None:
    for token in _PROPERTY_TOKENS:
        assert f"'{token}'" not in sql, (
            f"property token {token!r} leaked into SQL as a member value: {sql}"
        )
        # No WHERE filter should reference a property token at all.
        assert token not in sql, (
            f"property token {token!r} leaked into translated SQL: {sql}"
        )


def test_excel_pivot_bare_property_list_on_rows_is_not_a_member_filter():
    """Typical Excel flat-attribute pivot: bare property list on ROWS."""
    mdx = (
        "SELECT NON EMPTY {[Measures].[base_amount]} "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, MEMBER_KEY, MEMBER_VALUE, "
        "MEMBER_NAME, MEMBER_UNIQUE_NAME, MEMBER_CAPTION, LEVEL_UNIQUE_NAME, "
        "LEVEL_NUMBER ON COLUMNS, "
        "NON EMPTY {[account_type].[account_type].[account_type].Members} "
        "DIMENSION PROPERTIES MEMBER_KEY, MEMBER_VALUE, MEMBER_NAME, "
        "MEMBER_UNIQUE_NAME, MEMBER_CAPTION, LEVEL_UNIQUE_NAME, LEVEL_NUMBER "
        "ON ROWS "
        "FROM [modely] "
        "CELL PROPERTIES VALUE, FORMAT_STRING, LANGUAGE, BACK_COLOR, "
        "FORE_COLOR, FONT_FLAGS"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    assert 'SUM("base_amount")' in sql
    assert 'GROUP BY "account_type"' in sql
    # A bare .Members expansion must NOT produce a WHERE filter at all.
    assert "WHERE" not in sql.upper()
    _assert_no_property_tokens(sql)


def test_excel_pivot_level_qualified_property_refs_on_rows_are_not_members():
    """Excel level-qualified bracketed property refs — the exact live-defect
    shape from acme-demo query_logs 2026-07-07 21:50/21:55/21:58 UTC.

    ``[account_type].[account_type].[account_type].[MEMBER_KEY]`` is lexically a
    member ref; it must be recognised as an axis property, not a selection.
    """
    mdx = (
        "SELECT NON EMPTY {[Measures].[base_amount]} ON COLUMNS, "
        "NON EMPTY "
        "Hierarchize({[account_type].[account_type].[account_type].Members}) "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME, "
        "[account_type].[account_type].[account_type].[MEMBER_KEY], "
        "[account_type].[account_type].[account_type].[MEMBER_VALUE], "
        "[account_type].[account_type].[account_type].[MEMBER_NAME], "
        "[account_type].[account_type].[account_type].[MEMBER_UNIQUE_NAME], "
        "[account_type].[account_type].[account_type].[MEMBER_CAPTION] "
        "ON ROWS "
        "FROM [modely] "
        "CELL PROPERTIES VALUE, FORMAT_STRING, LANGUAGE"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    assert 'SUM("base_amount")' in sql
    assert 'GROUP BY "account_type"' in sql
    assert "WHERE" not in sql.upper()
    _assert_no_property_tokens(sql)


def test_subselect_dimension_properties_not_parsed_as_slicer_members():
    """Codex R2 finding 1: an Excel SLICER/SUBSELECT axis set can carry a
    DIMENSION PROPERTIES clause too. `_mdx_extract_subselect_filters` parses the
    subselect fragment BEFORE the main-axis cleaner runs, so the property clause
    must be stripped there as well — otherwise the level-qualified property refs
    poison the slicer filter with property tokens while the REAL slicer member
    must still apply."""
    mdx = (
        "SELECT NON EMPTY {[Measures].[base_amount]} ON COLUMNS, "
        "NON EMPTY {[account_type].[account_type].[account_type].Members} ON ROWS "
        "FROM (SELECT {[account_type].[account_type].[CHECKING]} "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, "
        "[account_type].[account_type].[account_type].[MEMBER_KEY], "
        "[account_type].[account_type].[account_type].[MEMBER_NAME] "
        "ON COLUMNS FROM [modely]) "
        "CELL PROPERTIES VALUE, FORMAT_STRING"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    assert 'GROUP BY "account_type"' in sql
    # The genuine slicer member still filters (a single member renders as
    # equality; property-token leakage would widen it to an IN list)...
    assert 'WHERE "account_type" = \'CHECKING\'' in sql
    # ...and no property token leaked in beside it.
    _assert_no_property_tokens(sql)


def test_dimension_properties_does_not_shadow_a_real_enumerated_member_filter():
    """Regression guard: stripping the property clause must NOT remove a real
    enumerated member set on the same axis — the genuine filter still applies."""
    mdx = (
        "SELECT NON EMPTY {[Measures].[base_amount]} ON COLUMNS, "
        "NON EMPTY {[account_type].[account_type].[CHECKING], "
        "[account_type].[account_type].[SAVINGS]} "
        "DIMENSION PROPERTIES MEMBER_KEY, MEMBER_CAPTION, LEVEL_NUMBER "
        "ON ROWS "
        "FROM [modely] "
        "CELL PROPERTIES VALUE, FORMAT_STRING"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    assert 'GROUP BY "account_type"' in sql
    assert 'WHERE "account_type" IN (' in sql
    assert "'CHECKING'" in sql
    assert "'SAVINGS'" in sql
    _assert_no_property_tokens(sql)
