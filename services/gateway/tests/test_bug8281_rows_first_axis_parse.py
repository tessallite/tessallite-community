"""Bug-8281: ``_mdx_axis_expr(mdx, 0)`` must isolate the COLUMNS fragment even
when the MDX lists ROWS BEFORE COLUMNS (``SELECT {rows} ON ROWS, {cols} ON
COLUMNS``).

The generic ``(?:SELECT|,)...ON COLUMNS`` fallback anchors on SELECT and, for a
ROWS-first query, captures the ROWS fragment into the axis-0 expression -- so
the COLUMNS axis (and every downstream extractor: measures, dims, axis member
filters, the Bug-8272 re-query merge) sees the wrong set. The fix adds the
ROWS-first probe for axis 0, mirroring the one ``_mdx_axis_has_non_empty``
already uses.
"""
from src.dax.xmla_server import _mdx_axis_expr


def test_rows_first_axis0_isolates_columns_fragment():
    mdx = (
        "SELECT {[Product].[Product].Members} ON ROWS, "
        "{[Measures].[Sales]} ON COLUMNS FROM [m]"
    )
    col_expr = _mdx_axis_expr(mdx, 0)
    row_expr = _mdx_axis_expr(mdx, 1)

    # Axis 0 (COLUMNS) must be the measures set, NOT the ROWS product set.
    assert "[Measures].[Sales]" in col_expr
    assert "[Product]" not in col_expr, (
        f"axis-0 captured the ROWS fragment (ROWS-first misparse): {col_expr!r}"
    )
    # Axis 1 (ROWS) must be the product set.
    assert "[Product].[Product].Members" in row_expr
    assert "[Measures]" not in row_expr


def test_columns_first_axis_parse_unchanged_no_regression():
    # The normal COLUMNS-first order must be unaffected by the new probe.
    mdx = (
        "SELECT {[Measures].[Sales]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    col_expr = _mdx_axis_expr(mdx, 0)
    row_expr = _mdx_axis_expr(mdx, 1)
    assert "[Measures].[Sales]" in col_expr and "[Product]" not in col_expr
    assert "[Product].[Product].Members" in row_expr and "[Measures]" not in row_expr


def test_rows_first_axis0_with_numeric_axis_labels():
    # ON 1 / ON 0 numeric axis labels, ROWS-first.
    mdx = (
        "SELECT {[Product].[Product].Members} ON 1, "
        "{[Measures].[Sales]} ON 0 FROM [m]"
    )
    col_expr = _mdx_axis_expr(mdx, 0)
    assert "[Measures].[Sales]" in col_expr
    assert "[Product]" not in col_expr


def test_rows_first_axis0_member_filters_isolated():
    # End-to-end consequence: an enumerated COLUMNS set must be extractable for
    # a ROWS-first query, so axis member filters (and the Bug-8272 denominator
    # merge) key off the correct axis.
    from src.dax.xmla_server import _mdx_extract_axis_member_filters

    mdx = (
        "SELECT {[Product].[Product].Members} ON ROWS, "
        "{[Region].[Region].&[EU], [Region].[Region].&[US]} ON COLUMNS FROM [m]"
    )
    col_expr = _mdx_axis_expr(mdx, 0)
    filters = _mdx_extract_axis_member_filters(col_expr, {"Region", "Product"})
    assert filters == {"Region": ["EU", "US"]}, (
        f"ROWS-first COLUMNS member set not isolated: col_expr={col_expr!r} "
        f"filters={filters!r}"
    )
