"""Tests for MDX calculated member evaluation (Show Values As)."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from shared.connector_qualify import quote_identifier
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


def test_pct_grand_total_with_avg_reaggregated():
    """F-002-04: for a NON-ADDITIVE measure (avg), the grand-total denominator
    must be the measure re-aggregated over the fact grain — NOT the sum of the
    already-averaged leaf cells (sum-of-averages is mathematically wrong).

    Displayed leaf averages 10 and 30 would give a WRONG sum-of-avgs total of 40
    (0.25 / 0.75). The true fact-grain AVG here is 20, so the business-correct
    percentages are 10/20 = 0.5 and 30/20 = 1.5.
    """
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
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    # True re-aggregated grand-total AVG over the whole fact grain (from a router
    # re-query), NOT sum-of-cells.
    denom = {("Pct of Total", "__grand__"): 20.0}
    evaluate_calc_members(
        calcs, rows, ["AvgPrice"], ["Region"],
        measures_meta=measures_meta, denom_requery_results=denom,
    )
    assert rows[0]["Pct of Total"] == pytest.approx(0.5)   # 10 / 20 (NOT 0.25)
    assert rows[1]["Pct of Total"] == pytest.approx(1.5)   # 30 / 20 (NOT 0.75)


def test_pct_grand_total_additive_still_sums_cells():
    """An additive (sum) measure keeps the sum-of-cells denominator and needs no
    re-query — the re-aggregation path must not disturb the correct additive case.
    """
    calcs = [CalcMember(
        name="Pct of Total",
        expression="[Measures].[Amount] / ([Measures].[Amount], [Region].[(All)])",
        calc_type="pct_grand_total",
        base_measure="Amount",
    )]
    rows = [
        {"Region": "A", "Amount": 300},
        {"Region": "B", "Amount": 500},
        {"Region": "C", "Amount": 200},
    ]
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    evaluate_calc_members(
        calcs, rows, ["Amount"], ["Region"], measures_meta=measures_meta,
    )
    assert rows[0]["Pct of Total"] == pytest.approx(0.3)
    assert rows[1]["Pct of Total"] == pytest.approx(0.5)
    assert rows[2]["Pct of Total"] == pytest.approx(0.2)


def test_pct_parent_with_avg_reaggregated():
    """F-002-04: % of Parent over a non-additive measure re-aggregates each
    parent's total at the parent grain, keyed by the parent-dimension tuple."""
    calcs = [CalcMember(
        name="Pct of Parent",
        expression="[Measures].[AvgPrice] / ([Measures].[AvgPrice], [Geography].[Geography].Parent)",
        calc_type="pct_parent",
        base_measure="AvgPrice",
    )]
    rows = [
        {"Continent": "Europe", "Country": "France", "AvgPrice": 10},
        {"Continent": "Europe", "Country": "Germany", "AvgPrice": 30},
        {"Continent": "Asia", "Country": "Japan", "AvgPrice": 50},
    ]
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    # Re-aggregated parent (Continent) AVGs, keyed by the parent-dim value tuple.
    denom = {
        ("Pct of Parent", ("Europe",)): 25.0,
        ("Pct of Parent", ("Asia",)): 50.0,
    }
    evaluate_calc_members(
        calcs, rows, ["AvgPrice"], ["Continent", "Country"],
        measures_meta=measures_meta, denom_requery_results=denom,
    )
    assert rows[0]["Pct of Parent"] == pytest.approx(10 / 25.0)   # 0.4 (NOT 10/40)
    assert rows[1]["Pct of Parent"] == pytest.approx(30 / 25.0)   # 1.2
    assert rows[2]["Pct of Parent"] == pytest.approx(1.0)         # 50 / 50


def test_plan_denominator_requeried_grand_and_parent():
    """F-002-04: the planner emits denominator re-query specs only for
    non-additive % of Grand Total / Parent, never for additive measures."""
    from src.dax.mdx_calc_members import plan_denominator_requeried

    gt = CalcMember(
        name="PctGT", expression="x", calc_type="pct_grand_total",
        base_measure="AvgPrice",
    )
    parent = CalcMember(
        name="PctP",
        expression="[Measures].[AvgPrice] / ([Measures].[AvgPrice], [Geo].[Geo].Parent)",
        calc_type="pct_parent", base_measure="AvgPrice",
    )
    additive = CalcMember(
        name="PctSum", expression="x", calc_type="pct_grand_total",
        base_measure="Amount",
    )
    measures_meta = [
        {"name": "AvgPrice", "default_agg": "avg"},
        {"name": "Amount", "default_agg": "sum"},
    ]
    rows = [
        {"Continent": "Europe", "Country": "France", "AvgPrice": 10, "Amount": 1},
        {"Continent": "Asia", "Country": "Japan", "AvgPrice": 50, "Amount": 2},
    ]
    specs = plan_denominator_requeried(
        [gt, parent, additive], measures_meta, "demo",
        ["Continent", "Country"], rows,
    )
    # Grand total: one spec. Parent: one per distinct parent (Europe, Asia).
    # Additive: none.
    gt_specs = [s for s in specs if s.calc_name == "PctGT"]
    parent_specs = [s for s in specs if s.calc_name == "PctP"]
    sum_specs = [s for s in specs if s.calc_name == "PctSum"]
    assert len(gt_specs) == 1
    assert gt_specs[0].partition_key == "__grand__"
    assert gt_specs[0].agg == "avg"
    assert {s.partition_key for s in parent_specs} == {("Europe",), ("Asia",)}
    assert sum_specs == []


def test_plan_denominator_requeried_axis_total_bug8206():
    """Bug-8206: the planner emits one denominator re-query per pinned tuple for
    a non-additive % of Row/Column Total; additive axis totals need none."""
    from src.dax.mdx_calc_members import (
        plan_denominator_requeried, build_denominator_requery_sql,
    )

    col_total = CalcMember(
        name="PctCol", expression="x", calc_type="pct_col_total",
        base_measure="AvgPrice", axis_total_axis=1,
    )
    row_total_additive = CalcMember(
        name="PctRowSum", expression="x", calc_type="pct_row_total",
        base_measure="Amount", axis_total_axis=0,
    )
    measures_meta = [
        {"name": "AvgPrice", "default_agg": "avg"},
        {"name": "Amount", "default_agg": "sum"},
    ]
    rows = [
        {"RowDim": "A", "ColDim": "X", "AvgPrice": 10, "Amount": 1},
        {"RowDim": "B", "ColDim": "X", "AvgPrice": 30, "Amount": 2},
        {"RowDim": "A", "ColDim": "Y", "AvgPrice": 50, "Amount": 3},
    ]
    specs = plan_denominator_requeried(
        [col_total, row_total_additive], measures_meta, "demo",
        ["RowDim", "ColDim"], rows,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    col_specs = [s for s in specs if s.calc_name == "PctCol"]
    sum_specs = [s for s in specs if s.calc_name == "PctRowSum"]
    # Column total pins the COLUMN axis: one spec per distinct ColDim (X, Y).
    assert {s.partition_key for s in col_specs} == {("X",), ("Y",)}
    assert all(s.agg == "avg" for s in col_specs)
    # The SQL pins the column dim, not the row dim.
    sql = build_denominator_requery_sql(
        next(s for s in col_specs if s.partition_key == ("X",))
    )
    assert '"ColDim" = ' in sql
    assert '"RowDim"' not in sql
    assert 'AVG(' in sql
    # Additive row total: no re-query.
    assert sum_specs == []


def test_plan_denominator_requeried_null_parent_normalized():
    """R1 finding 2: a NULL/empty parent member must key the denominator spec by
    the same '(blank)' member the evaluator sees (rows are normalised to
    '(blank)' before evaluation), else the producer key never matches and the
    non-additive % of Parent silently falls back to the wrong sum-of-cells."""
    from src.dax.mdx_calc_members import (
        plan_denominator_requeried, build_denominator_requery_sql, BLANK_MEMBER,
    )

    parent = CalcMember(
        name="PctP",
        expression="[Measures].[AvgPrice] / ([Measures].[AvgPrice], [Geo].[Geo].Parent)",
        calc_type="pct_parent", base_measure="AvgPrice",
    )
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    rows = [
        {"Continent": None, "Country": "Antarctica", "AvgPrice": 10},  # NULL parent
        {"Continent": "", "Country": "Nowhere", "AvgPrice": 20},        # empty parent
        {"Continent": "Asia", "Country": "Japan", "AvgPrice": 50},
    ]
    specs = plan_denominator_requeried(
        [parent], measures_meta, "demo", ["Continent", "Country"], rows,
    )
    keys = {s.partition_key for s in specs}
    # NULL and "" both normalise to the blank member; Asia is itself.
    assert (BLANK_MEMBER,) in keys
    assert ("Asia",) in keys

    # The SQL for the blank partition pins IS NULL / '' — NOT col = '(blank)'.
    blank_spec = next(s for s in specs if s.partition_key == (BLANK_MEMBER,))
    sql = build_denominator_requery_sql(blank_spec)
    assert "IS NULL" in sql
    assert "'(blank)'" not in sql


def test_plan_denominator_requeried_no_dim_parent_uses_all_key():
    """R1 finding 3: a pct_parent with no dim columns degenerates to a grand
    total; the planner must key it under ('__all__',) — the exact key the
    evaluator's no-dim branch reads — not '__grand__' (which would be discarded)."""
    from src.dax.mdx_calc_members import plan_denominator_requeried

    parent = CalcMember(
        name="PctP", expression="x", calc_type="pct_parent",
        base_measure="AvgPrice",
    )
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    rows = [{"AvgPrice": 10}]
    specs = plan_denominator_requeried([parent], measures_meta, "demo", [], rows)
    assert len(specs) == 1
    assert specs[0].partition_key == ("__all__",)

    # And the evaluator consumes it: value used as the grand denominator.
    calcs = [CalcMember(
        name="PctP", expression="x", calc_type="pct_parent",
        base_measure="AvgPrice",
    )]
    denom = {("PctP", ("__all__",)): 40.0}
    ev_rows = [{"AvgPrice": 10}]
    evaluate_calc_members(
        calcs, ev_rows, ["AvgPrice"], [],
        measures_meta=measures_meta, denom_requery_results=denom,
    )
    assert ev_rows[0]["PctP"] == pytest.approx(0.25)  # 10 / 40 (re-aggregated)


def test_plan_denominator_requeried_min_max_nonadditive_bug7857():
    """Bug-7857: MIN and MAX measures must also trigger a denominator re-query
    for % of Grand Total / % of Parent. The grand total of a MAX measure is
    MAX over the wider grain, not the sum of cells.

    Hand-computed expected for MAX measure 'Peak' with values [10, 50]:
      Grand total denominator = MAX(10, 50) = 50
      Cell 1 % of Grand Total = 10 / 50 = 0.20
      Cell 2 % of Grand Total = 50 / 50 = 1.00
    With the old code (sum-of-cells), denominator = 60, giving 10/60 = 0.167
    and 50/60 = 0.833 -- WRONG.
    """
    from src.dax.mdx_calc_members import (
        plan_denominator_requeried, build_denominator_requery_sql,
    )

    gt_max = CalcMember(
        name="PctGT_Max", expression="x", calc_type="pct_grand_total",
        base_measure="Peak",
    )
    gt_min = CalcMember(
        name="PctGT_Min", expression="x", calc_type="pct_grand_total",
        base_measure="Trough",
    )
    additive = CalcMember(
        name="PctGT_Sum", expression="x", calc_type="pct_grand_total",
        base_measure="Amount",
    )
    measures_meta = [
        {"name": "Peak", "default_agg": "max"},
        {"name": "Trough", "default_agg": "min"},
        {"name": "Amount", "default_agg": "sum"},
    ]
    rows = [
        {"Region": "East", "Peak": 50, "Trough": 10, "Amount": 100},
        {"Region": "West", "Peak": 30, "Trough": 5, "Amount": 200},
    ]
    specs = plan_denominator_requeried(
        [gt_max, gt_min, additive], measures_meta, "demo",
        ["Region"], rows,
    )
    # MAX and MIN measures must generate re-query specs; SUM must not.
    max_specs = [s for s in specs if s.calc_name == "PctGT_Max"]
    min_specs = [s for s in specs if s.calc_name == "PctGT_Min"]
    sum_specs = [s for s in specs if s.calc_name == "PctGT_Sum"]
    assert len(max_specs) == 1, "MAX measure should trigger a re-query"
    assert max_specs[0].agg == "max"
    assert len(min_specs) == 1, "MIN measure should trigger a re-query"
    assert min_specs[0].agg == "min"
    assert sum_specs == [], "SUM measure should NOT trigger a re-query"

    # Verify the re-query SQL uses the correct aggregate function.
    max_sql = build_denominator_requery_sql(max_specs[0])
    assert 'MAX("Peak")' in max_sql
    assert "SUM" not in max_sql
    min_sql = build_denominator_requery_sql(min_specs[0])
    assert 'MIN("Trough")' in min_sql
    assert "SUM" not in min_sql


def test_plan_denominator_unsupported_agg_blanks_not_wrong_bug7857():
    """Bug-7857 gate: a percentile (p50) or stddev measure is non-additive
    but the re-query SQL builder cannot emit the correct aggregate. The planner
    must NOT emit a spec, so the evaluator blanks the cell (fail-closed)
    instead of computing a wrong ratio with AVG as a fallback denominator.

    Hand-computed expected: a p50 measure with cells [10, 50] has no correct
    re-queryable denominator. The cell should be blank (None), never 10/30
    (AVG fallback) or 10/60 (sum-of-cells).
    """
    from src.dax.mdx_calc_members import plan_denominator_requeried

    gt_pct = CalcMember(
        name="PctGT_P50", expression="x", calc_type="pct_grand_total",
        base_measure="MedianPrice",
    )
    gt_count = CalcMember(
        name="PctGT_Count", expression="x", calc_type="pct_grand_total",
        base_measure="OrderCount",
    )
    measures_meta = [
        {"name": "MedianPrice", "default_agg": "p50"},
        {"name": "OrderCount", "default_agg": "count"},
    ]
    rows = [
        {"Region": "East", "MedianPrice": 10, "OrderCount": 100},
        {"Region": "West", "MedianPrice": 50, "OrderCount": 200},
    ]
    specs = plan_denominator_requeried(
        [gt_pct, gt_count], measures_meta, "demo",
        ["Region"], rows,
    )
    # p50 is non-additive but unsupported for re-query -> no spec emitted
    # (evaluator will blank the cell, fail-closed).
    pct_specs = [s for s in specs if s.calc_name == "PctGT_P50"]
    assert pct_specs == [], (
        "Unsupported agg type 'p50' should NOT emit a re-query spec "
        "(evaluator blanks = fail-closed)"
    )
    # COUNT is additive -> no spec needed (sum-of-cells is correct).
    count_specs = [s for s in specs if s.calc_name == "PctGT_Count"]
    assert count_specs == [], "COUNT is additive, no re-query needed"


def test_build_denominator_requery_sql_injection_safe():
    """F-002-04 / Bug-6074: partition values render as dialect-correct literals,
    never string-concat, so a value with a quote cannot break out of the SQL."""
    from src.dax.mdx_calc_members import (
        DenomReQuerySpec, build_denominator_requery_sql,
    )

    spec = DenomReQuerySpec(
        calc_name="PctP", measure_name="AvgPrice", agg="avg",
        model_slug="demo", partition_key=("Eu'rope",),
        partition_dims=["Continent"], partition_values=["Eu'rope"],
    )
    sql = build_denominator_requery_sql(spec)
    assert "AVG(" in sql
    assert "Continent" in sql
    # The embedded single quote is escaped by the literal quoter (postgres ->
    # doubled), so the value stays inside the string literal.
    assert "Eu''rope" in sql


def test_pct_row_total_all_pinned_subset_fails_loud():
    """Adversarial R3 F1: Excel emits '% of Row Total' as a tuple pinning ONE
    hierarchy to [(All)] (e.g. [Region].[Region].[(All)]) while another dim is on
    the other axis. That is greedily matched by _is_pct_grand_total; the evaluator
    must detect the pinned set is a PROPER SUBSET of the pivot dims and fail loud
    rather than silently compute cell/sum(all cells)."""
    calc = CalcMember(
        name="PctRow",
        expression="[Measures].[Sales] / ([Measures].[Sales], [Region].[Region].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    assert calc.calc_type == "pct_grand_total"
    assert calc.grand_total_all_dims == ["Region"]

    # Two dims on the pivot (Region on rows, Product on cols) but only Region
    # pinned -> row total -> must fault.
    rows = [
        {"Region": "US", "Product": "A", "Sales": 10},
        {"Region": "US", "Product": "B", "Sales": 30},
        {"Region": "EU", "Product": "A", "Sales": 60},
    ]
    with pytest.raises(ValueError, match="Column/Row Total"):
        evaluate_calc_members([calc], rows, ["Sales"], ["Region", "Product"])


def test_true_grand_total_all_dims_pinned_still_ratios():
    """A TRUE grand total over a multi-dim pivot pins EVERY hierarchy to [(All)];
    it must still compute the grand-total ratio, not be mistaken for a row total."""
    calc = CalcMember(
        name="PctGT",
        expression="[Measures].[Sales] / ([Measures].[Sales], "
                   "[Region].[Region].[(All)], [Product].[Product].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    assert calc.calc_type == "pct_grand_total"
    assert set(calc.grand_total_all_dims) == {"Region", "Product"}

    rows = [
        {"Region": "US", "Product": "A", "Sales": 10},
        {"Region": "US", "Product": "B", "Sales": 30},
        {"Region": "EU", "Product": "A", "Sales": 60},
    ]
    evaluate_calc_members([calc], rows, ["Sales"], ["Region", "Product"])
    assert rows[0]["PctGT"] == pytest.approx(0.1)   # 10/100
    assert rows[1]["PctGT"] == pytest.approx(0.3)   # 30/100
    assert rows[2]["PctGT"] == pytest.approx(0.6)   # 60/100


def test_single_dim_grand_total_not_flagged():
    """A grand total over a single-dim pivot pins that one hierarchy; with only
    one dim on the pivot it is unambiguously a grand total (not a row total)."""
    calc = CalcMember(
        name="PctGT",
        expression="[Measures].[Sales] / ([Measures].[Sales], [Region].[Region].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    rows = [{"Region": "US", "Sales": 40}, {"Region": "EU", "Sales": 60}]
    evaluate_calc_members([calc], rows, ["Sales"], ["Region"])
    assert rows[0]["PctGT"] == pytest.approx(0.4)
    assert rows[1]["PctGT"] == pytest.approx(0.6)


def test_pct_row_total_all_MEMBER_form_pin_fails_loud():
    """Adversarial R4 F1: clients echo the ALL-MEMBER unique name [All] (not just
    the [(All)] level name). A row total pinning [Region].[Region].[All] must be
    extracted and faulted too — otherwise the silent wrong number survives."""
    calc = CalcMember(
        name="PctRow",
        expression="[Measures].[Sales] / ([Measures].[Sales], [Region].[Region].[All])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    assert calc.calc_type == "pct_grand_total"
    assert "Region" in calc.grand_total_all_dims

    rows = [
        {"Region": "US", "Product": "A", "Sales": 10},
        {"Region": "EU", "Product": "A", "Sales": 60},
    ]
    with pytest.raises(ValueError, match="Column/Row Total"):
        evaluate_calc_members([calc], rows, ["Sales"], ["Region", "Product"])


def test_true_grand_total_on_hierarchy_pivot_not_faulted():
    """Adversarial R4 F2: a TRUE grand total pins EVERY hierarchy. On a pivot with
    a defined hierarchy (levels Continent, Country) plus a flat dim Region, the
    pinned hierarchy name must expand to its level dim_cols so full coverage is
    recognised and the query is NOT false-positive-faulted."""
    calc = CalcMember(
        name="PctGT",
        expression="[Measures].[Sales] / ([Measures].[Sales], "
                   "[Geography].[Geography].[(All)], [Region].[Region].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    rows = [
        {"Continent": "EU", "Country": "FR", "Region": "West", "Sales": 40},
        {"Continent": "AS", "Country": "JP", "Region": "East", "Sales": 60},
    ]
    hld = {"Geography": ["Continent", "Country"]}
    evaluate_calc_members(
        [calc], rows, ["Sales"], ["Continent", "Country", "Region"],
        hierarchy_level_dims=hld,
    )
    assert rows[0]["PctGT"] == pytest.approx(0.4)   # 40/100 grand total
    assert rows[1]["PctGT"] == pytest.approx(0.6)


def test_row_total_on_hierarchy_pivot_faults():
    """Adversarial R4 F2: on the same hierarchy pivot, pinning only the Geography
    hierarchy (a row total) leaves Region uncovered -> must fault."""
    calc = CalcMember(
        name="PctRow",
        expression="[Measures].[Sales] / ([Measures].[Sales], "
                   "[Geography].[Geography].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    rows = [
        {"Continent": "EU", "Country": "FR", "Region": "West", "Sales": 40},
        {"Continent": "AS", "Country": "JP", "Region": "East", "Sales": 60},
    ]
    hld = {"Geography": ["Continent", "Country"]}
    with pytest.raises(ValueError, match="Column/Row Total"):
        evaluate_calc_members(
            [calc], rows, ["Sales"], ["Continent", "Country", "Region"],
            hierarchy_level_dims=hld,
        )


def test_unresolved_pin_does_not_false_fault():
    """Adversarial R4 F2 corollary: if a pinned name resolves to no dim_col and no
    known hierarchy, the guard stays silent (never false-fault a shape we cannot
    fully map) — the ratio computes as a grand total."""
    calc = CalcMember(
        name="PctGT",
        expression="[Measures].[Sales] / ([Measures].[Sales], "
                   "[Mystery].[Mystery].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    rows = [
        {"Region": "US", "Product": "A", "Sales": 40},
        {"Region": "EU", "Product": "B", "Sales": 60},
    ]
    # "Mystery" maps to nothing -> all_resolved False -> no fault, computes ratio.
    evaluate_calc_members([calc], rows, ["Sales"], ["Region", "Product"])
    assert rows[0]["PctGT"] == pytest.approx(0.4)
    assert rows[1]["PctGT"] == pytest.approx(0.6)


def test_grand_total_hierarchy_named_after_its_level_not_faulted():
    """Adversarial R5: a hierarchy named identically to one of its own level
    columns (e.g. a 'Product' hierarchy: Category -> Product) must union both the
    direct dim_col match AND the hierarchy expansion, so a TRUE grand total
    pinning [Product].[Product].[(All)] on a Category+Product pivot is NOT
    false-faulted."""
    calc = CalcMember(
        name="PctGT",
        expression="[Measures].[Sales] / ([Measures].[Sales], "
                   "[Product].[Product].[(All)])",
        base_measure="Sales",
    )
    from src.dax.mdx_calc_members import _classify_expression
    _classify_expression(calc)
    rows = [
        {"Category": "Tech", "Product": "Laptop", "Sales": 40},
        {"Category": "Tech", "Product": "Phone", "Sales": 60},
    ]
    # "Product" is both a dim_col AND the hierarchy name; the hierarchy covers
    # both Category and Product levels -> full coverage -> no fault.
    hld = {"Product": ["Category", "Product"]}
    evaluate_calc_members(
        [calc], rows, ["Sales"], ["Category", "Product"],
        hierarchy_level_dims=hld,
    )
    assert rows[0]["PctGT"] == pytest.approx(0.4)
    assert rows[1]["PctGT"] == pytest.approx(0.6)


def test_pct_grand_total_nonadditive_missing_denom_blanks_not_sum_of_cells():
    """Adversarial R3 F2: a non-additive % of Grand Total whose re-aggregated
    denominator is unavailable (re-query returned empty/NULL for the grain) must
    leave the ratio BLANK — never fall back to the wrong sum-of-averages."""
    calcs = [CalcMember(
        name="Pct",
        expression="[Measures].[AvgPrice] / ([Measures].[AvgPrice], [Region].[(All)])",
        calc_type="pct_grand_total",
        base_measure="AvgPrice",
    )]
    rows = [
        {"Region": "A", "AvgPrice": 10},
        {"Region": "B", "AvgPrice": 30},
    ]
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    # No denom_requery_results supplied (re-query empty/failed-to-empty): the
    # non-additive path must blank, NOT compute 10/40 and 30/40 (sum-of-avgs).
    evaluate_calc_members(
        calcs, rows, ["AvgPrice"], ["Region"],
        measures_meta=measures_meta, denom_requery_results=None,
    )
    assert rows[0]["Pct"] is None
    assert rows[1]["Pct"] is None


def test_pct_parent_nonadditive_missing_denom_blanks_not_sum_of_cells():
    """Adversarial R3 F2: same fail-closed-on-number guard for % of Parent — a
    parent with no re-aggregated total is blanked, not divided by sum-of-avgs."""
    calcs = [CalcMember(
        name="PctP",
        expression="[Measures].[AvgPrice] / ([Measures].[AvgPrice], [Geo].[Geo].Parent)",
        calc_type="pct_parent",
        base_measure="AvgPrice",
    )]
    rows = [
        {"Continent": "Europe", "Country": "France", "AvgPrice": 10},
        {"Continent": "Europe", "Country": "Germany", "AvgPrice": 30},
        {"Continent": "Asia", "Country": "Japan", "AvgPrice": 50},
    ]
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    # Only Europe's re-aggregated total present; Asia's is missing -> Asia blanks.
    denom = {("PctP", ("Europe",)): 25.0}
    evaluate_calc_members(
        calcs, rows, ["AvgPrice"], ["Continent", "Country"],
        measures_meta=measures_meta, denom_requery_results=denom,
    )
    assert rows[0]["PctP"] == pytest.approx(10 / 25.0)
    assert rows[1]["PctP"] == pytest.approx(30 / 25.0)
    assert rows[2]["PctP"] is None  # Asia: no re-aggregated total -> blank


def test_pct_axis_total_classified_row_and_col():
    """Bug-8206: Axis(n) forms classify to pct_row_total / pct_col_total.

    Axis(0) (columns totalled) -> a ROW total; Axis(1) (rows totalled) -> a
    COLUMN total. The .CurrentMember...Members hierarchy form classifies as a
    column total and records the named denominator dim."""
    from src.dax.mdx_calc_members import _classify_expression

    row_total = CalcMember(
        name="PctRow",
        expression="[Measures].[Amount] / (Axis(0).Item(0), [Measures].[Amount])",
        base_measure="Amount",
    )
    _classify_expression(row_total)
    assert row_total.calc_type == "pct_row_total"
    assert row_total.axis_total_axis == 0

    col_total = CalcMember(
        name="PctCol",
        expression="[Measures].[Amount] / (Axis(1).Item(0), [Measures].[Amount])",
        base_measure="Amount",
    )
    _classify_expression(col_total)
    assert col_total.calc_type == "pct_col_total"
    assert col_total.axis_total_axis == 1

    hier_total = CalcMember(
        name="PctHier",
        expression="[Measures].[Amount] / ([Measures].[Amount], "
                   "[Geography].[Geography].CurrentMember.Level.Members)",
        base_measure="Amount",
    )
    _classify_expression(hier_total)
    # The hierarchy-named form is classified as generic pct_axis_total; the
    # evaluator resolves row-total vs col-total at eval time from the axis split.
    assert hier_total.calc_type == "pct_axis_total"
    assert "Geography" in hier_total.axis_total_denom_dims


def test_pct_row_total_known_answer():
    """Bug-8206: % of Row Total holds row-axis dims fixed and totals over the
    column axis. Row A cells 30 and 70 -> 0.3 and 0.7; row B cells 20, 80 ->
    0.2, 0.8. The additive-measure path sums the displayed cells (no re-query)."""
    calc = CalcMember(
        name="PctRow",
        expression="[Measures].[Amount] / (Axis(0), [Measures].[Amount])",
        calc_type="pct_row_total",
        base_measure="Amount",
        axis_total_axis=0,
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "Amount": 30},
        {"RowDim": "A", "ColDim": "Y", "Amount": 70},
        {"RowDim": "B", "ColDim": "X", "Amount": 20},
        {"RowDim": "B", "ColDim": "Y", "Amount": 80},
    ]
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    evaluate_calc_members(
        [calc], rows, ["Amount"],
        ["RowDim", "ColDim"], measures_meta=measures_meta,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    assert rows[0]["PctRow"] == pytest.approx(0.3)
    assert rows[1]["PctRow"] == pytest.approx(0.7)
    assert rows[2]["PctRow"] == pytest.approx(0.2)
    assert rows[3]["PctRow"] == pytest.approx(0.8)


def test_pct_col_total_known_answer():
    """Bug-8206: % of Column Total holds col-axis dims fixed and totals over the
    row axis. Column X cells 30 and 20 -> 0.6 and 0.4; column Y cells 70, 80 ->
    0.4667, 0.5333."""
    calc = CalcMember(
        name="PctCol",
        expression="[Measures].[Amount] / (Axis(1), [Measures].[Amount])",
        calc_type="pct_col_total",
        base_measure="Amount",
        axis_total_axis=1,
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "Amount": 30},
        {"RowDim": "A", "ColDim": "Y", "Amount": 70},
        {"RowDim": "B", "ColDim": "X", "Amount": 20},
        {"RowDim": "B", "ColDim": "Y", "Amount": 80},
    ]
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    evaluate_calc_members(
        [calc], rows, ["Amount"], ["RowDim", "ColDim"],
        measures_meta=measures_meta,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    assert rows[0]["PctCol"] == pytest.approx(30 / 50)
    assert rows[1]["PctCol"] == pytest.approx(70 / 150)
    assert rows[2]["PctCol"] == pytest.approx(20 / 50)
    assert rows[3]["PctCol"] == pytest.approx(80 / 150)


def test_pct_axis_total_unknown_split_blanks():
    """Bug-8206: without a row/col axis split the axis-total cannot be evaluated
    correctly, so cells are blanked (fail closed), never mis-computed."""
    calc = CalcMember(
        name="PctRow",
        expression="[Measures].[Amount] / (Axis(0), [Measures].[Amount])",
        calc_type="pct_row_total",
        base_measure="Amount",
        axis_total_axis=0,
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "Amount": 30},
        {"RowDim": "A", "ColDim": "Y", "Amount": 70},
    ]
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    evaluate_calc_members(
        [calc], rows, ["Amount"], ["RowDim", "ColDim"],
        measures_meta=measures_meta,
    )
    assert rows[0]["PctRow"] is None
    assert rows[1]["PctRow"] is None


def test_pct_axis_total_hierarchy_named_on_row_axis():
    """Bug-8206 (Opus R1 F2): the hierarchy-named .CurrentMember...Members form
    resolves the named dim against the axis split. If the named dim is on the ROW
    axis, the evaluator totals over rows (= COLUMN total, pinned = col axis)."""
    calc = CalcMember(
        name="PctHier",
        expression="[Measures].[Amount] / ([Measures].[Amount], "
                   "[RowDim].[RowDim].CurrentMember.Level.Members)",
        calc_type="pct_axis_total",
        base_measure="Amount",
        axis_total_axis=-1,
        axis_total_denom_dims=["RowDim"],
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "Amount": 30},
        {"RowDim": "A", "ColDim": "Y", "Amount": 70},
        {"RowDim": "B", "ColDim": "X", "Amount": 20},
        {"RowDim": "B", "ColDim": "Y", "Amount": 80},
    ]
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    evaluate_calc_members(
        [calc], rows, ["Amount"], ["RowDim", "ColDim"],
        measures_meta=measures_meta,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    # Named dim "RowDim" is on the row axis -> totalling over rows -> COLUMN total.
    # Column X: 30+20=50; column Y: 70+80=150.
    assert rows[0]["PctHier"] == pytest.approx(30 / 50)   # A/X
    assert rows[1]["PctHier"] == pytest.approx(70 / 150)  # A/Y
    assert rows[2]["PctHier"] == pytest.approx(20 / 50)   # B/X
    assert rows[3]["PctHier"] == pytest.approx(80 / 150)  # B/Y


def test_pct_axis_total_hierarchy_named_on_col_axis():
    """Bug-8206 (Opus R1 F2): named dim on the COL axis -> totalling over columns
    -> ROW total (pinned = row axis)."""
    calc = CalcMember(
        name="PctHier",
        expression="[Measures].[Amount] / ([Measures].[Amount], "
                   "[ColDim].[ColDim].CurrentMember.Level.Members)",
        calc_type="pct_axis_total",
        base_measure="Amount",
        axis_total_axis=-1,
        axis_total_denom_dims=["ColDim"],
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "Amount": 30},
        {"RowDim": "A", "ColDim": "Y", "Amount": 70},
        {"RowDim": "B", "ColDim": "X", "Amount": 20},
        {"RowDim": "B", "ColDim": "Y", "Amount": 80},
    ]
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    evaluate_calc_members(
        [calc], rows, ["Amount"], ["RowDim", "ColDim"],
        measures_meta=measures_meta,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    # Named dim "ColDim" is on the col axis -> totalling over columns -> ROW total.
    # Row A: 30+70=100; row B: 20+80=100.
    assert rows[0]["PctHier"] == pytest.approx(30 / 100)  # A/X
    assert rows[1]["PctHier"] == pytest.approx(70 / 100)  # A/Y
    assert rows[2]["PctHier"] == pytest.approx(20 / 100)  # B/X
    assert rows[3]["PctHier"] == pytest.approx(80 / 100)  # B/Y


def test_pct_col_total_nonadditive_uses_requery():
    """Bug-8206: a non-additive base measure (avg) uses the re-aggregated column
    total from the denominator re-query, never the sum of the averaged cells."""
    calc = CalcMember(
        name="PctCol",
        expression="[Measures].[AvgPrice] / (Axis(1), [Measures].[AvgPrice])",
        calc_type="pct_col_total",
        base_measure="AvgPrice",
        axis_total_axis=1,
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "AvgPrice": 10},
        {"RowDim": "B", "ColDim": "X", "AvgPrice": 30},
    ]
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    # Column X re-aggregated average over its rows = 25 (not (10+30)=40).
    denom = {("PctCol", ("X",)): 25.0}
    evaluate_calc_members(
        [calc], rows, ["AvgPrice"], ["RowDim", "ColDim"],
        measures_meta=measures_meta, denom_requery_results=denom,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    assert rows[0]["PctCol"] == pytest.approx(10 / 25.0)
    assert rows[1]["PctCol"] == pytest.approx(30 / 25.0)


def test_pct_col_total_nonadditive_missing_requery_blanks():
    """Bug-8206: a non-additive axis total with no re-aggregated denominator is
    blanked (fail closed), never divided by the wrong sum-of-averages."""
    calc = CalcMember(
        name="PctCol",
        expression="[Measures].[AvgPrice] / (Axis(1), [Measures].[AvgPrice])",
        calc_type="pct_col_total",
        base_measure="AvgPrice",
        axis_total_axis=1,
    )
    rows = [
        {"RowDim": "A", "ColDim": "X", "AvgPrice": 10},
        {"RowDim": "B", "ColDim": "X", "AvgPrice": 30},
    ]
    measures_meta = [{"name": "AvgPrice", "default_agg": "avg"}]
    evaluate_calc_members(
        [calc], rows, ["AvgPrice"], ["RowDim", "ColDim"],
        measures_meta=measures_meta, denom_requery_results=None,
        row_axis_dims=["RowDim"], col_axis_dims=["ColDim"],
    )
    assert rows[0]["PctCol"] is None
    assert rows[1]["PctCol"] is None


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


def test_difference_from_prev_member():
    """Bug-6067: a PrevMember-relative reference must compute cell - previous,
    not render an all-blank column (the absolute resolver has no member key)."""
    mdx = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Amount] - ([Measures].[Amount], [Time].[Time].CurrentMember.PrevMember)
SELECT
  {[Measures].[Diff]} ON COLUMNS,
  [Time].[Time].Members ON ROWS
FROM [demo]'''
    calcs = parse_calc_members(parse_mdx(mdx).with_members)
    assert calcs[0].calc_type == "difference"
    assert calcs[0].ref_offset == -1
    rows = [
        {"Time": "2023", "Amount": 100},
        {"Time": "2024", "Amount": 150},
        {"Time": "2025", "Amount": 120},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Time"])
    assert rows[0]["Diff"] is None            # no previous member
    assert rows[1]["Diff"] == pytest.approx(50.0)   # 150 - 100
    assert rows[2]["Diff"] == pytest.approx(-30.0)  # 120 - 150


def test_difference_from_lag_and_next_member():
    """Bug-6067: Lag(n) walks n back; NextMember walks one forward."""
    base = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Amount] - ([Measures].[Amount], [Time].[Time].CurrentMember.{NAV})
SELECT {{[Measures].[Diff]}} ON COLUMNS, [Time].[Time].Members ON ROWS FROM [demo]'''
    src_rows = [
        {"Time": "2023", "Amount": 100},
        {"Time": "2024", "Amount": 150},
        {"Time": "2025", "Amount": 120},
    ]

    lag = parse_calc_members(parse_mdx(base.replace("{NAV}", "Lag(2)")).with_members)
    assert lag[0].ref_offset == -2
    rows = [dict(r) for r in src_rows]
    evaluate_calc_members(lag, rows, ["Amount"], ["Time"])
    assert [r["Diff"] for r in rows] == [None, None, pytest.approx(20.0)]

    nxt = parse_calc_members(parse_mdx(base.replace("{NAV}", "NextMember")).with_members)
    assert nxt[0].ref_offset == 1
    rows = [dict(r) for r in src_rows]
    evaluate_calc_members(nxt, rows, ["Amount"], ["Time"])
    assert [r["Diff"] for r in rows] == [pytest.approx(-50.0), pytest.approx(30.0), None]


def test_pct_difference_from_prev_member():
    """Bug-6067: % Difference From (previous) yields (cell - prev)/prev."""
    mdx = '''WITH
MEMBER [Measures].[PctDiff] AS
  ([Measures].[Amount] - ([Measures].[Amount], [Time].[Time].CurrentMember.PrevMember))
  / ([Measures].[Amount], [Time].[Time].CurrentMember.PrevMember)
SELECT {[Measures].[PctDiff]} ON COLUMNS, [Time].[Time].Members ON ROWS FROM [demo]'''
    calcs = parse_calc_members(parse_mdx(mdx).with_members)
    assert calcs[0].calc_type == "pct_difference"
    assert calcs[0].ref_offset == -1
    rows = [
        {"Time": "2023", "Amount": 100},
        {"Time": "2024", "Amount": 150},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Time"])
    assert rows[0]["PctDiff"] is None
    assert rows[1]["PctDiff"] == pytest.approx(0.5)  # (150-100)/100


def test_difference_from_prev_member_partitioned():
    """Bug-6067: the previous member is resolved WITHIN each non-time partition,
    so products do not leak each other's series."""
    mdx = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Amount] - ([Measures].[Amount], [Month].[Month].CurrentMember.PrevMember)
SELECT {[Measures].[Diff]} ON COLUMNS, [Month].[Month].Members ON ROWS FROM [demo]'''
    calcs = parse_calc_members(parse_mdx(mdx).with_members)
    rows = [
        {"Product": "A", "Month": "Jan", "Amount": 10},
        {"Product": "A", "Month": "Feb", "Amount": 25},
        {"Product": "B", "Month": "Jan", "Amount": 100},
        {"Product": "B", "Month": "Feb", "Amount": 90},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Product", "Month"])
    by = {(r["Product"], r["Month"]): r["Diff"] for r in rows}
    assert by[("A", "Jan")] is None
    assert by[("A", "Feb")] == pytest.approx(15.0)   # 25 - 10, not 25 - 100
    assert by[("B", "Jan")] is None
    assert by[("B", "Feb")] == pytest.approx(-10.0)  # 90 - 100


def test_calc_member_circular_ref_raises():
    """A self-referential circular WITH MEMBER raises ValueError (already a
    client-facing message); the Bug-6066 wrapper must let it pass through."""
    mdx = '''WITH
MEMBER [Measures].[A] AS [Measures].[B] + 1
MEMBER [Measures].[B] AS [Measures].[A] + 1
SELECT {[Measures].[A]} ON COLUMNS, [Region].[Region].Members ON ROWS FROM [demo]'''
    calcs = parse_calc_members(parse_mdx(mdx).with_members)
    rows = [{"Region": "EU", "Amount": 10}]
    with pytest.raises(ValueError):
        evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])


def test_difference_from_lag_zero_is_self_zero():
    """R3: Lag(0)/Lead(0) is a valid self-reference (member offset 0), so the
    difference is 0, not an all-blank column. Routing must key off the detected
    target dimension, not offset truthiness."""
    mdx = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Amount] - ([Measures].[Amount], [Time].[Time].CurrentMember.Lag(0))
SELECT {[Measures].[Diff]} ON COLUMNS, [Time].[Time].Members ON ROWS FROM [demo]'''
    calcs = parse_calc_members(parse_mdx(mdx).with_members)
    assert calcs[0].calc_type == "difference"
    assert calcs[0].ref_offset == 0
    assert calcs[0].ref_offset_dim  # relative navigation detected
    rows = [
        {"Time": "2023", "Amount": 100},
        {"Time": "2024", "Amount": 150},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Time"])
    assert rows[0]["Diff"] == pytest.approx(0.0)
    assert rows[1]["Diff"] == pytest.approx(0.0)


def test_difference_from_prev_member_unrecognized_labels_use_axis_order():
    """MED (member ordering): PrevMember on UNRECOGNISED text labels must resolve
    against the real axis order (the order the rows arrive in), not caption
    alphabetical. 'Phase Two' precedes 'Phase Ten' on the axis; alphabetical
    ordering ('Phase Ten' < 'Phase Two') would compute the difference against
    the wrong peer and emit silently-wrong numbers."""
    mdx = (
        "WITH MEMBER [Measures].[Diff] AS "
        "[Measures].[Amount] - "
        "([Measures].[Amount], [Stage].[Stage].CurrentMember.PrevMember) "
        "SELECT {[Measures].[Diff]} ON COLUMNS, "
        "[Stage].[Stage].Members ON ROWS FROM [demo]"
    )
    calcs = parse_calc_members(parse_mdx(mdx).with_members)
    assert calcs[0].ref_offset == -1
    # Axis order (as returned): Two -> Ten -> Alpha. Alphabetical would be
    # Alpha, Ten, Two — a completely different previous-member chain.
    rows = [
        {"Stage": "Phase Two", "Amount": 100},
        {"Stage": "Phase Ten", "Amount": 150},
        {"Stage": "Phase Alpha", "Amount": 120},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Stage"])
    by = {r["Stage"]: r["Diff"] for r in rows}
    assert by["Phase Two"] is None                    # first on axis
    assert by["Phase Ten"] == pytest.approx(50.0)     # 150 - 100 (prev = Two)
    assert by["Phase Alpha"] == pytest.approx(-30.0)  # 120 - 150 (prev = Ten)


def test_running_total_unrecognized_labels_use_axis_order():
    """MED (member ordering): a running total over UNRECOGNISED text labels must
    accumulate in axis order, not alphabetical. Alphabetical order would sum the
    members in the wrong sequence and report wrong cumulative values."""
    calcs = [CalcMember(
        name="Running",
        expression="Sum(Head([Stage].[Stage].CurrentMember))",
        calc_type="running_total",
        base_measure="Amount",
    )]
    rows = [
        {"Stage": "Phase Two", "Amount": 100},
        {"Stage": "Phase Ten", "Amount": 30},
        {"Stage": "Phase Alpha", "Amount": 5},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Stage"])
    by = {r["Stage"]: r["Running"] for r in rows}
    # Axis order Two(100) -> Ten(130) -> Alpha(135). Alphabetical (Alpha, Ten,
    # Two) would give Alpha=5, Ten=35, Two=135 — wrong.
    assert by["Phase Two"] == pytest.approx(100.0)
    assert by["Phase Ten"] == pytest.approx(130.0)
    assert by["Phase Alpha"] == pytest.approx(135.0)


def test_calc_member_parse_failure_with_member_surfaces(monkeypatch):
    """R3: a parse failure on a statement that DECLARES a WITH MEMBER clause
    must SURFACE as a fault, not silently drop the calc-member column. A parse
    failure on a statement WITHOUT WITH MEMBER stays non-fatal (axis fallback)."""
    from src.dax import mdx_execute

    def _boom_parse(_mdx):
        raise RuntimeError("tree-sitter parse blew up")

    monkeypatch.setattr(mdx_execute, "parse_mdx", _boom_parse, raising=False)
    import src.dax.ts_mdx_parser as _tsp
    monkeypatch.setattr(_tsp, "parse_mdx", _boom_parse)

    with_member_mdx = '''WITH
MEMBER [Measures].[Diff] AS [Measures].[Amount] - 1
SELECT {[Measures].[Diff]} ON COLUMNS, [Time].[Time].Members ON ROWS FROM [demo]'''
    with pytest.raises(ValueError, match="Calculated member parse failed"):
        mdx_execute.build_real_execute_response(
            mdx=with_member_mdx, catalog="demo",
            columns=["Time", "Amount"],
            rows=[{"Time": "2023", "Amount": 100}],
            measures_meta=[{"name": "Amount"}],
            dimensions_meta=[{"name": "Time"}],
        )

    # No WITH MEMBER -> parse failure stays non-fatal (must NOT raise here).
    plain_mdx = 'SELECT {[Measures].[Amount]} ON COLUMNS, ' \
                '[Time].[Time].Members ON ROWS FROM [demo]'
    xml = mdx_execute.build_real_execute_response(
        mdx=plain_mdx, catalog="demo",
        columns=["Time", "Amount"],
        rows=[{"Time": "2023", "Amount": 100}],
        measures_meta=[{"name": "Amount"}],
        dimensions_meta=[{"name": "Time"}],
    )
    assert isinstance(xml, str) and xml


def test_calc_member_eval_failure_surfaced_not_swallowed(monkeypatch):
    """Bug-6066: build_real_execute_response must SURFACE a calc-member
    evaluation failure as a fault (ValueError) rather than swallowing it and
    returning a response with the WITH MEMBER column silently dropped."""
    from src.dax import mdx_execute

    def _boom(*_a, **_k):
        raise RuntimeError("kaboom in calc eval")

    monkeypatch.setattr(mdx_execute, "evaluate_calc_members", _boom)
    mdx = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Amount] - ([Measures].[Amount], [Time].[Time].[2023])
SELECT {[Measures].[Diff]} ON COLUMNS, [Time].[Time].Members ON ROWS FROM [demo]'''
    with pytest.raises(ValueError, match="Calculated member evaluation failed"):
        mdx_execute.build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["Time", "Amount"],
            rows=[{"Time": "2023", "Amount": 100}],
            measures_meta=[{"name": "Amount"}],
            dimensions_meta=[{"name": "Time"}],
        )


def test_calc_member_grammar_parse_error_with_member_faults():
    """Bug-6066 (root fix): the REALISTIC failure mode is a grammar-level parse
    error, where ``parse_mdx`` does NOT raise — it returns a ParsedMDX with a
    'Tree-sitter parse error' warning and with_members EMPTY (or partially/
    wrongly recovered). Previously the fault branch only fired for a
    monkeypatched environmental exception, so a genuinely unparseable
    WITH MEMBER statement STILL silently dropped its calc columns. This must now
    fault (SOAP client error) instead of returning a silently-incomplete result.
    No monkeypatching — a real unparseable WITH MEMBER statement is used."""
    from src.dax import mdx_execute

    # An unparseable WITH MEMBER body: the calc expression runs straight into a
    # nested SELECT the MDX grammar cannot accept, so root.has_error is set and
    # with_members comes back empty — the exact silent-drop mode Bug-6066 leaves.
    broken_mdx = (
        "WITH MEMBER [Measures].[Ratio] AS "
        "([Measures].[Amount] / SELECT FROM WHERE ON ROWS FROM [demo]"
    )
    parsed = parse_mdx(broken_mdx)
    # Precondition: the parser did NOT raise and produced a parse-error warning
    # with no usable members — the state that used to slip through silently.
    assert any("parse error" in w.lower() for w in parsed.warnings)
    assert parsed.with_members == []

    with pytest.raises(ValueError, match="Calculated member parse failed"):
        mdx_execute.build_real_execute_response(
            mdx=broken_mdx, catalog="demo",
            columns=["Amount"],
            rows=[{"Amount": 100}],
            measures_meta=[{"name": "Amount"}],
            dimensions_meta=[],
        )


def test_calc_member_grammar_partial_recovery_faults():
    """Bug-6066 (R1 finding): a parse error that drops SOME (not all) declared
    members must also fault. Here two members are declared but only one survives
    error recovery — returning the partial result would silently drop the other
    calculated column. The guard compares recovered-usable count against the
    declared MEMBER count. Real unparseable statement, no monkeypatching."""
    from src.dax import mdx_execute

    # Member B has no ``AS`` body, so the grammar recovers only A (a genuine
    # partial recovery: 1 usable of 2 declared MEMBERs).
    broken_mdx = (
        "WITH MEMBER [Measures].[A] AS [Measures].[Amount] * 2 "
        "MEMBER [Measures].[B] "
        "SELECT {[Measures].[A]} ON 0 FROM [demo]"
    )
    parsed = parse_mdx(broken_mdx)
    # Precondition: the statement is malformed (has_error), and the grammar
    # recovered fewer members than were declared.
    assert parsed.has_error
    assert 0 < len(parsed.with_members) < 2

    with pytest.raises(ValueError, match="Calculated member parse failed"):
        mdx_execute.build_real_execute_response(
            mdx=broken_mdx, catalog="demo",
            columns=["Amount"],
            rows=[{"Amount": 100}],
            measures_meta=[{"name": "Amount"}],
            dimensions_meta=[],
        )


def test_calc_member_caption_with_member_word_does_not_false_fault():
    """Bug-6066 (R2 finding): the declared-count guard must count only MEMBER
    DECLARATION keywords, not the word 'Member' inside a bracketed caption or
    string literal. This statement uses Generate/Ascendants (the grammar
    over-reports has_error) AND a measure caption containing 'Member', yet its
    single member is fully recovered — it must NOT fault. Real grammar, no
    monkeypatching."""
    from src.dax import mdx_execute

    mdx = (
        "WITH MEMBER [Measures].[Total Member Revenue] As "
        "'AddCalculatedMembers([account_type].[account_type].currentmember.children).count'\n"
        "Set FilteredMembers As '{[account_type].[account_type].[All]}'\n"
        "Select {[Measures].[Total Member Revenue]} on ROWS, "
        "Hierarchize(Generate(FilteredMembers, "
        "Ascendants([account_type].[account_type].currentmember))) "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, MEMBER_TYPE ON COLUMNS FROM [m]"
    )
    parsed = parse_mdx(mdx)
    # Wave C #3 grammar fix: Generate/Ascendants + a 'Member' caption now parse
    # CLEANLY (no has_error over-flag), and the member is recovered.
    assert not parsed.has_error
    assert len(parsed.with_members) == 1 and parsed.with_members[0].expression.strip()

    # Must build a response, NOT raise (the raw 'Member' word count is 3 here).
    xml = mdx_execute.build_real_execute_response(
        mdx=mdx, catalog="m",
        columns=["account_type"],
        rows=[{"account_type": "CURRENT"}, {"account_type": "LOAN"}],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
    )
    assert isinstance(xml, str) and "Total Member Revenue" in xml


def test_with_set_only_query_with_member_caption_does_not_false_fault():
    """Bug-6607 (R3 Fable finding): a valid WITH SET-only query (ZERO MEMBER
    declarations) whose measure caption contains the word 'Member', combined with
    a grammar has_error over-flag (Generate/Ascendants), must NOT fault. The
    guard now gates on the declared-MEMBER count (computed on the literal-stripped
    MDX), so a zero-declaration statement can never be mistaken for a dropped
    calc-member. Real grammar, no monkeypatching."""
    from src.dax import mdx_execute

    mdx = (
        "WITH SET FilteredMembers As '{[account_type].[account_type].[All]}'\n"
        "Select {[Measures].[Total Member Revenue]} on ROWS, "
        "Hierarchize(Generate(FilteredMembers, "
        "Ascendants([account_type].[account_type].currentmember))) "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, MEMBER_TYPE ON COLUMNS FROM [m]"
    )
    parsed = parse_mdx(mdx)
    # Wave C #3 grammar fix: a bare-named WITH SET with Generate/Ascendants now
    # parses CLEANLY; there are NO member declarations.
    assert not parsed.has_error
    assert parsed.with_members == []

    # Must NOT raise — a zero-declaration query has no calc column to drop.
    xml = mdx_execute.build_real_execute_response(
        mdx=mdx, catalog="m",
        columns=["account_type"],
        rows=[{"account_type": "CURRENT"}, {"account_type": "LOAN"}],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
    )
    assert isinstance(xml, str) and xml


@pytest.mark.parametrize("comment", ["// member calc\n", "-- member note\n", "/* member block */ "])
def test_valid_with_member_with_comment_does_not_false_fault(comment):
    """Bug-6611 (R4 Fable finding): the Tree-sitter MDX grammar has no comment
    rule, so ANY comment (//, --, /* */ — all valid SSAS MDX) sets has_error on
    an otherwise perfectly-parsed statement. A comment containing the word
    'member' must NOT inflate the declared-member count and false-fault a valid,
    fully-recovered WITH MEMBER query. The declared count is now taken on MDX with
    comments stripped. Real grammar, no monkeypatching."""
    from src.dax import mdx_execute

    mdx = (
        comment
        + "WITH MEMBER [Measures].[Diff] AS [Measures].[Amount] * 2 "
        "SELECT {[Measures].[Diff]} ON COLUMNS, "
        "[Time].[Time].Members ON ROWS FROM [demo]"
    )
    parsed = parse_mdx(mdx)
    # Wave C #3 grammar fix: comments are now a grammar `extra`, so a commented
    # WITH MEMBER parses CLEANLY (no has_error) and the member is recovered.
    assert not parsed.has_error
    assert len(parsed.with_members) == 1 and parsed.with_members[0].expression.strip()

    # Must NOT raise: exactly one member declared and one usable member recovered.
    xml = mdx_execute.build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["Time", "Amount"],
        rows=[{"Time": "2023", "Amount": 100}, {"Time": "2024", "Amount": 150}],
        measures_meta=[{"name": "Amount"}],
        dimensions_meta=[{"name": "Time"}],
    )
    assert isinstance(xml, str) and xml


def test_bug6612_nested_block_comment_does_not_false_fault():
    """Bug-6612: SSAS MDX block comments NEST. A non-greedy strip regex closed at
    the first ``*/`` and left the outer comment's tail (containing a standalone
    'member') as live text, inflating the declared-member count (2 vs 1) and
    faulting a VALID single-member query. The depth-aware strip scanner removes
    the whole nested comment, so the count matches the one recovered member and
    the query renders. Real grammar, no monkeypatching."""
    from src.dax import mdx_execute

    # Outer tail after the inner '*/' contains the standalone word 'member'.
    mdx = (
        "/* outer /* inner */ member tail */ "
        "WITH MEMBER [Measures].[Diff] AS [Measures].[Amount] * 2 "
        "SELECT {[Measures].[Diff]} ON COLUMNS, "
        "[Time].[Time].Members ON ROWS FROM [demo]"
    )
    parsed = parse_mdx(mdx)
    assert len(parsed.with_members) == 1 and parsed.with_members[0].expression.strip()

    # Declared-member count (strip scanner) must be exactly 1, not inflated to 2.
    stripped = mdx_execute._mdx_strip_literals_and_comments(mdx)
    import re as _re
    assert len(_re.findall(r"\bMEMBER\b", stripped, _re.IGNORECASE)) == 1

    # Must NOT raise: the valid single-member query renders.
    xml = mdx_execute.build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["Time", "Amount"],
        rows=[{"Time": "2023", "Amount": 100}, {"Time": "2024", "Amount": 150}],
        measures_meta=[{"name": "Amount"}],
        dimensions_meta=[{"name": "Time"}],
    )
    assert isinstance(xml, str) and xml


def test_calc_member_grammar_parse_error_without_member_non_fatal():
    """A grammar parse-error warning on a statement WITHOUT a WITH MEMBER clause
    stays non-fatal — the axis layout is recovered by the regex helpers and no
    calc columns are at risk of being dropped."""
    from src.dax import mdx_execute

    # Malformed-but-no-calc-member statement: still parses with a warning, but
    # there is nothing to silently drop, so it must NOT fault.
    plain_broken = "SELECT {[Measures].[Amount]} ON COLUMNS FROM [demo] WHERE @@@"
    xml = mdx_execute.build_real_execute_response(
        mdx=plain_broken, catalog="demo",
        columns=["Amount"],
        rows=[{"Amount": 100}],
        measures_meta=[{"name": "Amount"}],
        dimensions_meta=[],
    )
    assert isinstance(xml, str) and xml


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

    def test_bigquery_identifier_quoting(self):
        spec = ReQuerySpec(
            calc_name="Europe",
            measure_name="AvgSale",
            agg="avg",
            dim_col="Country",
            members=["France", "Germany"],
            model_slug="my_model",
            connector_type="bigquery",
            partition_dims=["Year"],
            partition_values=["2024"],
        )
        sql = build_requery_sql(spec)

        assert f"AVG({quote_identifier('bigquery', 'AvgSale')})" in sql
        assert f"AS {quote_identifier('bigquery', 'AvgSale')}" in sql
        assert f"FROM {quote_identifier('bigquery', 'my_model')}" in sql
        assert f"{quote_identifier('bigquery', 'Country')} IN" in sql
        assert f"{quote_identifier('bigquery', 'Year')} = '2024'" in sql
        assert '"AvgSale"' not in sql
        assert '"Country"' not in sql

    def test_sqlserver_identifier_quoting(self):
        spec = ReQuerySpec(
            calc_name="Europe",
            measure_name="Customer]Count",
            agg="count_distinct",
            dim_col="Country",
            members=["France"],
            model_slug="model]slug",
            connector_type="sqlserver",
        )
        sql = build_requery_sql(spec)

        assert f"COUNT(DISTINCT {quote_identifier('sqlserver', 'Customer]Count')})" in sql
        assert f"AS {quote_identifier('sqlserver', 'Customer]Count')}" in sql
        assert f"FROM {quote_identifier('sqlserver', 'model]slug')}" in sql
        assert f"{quote_identifier('sqlserver', 'Country')} IN" in sql
        assert '"Customer]Count"' not in sql
        assert '"model]slug"' not in sql

    def test_member_with_single_quote_escaped(self):
        spec = ReQuerySpec(
            calc_name="Group", measure_name="Count", agg="count_distinct",
            dim_col="Name", members=["O'Brien", "Smith"], model_slug="m",
        )
        sql = build_requery_sql(spec)
        assert "O''Brien" in sql

    def test_bug6074_backslash_member_cannot_break_out_bigquery(self):
        # Bug-6074: a client-derived member value ending in a backslash must
        # not escape the closing quote on backslash-aware dialects (BigQuery/
        # Spark/Snowflake). Naive ``''``-only escaping left ``'x\'`` open,
        # turning trailing SQL into naked injectable text. The dialect-correct
        # literal doubles the backslash so the value stays contained.
        import sqlglot
        from sqlglot import exp

        payload_tail = "x" + chr(92)  # "x\"
        injection = " UNION SELECT secret FROM users --"
        spec = ReQuerySpec(
            calc_name="G", measure_name="C", agg="count_distinct",
            dim_col="Name", members=[payload_tail, injection], model_slug="m",
            connector_type="bigquery",
        )
        sql = build_requery_sql(spec)
        # Backslash is doubled -> literal is terminated correctly.
        assert "'x" + chr(92) * 2 + "'" in sql
        # The whole statement parses on bigquery with the injection text still
        # inside a single string literal (never a naked UNION / SELECT node).
        parsed = sqlglot.parse_one(sql, read="bigquery")
        in_expr = parsed.find(exp.In)
        assert in_expr is not None
        literal_values = [
            e.this for e in in_expr.expressions if isinstance(e, exp.Literal)
        ]
        assert injection in literal_values
        # No stray UNION got parsed as a set operation (breakout signature).
        assert parsed.find(exp.Union) is None

    def test_bug6074_backslash_member_contained_redshift(self):
        # Redshift shares PostgreSQL's identifier quoting but its STRING LITERALS
        # are backslash-aware, so a trailing-backslash member must be doubled
        # (not left as a bare '\') to stay contained.
        spec = ReQuerySpec(
            calc_name="G", measure_name="C", agg="count_distinct",
            dim_col="Name", members=["x" + chr(92)], model_slug="m",
            connector_type="redshift",
        )
        sql = build_requery_sql(spec)
        assert "'x" + chr(92) * 2 + "'" in sql
        assert "'x" + chr(92) + "'" not in sql

    def test_bug6074_backslash_partition_value_contained_spark(self):
        # Same class of vector via the other-dimension partition predicate.
        spec = ReQuerySpec(
            calc_name="G", measure_name="C", agg="avg",
            dim_col="Region", members=["US"], model_slug="m",
            connector_type="hadoop_spark",
            partition_dims=["Year"],
            partition_values=["2024" + chr(92)],
        )
        sql = build_requery_sql(spec)
        assert "'2024" + chr(92) * 2 + "'" in sql

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


# ---------------------------------------------------------------------------
# F-002-01 / Bug-6065 — custom Aggregate group + grain-sensitive Show Values As
# must produce correct percentages with deterministic evaluation order.
# ---------------------------------------------------------------------------


def _bug6065_calcs():
    group = CalcMember(
        name="Bundle",
        expression="Aggregate({[Category].[Shoes], [Category].[Music]})",
        calc_type="aggregate_set",
        dim_name="Category",
        aggregate_members=["Shoes", "Music"],
        solve_order=0,  # evaluates BEFORE the percentage (the failing order)
    )
    pct = CalcMember(
        name="PctTotal",
        expression="[Measures].[Amount] / ([Measures].[Amount], [Category].[(All)])",
        calc_type="pct_grand_total",
        base_measure="Amount",
        solve_order=10,
    )
    return [group, pct]


def _bug6065_rows():
    return [
        {"Category": "Shoes", "Amount": 100},
        {"Category": "Music", "Amount": 200},
        {"Category": "Toys", "Amount": 700},
    ]


def _bug6065_pct_map(calc_members):
    out = evaluate_calc_members(
        calc_members, _bug6065_rows(), ["Amount"], ["Category"],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
    )
    return {r["Category"]: r.get("PctTotal") for r in out}


def test_bug6065_group_row_not_double_counted_in_pct_grand_total():
    rows = _bug6065_rows()
    out = evaluate_calc_members(
        _bug6065_calcs(), rows, ["Amount"], ["Category"],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
    )
    by_cat = {r["Category"]: r for r in out}
    # Grand total denominator must be the 1000 detail total, NOT 1300 (which
    # double-counts the 300 synthetic group row).
    assert by_cat["Shoes"]["PctTotal"] == pytest.approx(0.10)
    assert by_cat["Music"]["PctTotal"] == pytest.approx(0.20)
    assert by_cat["Toys"]["PctTotal"] == pytest.approx(0.70)


def test_bug6065_group_row_shows_its_own_percentage():
    rows = _bug6065_rows()
    out = evaluate_calc_members(
        _bug6065_calcs(), rows, ["Amount"], ["Category"],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
    )
    bundle = next(r for r in out if r["Category"] == "Bundle")
    assert bundle["Amount"] == 300              # Shoes + Music aggregated
    assert bundle["PctTotal"] == pytest.approx(0.30)   # 300 / 1000, not blank


def test_bug6065_evaluation_is_deterministic():
    # Reordering independent calc members must not change the output. If the
    # stable ordering in _check_circular_references is weakened to input order,
    # the reversed case evaluates the percentage before the group and leaves
    # the group row blank.
    expected = {
        "Shoes": pytest.approx(0.10),
        "Music": pytest.approx(0.20),
        "Toys": pytest.approx(0.70),
        "Bundle": pytest.approx(0.30),
    }
    assert _bug6065_pct_map(_bug6065_calcs()) == expected
    assert _bug6065_pct_map(list(reversed(_bug6065_calcs()))) == expected


def test_bug6065_evaluation_is_stable_across_hash_seeds():
    """Different hash seeds must not alter independent calc-member ordering."""
    script = r"""
import json
from src.dax.mdx_calc_members import CalcMember, evaluate_calc_members

rows = [
    {"Category": "Shoes", "Amount": 100},
    {"Category": "Music", "Amount": 200},
    {"Category": "Toys", "Amount": 700},
]
calcs = [
    CalcMember(
        name="PctTotal",
        expression="[Measures].[Amount] / ([Measures].[Amount], [Category].[(All)])",
        calc_type="pct_grand_total",
        base_measure="Amount",
        solve_order=0,
    ),
    CalcMember(
        name="Bundle",
        expression="Aggregate({[Category].[Shoes], [Category].[Music]})",
        calc_type="aggregate_set",
        dim_name="Category",
        aggregate_members=["Shoes", "Music"],
        solve_order=0,
    ),
]
out = evaluate_calc_members(
    calcs, rows, ["Amount"], ["Category"],
    measures_meta=[{"name": "Amount", "default_agg": "sum"}],
)
print(json.dumps({r["Category"]: r.get("PctTotal") for r in out}, sort_keys=True))
"""
    _parents = Path(__file__).resolve().parents
    gateway_dir = _parents[1]
    # Host layout: repo/tessallite/services/gateway/tests -> parents[3] is the
    # `tessallite` package root that must be on PYTHONPATH so the subprocess can
    # `import shared`. In the in-image test runner the tree is /app/tests, so
    # parents[3] does not exist and `shared` is already installed — leave
    # PYTHONPATH untouched there instead of raising IndexError.
    repo_tessallite_dir = _parents[3] if len(_parents) > 3 else None
    env_base = os.environ.copy()
    if repo_tessallite_dir is not None:
        existing_pythonpath = env_base.get("PYTHONPATH")
        env_base["PYTHONPATH"] = (
            str(repo_tessallite_dir)
            if not existing_pythonpath
            else str(repo_tessallite_dir) + os.pathsep + existing_pythonpath
        )
    outputs = []
    for seed in ("1", "2"):
        env = dict(env_base)
        env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(gateway_dir),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(json.loads(completed.stdout))

    assert outputs[0] == outputs[1]
    assert outputs[0]["Bundle"] == pytest.approx(0.30)
    assert outputs[0]["Shoes"] == pytest.approx(0.10)


def test_aggregate_set_reordering_does_not_change_detail_grain_numbers():
    """Bug-8757 moved every ``aggregate_set`` member ahead of all others. That
    is a change to EVALUATION SEMANTICS, so it must be proved not to move any
    number that was already right. Known answers: 10/90, 30/90, 50/90 on the
    detail rows -- identical with and without the group present -- and 40/90 on
    the group row, which was a BLANK cell before the fix.
    """
    rows = [
        {"Region": "P1", "Amount": 10},
        {"Region": "P2", "Amount": 30},
        {"Region": "P3", "Amount": 50},
    ]
    grp = CalcMember(
        name="Grp",
        expression="AGGREGATE({[Region].[Region].[P1],[Region].[Region].[P2]})",
        calc_type="aggregate_set", dim_name="Region",
        aggregate_members=["P1", "P2"],
    )
    pct = CalcMember(
        name="APct",
        expression="[Measures].[Amount] / ([Measures].[Amount],[Region].[Region].[All])",
        calc_type="pct_grand_total", base_measure="Amount",
    )
    meta = [{"name": "Amount", "default_agg": "sum"}]

    # "APct" sorts BEFORE "Grp": the exact name tie-break that produced the
    # blank cell before the producer-first partition.
    out = evaluate_calc_members([pct, grp], list(rows), ["Amount"], ["Region"], meta)
    got = {r["Region"]: (r["Amount"], r["APct"]) for r in out}
    assert [round(got[k][1], 6) for k in ("P1", "P2", "P3")] == [
        round(10 / 90, 6), round(30 / 90, 6), round(50 / 90, 6)
    ], got
    assert got["Grp"][0] == 40.0
    assert round(got["Grp"][1], 6) == round(40 / 90, 6), got

    # Control: without the group at all, the detail numbers are the same.
    ctrl = evaluate_calc_members([pct], list(rows), ["Amount"], ["Region"], meta)
    assert [round(r["APct"], 6) for r in ctrl] == [
        round(10 / 90, 6), round(30 / 90, 6), round(50 / 90, 6)
    ]


def test_running_total_is_unaffected_by_the_producer_first_partition():
    """Sequence types have no natural position for a summary row: the cumulative
    sum must still run over the DETAIL peers only (10, 40, 90) and the group row
    must stay explicitly blank, not join the sequence."""
    rows = [
        {"Region": "P1", "Amount": 10},
        {"Region": "P2", "Amount": 30},
        {"Region": "P3", "Amount": 50},
    ]
    grp = CalcMember(
        name="AGrp", expression="AGGREGATE({...})", calc_type="aggregate_set",
        dim_name="Region", aggregate_members=["P1", "P2"],
    )
    run = CalcMember(
        name="ZRun", expression="[Measures].[Amount]",
        calc_type="running_total", base_measure="Amount",
    )
    out = evaluate_calc_members(
        [run, grp], list(rows), ["Amount"], ["Region"],
        [{"name": "Amount", "default_agg": "sum"}],
    )
    got = {r["Region"]: r.get("ZRun") for r in out}
    assert [got["P1"], got["P2"], got["P3"]] == [10, 40, 90], got
    assert got["AGrp"] is None, got


def test_two_custom_groups_on_different_dimensions_do_not_double_count():
    """The producer-first partition puts BOTH groups in the first partition, so
    the second one sees the first one's synthetic rows. Known answer: with a
    4-cell grid 10/20/30/40, each group's cells are the correct per-partition
    totals and the group-x-group intersection is 100 -- the grand total, counted
    ONCE, not 200."""
    rows = [
        {"Product": "P1", "Region": "R1", "Amt": 10},
        {"Product": "P1", "Region": "R2", "Amt": 20},
        {"Product": "P2", "Region": "R1", "Amt": 30},
        {"Product": "P2", "Region": "R2", "Amt": 40},
    ]
    gp = CalcMember(
        name="GrpP", expression="AGGREGATE({...})", calc_type="aggregate_set",
        dim_name="Product", aggregate_members=["P1", "P2"],
    )
    gr = CalcMember(
        name="GrpR", expression="AGGREGATE({...})", calc_type="aggregate_set",
        dim_name="Region", aggregate_members=["R1", "R2"],
    )
    out = evaluate_calc_members(
        [gp, gr], list(rows), ["Amt"], ["Product", "Region"],
        [{"name": "Amt", "default_agg": "sum"}],
    )
    got = {(r["Product"], r["Region"]): r["Amt"] for r in out}
    assert got[("GrpP", "R1")] == 40.0 and got[("GrpP", "R2")] == 60.0, got
    assert got[("P1", "GrpR")] == 30.0 and got[("P2", "GrpR")] == 70.0, got
    assert got[("GrpP", "GrpR")] == 100.0, (
        f"the group-x-group intersection double-counted: {got}"
    )
