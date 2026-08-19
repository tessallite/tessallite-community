"""MDX edge case tests — Phase 3 XMLA protocol audit.

Covers edge cases identified in the XMLA audit:
- Subselect FROM parsing
- DRILLTHROUGH with complex WHERE clauses
- Unsupported MDX function detection
- WITH MEMBER expression edge cases
- Special character handling in member names
- Empty and degenerate MDX statements
"""
from __future__ import annotations

import pytest

from src.dax.ts_mdx_parser import parse_mdx
from src.dax.mdx_calc_members import (
    CalcMember,
    parse_calc_members,
    evaluate_calc_members,
    _check_circular_references,
)
from src.dax.mdx_validators import (
    check_unsupported_mdx_constructs,
)
from src.dax.xmla_server import (
    _extract_topn_spec,
    _extract_filter_spec,
    _translate_label_filter_calls,
    _label_filter_to_sql,
    _RANGE_PREFIX,
    _RANGE_SEP,
)


# ---------------------------------------------------------------------------
# Subselect FROM parsing
# ---------------------------------------------------------------------------

class TestSubselect:
    def test_subselect_parsed(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM (SELECT {[Date].[Year].&[2025]} ON COLUMNS FROM [Sales])"
        )
        assert r.subselect is not None
        assert r.subselect.cube_name == "Sales"
        assert len(r.subselect.axes) == 1

    def test_subselect_preserves_outer_axes(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "{[Region].[Country].Members} ON ROWS "
            "FROM (SELECT {[Date].[Year].&[2025]} ON COLUMNS FROM [Sales])"
        )
        assert len(r.axes) == 2
        assert r.axes[0].axis_name == "COLUMNS"
        assert r.axes[1].axis_name == "ROWS"
        assert r.subselect is not None

    def test_drillthrough_with_subselect(self):
        r = parse_mdx(
            "DRILLTHROUGH SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM (SELECT {[Date].[Year].&[2025]} ON COLUMNS FROM [Sales])"
        )
        assert r.is_drillthrough is True
        assert r.subselect is not None
        assert r.subselect.cube_name == "Sales"

    def test_subselect_where_clause(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM (SELECT {[Date].[Year].&[2025]} ON COLUMNS "
            "FROM [Sales] WHERE ([Region].[Country].&[US]))"
        )
        assert r.subselect is not None


# ---------------------------------------------------------------------------
# WITH MEMBER edge cases
# ---------------------------------------------------------------------------

class TestWithMember:
    def test_multiple_with_members(self):
        mdx = (
            "WITH "
            "MEMBER [Measures].[Calc1] AS [Measures].[Sales] * 1.1 "
            "MEMBER [Measures].[Calc2] AS [Measures].[Sales] - [Measures].[Cost] "
            "SELECT {[Measures].[Calc1], [Measures].[Calc2]} ON COLUMNS "
            "FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert len(r.with_members) == 2
        assert r.with_members[0].name == "[Measures].[Calc1]"
        assert r.with_members[1].name == "[Measures].[Calc2]"

    def test_with_member_format_string(self):
        mdx = (
            "WITH MEMBER [Measures].[Pct] AS "
            "[Measures].[Sales] / [Measures].[Total], "
            "FORMAT_STRING = '0.00%' "
            "SELECT {[Measures].[Pct]} ON COLUMNS FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert len(r.with_members) == 1
        calcs = parse_calc_members(r.with_members)
        assert len(calcs) == 1

    def test_with_set_definition(self):
        mdx = (
            "WITH SET [TopRegions] AS "
            "TopCount([Region].[Region].Members, 5, [Measures].[Sales]) "
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "[TopRegions] ON ROWS FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert len(r.with_sets) == 1
        assert r.with_sets[0].name == "[TopRegions]"


# ---------------------------------------------------------------------------
# Calculated member evaluation edge cases
# ---------------------------------------------------------------------------

class TestCalcMemberEval:
    def test_pct_grand_total_zero_total(self):
        calcs = [CalcMember(
            name="Pct",
            expression="[Measures].[Amount] / ([Measures].[Amount], [Region].[(All)])",
            calc_type="pct_grand_total",
            base_measure="Amount",
        )]
        rows = [
            {"Region": "A", "Amount": 0},
            {"Region": "B", "Amount": 0},
        ]
        evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
        for row in rows:
            assert row["Pct"] is None or row["Pct"] == 0

    def test_running_total(self):
        calcs = [CalcMember(
            name="RT",
            expression="Sum([Measures].[Amount])",
            calc_type="running_total",
            base_measure="Amount",
        )]
        rows = [
            {"Region": "A", "Amount": 10},
            {"Region": "B", "Amount": 20},
            {"Region": "C", "Amount": 30},
        ]
        evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
        assert rows[0]["RT"] == 10
        assert rows[1]["RT"] == 30
        assert rows[2]["RT"] == 60

    def test_difference_from_reference_member(self):
        calcs = [CalcMember(
            name="Diff",
            expression="[Measures].[Amount] - ([Measures].[Amount], [Month].[Month].&[Jan])",
            calc_type="difference",
            base_measure="Amount",
            ref_member_parts=["Month", "Month", "Jan"],
        )]
        rows = [
            {"Month": "Jan", "Amount": 100},
            {"Month": "Feb", "Amount": 150},
            {"Month": "Mar", "Amount": 130},
        ]
        evaluate_calc_members(calcs, rows, ["Amount"], ["Month"])
        assert rows[0]["Diff"] == 0
        assert rows[1]["Diff"] == 50
        assert rows[2]["Diff"] == 30

    def test_single_row_dataset(self):
        calcs = [CalcMember(
            name="Pct",
            expression="[Measures].[Amount] / ([Measures].[Amount], [Region].[(All)])",
            calc_type="pct_grand_total",
            base_measure="Amount",
        )]
        rows = [{"Region": "Only", "Amount": 42}]
        evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
        assert rows[0]["Pct"] == pytest.approx(1.0)

    def test_empty_row_dataset(self):
        calcs = [CalcMember(
            name="Pct",
            expression="[Measures].[Amount] / ([Measures].[Amount], [Region].[(All)])",
            calc_type="pct_grand_total",
            base_measure="Amount",
        )]
        rows: list[dict] = []
        evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
        assert rows == []

    def test_null_measure_values(self):
        calcs = [CalcMember(
            name="Pct",
            expression="[Measures].[Amount] / ([Measures].[Amount], [Region].[(All)])",
            calc_type="pct_grand_total",
            base_measure="Amount",
        )]
        rows = [
            {"Region": "A", "Amount": None},
            {"Region": "B", "Amount": 100},
        ]
        evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
        assert rows[1].get("Pct") is not None


# ---------------------------------------------------------------------------
# WHERE clause parsing edge cases
# ---------------------------------------------------------------------------

class TestWhereClause:
    def test_single_member_where(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales] "
            "WHERE ([Date].[Year].&[2025])"
        )
        assert len(r.where_members) == 1

    def test_multi_member_where(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales] "
            "WHERE ([Date].[Year].&[2025], [Region].[Country].&[US], "
            "[Product].[Category].&[Electronics])"
        )
        assert len(r.where_members) == 3

    def test_member_with_space_in_value(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales] "
            "WHERE ([Region].[Country].&[United States])"
        )
        assert len(r.where_members) == 1

    def test_member_with_special_chars(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales] "
            "WHERE ([Product].[Name].&[Widget's Best])"
        )
        assert len(r.where_members) >= 1


# ---------------------------------------------------------------------------
# DRILLTHROUGH edge cases
# ---------------------------------------------------------------------------

class TestDrillthroughEdgeCases:
    def test_drillthrough_maxrows_and_where(self):
        r = parse_mdx(
            "DRILLTHROUGH MAXROWS 500 "
            "SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM [Sales] WHERE ([Date].[Year].&[2025])"
        )
        assert r.is_drillthrough is True
        assert r.maxrows == 500
        assert len(r.where_members) > 0

    def test_drillthrough_return_with_maxrows(self):
        r = parse_mdx(
            "DRILLTHROUGH MAXROWS 100 "
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales] "
            "RETURN [$Fact].[order_id], [$Fact].[amount]"
        )
        assert r.is_drillthrough is True
        assert r.maxrows == 100
        assert len(r.return_columns) == 2

    def test_drillthrough_non_empty_rows(self):
        r = parse_mdx(
            "DRILLTHROUGH "
            "SELECT NON EMPTY {[Measures].[Sales]} ON COLUMNS, "
            "NON EMPTY {[Region].[Country].Members} ON ROWS "
            "FROM [Sales]"
        )
        assert r.is_drillthrough is True
        assert r.axes[0].non_empty is True
        assert r.axes[1].non_empty is True


# ---------------------------------------------------------------------------
# Axis expression variations
# ---------------------------------------------------------------------------

class TestAxisExpressions:
    def test_numeric_axis_names(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON 0, "
            "{[Region].[Country].Members} ON 1 "
            "FROM [Sales]"
        )
        assert len(r.axes) == 2
        assert r.axis_expr("COLUMNS") != "" or r.axis_expr("0") != ""

    def test_hierarchize_wrapper(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "Hierarchize({[Region].[Region].Members}) ON ROWS "
            "FROM [Sales]"
        )
        assert len(r.axes) == 2
        assert "Hierarchize" in r.axes[1].raw_expr

    def test_union_axis(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales], [Measures].[Cost]} ON COLUMNS, "
            "Union({[Region].[Country].&[US]}, {[Region].[Country].&[UK]}) ON ROWS "
            "FROM [Sales]"
        )
        assert len(r.axes) == 2

    def test_crossjoin_axis(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "CrossJoin({[Date].[Year].Members}, {[Region].[Country].Members}) ON ROWS "
            "FROM [Sales]"
        )
        assert len(r.axes) == 2
        assert "CrossJoin" in r.axes[1].raw_expr

    def test_dimension_properties_on_rows(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "{[Region].[Country].Members} "
            "DIMENSION PROPERTIES [Region].[Country].[MEMBER_KEY] ON ROWS "
            "FROM [Sales]"
        )
        assert len(r.axes) == 2
        assert len(r.axes[1].dim_properties) > 0


# ---------------------------------------------------------------------------
# Cube name extraction
# ---------------------------------------------------------------------------

class TestCubeName:
    def test_simple_cube(self):
        r = parse_mdx("SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales]")
        assert r.cube_name == "Sales"

    def test_cube_with_spaces(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [My Sales Cube]"
        )
        assert r.cube_name == "My Sales Cube"

    def test_cube_case_preserved(self):
        r = parse_mdx(
            "SELECT {[Measures].[Sales]} ON COLUMNS FROM [SalesCube]"
        )
        assert r.cube_name == "SalesCube"


# ---------------------------------------------------------------------------
# Bug-490 — Unsupported MDX function detection
# ---------------------------------------------------------------------------

class TestUnsupportedMdxFunctions:
    def test_except_raises(self):
        with pytest.raises(ValueError, match="Except"):
            check_unsupported_mdx_constructs(
                "Except({[Region].[Country].Members}, {[Region].[Country].&[US]})",
                "ROWS axis",
            )

    def test_intersect_raises(self):
        with pytest.raises(ValueError, match="Intersect"):
            check_unsupported_mdx_constructs(
                "Intersect({[Region].[Country].Members}, {[Date].[Year].Members})",
                "COLUMNS axis",
            )

    def test_order_raises(self):
        with pytest.raises(ValueError, match="Order"):
            check_unsupported_mdx_constructs(
                "Order({[Region].[Country].Members}, [Measures].[Sales], DESC)",
                "ROWS axis",
            )

    def test_filter_raises(self):
        with pytest.raises(ValueError, match="Filter"):
            check_unsupported_mdx_constructs(
                "Filter([Region].[Country].Members, [Measures].[Sales] > 100)",
                "ROWS axis",
            )

    def test_range_operator_raises(self):
        with pytest.raises(ValueError, match="range"):
            check_unsupported_mdx_constructs(
                "{[Date].[Year].&[2020]:[Date].[Year].&[2025]}",
                "subselect",
            )

    def test_supported_functions_pass(self):
        for expr in (
            "CrossJoin({[Date].[Year].Members}, {[Region].[Country].Members})",
            "DrilldownLevel({[Region].[Country].[All]})",
            "Hierarchize({[Region].[Country].Members})",
            "AddCalculatedMembers({[Measures].[Sales]})",
            "{[Measures].[Sales], [Measures].[Cost]}",
            "[Region].[Country].Members",
        ):
            check_unsupported_mdx_constructs(expr, "test")

    def test_case_insensitive(self):
        with pytest.raises(ValueError, match="EXCEPT"):
            check_unsupported_mdx_constructs(
                "EXCEPT({[A].[B].Members}, {[A].[B].&[X]})",
                "axis",
            )

    def test_empty_expr_passes(self):
        check_unsupported_mdx_constructs("", "test")


# ---------------------------------------------------------------------------
# Bug-488 — Nested subselect detection
# ---------------------------------------------------------------------------

class TestNestedSubselect:
    def test_nested_subselect_detected_by_parser(self):
        mdx = (
            "SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM (SELECT {[Date].[Year].&[2025]} ON COLUMNS "
            "FROM (SELECT {[Region].[Country].&[US]} ON COLUMNS FROM [Sales]))"
        )
        r = parse_mdx(mdx)
        assert r.subselect is not None

    def test_two_level_subselect_filters_merged(self):
        from src.dax.xmla_server import _mdx_extract_subselect_filters
        mdx = (
            "SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM (SELECT {[Date].[Year].&[2025]} ON 0 "
            "FROM (SELECT {[Region].[Country].&[US]} ON 0 FROM [Sales]))"
        )
        dim_names = {"Date", "Region"}
        filters = _mdx_extract_subselect_filters(mdx, dim_names)
        assert "Date" in filters
        assert "2025" in filters["Date"]
        assert "Region" in filters
        assert "US" in filters["Region"]

    def test_three_level_subselect_filters_merged(self):
        from src.dax.xmla_server import _mdx_extract_subselect_filters
        mdx = (
            "SELECT {[Measures].[Sales]} ON COLUMNS "
            "FROM (SELECT {[Product].[Category].&[Electronics]} ON 0 "
            "FROM (SELECT {[Date].[Year].&[2025]} ON 0 "
            "FROM (SELECT {[Region].[Country].&[US]} ON 0 FROM [Sales])))"
        )
        dim_names = {"Date", "Region", "Product"}
        filters = _mdx_extract_subselect_filters(mdx, dim_names)
        assert len(filters) == 3
        assert "US" in filters["Region"]
        assert "2025" in filters["Date"]
        assert "Electronics" in filters["Product"]


# ---------------------------------------------------------------------------
# Bug-489 — Circular WITH MEMBER detection
# ---------------------------------------------------------------------------

class TestCircularWithMember:
    def test_simple_cycle_detected(self):
        calcs = [
            CalcMember(
                name="A",
                expression="[Measures].[B] * 2",
                calc_type="arithmetic",
                base_measure="B",
            ),
            CalcMember(
                name="B",
                expression="[Measures].[A] + 1",
                calc_type="arithmetic",
                base_measure="A",
            ),
        ]
        with pytest.raises(ValueError, match="Circular"):
            _check_circular_references(calcs)

    def test_transitive_cycle_detected(self):
        calcs = [
            CalcMember(name="A", expression="[Measures].[B] * 2",
                       calc_type="arithmetic", base_measure="B"),
            CalcMember(name="B", expression="[Measures].[C] + 1",
                       calc_type="arithmetic", base_measure="C"),
            CalcMember(name="C", expression="[Measures].[A] - 3",
                       calc_type="arithmetic", base_measure="A"),
        ]
        with pytest.raises(ValueError, match="Circular"):
            _check_circular_references(calcs)

    def test_no_cycle_dependency_order(self):
        calcs = [
            CalcMember(name="Margin", expression="[Measures].[Revenue] - [Measures].[Cost]",
                       calc_type="arithmetic", base_measure="Revenue"),
            CalcMember(name="MarginPct",
                       expression="[Measures].[Margin] / [Measures].[Revenue]",
                       calc_type="arithmetic", base_measure="Margin"),
        ]
        ordered = _check_circular_references(calcs)
        names = [c.name for c in ordered]
        assert names.index("Margin") < names.index("MarginPct")

    def test_no_cycle_independent_members(self):
        calcs = [
            CalcMember(name="TaxRate", expression="[Measures].[Tax] / [Measures].[Revenue]",
                       calc_type="arithmetic", base_measure="Tax"),
            CalcMember(name="Margin", expression="[Measures].[Revenue] - [Measures].[Cost]",
                       calc_type="arithmetic", base_measure="Revenue"),
        ]
        ordered = _check_circular_references(calcs)
        assert len(ordered) == 2

    def test_single_member_no_cycle(self):
        calcs = [
            CalcMember(name="Pct", expression="[Measures].[Amount] / 100",
                       calc_type="arithmetic", base_measure="Amount"),
        ]
        ordered = _check_circular_references(calcs)
        assert len(ordered) == 1

    def test_self_reference_detected(self):
        calcs = [
            CalcMember(name="Loop", expression="[Measures].[Loop] + 1",
                       calc_type="arithmetic", base_measure="Loop"),
        ]
        with pytest.raises(ValueError, match="Circular"):
            _check_circular_references(calcs)

    def test_case_variant_self_reference_detected(self):
        calcs = [
            CalcMember(name="Loop", expression="[Measures].[loop] + 1",
                       calc_type="arithmetic", base_measure="loop"),
        ]
        with pytest.raises(ValueError, match="Circular"):
            _check_circular_references(calcs)

    def test_case_variant_cycle_detected(self):
        calcs = [
            CalcMember(name="Alpha", expression="[Measures].[beta] * 2",
                       calc_type="arithmetic", base_measure="beta"),
            CalcMember(name="Beta", expression="[Measures].[ALPHA] + 1",
                       calc_type="arithmetic", base_measure="ALPHA"),
        ]
        with pytest.raises(ValueError, match="Circular"):
            _check_circular_references(calcs)

    def test_circular_through_evaluate(self):
        calcs = [
            CalcMember(name="A", expression="[Measures].[B] * 2",
                       calc_type="arithmetic", base_measure="B"),
            CalcMember(name="B", expression="[Measures].[A] + 1",
                       calc_type="arithmetic", base_measure="A"),
        ]
        rows = [{"Revenue": 100, "Cost": 40}]
        with pytest.raises(ValueError, match="Circular"):
            evaluate_calc_members(calcs, rows, ["Revenue", "Cost"], [])


# ---------------------------------------------------------------------------
# TopCount / BottomCount extraction
# ---------------------------------------------------------------------------

class TestTopNExtraction:
    def test_topcount_basic(self):
        axis = "TopCount({[Region].[Country].Members}, 10, [Measures].[Sales])"
        spec = _extract_topn_spec(axis)
        assert spec is not None
        assert spec.count == 10
        assert spec.measure == "Sales"
        assert spec.descending is True

    def test_bottomcount_basic(self):
        axis = "BottomCount({[Region].[Country].Members}, 5, [Measures].[Cost])"
        spec = _extract_topn_spec(axis)
        assert spec is not None
        assert spec.count == 5
        assert spec.measure == "Cost"
        assert spec.descending is False

    def test_topcount_case_insensitive(self):
        axis = "TOPCOUNT({[Region].[Country].Members}, 3, [Measures].[Revenue])"
        spec = _extract_topn_spec(axis)
        assert spec is not None
        assert spec.count == 3
        assert spec.measure == "Revenue"
        assert spec.descending is True

    def test_no_topn_in_plain_members(self):
        axis = "{[Region].[Country].Members}"
        spec = _extract_topn_spec(axis)
        assert spec is None

    def test_topcount_with_hierarchize_wrapper(self):
        axis = "Hierarchize(TopCount({[Region].[Country].Members}, 7, [Measures].[Amount]))"
        spec = _extract_topn_spec(axis)
        assert spec is not None
        assert spec.count == 7
        assert spec.measure == "Amount"


# ---------------------------------------------------------------------------
# Filter extraction
# ---------------------------------------------------------------------------

class TestFilterExtraction:
    def test_filter_greater_than(self):
        axis = "Filter({[Region].[Country].Members}, [Measures].[Sales] > 100)"
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is not None
        assert len(spec.conditions) == 1
        assert spec.conditions[0].measure == "Sales"
        assert spec.conditions[0].operator == ">"
        assert spec.conditions[0].value == "100"

    def test_filter_greater_or_equal(self):
        axis = "Filter({[Region].[Country].Members}, [Measures].[Amount] >= 50.5)"
        spec = _extract_filter_spec(axis, {"Amount"})
        assert spec is not None
        assert spec.conditions[0].operator == ">="
        assert spec.conditions[0].value == "50.5"

    def test_filter_less_than(self):
        axis = "Filter({[Region].[Country].Members}, [Measures].[Cost] < 200)"
        spec = _extract_filter_spec(axis, {"Cost"})
        assert spec is not None
        assert spec.conditions[0].operator == "<"

    def test_filter_not_equal(self):
        axis = "Filter({[Region].[Country].Members}, [Measures].[Sales] <> 0)"
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is not None
        assert spec.conditions[0].operator == "<>"

    def test_filter_measure_not_in_set(self):
        axis = "Filter({[Region].[Country].Members}, [Measures].[Unknown] > 100)"
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is None

    def test_filter_case_insensitive_measure_match(self):
        axis = "Filter({[Region].[Country].Members}, [Measures].[sales] > 100)"
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is not None
        assert spec.conditions[0].measure == "sales"

    def test_filter_multiple_conditions_all_captured(self):
        """Multi-condition filters (the compiler joins with one AND/OR) must
        capture every predicate and the joining logic — not just the first."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 100 AND [Measures].[Cost] < 50)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is not None
        assert spec.logic == "AND"
        assert len(spec.conditions) == 2
        assert spec.conditions[0].measure == "Sales"
        assert spec.conditions[1].measure == "Cost"
        assert spec.conditions[1].operator == "<"
        assert spec.conditions[1].value == "50"

    def test_filter_mixed_and_or_rejected(self):
        """A mixed AND/OR body cannot be flattened to one HAVING without
        changing membership semantics — defer to the fail-loud guard."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 100 AND [Measures].[Cost] < 50 "
            "OR [Measures].[Revenue] > 10)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost", "Revenue"})
        assert spec is None

    def test_filter_unknown_measure_in_multi_defers(self):
        """If any predicate references an unknown measure the spec is None so
        the fail-loud guard rejects the whole Filter (no partial translation)."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 100 AND [Measures].[Unknown] < 50)"
        )
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is None

    def test_filter_multiple_or_conditions_use_or_logic(self):
        """An all-numeric OR-joined filter keeps OR as the HAVING joiner."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 100 OR [Measures].[Cost] < 50)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is not None
        assert spec.logic == "OR"
        assert len(spec.conditions) == 2

    def test_filter_mixed_numeric_and_string_predicate_defers(self):
        """A string-valued measure predicate cannot be expressed as an aggregate
        HAVING threshold; mixing it with a numeric one must NOT silently drop the
        string clause — the spec is None so the fail-loud guard rejects it."""
        axis = (
            'Filter({[Region].[Country].Members}, '
            '[Measures].[Sales] > 100 AND [Measures].[Grade] = "A")'
        )
        spec = _extract_filter_spec(axis, {"Sales", "Grade"})
        assert spec is None

    def test_filter_outer_wrapper_does_not_over_capture_predicate(self):
        """An outer wrapper carrying its own later argument must not leak that
        argument into the Filter condition — only predicates inside the Filter
        call's own parentheses are captured (paren-depth bounded, not rfind)."""
        axis = (
            "OuterFn(Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 100), [Measures].[Grade] > 5)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Grade"})
        assert spec is not None
        assert len(spec.conditions) == 1
        assert spec.conditions[0].measure == "Sales"

    def test_no_filter_in_plain_axis(self):
        axis = "{[Region].[Country].Members}"
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is None

    # -- Bug-5505: wrapped measure operands inside Filter() --------------------

    def test_filter_paren_wrapped_measure_operand(self):
        """`Filter(set, ([Measures].[net_sales]) > 5)` must translate to the same
        single-value filter as the bare form, not a SOAP Fault."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "([Measures].[net_sales]) > 5)"
        )
        spec = _extract_filter_spec(axis, {"net_sales"})
        assert spec is not None
        assert len(spec.conditions) == 1
        assert spec.conditions[0].measure == "net_sales"
        assert spec.conditions[0].operator == ">"
        assert spec.conditions[0].value == "5"

    def test_filter_double_paren_wrapped_measure_operand(self):
        axis = (
            "Filter({[Region].[Country].Members}, "
            "(( [Measures].[net_sales] )) >= 10)"
        )
        spec = _extract_filter_spec(axis, {"net_sales"})
        assert spec is not None
        assert spec.conditions[0].operator == ">="
        assert spec.conditions[0].value == "10"

    def test_filter_function_wrapped_measure_operand(self):
        """Bug-5523: `Filter(set, CoalesceEmpty([Measures].[m], 0) > 5)` is NOT
        collapsed to the bare form. It must capture the coalesce default so the
        HAVING can render `COALESCE(AGG(m), 0) > 5` and reproduce MDX's empty/null
        handling — for `> 5` the result is identical to bare, but the spec must
        still carry the default so non-neutral operators (`= 0`, `<= 0`) render
        correctly through the same path."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "CoalesceEmpty([Measures].[net_sales], 0) > 5)"
        )
        spec = _extract_filter_spec(axis, {"net_sales"})
        assert spec is not None
        assert len(spec.conditions) == 1
        assert spec.conditions[0].measure == "net_sales"
        assert spec.conditions[0].operator == ">"
        assert spec.conditions[0].value == "5"
        assert spec.conditions[0].coalesce_default == "0"

    def test_filter_wrapped_unknown_measure_still_defers(self):
        """A wrapped operand referencing an unknown measure must still defer to
        the fail-loud guard (no partial / wrong translation)."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "CoalesceEmpty([Measures].[Unknown], 0) > 5)"
        )
        spec = _extract_filter_spec(axis, {"net_sales"})
        assert spec is None

    def test_filter_wrapped_multi_condition_all_captured(self):
        """Wrapped operands joined with AND must each translate, mirroring the
        bare multi-condition path."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "([Measures].[Sales]) > 100 AND CoalesceEmpty([Measures].[Cost], 0) < 50)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is not None
        assert spec.logic == "AND"
        assert len(spec.conditions) == 2
        assert {c.measure for c in spec.conditions} == {"Sales", "Cost"}
        by_measure = {c.measure: c for c in spec.conditions}
        # Bug-5523: the paren-wrapped Sales operand is neutral (no default); the
        # CoalesceEmpty Cost operand must preserve its default for the HAVING.
        assert by_measure["Sales"].coalesce_default is None
        assert by_measure["Cost"].coalesce_default == "0"

    def test_set_wrapped_axis_measure_not_unwrapped(self):
        """A genuine set-wrapped axis measure `{[Measures].[m]}` is never adjacent
        to a comparison operator, so it must NOT be collapsed into a predicate."""
        from src.dax.xmla_server import _normalize_wrapped_filter_operands
        cond = "{[Measures].[net_sales]}"
        assert _normalize_wrapped_filter_operands(cond) == cond

    # Bug-5523 hardening (Codex finding): an unconsumed CoalesceEmpty-on-a-measure
    # operand on the RIGHT of the comparison, in a reversed value-op-function form,
    # or NESTED inside another scalar function must make the whole Filter() fail
    # loud (None spec → SOAP fault in `_mdx_to_sql`). The earlier guard only
    # detected a coalesce IMMEDIATELY followed by an operator, so a DIFFERENT bare
    # predicate could be extracted and the coalesce silently dropped — a
    # partial/wrong spec instead of a fail-loud rejection.

    def test_filter_coalesce_on_right_with_bare_left_fails_loud(self):
        """Case 1: a bare `[Sales] > 0` AND a coalesce on the RIGHT of the
        comparison (`5 < CoalesceEmpty([Cost], 0)`). The bare predicate must NOT be
        extracted on its own while the coalesce predicate is dropped — the whole
        Filter() must fail loud."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 0 AND 5 < CoalesceEmpty([Measures].[Cost], 0))"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is None

    def test_filter_reversed_value_op_coalesce_fails_loud(self):
        """Case 2: a reversed `value op CoalesceEmpty([m], d)` form. The coalesce
        numeric matcher only consumes the `Coalesce(m, d) op value` order, so this
        operand is unconsumed and must fail loud rather than yield an empty/partial
        spec."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "5 < CoalesceEmpty([Measures].[Cost], 0))"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is None

    # Bug-5523 (Codex round 3): the WHOLE-CONDITION CONSUMED-SPAN CHECK. The
    # earlier per-shape occurrence count guards only counted coalesce/bare
    # comparisons *on a measure*, so a coalesce on a NON-measure attribute, an
    # unhandled scalar function, or any other sub-condition under AND/OR could be
    # left UNCONSUMED while a recognised predicate was still returned — a partial
    # spec that silently dropped the unrecognised clause. The consumed-span check
    # blanks every recognised predicate, strips only AND/OR/parens/whitespace, and
    # rejects any residual content, closing the whole leak class.

    def test_filter_coalesce_on_non_measure_attribute_fails_loud(self):
        """Codex round 3 headline case: `[Sales] > 0 AND CoalesceEmpty([Dim].[Attr],
        0) > 5`. The coalesce is on a NON-measure attribute, which the coalesce
        matcher (measure-only) never consumes — the earlier count guard counted only
        coalesce-on-a-measure and so left this clause unconsumed while returning a
        partial Sales-only spec. The consumed-span check must now reject the whole
        Filter() (None spec → SOAP fault)."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 0 AND CoalesceEmpty([Dim].[Attr], 0) > 5)"
        )
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is None

    def test_filter_bare_left_and_coalesce_right_fails_loud(self):
        """`[Sales] > 0 AND 5 < CoalesceEmpty([Measures].[Cost], 0)` — the coalesce
        sits on the RIGHT of the comparison (reversed order the matcher cannot
        consume). The Sales clause alone must NOT be returned; the consumed-span
        check leaves the reversed-coalesce text as residual and fails loud."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 0 AND 5 < CoalesceEmpty([Measures].[Cost], 0))"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is None

    def test_filter_nested_coalesce_in_scalar_function_fails_loud(self):
        """A coalesce NESTED inside another scalar function
        (`Abs(CoalesceEmpty([m], 0)) > 5`) is not a recognised predicate shape; the
        `Abs(...)` wrapper survives as residual and the whole Filter() fails loud."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "Abs(CoalesceEmpty([Measures].[Cost], 0)) > 5)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is None

    def test_filter_and_with_unhandled_scalar_function_fails_loud(self):
        """An AND where one side is an unrecognised scalar-function comparison
        (`Abs([Measures].[Cost]) > 5`) must fail loud — only the bare Sales clause is
        consumed, the `Abs(...)` comparison is residual."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 0 AND Abs([Measures].[Cost]) > 5)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is None

    def test_filter_and_with_dimension_member_predicate_fails_loud(self):
        """An AND with a dimension-attribute comparison that is not a measure
        threshold (`[Dim].[Attr] = "X"`) leaves a residual member reference and must
        fail loud rather than translate only the Sales clause."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            '[Measures].[Sales] > 0 AND [Dim].[Attr] = "X")'
        )
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is None

    def test_filter_or_with_unrecognised_side_fails_loud(self):
        """The leak class is not AND-specific: an OR where one side is unrecognised
        (`CoalesceEmpty([Dim].[Attr], 0) > 5`) must also fail loud."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 0 OR CoalesceEmpty([Dim].[Attr], 0) > 5)"
        )
        spec = _extract_filter_spec(axis, {"Sales"})
        assert spec is None

    def test_filter_consumed_span_check_keeps_supported_multi_coalesce(self):
        """No false fail-loud regression: a multi-condition AND of a bare measure
        and a measure-coalesce predicate (a form already supported) must still
        succeed with the consumed-span check in place."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Sales] > 100 AND CoalesceEmpty([Measures].[Cost], 0) < 50)"
        )
        spec = _extract_filter_spec(axis, {"Sales", "Cost"})
        assert spec is not None
        assert spec.logic == "AND"
        assert len(spec.conditions) == 2
        by_measure = {c.measure: c for c in spec.conditions}
        assert by_measure["Sales"].coalesce_default is None
        assert by_measure["Cost"].coalesce_default == "0"

    def test_filter_measure_name_with_embedded_and_or_not_a_joiner(self):
        """A measure name that literally contains a standalone AND/OR word
        (`[Net AND Gross]`, `[Sub OR Total]`) must NOT be read as a boolean joiner.
        Both the residual check and the AND/OR joiner detection read from the
        span-blanked condition, so the bracket-embedded keyword (inside a consumed
        predicate span) is already blanked and cannot poison `logic_tokens` into a
        spurious mixed-AND/OR rejection."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Net AND Gross] > 5 OR [Measures].[Sub OR Total] < 9)"
        )
        spec = _extract_filter_spec(axis, {"Net AND Gross", "Sub OR Total"})
        assert spec is not None
        # The only genuine joiner is the OR between the two predicates.
        assert spec.logic == "OR"
        assert len(spec.conditions) == 2
        assert {c.measure for c in spec.conditions} == {"Net AND Gross", "Sub OR Total"}

    def test_filter_single_measure_name_with_embedded_and_keeps_and_default(self):
        """A single predicate on a measure whose name contains a standalone `AND`
        word must still produce a spec (default joiner AND, no second condition)."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "[Measures].[Net AND Gross] > 5)"
        )
        spec = _extract_filter_spec(axis, {"Net AND Gross"})
        assert spec is not None
        assert spec.logic == "AND"
        assert len(spec.conditions) == 1
        assert spec.conditions[0].measure == "Net AND Gross"

    def test_filter_coalesce_nested_in_function_fails_loud(self):
        """Case 3: a CoalesceEmpty nested inside another scalar function
        (`Abs(CoalesceEmpty([m], 0)) > 5`). The aggregate HAVING cannot express the
        outer function, so the operand is unconsumed and must fail loud."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "Abs(CoalesceEmpty([Measures].[m], 0)) > 5)"
        )
        spec = _extract_filter_spec(axis, {"m"})
        assert spec is None


# ---------------------------------------------------------------------------
# Validator allow_range / allow_topn / allow_filter flags
# ---------------------------------------------------------------------------

class TestValidatorAllowFlags:
    def test_allow_range_suppresses_range_error(self):
        expr = "{[Date].[Year].&[2020]:[Date].[Year].&[2025]}"
        check_unsupported_mdx_constructs(expr, "subselect", allow_range=True)

    def test_allow_filter_suppresses_filter_error(self):
        expr = "Filter([Region].[Country].Members, [Measures].[Sales] > 100)"
        check_unsupported_mdx_constructs(expr, "ROWS axis", allow_filter=True)

    def test_allow_filter_false_still_raises(self):
        expr = "Filter([Region].[Country].Members, [Measures].[Sales] > 100)"
        with pytest.raises(ValueError, match="Filter"):
            check_unsupported_mdx_constructs(expr, "ROWS axis", allow_filter=False)

    def test_allow_filter_does_not_suppress_other_functions(self):
        expr = "Except({[A].Members}, {[A].&[X]})"
        with pytest.raises(ValueError, match="Except"):
            check_unsupported_mdx_constructs(expr, "axis", allow_filter=True)

    def test_allow_topn_suppresses_topcount_error(self):
        expr = "TopCount({[Region].[Country].Members}, 10, [Measures].[Sales])"
        check_unsupported_mdx_constructs(expr, "ROWS axis", allow_topn=True)

    def test_topcount_rejected_when_not_allowed(self):
        expr = "TopCount({[Region].[Country].Members}, 10, [Measures].[Sales])"
        with pytest.raises(ValueError, match="TopCount"):
            check_unsupported_mdx_constructs(expr, "ROWS axis", allow_topn=False)


# ---------------------------------------------------------------------------
# F-002-05 — fail-loud on Top-N / Filter the translator cannot consume
# ---------------------------------------------------------------------------

class TestTopNFilterFailLoud:
    _MEAS = [{"name": "Sales", "default_agg": "sum"}]
    _DIMS = [{"name": "Country"}]

    def test_simple_topcount_produces_limit(self):
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "TopCount({[Country].[Country].[Country].Members}, 5, [Measures].[Sales]) ON ROWS "
            "FROM [demo]"
        )
        sql, proto = _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")
        assert proto == "jdbc"
        assert "LIMIT 5" in sql
        assert "ORDER BY" in sql

    def test_composite_topcount_first_arg_fails_loud(self):
        """TopCount(CrossJoin(...), N, ...) cannot be translated — must fault,
        not silently return an unfiltered (over-complete) result."""
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "TopCount(CrossJoin({[Country].[Country].[Country].Members}, "
            "{[Region].[Region].[Region].Members}), 5, [Measures].[Sales]) ON ROWS "
            "FROM [demo]"
        )
        with pytest.raises(ValueError, match="TopCount"):
            _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")

    def test_second_filter_call_fails_loud(self):
        """A second Filter() the extractor cannot consume must fault."""
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[Sales]} ON COLUMNS, "
            "Filter(Filter({[Country].[Country].[Country].Members}, "
            "[Measures].[Sales] > 100), [Measures].[Sales] < 900) ON ROWS "
            "FROM [demo]"
        )
        with pytest.raises(ValueError, match="Filter"):
            _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# Bug-5523 — CoalesceEmpty filter must preserve null/empty semantics
# ---------------------------------------------------------------------------

class TestCoalesceEmptyFilterSemantics:
    """A `CoalesceEmpty([Measures].[m], d) op value` operand replaces an
    empty/null measure cell with `d` BEFORE the comparison in MDX, so a group
    whose aggregate is NULL is tested as `d op value`. The earlier Bug-5505 fix
    stripped the wrapper to a bare `AGG(m) op value`, which evaluates NULL groups
    as unknown (excluded) — a different-cardinality result for `= 0`, `<= 0`,
    `< d`. The fix renders `COALESCE(AGG(m), d) op value` so SQL reproduces the
    MDX semantics for every operator.
    """

    _MEAS = [{"name": "net_sales", "default_agg": "sum"}]
    _DIMS = [{"name": "Country"}]

    def _sql(self, cond: str) -> str:
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            f"Filter({{[Country].[Country].[Country].Members}}, {cond}) ON ROWS "
            "FROM [demo]"
        )
        sql, proto = _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")
        assert proto == "jdbc"
        return sql

    def test_coalesce_eq_zero_includes_null_groups(self):
        """`= 0` is NOT neutral: MDX keeps NULL/empty groups (default 0 = 0). The
        HAVING must wrap the aggregate in COALESCE so those groups survive — a
        bare `SUM(...) = 0` would wrongly exclude them."""
        sql = self._sql("CoalesceEmpty([Measures].[net_sales], 0) = 0")
        assert "HAVING" in sql
        assert 'COALESCE(SUM("net_sales"), 0) = 0' in sql
        # Must NOT degrade to the unsafe bare rewrite.
        assert 'HAVING SUM("net_sales") = 0' not in sql

    def test_coalesce_leq_zero_includes_null_groups(self):
        """`<= 0` is NOT neutral with default 0 — same COALESCE requirement."""
        sql = self._sql("CoalesceEmpty([Measures].[net_sales], 0) <= 0")
        assert 'COALESCE(SUM("net_sales"), 0) <= 0' in sql
        assert 'HAVING SUM("net_sales") <= 0' not in sql

    def test_coalesce_gt_five_neutral_still_renders_coalesce(self):
        """`> 5` happens to be neutral (default 0 excludes NULL groups either
        way), but the fix renders COALESCE uniformly — correct for both cases and
        never the unsafe assumption-dependent rewrite."""
        sql = self._sql("CoalesceEmpty([Measures].[net_sales], 0) > 5")
        assert 'COALESCE(SUM("net_sales"), 0) > 5' in sql

    def test_coalesce_nonzero_default_rendered(self):
        """A non-zero coalesce default must flow through to the HAVING unchanged."""
        sql = self._sql("CoalesceEmpty([Measures].[net_sales], 7) >= 7")
        assert 'COALESCE(SUM("net_sales"), 7) >= 7' in sql

    def test_coalesce_nonnumeric_default_fails_loud(self):
        """A non-numeric coalesce default cannot be expressed as an aggregate
        threshold; rather than silently mistranslate, the whole Filter must
        fail loud (the 'Unsupported Filter() usage' SOAP fault)."""
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "Filter({[Country].[Country].[Country].Members}, "
            'CoalesceEmpty([Measures].[net_sales], "x") = 0) ON ROWS '
            "FROM [demo]"
        )
        with pytest.raises(ValueError, match="Filter"):
            _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")

    def test_coalesce_on_right_with_bare_left_raises_soap_fault(self):
        """Bug-5523 hardening (Codex case 1): a bare predicate must NOT be
        extracted on its own while an unconsumed coalesce on the RIGHT of the
        comparison is silently dropped. The whole Filter() must raise the
        'Unsupported Filter() usage' SOAP fault, never emit a partial HAVING."""
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "Filter({[Country].[Country].[Country].Members}, "
            "[Measures].[net_sales] > 0 AND 5 < CoalesceEmpty([Measures].[net_sales], 0)) "
            "ON ROWS FROM [demo]"
        )
        with pytest.raises(ValueError, match="Filter"):
            _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")

    def test_coalesce_reversed_value_op_fn_raises_soap_fault(self):
        """Bug-5523 hardening (Codex case 2): a reversed `value op
        CoalesceEmpty([m], d)` form is unconsumed and must fail loud."""
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "Filter({[Country].[Country].[Country].Members}, "
            "5 < CoalesceEmpty([Measures].[net_sales], 0)) "
            "ON ROWS FROM [demo]"
        )
        with pytest.raises(ValueError, match="Filter"):
            _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")

    def test_coalesce_nested_in_function_raises_soap_fault(self):
        """Bug-5523 hardening (Codex case 3): a CoalesceEmpty nested inside another
        scalar function (`Abs(CoalesceEmpty([m], 0)) > 5`) cannot be expressed by
        the aggregate HAVING and must fail loud."""
        from src.dax.xmla_server import _mdx_to_sql
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "Filter({[Country].[Country].[Country].Members}, "
            "Abs(CoalesceEmpty([Measures].[net_sales], 0)) > 5) "
            "ON ROWS FROM [demo]"
        )
        with pytest.raises(ValueError, match="Filter"):
            _mdx_to_sql(mdx, self._MEAS, self._DIMS, model_slug="demo")

    def test_coalesce_spec_carries_default_eq_zero(self):
        """Unit-level: the extracted spec keeps the default and the raw operator
        for `= 0`, so the HAVING render is driven by real semantics."""
        axis = (
            "Filter({[Region].[Country].Members}, "
            "CoalesceEmpty([Measures].[net_sales], 0) = 0)"
        )
        spec = _extract_filter_spec(axis, {"net_sales"})
        assert spec is not None
        assert len(spec.conditions) == 1
        assert spec.conditions[0].operator == "="
        assert spec.conditions[0].value == "0"
        assert spec.conditions[0].coalesce_default == "0"


# ---------------------------------------------------------------------------
# F-002-12 — DAX string literals keep string typing
# ---------------------------------------------------------------------------

class TestSqlLiteralStringTyping:
    def test_numeric_looking_string_stays_quoted(self):
        from src.dax.xmla_server import _sql_literal
        # Parser saw a quoted literal — must stay quoted even though it is digits.
        assert _sql_literal("00123", is_string=True) == "'00123'"
        assert _sql_literal("true", is_string=True) == "'true'"

    def test_unquoted_numeric_emits_bare(self):
        from src.dax.xmla_server import _sql_literal
        assert _sql_literal("123", is_string=False) == "123"
        assert _sql_literal("12.5", is_string=False) == "12.5"

    def test_dax_string_filter_on_numeric_code_is_quoted(self):
        from src.dax.xmla_server import _dax_to_sql
        # CALCULATETABLE with a zero-padded string code filter.
        dax = 'EVALUATE CALCULATETABLE(VALUES(Orders[Country]), Orders[Code] = "00123")'
        measures = [{"name": "Sales", "default_agg": "sum"}]
        dims = [{"name": "Country"}, {"name": "Code"}]
        sql, _ = _dax_to_sql(dax, measures, dims, model_slug="demo")
        assert "'00123'" in sql
        assert "= 00123" not in sql


# ---------------------------------------------------------------------------
# Task 1.8 — Date grouping and custom grouping
# ---------------------------------------------------------------------------

class TestDateGroupingMDX:
    """Date grouping via date_embedded hierarchy level references."""

    def test_date_hierarchy_levels_parsed(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS, "
            "NON EMPTY {[Date].[Calendar].[Year].Members} ON ROWS "
            "FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert len(r.axes) == 2
        assert "[Date].[Calendar].[Year]" in r.axes[1].raw_expr

    def test_date_year_quarter_crossjoin(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS, "
            "NON EMPTY CrossJoin("
            "{[Date].[Calendar].[Year].Members}, "
            "{[Date].[Calendar].[Quarter].Members}) ON ROWS "
            "FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert "CrossJoin" in r.axes[1].raw_expr
        assert "[Year]" in r.axes[1].raw_expr
        assert "[Quarter]" in r.axes[1].raw_expr

    def test_date_drilldown_level(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS, "
            "DrilldownLevel({[Date].[Calendar].[All]}) ON ROWS "
            "FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert "DrilldownLevel" in r.axes[1].raw_expr

    def test_date_subselect_range_filter(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS, "
            "NON EMPTY {[Date].[Calendar].[Month].Members} ON ROWS "
            "FROM (SELECT {[Date].[Calendar].[Year].&[2024]:"
            "[Date].[Calendar].[Year].&[2025]} ON COLUMNS FROM [Sales])"
        )
        r = parse_mdx(mdx)
        assert r.subselect is not None
        check_unsupported_mdx_constructs(
            r.subselect.axes[0].raw_expr, "subselect", allow_range=True,
        )


class TestCustomGroupingMDX:
    """Custom grouping via WITH MEMBER Aggregate pattern.

    Excel custom grouping generates WITH MEMBER on a dimension (not [Measures]),
    e.g. WITH MEMBER [Geography].[Custom Group] AS Aggregate({...}).
    The evaluator creates a synthetic row with aggregated values.
    """

    def test_dimension_aggregate_classifies_as_aggregate_set(self):
        mdx = (
            "WITH MEMBER [Geography].[Custom Group] AS "
            "Aggregate({[Geography].[France], [Geography].[Germany]}) "
            "SELECT {[Measures].[Revenue]} ON COLUMNS, "
            "{[Geography].[Custom Group]} ON ROWS "
            "FROM [Sales]"
        )
        r = parse_mdx(mdx)
        assert len(r.with_members) == 1
        assert "Aggregate" in r.with_members[0].expression
        calcs = parse_calc_members(r.with_members)
        assert calcs[0].calc_type == "aggregate_set"
        assert calcs[0].dim_name == "Geography"
        assert calcs[0].name == "Custom Group"
        assert set(calcs[0].aggregate_members) == {"France", "Germany"}

    def test_measure_aggregate_classifies_as_custom(self):
        calcs = parse_calc_members([_FakeWithMember(
            "[Measures].[Group Total]",
            "Aggregate({[Region].[US], [Region].[Canada]})",
        )])
        assert calcs[0].calc_type == "custom"


class _FakeWithMember:
    """Minimal stand-in for WithMemberDef for unit test convenience."""
    def __init__(self, name: str, expression: str, props: dict | None = None):
        self.name = name
        self.expression = expression
        self.properties = props or {}


# ---------------------------------------------------------------------------
# Task 1.6 — GETPIVOTDATA point queries
# ---------------------------------------------------------------------------

class TestGetPivotData:
    """GETPIVOTDATA generates point query MDX with a single measure on COLUMNS
    and dimension member filters in the WHERE clause."""

    def test_single_measure_single_where(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS "
            "FROM [Sales] WHERE ([Geography].[Country].&[France])"
        )
        r = parse_mdx(mdx)
        assert r.cube_name == "Sales"
        assert len(r.axes) == 1
        assert "[Measures].[Revenue]" in r.axes[0].raw_expr
        assert len(r.where_members) == 1

    def test_multi_dimension_where(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS "
            "FROM [Sales] WHERE ("
            "[Geography].[Country].&[France], "
            "[Date].[Calendar].[Year].&[2024])"
        )
        r = parse_mdx(mdx)
        assert len(r.where_members) == 2

    def test_hierarchy_level_qualified_where(self):
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS "
            "FROM [Sales] WHERE ("
            "[Date].[Calendar].[Year].&[2024].[Quarter].&[Q1])"
        )
        r = parse_mdx(mdx)
        assert len(r.where_members) >= 1

    def test_no_rows_axis_is_valid(self):
        mdx = (
            "SELECT {[Measures].[Cost]} ON COLUMNS "
            "FROM [Sales] WHERE ([Product].[Category].&[Electronics])"
        )
        r = parse_mdx(mdx)
        assert len(r.axes) == 1
        assert r.axes[0].axis_name == "COLUMNS"

    def test_multiple_measures_in_getpivotdata(self):
        mdx = (
            "SELECT {[Measures].[Revenue], [Measures].[Cost]} ON COLUMNS "
            "FROM [Sales] WHERE ([Geography].[Country].&[US])"
        )
        r = parse_mdx(mdx)
        assert "[Revenue]" in r.axes[0].raw_expr
        assert "[Cost]" in r.axes[0].raw_expr
        assert len(r.where_members) == 1


# ---------------------------------------------------------------------------
# Label filter extraction (Task 1.12)
# ---------------------------------------------------------------------------

class TestLabelFilterExtraction:
    """Tests for _translate_label_filter_calls — Begins With, Contains, Ends With."""

    _DIM_NAMES = {"country_name", "region"}
    _HIER_MAP = {"geography": {"country": "country_name"}}
    _DEFAULT_MAP = {"geography": "country_name"}

    def test_begins_with(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'Left([Geography].CurrentMember.Name, 3) = "Uni")'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].operation == "begins_with"
        assert specs[0].value == "Uni"
        assert specs[0].negated is False

    def test_does_not_begin_with(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'Left([Geography].CurrentMember.Name, 5) <> "China")'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].operation == "begins_with"
        assert specs[0].negated is True

    def test_contains(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'InStr([Geography].CurrentMember.Name, "land") > 0)'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].operation == "contains"
        assert specs[0].value == "land"
        assert specs[0].negated is False

    def test_does_not_contain(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'InStr([Geography].CurrentMember.Name, "land") = 0)'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].operation == "contains"
        assert specs[0].negated is True

    def test_ends_with(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'Right([Geography].CurrentMember.Name, 2) = "ia")'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].operation == "ends_with"
        assert specs[0].value == "ia"
        assert specs[0].negated is False

    def test_does_not_end_with(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'Right([Geography].CurrentMember.Name, 2) <> "ia")'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].operation == "ends_with"
        assert specs[0].negated is True

    def test_no_label_filter_in_value_filter(self):
        axis = (
            'Filter([Geography].Members, [Measures].[Sales] > 1000)'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 0

    def test_case_insensitive_function_names(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'LEFT([Geography].CurrentMember.Name, 3) = "Uni")'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1

    def test_hierarchy_qualified_dim_ref(self):
        axis = (
            'Filter([Geography].[Geography].Members, '
            'InStr([Geography].[Geography].CurrentMember.Name, "US") > 0)'
        )
        specs = _translate_label_filter_calls(
            axis, self._DIM_NAMES, self._HIER_MAP, self._DEFAULT_MAP,
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].dim_ref == "country_name"


class TestLabelFilterToSql:
    """Tests for _label_filter_to_sql — LIKE/NOT LIKE generation."""

    def _q(self, name):
        return f'"{name}"'

    def test_begins_with_like(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="country", operation="begins_with",
                                value="Uni", negated=False)
        sql = _label_filter_to_sql(spec, self._q)
        assert sql == "LOWER(\"country\") LIKE 'uni%' ESCAPE '\\'"

    def test_contains_like(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="country", operation="contains",
                                value="land", negated=False)
        sql = _label_filter_to_sql(spec, self._q)
        assert sql == "LOWER(\"country\") LIKE '%land%' ESCAPE '\\'"

    def test_ends_with_like(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="country", operation="ends_with",
                                value="ia", negated=False)
        sql = _label_filter_to_sql(spec, self._q)
        assert sql == "LOWER(\"country\") LIKE '%ia' ESCAPE '\\'"

    def test_not_contains(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="country", operation="contains",
                                value="land", negated=True)
        sql = _label_filter_to_sql(spec, self._q)
        assert sql == "LOWER(\"country\") NOT LIKE '%land%' ESCAPE '\\'"

    def test_special_chars_escaped(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="col", operation="contains",
                                value="100%", negated=False)
        sql = _label_filter_to_sql(spec, self._q)
        assert "100\\%" in sql

    def test_mixed_case_value_lowered(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="country", operation="contains",
                                value="LAND", negated=False)
        sql = _label_filter_to_sql(spec, self._q)
        assert sql == "LOWER(\"country\") LIKE '%land%' ESCAPE '\\'"

    def test_single_quote_escaped(self):
        from src.dax.xmla_server import _LabelFilterSpec
        spec = _LabelFilterSpec(dim_ref="col", operation="begins_with",
                                value="O'Brien", negated=False)
        sql = _label_filter_to_sql(spec, self._q)
        assert "o''brien" in sql


# ---------------------------------------------------------------------------
# Bug-587 — Subselect slicer filter extraction
# ---------------------------------------------------------------------------

class TestSubselectFilterExtraction:

    DIM_NAMES = {"Geography", "Product", "Year"}
    LEVEL_DIM_MAP = {"geography": {"geography": "Geography", "country": "Geography"}}
    DEFAULT_DIM_MAP = {"geography": "Geography"}

    def test_single_member_subselect(self):
        from src.dax.xmla_server import _mdx_extract_subselect_filters
        mdx = (
            "SELECT {[Measures].[Amount]} ON 0, [Product].[Product].Members ON 1 "
            "FROM (SELECT {[Geography].[Geography].[France]} ON 0 FROM [demo])"
        )
        result = _mdx_extract_subselect_filters(
            mdx, self.DIM_NAMES,
            hierarchy_level_dim_map=self.LEVEL_DIM_MAP,
            hierarchy_default_dim_map=self.DEFAULT_DIM_MAP,
        )
        assert "Geography" in result
        assert "France" in result["Geography"]

    def test_multi_member_subselect(self):
        from src.dax.xmla_server import _mdx_extract_subselect_filters
        mdx = (
            "SELECT {[Measures].[Amount]} ON 0 "
            "FROM (SELECT {[Geography].[Geography].[France], "
            "[Geography].[Geography].[Germany]} ON 0 FROM [demo])"
        )
        result = _mdx_extract_subselect_filters(
            mdx, self.DIM_NAMES,
            hierarchy_level_dim_map=self.LEVEL_DIM_MAP,
            hierarchy_default_dim_map=self.DEFAULT_DIM_MAP,
        )
        assert "Geography" in result
        assert "France" in result["Geography"]
        assert "Germany" in result["Geography"]

    def test_no_subselect_returns_empty(self):
        from src.dax.xmla_server import _mdx_extract_subselect_filters
        mdx = "SELECT {[Measures].[Amount]} ON 0 FROM [demo] WHERE ([Geography].[Geography].[France])"
        result = _mdx_extract_subselect_filters(
            mdx, self.DIM_NAMES,
            hierarchy_level_dim_map=self.LEVEL_DIM_MAP,
            hierarchy_default_dim_map=self.DEFAULT_DIM_MAP,
        )
        assert result == {}


# ---------------------------------------------------------------------------
# B8 round 2 (deep-review Finding 4) — path-qualified member unames must
# round-trip through the WHERE/subselect/drilldown parsers. The server emits
# [Cal].[Cal].[Month].&[2025]&[4] on subtotal axes; when a client (Excel
# keep-only, report filter, timeline) echoes that uname back, the filter must
# resolve to month 4 OF year 2025 — not to "2025" applied as a month filter.
# ---------------------------------------------------------------------------

class TestPathQualifiedUnameRoundTrip:

    DIM_NAMES = {"business_date_year", "business_date_month", "country"}
    LEVEL_DIM_MAP = {
        "cal": {"year": "business_date_year", "month": "business_date_month"},
    }
    DEFAULT_DIM_MAP = {"cal": "business_date_month"}

    def _extract(self, where_expr):
        from src.dax.xmla_server import _mdx_extract_where_filters
        return _mdx_extract_where_filters(
            where_expr, self.DIM_NAMES,
            hierarchy_level_dim_map=self.LEVEL_DIM_MAP,
            hierarchy_default_dim_map=self.DEFAULT_DIM_MAP,
        )

    def test_composite_uname_filters_named_level_on_deepest_key(self):
        """The exact reviewer probe: ([Date].[Cal].[Month].&[2025]&[4])
        previously extracted {'business_date_month': ['2025']} — the year
        applied as a month filter."""
        filters = self._extract("([Date].[Cal].[Month].&[2025]&[4])")
        assert filters["business_date_month"] == ["4"]

    def test_composite_uname_filters_ancestor_levels_on_their_own_dims(self):
        """Keep-only on 'Apr 2025' must pin the year too — month 4 of 2025,
        not every April."""
        filters = self._extract("([Date].[Cal].[Month].&[2025]&[4])")
        assert filters["business_date_year"] == ["2025"]
        assert filters["business_date_month"] == ["4"]

    def test_single_key_uname_behaviour_unchanged(self):
        filters = self._extract("([Date].[Cal].[Month].&[4])")
        assert filters == {"business_date_month": ["4"]}

    def test_composite_uname_without_level_resolves_path_from_root(self):
        """[Date].[Cal].&[2025]&[4] — no level name: the path runs from the
        root level, so the named member is at the second level (Month)."""
        filters = self._extract("([Date].[Cal].&[2025]&[4])")
        assert filters["business_date_month"] == ["4"]
        assert filters["business_date_year"] == ["2025"]

    def test_composite_range_same_ancestor_pins_ancestor_and_ranges_deepest(self):
        """Timeline-style range within one year: months 4..6 of 2025."""
        filters = self._extract(
            "{[Date].[Cal].[Month].&[2025]&[4]:[Date].[Cal].[Month].&[2025]&[6]}"
        )
        assert filters["business_date_month"] == [f"{_RANGE_PREFIX}4{_RANGE_SEP}6"]
        assert filters["business_date_year"] == ["2025"]

    def test_single_key_range_behaviour_unchanged(self):
        filters = self._extract(
            "{[Date].[Cal].[Month].&[202501]:[Date].[Cal].[Month].&[202503]}"
        )
        assert filters == {"business_date_month": [f"{_RANGE_PREFIX}202501{_RANGE_SEP}202503"]}

    def test_multi_select_composite_unames_within_one_ancestor(self):
        """Excel keep-only of two months of the same year."""
        filters = self._extract(
            "{[Date].[Cal].[Month].&[2025]&[4], [Date].[Cal].[Month].&[2025]&[5]}"
        )
        assert filters["business_date_month"] == ["4", "5"]
        assert filters["business_date_year"] == ["2025"]

    def test_drilldown_member_expansion_uses_deepest_key(self):
        """DrilldownMember echoing a server-emitted path-qualified uname must
        expand the member named by the deepest key, not the level caption."""
        from src.dax.mdx_execute import _extract_drilldown_member_expansions
        expr = (
            "DrilldownMember({{DrilldownLevel({[Date].[Cal].[(All)]})}}, "
            "{[Date].[Cal].[Year].&[2025]})"
        )
        result = _extract_drilldown_member_expansions(expr)
        assert result == {"[Date].[Cal]": ["2025"]}

    def test_drilldown_member_expansion_composite_key_path(self):
        from src.dax.mdx_execute import _extract_drilldown_member_expansions
        expr = (
            "DrilldownMember({{DrilldownLevel({[Date].[Cal].[(All)]})}}, "
            "{[Date].[Cal].[Month].&[2025]&[4]})"
        )
        result = _extract_drilldown_member_expansions(expr)
        assert result == {"[Date].[Cal]": ["4"]}

    def test_drilldown_member_expansion_caption_form_unchanged(self):
        from src.dax.mdx_execute import _extract_drilldown_member_expansions
        expr = (
            "DrilldownMember({{DrilldownLevel({[Time].[Time].[All]})}}, "
            "{[Time].[Time].[2024]})"
        )
        result = _extract_drilldown_member_expansions(expr)
        assert result == {"[Time].[Time]": ["2024"]}


# ---------------------------------------------------------------------------
# B8 round-3, Bug-1051 — composite key paths must not bypass the member
# range validator: a WHERE-clause range gets the same clean rejection
# whether its endpoints carry one key or a full ancestor path.
# ---------------------------------------------------------------------------

class TestCompositeRangeValidator:
    def test_composite_range_rejected_in_where(self):
        with pytest.raises(ValueError, match="range"):
            check_unsupported_mdx_constructs(
                "([Date].[Cal].[Month].&[2025]&[5]:[Date].[Cal].[Month].&[2025]&[7])",
                "WHERE clause",
            )

    def test_composite_range_without_level_rejected(self):
        with pytest.raises(ValueError, match="range"):
            check_unsupported_mdx_constructs(
                "([Date].[Cal].&[2025]&[5]:[Date].[Cal].&[2025]&[7])",
                "WHERE clause",
            )

    def test_composite_range_allowed_where_ranges_are_supported(self):
        check_unsupported_mdx_constructs(
            "{[Date].[Cal].[Month].&[2025]&[5]:[Date].[Cal].[Month].&[2025]&[7]}",
            "subselect",
            allow_range=True,
        )

    def test_single_key_range_still_rejected(self):
        with pytest.raises(ValueError, match="range"):
            check_unsupported_mdx_constructs(
                "([Date].[Year].&[2020]:[Date].[Year].&[2025])",
                "WHERE clause",
            )

    def test_plain_composite_member_passes(self):
        check_unsupported_mdx_constructs(
            "([Date].[Cal].[Month].&[2025]&[5])", "WHERE clause",
        )


# ---------------------------------------------------------------------------
# B8 round-3, Bug-1052 — SSAS ``]]`` escape: a member key containing ``]``
# must round-trip through the unified grammar (emit -> parse).
# ---------------------------------------------------------------------------

class TestMemberKeyBracketEscape:
    def test_qualify_escapes_closing_bracket(self):
        from src.dax.member_uname import qualify_member_uname
        uname = qualify_member_uname("[geo].[geo]", "City", ["A]B"])
        assert uname == "[geo].[geo].[City].&[A]]B]"

    def test_round_trip_key_containing_bracket(self):
        from src.dax.member_uname import (
            parse_member_keys, qualify_member_uname,
        )
        uname = qualify_member_uname("[geo].[geo]", "City", ["Ger]many", "Ber]lin"])
        keys_part = uname.split("].[City].")[1]
        assert parse_member_keys(keys_part) == ["Ger]many", "Ber]lin"]

    def test_deepest_key_with_escaped_bracket(self):
        from src.dax.member_uname import deepest_member_key
        assert deepest_member_key("&[2025]&[Q]]4]") == "Q]4"

    def test_caption_form_unescapes(self):
        from src.dax.member_uname import parse_member_keys
        assert parse_member_keys("[Apr]]il]") == ["Apr]il"]

    def test_adjacent_segments_do_not_merge(self):
        from src.dax.member_uname import parse_member_keys
        # ``&[A]]]`` is the escaped key ``A]`` — it must not swallow the
        # following segment.
        assert parse_member_keys("&[A]]]&[B]") == ["A]", "B"]

    def test_plain_keys_unaffected(self):
        from src.dax.member_uname import parse_member_keys, qualify_member_uname
        uname = qualify_member_uname("[Cal].[Cal]", "Month", ["2025", "4"])
        assert uname == "[Cal].[Cal].[Month].&[2025]&[4]"
        assert parse_member_keys("&[2025]&[4]") == ["2025", "4"]

    def test_where_extraction_with_escaped_key(self):
        """The shared regex fragments must carry the escape through the
        WHERE-clause parser end to end."""
        from src.dax.xmla_server import _mdx_extract_where_filters
        filters = _mdx_extract_where_filters(
            "([geo].[geo].[City].&[DE]&[Ber]]lin])",
            {"city", "country"},
            hierarchy_level_dim_map={
                "geo": {"country": "country", "city": "city"},
            },
            hierarchy_default_dim_map={"geo": "city"},
        )
        assert filters["city"] == ["Ber]lin"]
        assert filters["country"] == ["DE"]


# ---------------------------------------------------------------------------
# Bug-5514 — per-level .Members enumeration must return the requested level's
# grain, not the hierarchy leaf. The failing client form is the TWO-bracket
# abbreviation [Hierarchy].[Level].Members (Excel/Power BI collapse the
# canonical three-bracket [Dim].[Dim].[Level] when dim and hierarchy share a
# name). Each level's grain is a distinct backing dimension/column, so a level
# request must group by that level's key — Year -> d_year, Quarter -> d_qoy,
# Month -> d_moy, Date -> d_date (leaf).
# ---------------------------------------------------------------------------

class TestBug5514LevelGrainEnumeration:
    # UDA-backed time hierarchy: each level maps to its own grain dimension.
    UDA_LEVEL_DIM_MAP = {
        "sales_date_hierarchy": {
            "year": "d_year",
            "quarter": "d_qoy",
            "month": "d_moy",
            "date": "d_date",
        },
    }
    # Leaf (deepest) level is the default for a bare hierarchy reference.
    UDA_DEFAULT_DIM_MAP = {"sales_date_hierarchy": "d_date"}
    UDA_DIM_NAMES = {
        "Sales_Date_Hierarchy", "d_year", "d_qoy", "d_moy", "d_date", "year",
    }

    # Physical-column hierarchy: levels resolve straight to physical columns.
    PHYS_LEVEL_DIM_MAP = {
        "sales date (physical)": {
            "year": "d_year",
            "quarter": "d_qoy",
            "month": "d_moy",
            "date": "d_date",
        },
    }
    PHYS_DEFAULT_DIM_MAP = {"sales date (physical)": "d_date"}
    PHYS_DIM_NAMES = {
        "Sales Date (physical)", "d_year", "d_qoy", "d_moy", "d_date",
    }

    def _dims(self, expr, dim_names, level_map, default_map):
        from src.dax.xmla_server import _mdx_extract_dimensions
        return _mdx_extract_dimensions(
            expr,
            dim_names=dim_names,
            hierarchy_level_dim_map=level_map,
            hierarchy_default_dim_map=default_map,
        )

    def test_two_bracket_year_level_resolves_to_year_grain_uda(self):
        dims = self._dims(
            "[Sales_Date_Hierarchy].[Year].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        # Year level must group by d_year, NOT the leaf d_date.
        assert dims == ["d_year"]
        assert "d_date" not in dims

    def test_two_bracket_quarter_level_resolves_to_quarter_grain_uda(self):
        dims = self._dims(
            "[Sales_Date_Hierarchy].[Quarter].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        assert dims == ["d_qoy"]

    def test_two_bracket_month_level_resolves_to_month_grain_uda(self):
        dims = self._dims(
            "[Sales_Date_Hierarchy].[Month].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        assert dims == ["d_moy"]

    def test_two_bracket_year_level_resolves_to_year_grain_physical(self):
        dims = self._dims(
            "[Sales Date (physical)].[Year].Members",
            self.PHYS_DIM_NAMES, self.PHYS_LEVEL_DIM_MAP, self.PHYS_DEFAULT_DIM_MAP,
        )
        assert dims == ["d_year"]
        assert "d_date" not in dims

    def test_three_bracket_level_form_still_resolves_to_level_grain(self):
        # Canonical MDSCHEMA form must keep working (no regression).
        dims = self._dims(
            "[Sales_Date_Hierarchy].[Sales_Date_Hierarchy].[Year].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        assert dims == ["d_year"]

    def test_bare_hierarchy_reference_still_resolves_to_leaf_default(self):
        # A bare [Hierarchy].Members (no level) must still resolve to the leaf
        # grain — the fix must not hijack the default-member path.
        dims = self._dims(
            "[Sales_Date_Hierarchy].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        assert dims == ["d_date"]

    def test_flat_dim_members_unaffected(self):
        # CONTROL: a flat dimension [year].[year].Members must keep resolving to
        # its own grain regardless of the level-grain fix.
        dims = self._dims(
            "[year].[year].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        assert dims == ["year"]

    def test_second_segment_not_a_level_falls_back_to_dim(self):
        # [Hierarchy].[NotALevel] where the 2nd segment is neither a level nor a
        # known hierarchy must fall back to the hierarchy's own dim name.
        dims = self._dims(
            "[Sales_Date_Hierarchy].[Bogus].Members",
            self.UDA_DIM_NAMES, self.UDA_LEVEL_DIM_MAP, self.UDA_DEFAULT_DIM_MAP,
        )
        # Falls through to the hierarchy's own dim name (legacy behaviour) — the
        # fix only activates when the 2nd segment is a real level of the
        # hierarchy named in the 1st segment.
        assert dims == ["Sales_Date_Hierarchy"]
