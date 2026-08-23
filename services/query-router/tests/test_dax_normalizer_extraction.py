"""
H15 / F-003-03 — DAX normalizer dimension/filter/measure extraction.

The router-side DAX normalizer used to silently drop grain (column names with
spaces / quoted tables), filters (any operator other than ``=``), and bind
measures to the alias string instead of the column. It also degraded raw MDX
to source passthrough (an opaque 502). These tests pin the behavioural
contract: faithful extraction where possible, a typed ``UnsupportedSQL`` (mapped
to 422 feature_not_supported) where not — never a silent drop.

Run from tessallite/services/query-router/:
    pytest tests/test_dax_normalizer_extraction.py
"""
from __future__ import annotations

import pytest

from src.parsing.dax_normalizer import parse_dax_to_ir
from src.ir.logical_query import UnsupportedSQL


# ---------------------------------------------------------------------------
# Dimension extraction — bare, spaced, and quoted-table forms
# ---------------------------------------------------------------------------

def test_summarizecolumns_plain_dimension_and_measure():
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", [Revenue])', "m1"
    )
    assert q.requested_dimensions == ["Region"]
    assert q.requested_measures == ["Revenue"]


def test_summarizecolumns_spaced_column_name_not_dropped():
    # F-003-03: ``Sales[Order Date]`` used to fail the dimension regex and the
    # grain was silently dropped (grand total instead of per-date breakdown).
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Order Date], "Revenue", [Revenue])', "m1"
    )
    assert q.requested_dimensions == ["Order Date"]
    assert q.requested_measures == ["Revenue"]
    assert q.grain == ["Order Date"]


def test_summarizecolumns_quoted_table_with_spaced_column():
    q = parse_dax_to_ir(
        "EVALUATE SUMMARIZECOLUMNS('Sales Table'[Order Date], \"Rev\", [Revenue])",
        "m1",
    )
    assert q.requested_dimensions == ["Order Date"]
    assert q.requested_measures == ["Revenue"]


# ---------------------------------------------------------------------------
# Filter extraction — operator matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "op_token, operator, rhs, expected_value",
    [
        ("=", "eq", '"WEB"', "WEB"),
        ("<>", "neq", '"WEB"', "WEB"),
        ("!=", "neq", '"WEB"', "WEB"),
        (">=", "gte", "100", 100),
        ("<=", "lte", "100", 100),
        (">", "gt", "100", 100),
        ("<", "lt", "100", 100),
        ("=", "eq", "12.5", 12.5),
    ],
)
def test_filter_operator_matrix(op_token, operator, rhs, expected_value):
    # F-003-03: only ``=`` was handled before; every other comparison silently
    # produced no filter (unfiltered data — potential over-exposure).
    dax = (
        f'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
        f'FILTER(Sales, Sales[Amount] {op_token} {rhs}), "Rev", [Revenue])'
    )
    q = parse_dax_to_ir(dax, "m1")
    assert len(q.filters) == 1
    f = q.filters[0]
    assert (f.dimension_name, f.operator, f.value) == ("Amount", operator, expected_value)


def test_filter_with_spaced_column():
    q = parse_dax_to_ir(
        "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
        "FILTER(Sales, 'Sales Table'[Order Amount] >= 100), \"Rev\", [Revenue])",
        "m1",
    )
    assert len(q.filters) == 1
    assert q.filters[0].dimension_name == "Order Amount"
    assert q.filters[0].operator == "gte"


# ---------------------------------------------------------------------------
# Bug-6086 — query_fingerprint uses the shared fingerprint_shape authority
# ---------------------------------------------------------------------------

def test_dax_fingerprint_uses_shared_shape_authority():
    # Bug-6086: the DAX normalizer previously hashed a PRIVATE payload
    # ({measures, grain, filter_cols}) that omitted the ``dimensions`` and
    # ``having_cols`` keys the shared ``fingerprint_shape`` authority (and the
    # SQL parser) include. That made an equivalent DAX and SQL query hash
    # DIFFERENTLY, so cross-protocol QueryLog dedup / miss-log grouping split.
    # The normalizer must now delegate to the shared authority.
    from shared.pocket.fingerprint import fingerprint_shape

    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", [Revenue])', "m1"
    )
    expected = fingerprint_shape(
        measures=["Revenue"],
        dimensions=["Region"],
        grain=["Region"],
        filter_cols=[],
        having_cols=[],
    )
    assert q.query_fingerprint == expected


def test_dax_fingerprint_matches_sql_parser_for_equivalent_shape():
    # Bug-6086: a DAX query and the SQL parser's fingerprint for the SAME
    # measures/dimensions/grain/filters must be byte-identical — both go
    # through the single shared authority. Compare the two parser fingerprint
    # helpers directly so the schemes can never drift again.
    from src.parsing import sql_parser
    from src.ir.logical_query import LogicalFilter

    filt = LogicalFilter(dimension_name="Channel", operator="eq", value="WEB")
    dax_fp = __import__(
        "src.parsing.dax_normalizer", fromlist=["_compute_fingerprint"]
    )._compute_fingerprint(["Revenue"], ["Region"], ["Region"], [filt])
    sql_fp = sql_parser._compute_fingerprint(
        ["Revenue"], ["Region"], ["Region"], [filt], having_columns=[],
    )
    assert dax_fp == sql_fp


def test_dax_fingerprint_includes_filter_column():
    # A filtered DAX query hashes differently from the same shape without the
    # filter (filter_cols participate in the shared shape payload).
    unfiltered = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", [Revenue])', "m1"
    )
    filtered = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
        'FILTER(Sales, Sales[Channel] = "WEB"), "Revenue", [Revenue])',
        "m1",
    )
    assert unfiltered.query_fingerprint != filtered.query_fingerprint


# ---------------------------------------------------------------------------
# Measure expression binding (SUMMARIZE) — bind to column, not alias
# ---------------------------------------------------------------------------

def test_summarize_binds_measure_column_not_alias():
    # F-003-03: ``"Total Sales", SUM(Sales[Amount])`` used to register the
    # alias string 'Total Sales' as the measure name (binds only by accident).
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZE(Sales, Sales[Region], "Total Sales", SUM(Sales[Amount]))',
        "m1",
    )
    assert q.requested_dimensions == ["Region"]
    assert q.requested_measures == ["Amount"]


def test_summarize_binds_column_inside_nested_expression():
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZE(Sales, Sales[Region], "Net", '
        "CALCULATE(SUM('Sales Table'[Net Amount])))",
        "m1",
    )
    assert q.requested_measures == ["Net Amount"]


def test_summarizecolumns_calculate_measure_ref_preserves_filter_context():
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Web Revenue", '
        'CALCULATE([Revenue], FILTER(Sales, Sales[Channel] = "WEB")))',
        "m1",
    )

    assert q.requested_dimensions == ["Region"]
    assert q.requested_measures == ["Revenue"]
    assert len(q.filters) == 1
    assert (q.filters[0].dimension_name, q.filters[0].operator, q.filters[0].value) == (
        "Channel",
        "eq",
        "WEB",
    )


def test_summarizecolumns_calculate_unrepresentable_filter_raises():
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", '
            'CALCULATE([Revenue], Sales[Channel] = "WEB"))',
            "m1",
        )


# ---------------------------------------------------------------------------
# Fail-loud — never silently drop or route raw MDX to source
# ---------------------------------------------------------------------------

def test_raw_mdx_raises_unsupported_not_passthrough():
    # F-003-04: the gateway translator fallback ships raw MDX. Routing it to
    # the source DB produces a 502 — reject loudly instead.
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir("SELECT [Measures].[Sales] ON 0 FROM [Cube]", "m1")


def test_unparseable_filter_raises_unsupported():
    # A FILTER whose condition is not a representable comparison must not be
    # silently dropped — reject the whole query.
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(
            "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
            "FILTER(Sales, Sales[Amount] + Sales[Tax] > 100), \"Rev\", [Revenue])",
            "m1",
        )


def test_unrecognised_top_level_argument_raises_unsupported():
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], TOPN(5, Sales), "Rev", [Revenue])',
            "m1",
        )


def test_alias_without_measure_ref_raises_unsupported():
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Orphan Alias")',
            "m1",
        )


def test_time_variant_hints_still_applied_on_valid_dax():
    # No-regression: hint propagation (the only previously-tested path) still
    # works for a well-formed SUMMARIZECOLUMNS.
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Calendar[Year], "Revenue", [Revenue])',
        "m1",
        parsed_dax={"time_variant_hints": {"Revenue": "ytd"}},
    )
    assert q.time_variant_hints == {"Revenue": "ytd"}
    assert q.requested_measures == ["Revenue"]


# ---------------------------------------------------------------------------
# Bug-7595: same-column repeated expressions must be rejected
# ---------------------------------------------------------------------------

def test_same_column_repeated_in_expression_raises():
    """Bug-7595: SUM(Sales[Amount]) + SUM(Sales[Amount]) references the same
    column twice.  The expression semantics (2*SUM) would be silently lost if
    we bind to a single measure.  Must raise UnsupportedSQL."""
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
            '"Double", SUM(Sales[Amount]) + SUM(Sales[Amount]))',
            "m1",
        )


def test_same_column_single_agg_passes():
    """A single SUM(Sales[Amount]) with no repetition should still bind
    successfully (regression guard for Bug-7595 fix).
    Bug-7796: also carries the 'sum' override (lowercased key) so the
    source rewriter applies the requested function."""
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
        '"Total", SUM(Sales[Amount]))',
        "m1",
    )
    assert q.requested_measures == ["Amount"]
    assert q.measure_agg_overrides == {"amount": "sum"}
