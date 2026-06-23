"""Tests for MDX calculated member evaluation (Show Values As)."""
import pytest

from src.dax.ts_mdx_parser import parse_mdx
from src.dax.mdx_calc_members import (
    CalcMember,
    ReQuerySpec,
    parse_calc_members,
    evaluate_calc_members,
    plan_aggregate_requeried,
    build_requery_sql,
)


def _make_rows(*tuples_):
    """Build row dicts from (dim_val, measure_val) pairs."""
    rows = []
    for dim_val, amount in tuples_:
        rows.append({"Region": dim_val, "Amount": amount})
    return rows


# ---------------------------------------------------------------------------
# B.2 — Percentage calculations
# ---------------------------------------------------------------------------

def test_pct_grand_total_with_sum():
    mdx = '''WITH
MEMBER [Measures].[Pct of Total] AS
  [Measures].[Amount] / ([Measures].[Amount], [Geography].[Geography].[(All)])
SELECT
  {[Measures].[Amount], [Measures].[Pct of Total]} ON COLUMNS,
  [Geography].[Geography].Members ON ROWS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert len(calcs) == 1
    assert calcs[0].name == "Pct of Total"
    assert calcs[0].calc_type == "pct_grand_total"

    rows = _make_rows(("France", 300), ("Germany", 500), ("UK", 200))
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])

    assert rows[0]["Pct of Total"] == pytest.approx(0.3)
    assert rows[1]["Pct of Total"] == pytest.approx(0.5)
    assert rows[2]["Pct of Total"] == pytest.approx(0.2)


def test_pct_grand_total_with_avg():
    calcs = [CalcMember(
        name="Pct of Total",
        expression="[Measures].[AvgPrice] / ([Measures].[AvgPrice], [Region].[(All)])",
        calc_type="pct_grand_total",
        base_measure="AvgPrice",
    )]
    rows = [
        {"Region": "A", "AvgPrice": 10},
        {"Region": "B", "AvgPrice": 30},
    ]
    evaluate_calc_members(calcs, rows, ["AvgPrice"], ["Region"])
    assert rows[0]["Pct of Total"] == pytest.approx(0.25)
    assert rows[1]["Pct of Total"] == pytest.approx(0.75)


def test_pct_parent_3_level_hierarchy():
    calcs = [CalcMember(
        name="Pct of Parent",
        expression="[Measures].[Amount] / ([Measures].[Amount], [Geography].[Geography].Parent)",
        calc_type="pct_parent",
        base_measure="Amount",
    )]
    rows = [
        {"Continent": "Europe", "Country": "France", "Amount": 300},
        {"Continent": "Europe", "Country": "Germany", "Amount": 200},
        {"Continent": "Asia", "Country": "Japan", "Amount": 500},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Continent", "Country"])

    assert rows[0]["Pct of Parent"] == pytest.approx(0.6)
    assert rows[1]["Pct of Parent"] == pytest.approx(0.4)
    assert rows[2]["Pct of Parent"] == pytest.approx(1.0)


def test_division_by_zero_returns_none():
    calcs = [CalcMember(
        name="Pct of Total",
        expression="[Measures].[Amount] / ([Measures].[Amount], [Region].[(All)])",
        calc_type="pct_grand_total",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 0},
        {"Region": "B", "Amount": 0},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[0]["Pct of Total"] is None
    assert rows[1]["Pct of Total"] is None


# ---------------------------------------------------------------------------
# B.3 — Difference and running calculations
# ---------------------------------------------------------------------------

def test_difference_from_member():
    mdx = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Amount] - ([Measures].[Amount], [Time].[Time].[2023])
SELECT
  {[Measures].[Diff]} ON COLUMNS,
  [Time].[Time].Members ON ROWS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert calcs[0].calc_type == "difference"

    rows = [
        {"Time": "2023", "Amount": 100},
        {"Time": "2024", "Amount": 150},
        {"Time": "2025", "Amount": 120},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Time"])

    assert rows[0]["Diff"] == pytest.approx(0.0)
    assert rows[1]["Diff"] == pytest.approx(50.0)
    assert rows[2]["Diff"] == pytest.approx(20.0)


def test_running_total():
    calcs = [CalcMember(
        name="Running",
        expression="Sum(Head([Measures].[Amount]))",
        calc_type="running_total",
        base_measure="Amount",
    )]
    rows = [
        {"Month": "Jan", "Amount": 10},
        {"Month": "Feb", "Amount": 20},
        {"Month": "Mar", "Amount": 30},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Month"])
    assert rows[0]["Running"] == pytest.approx(10.0)
    assert rows[1]["Running"] == pytest.approx(30.0)
    assert rows[2]["Running"] == pytest.approx(60.0)


def test_rank_with_ties():
    calcs = [CalcMember(
        name="MyRank",
        expression="Rank(current, [Measures].[Amount])",
        calc_type="rank_desc",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 300},
        {"Region": "B", "Amount": 100},
        {"Region": "C", "Amount": 500},
        {"Region": "D", "Amount": 100},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])

    # F-002-14: competition ranking — equal values share the SAME rank
    # (Excel semantics): 500 -> 1, 300 -> 2, 100 (twice) -> 3 and 3.
    assert rows[2]["MyRank"] == 1   # Amount 500
    assert rows[0]["MyRank"] == 2   # Amount 300
    assert rows[1]["MyRank"] == 3   # Amount 100 (tie)
    assert rows[3]["MyRank"] == 3   # Amount 100 (tie)


def test_rank_smallest_to_largest():
    calcs = [CalcMember(
        name="MyRank",
        expression="Rank(current, [Measures].[Amount], ASC)",
        calc_type="rank_asc",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 300},
        {"Region": "B", "Amount": 100},
        {"Region": "C", "Amount": 500},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[1]["MyRank"] == 1
    assert rows[0]["MyRank"] == 2
    assert rows[2]["MyRank"] == 3


def test_running_total_shuffled_rows():
    """Running total must sort by target dim, not depend on DB row order."""
    calcs = [CalcMember(
        name="Running",
        expression="Sum(Head([Month].[Month].CurrentMember.Level.Members, ...))",
        calc_type="running_total",
        base_measure="Amount",
    )]
    rows = [
        {"Month": "2025-03", "Amount": 30},
        {"Month": "2025-01", "Amount": 10},
        {"Month": "2025-02", "Amount": 20},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Month"])
    vals = {r["Month"]: r["Running"] for r in rows}
    assert vals["2025-01"] == pytest.approx(10.0)
    assert vals["2025-02"] == pytest.approx(30.0)
    assert vals["2025-03"] == pytest.approx(60.0)


def test_running_total_month_names():
    """Running total with abbreviated month names must sort chronologically."""
    calcs = [CalcMember(
        name="Running",
        expression="Sum(Head([Month].[Month].CurrentMember.Level.Members, ...))",
        calc_type="running_total",
        base_measure="Amount",
    )]
    rows = [
        {"Month": "Mar", "Amount": 30},
        {"Month": "Jan", "Amount": 10},
        {"Month": "Feb", "Amount": 20},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Month"])
    vals = {r["Month"]: r["Running"] for r in rows}
    assert vals["Jan"] == pytest.approx(10.0)
    assert vals["Feb"] == pytest.approx(30.0)
    assert vals["Mar"] == pytest.approx(60.0)


def test_running_total_quarter_labels():
    """Running total with Q1/Q2/Q10 labels must sort numerically."""
    calcs = [CalcMember(
        name="Running",
        expression="Sum(Head([Quarter].[Quarter].CurrentMember.Level.Members, ...))",
        calc_type="running_total",
        base_measure="Amount",
    )]
    rows = [
        {"Quarter": "Q3", "Amount": 30},
        {"Quarter": "Q1", "Amount": 10},
        {"Quarter": "Q2", "Amount": 20},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Quarter"])
    vals = {r["Quarter"]: r["Running"] for r in rows}
    assert vals["Q1"] == pytest.approx(10.0)
    assert vals["Q2"] == pytest.approx(30.0)
    assert vals["Q3"] == pytest.approx(60.0)


def test_rank_partitioned_by_non_target_dims():
    """Rank must restart per partition (e.g., per Year), not be global."""
    calcs = [CalcMember(
        name="MyRank",
        expression="Rank([Product].[Product].CurrentMember, [Set], [Measures].[Amount])",
        calc_type="rank_desc",
        base_measure="Amount",
    )]
    rows = [
        {"Year": "2024", "Product": "A", "Amount": 100},
        {"Year": "2024", "Product": "B", "Amount": 300},
        {"Year": "2025", "Product": "A", "Amount": 500},
        {"Year": "2025", "Product": "B", "Amount": 200},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Year", "Product"])
    assert rows[0]["MyRank"] == 2  # 2024 A=100 is rank 2 within 2024
    assert rows[1]["MyRank"] == 1  # 2024 B=300 is rank 1 within 2024
    assert rows[2]["MyRank"] == 1  # 2025 A=500 is rank 1 within 2025
    assert rows[3]["MyRank"] == 2  # 2025 B=200 is rank 2 within 2025


# ---------------------------------------------------------------------------
# B.3b — Index (Show Values As > Index)
# ---------------------------------------------------------------------------

def test_index_classification():
    mdx = '''WITH
MEMBER [Measures].[Idx] AS
  [Measures].[Amount] / Average(Descendants([Geography].[Geography].[(All)], [Geography].[Geography].[Country]), [Measures].[Amount]) * 100
SELECT
  {[Measures].[Idx]} ON COLUMNS,
  [Geography].[Geography].Members ON ROWS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert len(calcs) == 1
    assert calcs[0].calc_type == "index"


def test_index_evaluation():
    calcs = [CalcMember(
        name="Idx",
        expression="[Measures].[Amount] / Average(Descendants([Geo].[(All)], [Geo].[Country]), [Measures].[Amount]) * 100",
        calc_type="index",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 100},
        {"Region": "B", "Amount": 200},
        {"Region": "C", "Amount": 300},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[0]["Idx"] == pytest.approx(50.0)
    assert rows[1]["Idx"] == pytest.approx(100.0)
    assert rows[2]["Idx"] == pytest.approx(150.0)


def test_index_with_nulls():
    calcs = [CalcMember(
        name="Idx",
        expression="[Measures].[Amount] / Average(Descendants([Geo].[(All)]), [Measures].[Amount]) * 100",
        calc_type="index",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 100},
        {"Region": "B", "Amount": None},
        {"Region": "C", "Amount": 300},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[0]["Idx"] == pytest.approx(50.0)
    assert rows[1]["Idx"] is None
    assert rows[2]["Idx"] == pytest.approx(150.0)


def test_index_all_zeros():
    calcs = [CalcMember(
        name="Idx",
        expression="[Measures].[Amount] / Average(Descendants([Geo].[(All)]), [Measures].[Amount]) * 100",
        calc_type="index",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 0},
        {"Region": "B", "Amount": 0},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[0]["Idx"] is None
    assert rows[1]["Idx"] is None


# ---------------------------------------------------------------------------
# B.4 — Integration and edge cases
# ---------------------------------------------------------------------------

def test_multiple_calc_members_in_same_pivot():
    calcs = [
        CalcMember(
            name="Pct",
            expression="[Measures].[Amount] / ([Measures].[Amount], [R].[(All)])",
            calc_type="pct_grand_total",
            base_measure="Amount",
        ),
        CalcMember(
            name="Running",
            expression="Sum(Head([Measures].[Amount]))",
            calc_type="running_total",
            base_measure="Amount",
        ),
    ]
    rows = [
        {"Region": "A", "Amount": 40},
        {"Region": "B", "Amount": 60},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[0]["Pct"] == pytest.approx(0.4)
    assert rows[1]["Pct"] == pytest.approx(0.6)
    assert rows[0]["Running"] == pytest.approx(40.0)
    assert rows[1]["Running"] == pytest.approx(100.0)


def test_ratio_expression():
    mdx = '''WITH
MEMBER [Measures].[Margin] AS
  [Measures].[Profit] / [Measures].[Revenue]
SELECT
  {[Measures].[Margin]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert calcs[0].calc_type == "arithmetic"

    rows = [
        {"Region": "A", "Revenue": 1000, "Profit": 200},
        {"Region": "B", "Revenue": 500, "Profit": 150},
    ]
    evaluate_calc_members(calcs, rows, ["Revenue", "Profit"], ["Region"])
    assert rows[0]["Margin"] == pytest.approx(0.2)
    assert rows[1]["Margin"] == pytest.approx(0.3)


def test_ratio_division_by_zero():
    calcs = [CalcMember(
        name="Margin",
        expression="[Measures].[Profit] / [Measures].[Revenue]",
        calc_type="ratio",
        base_measure="Profit",
    )]
    rows = [{"Region": "A", "Revenue": 0, "Profit": 100}]
    evaluate_calc_members(calcs, rows, ["Revenue", "Profit"], ["Region"])
    assert rows[0]["Margin"] is None


def test_with_member_format_string_preserved():
    mdx = '''WITH
MEMBER [Measures].[Pct] AS
  [Measures].[Amount] / ([Measures].[Amount], [R].[(All)]),
  FORMAT_STRING = "Percent"
SELECT {[Measures].[Pct]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert calcs[0].format_string == "Percent"


def test_pct_parent_currentmember_parent_extracts_ref():
    """Bug-569 regression: CurrentMember.Parent MDX must be parsed by _extract_parent_ref."""
    from src.dax.mdx_calc_members import _extract_parent_ref
    expr = "[Measures].[Amount] / ([Measures].[Amount], [Date].[Calendar].CurrentMember.Parent)"
    assert _extract_parent_ref(expr) == (["Date", "Calendar"], [])
    expr_short = "[Measures].[Amount] / ([Measures].[Amount], [Date].[Calendar].Parent)"
    assert _extract_parent_ref(expr_short) == (["Date", "Calendar"], [])


def test_extract_difference_ref_composite_key_path():
    """Bug-3619 (F-P4b1-01): a composite-key reference (`&[k0]&[k1]`) must
    extract the deepest key as the member value and tag the ancestor key with
    its DEPTH above the named level — NOT the named level itself. The uname
    carries no ancestor level name, so depth is resolved positionally against
    the result columns at scope time."""
    from src.dax.mdx_calc_members import _extract_difference_ref
    expr = (
        "[Measures].[Amount] - ([Measures].[Amount], "
        "[geo].[geo].[City].&[Germany]&[Berlin])"
    )
    parts, ancestors = _extract_difference_ref(expr)
    # member value is the deepest key (Berlin), matched on the City level.
    assert parts == ["City", "geo", "Berlin"]
    # Germany is the immediate parent of the named level (depth 1), NOT tagged
    # with the named level "City" (the previous self-referential bug).
    assert ancestors == [(1, "Germany")]


def test_extract_difference_ref_single_key_unchanged():
    """Bug-3619: a single-key reference extracts the key, no ancestor."""
    from src.dax.mdx_calc_members import _extract_difference_ref
    expr = (
        "[Measures].[Amount] - ([Measures].[Amount], "
        "[geo].[geo].[City].&[Berlin])"
    )
    parts, ancestors = _extract_difference_ref(expr)
    assert parts == ["City", "geo", "Berlin"]
    assert ancestors == []


def test_extract_difference_ref_caption_form_unchanged():
    """Bug-3619: the legacy caption form keeps its existing behaviour."""
    from src.dax.mdx_calc_members import _extract_difference_ref
    expr = "[Measures].[Amount] - ([Measures].[Amount], [Time].[Time].[2023])"
    parts, ancestors = _extract_difference_ref(expr)
    assert parts == ["Time", "Time", "2023"]
    assert ancestors == []


def test_difference_composite_key_ancestor_scoped():
    """Bug-3619 (F-P4b1-01): with a composite-key reference, the reference cell
    is scoped by the ancestor key, and the difference is computed per peer
    outside the reference scope.

    This test drives the REAL parser end-to-end — ``ref_member_parts`` and
    ``ref_ancestor_filters`` come from ``_extract_difference_ref``, NOT a
    hand-fed corrected ancestor column. The previous version bypassed the
    parser by feeding ``[("Country","Germany")]`` (a value the parser never
    produced), masking the broken ancestor-level mapping."""
    from src.dax.mdx_calc_members import _extract_difference_ref
    expr = (
        "[Measures].[Amount] - ([Measures].[Amount], "
        "[geo].[geo].[City].&[Germany]&[Berlin])"
    )
    parts, ancestors = _extract_difference_ref(expr)
    calc = CalcMember(
        name="Diff",
        expression=expr,
        calc_type="difference",
        base_measure="Amount",
        ref_member_parts=parts,
        ref_ancestor_filters=ancestors,
    )
    rows = [
        {"Country": "Germany", "City": "Berlin", "Amount": 100},
        {"Country": "Germany", "City": "Munich", "Amount": 160},
        # A Berlin in another country must NOT be picked as the reference.
        {"Country": "France", "City": "Berlin", "Amount": 999},
    ]
    evaluate_calc_members([calc], rows, ["Amount"], ["Country", "City"])
    # Reference = Germany/Berlin = 100. Peer key excludes Country (ancestor) and
    # City (named level), so every row pairs with the single reference 100.
    assert rows[0]["Diff"] == pytest.approx(0.0)     # 100 - 100
    assert rows[1]["Diff"] == pytest.approx(60.0)    # 160 - 100
    # France/Berlin must not have matched its OWN Berlin (899 not 0) — the
    # Germany ancestor constraint scopes the reference to Germany/Berlin=100.
    assert rows[2]["Diff"] == pytest.approx(899.0)   # 999 - 100


def test_pct_parent_resolves_named_dim_not_last():
    """Bug-569 regression: when ref_member_parts match a dim_col that is NOT last, use it."""
    calcs = [CalcMember(
        name="Pct Parent",
        expression="[Measures].[Amount] / ([Measures].[Amount], [Country].[Country].CurrentMember.Parent)",
        calc_type="pct_parent",
        base_measure="Amount",
        ref_member_parts=["Country", "Country"],
    )]
    rows = [
        {"Country": "France", "Year": "2024", "Amount": 100},
        {"Country": "Germany", "Year": "2024", "Amount": 200},
        {"Country": "France", "Year": "2025", "Amount": 150},
        {"Country": "Germany", "Year": "2025", "Amount": 250},
    ]
    # Country is dim_cols[0], Year is dim_cols[1] — without fix, dim_cols[-1]=Year is used as child
    evaluate_calc_members(calcs, rows, ["Amount"], ["Country", "Year"])
    # child_dim=Country => parent_dims=[Year] => group by Year
    # Year 2024: France 100/(100+200)=0.333, Germany 200/300=0.667
    # Year 2025: France 150/(150+250)=0.375, Germany 250/400=0.625
    assert rows[0]["Pct Parent"] == pytest.approx(100 / 300)
    assert rows[1]["Pct Parent"] == pytest.approx(200 / 300)
    assert rows[2]["Pct Parent"] == pytest.approx(150 / 400)
    assert rows[3]["Pct Parent"] == pytest.approx(250 / 400)


def test_aggregate_set_count_distinct_returns_none():
    """Bug-563 regression: count_distinct is not composable from pre-aggregated rows."""
    calcs = [CalcMember(
        name="Europe",
        expression="Aggregate({[Country].[France], [Country].[Germany]})",
        calc_type="aggregate_set",
        base_measure="",
        dim_name="Country",
        aggregate_members=["France", "Germany"],
    )]
    rows = [
        {"Country": "France", "Customers": 5},
        {"Country": "Germany", "Customers": 7},
        {"Country": "Japan", "Customers": 3},
    ]
    meta = [{"name": "Customers", "default_agg": "count_distinct"}]
    evaluate_calc_members(calcs, rows, ["Customers"], ["Country"], measures_meta=meta)
    synth = [r for r in rows if r.get("Country") == "Europe"]
    assert len(synth) == 1
    assert synth[0]["Customers"] is None


def test_aggregate_set_avg_returns_none():
    """Bug-563 regression: avg is not composable from pre-aggregated rows."""
    calcs = [CalcMember(
        name="Europe",
        expression="Aggregate({[Country].[France], [Country].[Germany]})",
        calc_type="aggregate_set",
        base_measure="",
        dim_name="Country",
        aggregate_members=["France", "Germany"],
    )]
    rows = [
        {"Country": "France", "AvgSale": 100},
        {"Country": "Germany", "AvgSale": 200},
    ]
    meta = [{"name": "AvgSale", "default_agg": "avg"}]
    evaluate_calc_members(calcs, rows, ["AvgSale"], ["Country"], measures_meta=meta)
    synth = [r for r in rows if r.get("Country") == "Europe"]
    assert len(synth) == 1
    assert synth[0]["AvgSale"] is None


# ---------------------------------------------------------------------------
# Bug-575 — Custom group re-query for non-composable aggregations
# ---------------------------------------------------------------------------


def _make_europe_calc():
    return CalcMember(
        name="Europe",
        expression="Aggregate({[Country].[France], [Country].[Germany]})",
        calc_type="aggregate_set",
        base_measure="",
        dim_name="Country",
        aggregate_members=["France", "Germany"],
    )


class TestAggregateSetPartitioning:
    """F-002-09 — custom grouping must aggregate per other-dimension partition,
    emitting one synthetic group row per partition (not one row that folds
    every partition's facts together and mislabels them)."""

    def _ew_calc(self):
        return CalcMember(
            name="EW",
            expression="Aggregate({[Region].[East], [Region].[West]})",
            calc_type="aggregate_set",
            base_measure="",
            dim_name="Region",
            aggregate_members=["East", "West"],
        )

    def test_group_partitioned_by_second_dimension(self):
        calcs = [self._ew_calc()]
        rows = [
            {"Region": "East", "Year": "2023", "Sales": 100},
            {"Region": "East", "Year": "2024", "Sales": 200},
            {"Region": "West", "Year": "2023", "Sales": 50},
        ]
        meta = [{"name": "Sales", "default_agg": "sum"}]
        evaluate_calc_members(calcs, rows, ["Sales"], ["Region", "Year"],
                              measures_meta=meta)
        groups = [r for r in rows if r["Region"] == "EW"]
        by_year = {g["Year"]: g["Sales"] for g in groups}
        # 2023: East 100 + West 50 = 150 ; 2024: East 200 (no West) = 200
        assert by_year == {"2023": 150, "2024": 200}

    def test_single_dimension_unchanged(self):
        calcs = [self._ew_calc()]
        rows = [
            {"Region": "East", "Sales": 100},
            {"Region": "West", "Sales": 50},
        ]
        meta = [{"name": "Sales", "default_agg": "sum"}]
        evaluate_calc_members(calcs, rows, ["Sales"], ["Region"],
                              measures_meta=meta)
        groups = [r for r in rows if r["Region"] == "EW"]
        assert len(groups) == 1
        assert groups[0]["Sales"] == 150

    def test_planner_emits_one_requery_per_partition(self):
        calc = self._ew_calc()
        rows = [
            {"Region": "East", "Year": "2023", "Customers": 3},
            {"Region": "East", "Year": "2024", "Customers": 4},
            {"Region": "West", "Year": "2023", "Customers": 2},
        ]
        meta = [{"name": "Customers", "default_agg": "count_distinct"}]
        specs = plan_aggregate_requeried(
            [calc], meta, "demo", ["Region", "Year"], rows,
        )
        # Two distinct Year partitions => two specs, each pinned to its year.
        part_keys = {s.partition_key for s in specs}
        assert part_keys == {("2023",), ("2024",)}
        sql_2023 = build_requery_sql(next(s for s in specs if s.partition_key == ("2023",)))
        assert '"Year" = \'2023\'' in sql_2023
        assert "COUNT(DISTINCT" in sql_2023


class TestReQueryCountDistinct:

    def test_requery_returns_correct_deduplicated_count(self):
        """Pre-aggregated counts are 5+7=12, but true distinct is 10 (overlap)."""
        calcs = [_make_europe_calc()]
        rows = [
            {"Country": "France", "Customers": 5},
            {"Country": "Germany", "Customers": 7},
        ]
        meta = [{"name": "Customers", "default_agg": "count_distinct"}]
        requery_results = {("Europe", "Customers", ()): 10}
        evaluate_calc_members(calcs, rows, ["Customers"], ["Country"],
                              measures_meta=meta, requery_results=requery_results)
        synth = next(r for r in rows if r["Country"] == "Europe")
        assert synth["Customers"] == 10

    def test_requery_without_results_still_returns_none(self):
        """When no requery_results provided, count_distinct still returns None."""
        calcs = [_make_europe_calc()]
        rows = [
            {"Country": "France", "Customers": 5},
            {"Country": "Germany", "Customers": 7},
        ]
        meta = [{"name": "Customers", "default_agg": "count_distinct"}]
        evaluate_calc_members(calcs, rows, ["Customers"], ["Country"],
                              measures_meta=meta, requery_results=None)
        synth = next(r for r in rows if r["Country"] == "Europe")
        assert synth["Customers"] is None


class TestReQueryAvg:

    def test_requery_returns_correct_weighted_average(self):
        """Pre-aggregated avgs are 100,200 (unweighted avg=150), but true weighted avg is 133."""
        calcs = [_make_europe_calc()]
        rows = [
            {"Country": "France", "AvgSale": 100},
            {"Country": "Germany", "AvgSale": 200},
        ]
        meta = [{"name": "AvgSale", "default_agg": "avg"}]
        requery_results = {("Europe", "AvgSale", ()): 133.33}
        evaluate_calc_members(calcs, rows, ["AvgSale"], ["Country"],
                              measures_meta=meta, requery_results=requery_results)
        synth = next(r for r in rows if r["Country"] == "Europe")
        assert synth["AvgSale"] == 133.33


class TestReQueryMixedMeasures:

    def test_sum_composable_count_distinct_requeried(self):
        """SUM uses pre-aggregated rows; count_distinct uses re-query result."""
        calcs = [_make_europe_calc()]
        rows = [
            {"Country": "France", "Revenue": 1000, "Customers": 5},
            {"Country": "Germany", "Revenue": 2000, "Customers": 7},
        ]
        meta = [
            {"name": "Revenue", "default_agg": "sum"},
            {"name": "Customers", "default_agg": "count_distinct"},
        ]
        requery_results = {("Europe", "Customers", ()): 10}
        evaluate_calc_members(calcs, rows, ["Revenue", "Customers"], ["Country"],
                              measures_meta=meta, requery_results=requery_results)
        synth = next(r for r in rows if r["Country"] == "Europe")
        assert synth["Revenue"] == 3000
        assert synth["Customers"] == 10

    def test_mixed_sum_avg_count_distinct(self):
        """SUM composable, AVG and COUNT_DISTINCT both re-queried."""
        calcs = [_make_europe_calc()]
        rows = [
            {"Country": "France", "Revenue": 1000, "AvgSale": 100, "Customers": 5},
            {"Country": "Germany", "Revenue": 2000, "AvgSale": 200, "Customers": 7},
        ]
        meta = [
            {"name": "Revenue", "default_agg": "sum"},
            {"name": "AvgSale", "default_agg": "avg"},
            {"name": "Customers", "default_agg": "count_distinct"},
        ]
        requery_results = {
            ("Europe", "AvgSale", ()): 140.0,
            ("Europe", "Customers", ()): 10,
        }
        evaluate_calc_members(
            calcs, rows, ["Revenue", "AvgSale", "Customers"], ["Country"],
            measures_meta=meta, requery_results=requery_results,
        )
        synth = next(r for r in rows if r["Country"] == "Europe")
        assert synth["Revenue"] == 3000
        assert synth["AvgSale"] == 140.0
        assert synth["Customers"] == 10


class TestReQueryNoRegression:

    def test_sum_only_no_requery_needed(self):
        """SUM-only measures do not trigger re-query planning."""
        calcs = [_make_europe_calc()]
        meta = [{"name": "Revenue", "default_agg": "sum"}]
        rows = [
            {"Country": "France", "Revenue": 1000},
            {"Country": "Germany", "Revenue": 2000},
        ]
        specs = plan_aggregate_requeried(calcs, meta, "my_model", ["Country"], rows)
        assert specs == []

    def test_max_min_no_requery_needed(self):
        """MAX/MIN are composable and don't trigger re-query."""
        calcs = [_make_europe_calc()]
        meta = [
            {"name": "MaxSale", "default_agg": "max"},
            {"name": "MinSale", "default_agg": "min"},
        ]
        rows = [
            {"Country": "France", "MaxSale": 500, "MinSale": 10},
            {"Country": "Germany", "MaxSale": 800, "MinSale": 5},
        ]
        specs = plan_aggregate_requeried(calcs, meta, "m", ["Country"], rows)
        assert specs == []

    def test_sum_measures_unchanged_with_requery_results(self):
        """Re-query results for other measures don't interfere with SUM aggregation."""
        calcs = [_make_europe_calc()]
        rows = [
            {"Country": "France", "Revenue": 1000, "Customers": 5},
            {"Country": "Germany", "Revenue": 2000, "Customers": 7},
        ]
        meta = [
            {"name": "Revenue", "default_agg": "sum"},
            {"name": "Customers", "default_agg": "count_distinct"},
        ]
        requery_results = {("Europe", "Customers", ()): 10}
        evaluate_calc_members(calcs, rows, ["Revenue", "Customers"], ["Country"],
                              measures_meta=meta, requery_results=requery_results)
        synth = next(r for r in rows if r["Country"] == "Europe")
        assert synth["Revenue"] == 3000


class TestPlanAggregateRequeried:

    def test_plans_count_distinct_requery(self):
        calcs = [_make_europe_calc()]
        meta = [{"name": "Customers", "default_agg": "count_distinct"}]
        rows = [{"Country": "France", "Customers": 5}]
        specs = plan_aggregate_requeried(calcs, meta, "my_model", ["Country"], rows)
        assert len(specs) == 1
        assert specs[0].calc_name == "Europe"
        assert specs[0].measure_name == "Customers"
        assert specs[0].agg == "count_distinct"
        assert specs[0].dim_col == "Country"
        assert specs[0].members == ["France", "Germany"]

    def test_plans_avg_requery(self):
        calcs = [_make_europe_calc()]
        meta = [{"name": "AvgSale", "default_agg": "avg"}]
        rows = [{"Country": "France", "AvgSale": 100}]
        specs = plan_aggregate_requeried(calcs, meta, "my_model", ["Country"], rows)
        assert len(specs) == 1
        assert specs[0].agg == "avg"

    def test_plans_multiple_measures(self):
        calcs = [_make_europe_calc()]
        meta = [
            {"name": "Revenue", "default_agg": "sum"},
            {"name": "Customers", "default_agg": "count_distinct"},
            {"name": "AvgSale", "default_agg": "avg"},
        ]
        rows = [{"Country": "France", "Revenue": 1000, "Customers": 5, "AvgSale": 100}]
        specs = plan_aggregate_requeried(calcs, meta, "m", ["Country"], rows)
        assert len(specs) == 2
        agg_types = {s.agg for s in specs}
        assert agg_types == {"count_distinct", "avg"}

    def test_no_calcs_returns_empty(self):
        specs = plan_aggregate_requeried([], [{"name": "Customers", "default_agg": "count_distinct"}], "m", ["Country"], [])
        assert specs == []

    def test_no_meta_returns_empty(self):
        calcs = [_make_europe_calc()]
        specs = plan_aggregate_requeried(calcs, None, "m", ["Country"], [])
        assert specs == []

    def test_queried_measures_filters_to_subset(self):
        calcs = [_make_europe_calc()]
        meta = [
            {"name": "Revenue", "default_agg": "sum"},
            {"name": "Customers", "default_agg": "count_distinct"},
            {"name": "AvgSale", "default_agg": "avg"},
        ]
        rows = [{"Country": "France", "Revenue": 1000, "Customers": 5, "AvgSale": 100}]
        specs = plan_aggregate_requeried(
            calcs, meta, "m", ["Country"], rows,
            queried_measures={"Customers"},
        )
        assert len(specs) == 1
        assert specs[0].measure_name == "Customers"

    def test_queried_measures_none_returns_all_non_composable(self):
        calcs = [_make_europe_calc()]
        meta = [
            {"name": "Customers", "default_agg": "count_distinct"},
            {"name": "AvgSale", "default_agg": "avg"},
        ]
        rows = [{"Country": "France", "Customers": 5, "AvgSale": 100}]
        specs = plan_aggregate_requeried(calcs, meta, "m", ["Country"], rows, queried_measures=None)
        assert len(specs) == 2

    def test_queried_measures_empty_set_returns_empty(self):
        calcs = [_make_europe_calc()]
        meta = [{"name": "Customers", "default_agg": "count_distinct"}]
        rows = [{"Country": "France", "Customers": 5}]
        specs = plan_aggregate_requeried(
            calcs, meta, "m", ["Country"], rows,
            queried_measures=set(),
        )
        assert specs == []


class TestBuildRequerySql:

    def test_count_distinct_sql(self):
        spec = ReQuerySpec(
            calc_name="Europe", measure_name="Customers", agg="count_distinct",
            dim_col="Country", members=["France", "Germany"], model_slug="my_model",
        )
        sql = build_requery_sql(spec)
        assert 'COUNT(DISTINCT "Customers")' in sql
        assert '"my_model"' in sql
        assert "'France'" in sql
        assert "'Germany'" in sql

    def test_avg_sql(self):
        spec = ReQuerySpec(
            calc_name="Europe", measure_name="AvgSale", agg="avg",
            dim_col="Country", members=["France", "Germany"], model_slug="my_model",
        )
        sql = build_requery_sql(spec)
        assert 'AVG("AvgSale")' in sql

    def test_member_with_single_quote_escaped(self):
        spec = ReQuerySpec(
            calc_name="Group", measure_name="Count", agg="count_distinct",
            dim_col="Name", members=["O'Brien", "Smith"], model_slug="m",
        )
        sql = build_requery_sql(spec)
        assert "O''Brien" in sql

    def test_extra_where_appended(self):
        spec = ReQuerySpec(
            calc_name="Europe", measure_name="Customers", agg="count_distinct",
            dim_col="Country", members=["France", "Germany"], model_slug="m",
            extra_where=['"year" = \'2024\''],
        )
        sql = build_requery_sql(spec)
        assert '"Country" IN' in sql
        assert '"year" = \'2024\'' in sql
        assert " AND " in sql

    def test_extra_where_multiple_clauses(self):
        spec = ReQuerySpec(
            calc_name="G", measure_name="C", agg="avg",
            dim_col="Region", members=["US"], model_slug="m",
            extra_where=['"year" = \'2024\'', '"status" = \'active\''],
        )
        sql = build_requery_sql(spec)
        assert sql.count(" AND ") == 2
