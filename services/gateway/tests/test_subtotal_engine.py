"""Tests for the subtotal engine — detection, query generation, and result merging."""

import pytest
from src.dax.subtotal_engine import (
    SubtotalHierarchy,
    SubtotalLevel,
    GrainQuery,
    GrainResult,
    SUBTOTAL_LEVEL_KEY,
    SUBTOTAL_GRAIN_KEY,
    SUBTOTAL_GRAIN_PREFIX,
    detect_subtotal_hierarchies,
    build_subtotal_queries,
    build_multi_subtotal_queries,
    compute_last_non_empty_subtotals,
    compute_multi_lne_subtotals,
    merge_grain_results,
    merge_multi_hierarchy_results,
    _multi_hierarchy_sort_key,
)


# ---- Fixtures ----

def _make_hierarchy_meta():
    return [
        {
            "name": "Calendar",
            "levels": [
                {"name": "Year", "ordinal": 0, "time_unit": "year",
                 "key_attribute_id": "y-attr", "key_attribute_source": "user_defined_attribute"},
                {"name": "Month", "ordinal": 1, "time_unit": "month",
                 "key_attribute_id": "m-attr", "key_attribute_source": "user_defined_attribute"},
                {"name": "Day", "ordinal": 2, "time_unit": "day",
                 "key_attribute_id": "d-attr", "key_attribute_source": "physical_column"},
            ],
        }
    ]


def _make_level_dim_map():
    return {
        "calendar": {
            "year": "business_date_year",
            "month": "business_date_month",
            "day": "business_date",
        }
    }


def _make_hierarchy():
    return SubtotalHierarchy(
        hierarchy_name="Calendar",
        mdx_dim_name="Business Date",
        mdx_hier_name="Calendar",
        levels=[
            SubtotalLevel(name="Year", ordinal=0, dim_name="business_date_year", time_unit="year"),
            SubtotalLevel(name="Month", ordinal=1, dim_name="business_date_month", time_unit="month"),
            SubtotalLevel(name="Day", ordinal=2, dim_name="business_date", time_unit="day"),
        ],
    )


def _make_measures_meta():
    return [
        {"name": "transaction_amount", "default_agg": "sum", "semi_additive_behavior": None},
        {"name": "account_balance", "default_agg": "last_non_empty", "semi_additive_behavior": "last_non_empty",
         "semi_additive_account_column_id": "cust-col", "date_dimension_column_id": "date-col"},
        {"name": "unique_customers", "default_agg": "count_distinct", "semi_additive_behavior": None},
        {"name": "max_transaction", "default_agg": "max", "semi_additive_behavior": None},
    ]


# ---- Detection tests ----

class TestDetectSubtotalHierarchies:

    def test_detects_members_on_hierarchy(self):
        col_expr = "{[Measures].[transaction_amount]}"
        row_expr = "NON EMPTY [Business Date].[Calendar].MEMBERS"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert len(result) == 1
        assert result[0].hierarchy_name == "Calendar"
        assert len(result[0].levels) == 3
        assert result[0].levels[0].name == "Year"

    def test_detects_allMembers_variant(self):
        col_expr = "{[Measures].[amount]}"
        row_expr = "[Business Date].[Calendar].AllMembers"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert len(result) == 1

    def test_ignores_level_specific_members(self):
        col_expr = "{[Measures].[amount]}"
        row_expr = "[Business Date].[Calendar].[Month].MEMBERS"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert len(result) == 0

    def test_two_part_level_scoped_members_not_subtotal(self):
        """Bug-6892: Excel emits a level-scoped request as
        [Hierarchy].[Level].Members — the same two-part shape as
        [Dim].[Hier].Members. When the first part is the hierarchy and the
        second names one of its LEVELS, no subtotal expansion may trigger
        (it grouped the SQL by every grain and a Year pivot returned
        day-level rows)."""
        col_expr = "{[Measures].[amount]}"
        row_expr = "{[Calendar].[Year].Members}"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert result == []

    def test_two_part_hier_repeated_still_subtotal(self):
        """[Hier].[Hier].Members (SSAS self-qualified form) is still a full
        hierarchy expansion."""
        col_expr = "{[Measures].[amount]}"
        row_expr = "{[Calendar].[Calendar].Members}"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert len(result) == 1
        assert result[0].hierarchy_name == "Calendar"

    def test_ignores_when_no_members(self):
        col_expr = "{[Measures].[amount]}"
        row_expr = "[Business Date].[Calendar].[2024]"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert len(result) == 0

    def test_no_duplicate_detection(self):
        col_expr = ""
        row_expr = "[Business Date].[Calendar].MEMBERS, [Business Date].[Calendar].MEMBERS"
        result = detect_subtotal_hierarchies(
            col_expr, row_expr,
            _make_hierarchy_meta(), _make_level_dim_map(),
        )
        assert len(result) == 1


# ---- Query generation tests ----

class TestBuildSubtotalQueries:

    def test_generates_queries_for_each_grain(self):
        hierarchy = _make_hierarchy()
        canonical = {
            "transaction_amount": "transaction_amount",
            "unique_customers": "unique_customers",
        }
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["transaction_amount", "unique_customers"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=_make_measures_meta(),
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        assert len(queries) == 3
        ordinals = sorted(q.grain_ordinal for q in queries)
        assert ordinals == [-1, 0, 1]

    def test_month_subtotal_has_year_and_month(self):
        hierarchy = _make_hierarchy()
        canonical = {"transaction_amount": "transaction_amount"}
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["transaction_amount"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=[{"name": "transaction_amount", "default_agg": "sum"}],
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        month_q = next(q for q in queries if q.grain_ordinal == 1)
        assert '"business_date_year"' in month_q.sql
        assert '"business_date_month"' in month_q.sql
        assert '"business_date"' not in month_q.sql

    def test_grand_total_has_no_hierarchy_dims(self):
        hierarchy = _make_hierarchy()
        canonical = {"transaction_amount": "transaction_amount"}
        queries = build_subtotal_queries(
            mdx_dims=["region_code", "business_date_year", "business_date_month", "business_date"],
            mdx_measures=["transaction_amount"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=[{"name": "transaction_amount", "default_agg": "sum"}],
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        gt_q = next(q for q in queries if q.grain_ordinal == -1)
        assert '"region_code"' in gt_q.sql
        assert '"business_date_year"' not in gt_q.sql

    def test_last_non_empty_uses_sum_proxy(self):
        hierarchy = _make_hierarchy()
        canonical = {"account_balance": "account_balance"}
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["account_balance"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=_make_measures_meta(),
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert 'SUM("account_balance")' in year_q.sql

    def test_count_distinct_preserved(self):
        hierarchy = _make_hierarchy()
        canonical = {"unique_customers": "unique_customers"}
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["unique_customers"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=_make_measures_meta(),
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert 'COUNT(DISTINCT "unique_customers")' in year_q.sql

    def test_where_clauses_propagated(self):
        hierarchy = _make_hierarchy()
        canonical = {"transaction_amount": "transaction_amount"}
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["transaction_amount"],
            where_sql_clauses=['"region_code" = \'US\''],
            model_slug="modely",
            measures_meta=[{"name": "transaction_amount", "default_agg": "sum"}],
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        for q in queries:
            assert "region_code" in q.sql

    def test_max_aggregation_preserved(self):
        hierarchy = _make_hierarchy()
        canonical = {"max_transaction": "max_transaction"}
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["max_transaction"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=_make_measures_meta(),
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert 'MAX("max_transaction")' in year_q.sql

    def test_avg_aggregation_preserved(self):
        hierarchy = _make_hierarchy()
        meta = _make_measures_meta() + [
            {"name": "avg_amount", "default_agg": "avg", "semi_additive_behavior": None},
        ]
        canonical = {"avg_amount": "avg_amount"}
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["avg_amount"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=meta,
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert 'AVG("avg_amount")' in year_q.sql

    def test_mixed_measures_each_uses_own_agg(self):
        hierarchy = _make_hierarchy()
        canonical = {
            "transaction_amount": "transaction_amount",
            "account_balance": "account_balance",
            "unique_customers": "unique_customers",
            "max_transaction": "max_transaction",
        }
        queries = build_subtotal_queries(
            mdx_dims=["business_date_year", "business_date_month", "business_date"],
            mdx_measures=["transaction_amount", "account_balance", "unique_customers", "max_transaction"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=_make_measures_meta(),
            hierarchy=hierarchy,
            measure_canonical=canonical,
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert 'SUM("transaction_amount")' in year_q.sql
        assert 'SUM("account_balance")' in year_q.sql  # LNE uses SUM proxy
        assert 'COUNT(DISTINCT "unique_customers")' in year_q.sql
        assert 'MAX("max_transaction")' in year_q.sql

    def test_single_level_hierarchy_produces_grand_total_only(self):
        single_level = SubtotalHierarchy(
            hierarchy_name="Region",
            mdx_dim_name="Region",
            mdx_hier_name="Region",
            levels=[
                SubtotalLevel(name="Country", ordinal=0, dim_name="country_code"),
            ],
        )
        canonical = {"transaction_amount": "transaction_amount"}
        queries = build_subtotal_queries(
            mdx_dims=["country_code"],
            mdx_measures=["transaction_amount"],
            where_sql_clauses=[],
            model_slug="modely",
            measures_meta=[{"name": "transaction_amount", "default_agg": "sum"}],
            hierarchy=single_level,
            measure_canonical=canonical,
        )
        assert len(queries) == 1
        assert queries[0].grain_ordinal == -1
        assert queries[0].level_name == "Grand Total"


# ---- LAST_NON_EMPTY computation tests ----

class TestComputeLastNonEmptySubtotals:

    def test_takes_last_value_per_group(self):
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-15", "account_balance": 100},
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-31", "account_balance": 150},
            {"business_date_year": "2024", "business_date_month": "2024-02",
             "business_date": "2024-02-28", "account_balance": 200},
        ]
        result = compute_last_non_empty_subtotals(
            detail_rows, hierarchy, ["account_balance"],
        )
        month_vals = result[1]
        assert month_vals[("2024", "2024-01")]["account_balance"] == 150
        assert month_vals[("2024", "2024-02")]["account_balance"] == 200

    def test_year_subtotal_takes_last_month(self):
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-31", "account_balance": 100},
            {"business_date_year": "2024", "business_date_month": "2024-12",
             "business_date": "2024-12-31", "account_balance": 500},
        ]
        result = compute_last_non_empty_subtotals(
            detail_rows, hierarchy, ["account_balance"],
        )
        year_vals = result[0]
        assert year_vals[("2024",)]["account_balance"] == 500

    def test_grand_total_takes_last_overall(self):
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-06",
             "business_date": "2024-06-30", "account_balance": 300},
            {"business_date_year": "2025", "business_date_month": "2025-01",
             "business_date": "2025-01-15", "account_balance": 400},
        ]
        result = compute_last_non_empty_subtotals(
            detail_rows, hierarchy, ["account_balance"],
        )
        gt_vals = result[-1]
        assert gt_vals[()]["account_balance"] == 400

    def test_empty_rows_returns_empty(self):
        hierarchy = _make_hierarchy()
        result = compute_last_non_empty_subtotals([], hierarchy, ["account_balance"])
        assert result == {}

    def test_multiple_lne_measures(self):
        """Multiple LNE measures all pick from the same last row per group."""
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-15", "balance_a": 100, "balance_b": 500},
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-31", "balance_a": 150, "balance_b": 600},
        ]
        result = compute_last_non_empty_subtotals(
            detail_rows, hierarchy, ["balance_a", "balance_b"],
        )
        month_vals = result[1]
        assert month_vals[("2024", "2024-01")]["balance_a"] == 150
        assert month_vals[("2024", "2024-01")]["balance_b"] == 600

    def test_lne_across_years(self):
        """Grand total LNE picks the last row across all years."""
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2023", "business_date_month": "2023-12",
             "business_date": "2023-12-31", "account_balance": 900},
            {"business_date_year": "2024", "business_date_month": "2024-03",
             "business_date": "2024-03-31", "account_balance": 1200},
        ]
        result = compute_last_non_empty_subtotals(
            detail_rows, hierarchy, ["account_balance"],
        )
        assert result[0][("2023",)]["account_balance"] == 900
        assert result[0][("2024",)]["account_balance"] == 1200
        assert result[-1][()]["account_balance"] == 1200

    def test_lne_partitions_by_non_hierarchy_dims(self):
        """Two regions must get independent LNE values at every grain."""
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"region": "US", "business_date_year": "2024",
             "business_date_month": "2024-01", "business_date": "2024-01-31",
             "account_balance": 100},
            {"region": "US", "business_date_year": "2024",
             "business_date_month": "2024-02", "business_date": "2024-02-28",
             "account_balance": 200},
            {"region": "EU", "business_date_year": "2024",
             "business_date_month": "2024-01", "business_date": "2024-01-31",
             "account_balance": 500},
            {"region": "EU", "business_date_year": "2024",
             "business_date_month": "2024-02", "business_date": "2024-02-28",
             "account_balance": 800},
        ]
        result = compute_last_non_empty_subtotals(
            detail_rows, hierarchy, ["account_balance"],
            non_hier_dims=["region"],
        )
        year_vals = result[0]
        assert year_vals[("US", "2024")]["account_balance"] == 200
        assert year_vals[("EU", "2024")]["account_balance"] == 800

        gt_vals = result[-1]
        assert gt_vals[("US",)]["account_balance"] == 200
        assert gt_vals[("EU",)]["account_balance"] == 800


# ---- Merge tests ----

class TestMergeGrainResults:

    def _make_detail_result(self, hierarchy):
        rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-15", "amount": 100},
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-20", "amount": 200},
            {"business_date_year": "2024", "business_date_month": "2024-02",
             "business_date": "2024-02-10", "amount": 300},
        ]
        return GrainResult(
            query=GrainQuery(
                sql="", protocol="jdbc", grain_ordinal=2,
                level_name="Day",
                dim_cols=["business_date_year", "business_date_month", "business_date"],
            ),
            columns=["business_date_year", "business_date_month", "business_date", "amount"],
            rows=rows,
        )

    def _make_subtotal_results(self):
        month_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01", "amount": 300},
            {"business_date_year": "2024", "business_date_month": "2024-02", "amount": 300},
        ]
        year_rows = [
            {"business_date_year": "2024", "amount": 600},
        ]
        grand_rows = [
            {"amount": 600},
        ]
        return [
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=1,
                                 level_name="Month", dim_cols=["business_date_year", "business_date_month"]),
                columns=["business_date_year", "business_date_month", "amount"],
                rows=month_rows,
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=0,
                                 level_name="Year", dim_cols=["business_date_year"]),
                columns=["business_date_year", "amount"],
                rows=year_rows,
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=-1,
                                 level_name="Grand Total", dim_cols=[]),
                columns=["amount"],
                rows=grand_rows,
            ),
        ]

    def test_merge_produces_hierarchical_order(self):
        hierarchy = _make_hierarchy()
        detail = self._make_detail_result(hierarchy)
        subtotals = self._make_subtotal_results()

        columns, rows = merge_grain_results(detail, subtotals, hierarchy)

        levels = [r[SUBTOTAL_LEVEL_KEY] for r in rows]
        assert levels[0] == "Grand Total"
        assert levels[1] == "Year"
        assert levels[2] == "Month"
        assert levels[3] == "detail"
        assert levels[4] == "detail"
        assert levels[5] == "Month"
        assert levels[6] == "detail"

    def test_merge_preserves_all_rows(self):
        hierarchy = _make_hierarchy()
        detail = self._make_detail_result(hierarchy)
        subtotals = self._make_subtotal_results()

        columns, rows = merge_grain_results(detail, subtotals, hierarchy)
        assert len(rows) == 7  # 3 detail + 2 month + 1 year + 1 grand

    def test_lne_overrides_applied(self):
        hierarchy = _make_hierarchy()
        detail = self._make_detail_result(hierarchy)
        subtotals = self._make_subtotal_results()
        lne = {
            0: {("2024",): {"amount": 999}},
        }

        columns, rows = merge_grain_results(
            detail, subtotals, hierarchy, lne_overrides=lne,
        )
        year_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Year")
        assert year_row["amount"] == 999

    def test_lne_overrides_applied_to_grand_total(self):
        hierarchy = _make_hierarchy()
        detail = self._make_detail_result(hierarchy)
        subtotals = self._make_subtotal_results()
        lne = {
            -1: {(): {"amount": 777}},
        }
        columns, rows = merge_grain_results(
            detail, subtotals, hierarchy, lne_overrides=lne,
        )
        gt_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Grand Total")
        assert gt_row["amount"] == 777

    def test_sum_subtotal_values_correct(self):
        """SUM subtotal at month grain = sum of detail rows in that month."""
        hierarchy = _make_hierarchy()
        detail = self._make_detail_result(hierarchy)
        subtotals = self._make_subtotal_results()
        columns, rows = merge_grain_results(detail, subtotals, hierarchy)
        jan_sub = next(
            r for r in rows
            if r[SUBTOTAL_LEVEL_KEY] == "Month" and r.get("business_date_month") == "2024-01"
        )
        assert jan_sub["amount"] == 300  # 100 + 200
        gt = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Grand Total")
        assert gt["amount"] == 600

    def test_max_subtotal_values_correct(self):
        """MAX subtotal at month grain = max of detail rows in that month."""
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-15", "max_val": 50},
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-20", "max_val": 80},
            {"business_date_year": "2024", "business_date_month": "2024-02",
             "business_date": "2024-02-10", "max_val": 60},
        ]
        detail = GrainResult(
            query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=2,
                             level_name="Day",
                             dim_cols=["business_date_year", "business_date_month", "business_date"]),
            columns=["business_date_year", "business_date_month", "business_date", "max_val"],
            rows=detail_rows,
        )
        subtotals = [
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=1,
                                 level_name="Month",
                                 dim_cols=["business_date_year", "business_date_month"]),
                columns=["business_date_year", "business_date_month", "max_val"],
                rows=[
                    {"business_date_year": "2024", "business_date_month": "2024-01", "max_val": 80},
                    {"business_date_year": "2024", "business_date_month": "2024-02", "max_val": 60},
                ],
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=0,
                                 level_name="Year", dim_cols=["business_date_year"]),
                columns=["business_date_year", "max_val"],
                rows=[{"business_date_year": "2024", "max_val": 80}],
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=-1,
                                 level_name="Grand Total", dim_cols=[]),
                columns=["max_val"],
                rows=[{"max_val": 80}],
            ),
        ]
        columns, rows = merge_grain_results(detail, subtotals, hierarchy)
        year_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Year")
        assert year_row["max_val"] == 80
        gt_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Grand Total")
        assert gt_row["max_val"] == 80

    def test_mixed_measures_merge_with_lne_override(self):
        """SUM and LNE measures side by side: SUM sums, LNE gets overridden."""
        hierarchy = _make_hierarchy()
        detail_rows = [
            {"business_date_year": "2024", "business_date_month": "2024-01",
             "business_date": "2024-01-31", "revenue": 100, "balance": 1000},
            {"business_date_year": "2024", "business_date_month": "2024-02",
             "business_date": "2024-02-28", "revenue": 200, "balance": 1500},
        ]
        detail = GrainResult(
            query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=2,
                             level_name="Day",
                             dim_cols=["business_date_year", "business_date_month", "business_date"]),
            columns=["business_date_year", "business_date_month", "business_date", "revenue", "balance"],
            rows=detail_rows,
        )
        subtotals = [
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=0,
                                 level_name="Year", dim_cols=["business_date_year"]),
                columns=["business_date_year", "revenue", "balance"],
                rows=[{"business_date_year": "2024", "revenue": 300, "balance": 2500}],
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=-1,
                                 level_name="Grand Total", dim_cols=[]),
                columns=["revenue", "balance"],
                rows=[{"revenue": 300, "balance": 2500}],
            ),
        ]
        lne_overrides = {
            0: {("2024",): {"balance": 1500}},
            -1: {(): {"balance": 1500}},
        }
        columns, rows = merge_grain_results(
            detail, subtotals, hierarchy, lne_overrides=lne_overrides,
        )
        year_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Year")
        assert year_row["revenue"] == 300
        assert year_row["balance"] == 1500
        gt_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Grand Total")
        assert gt_row["revenue"] == 300
        assert gt_row["balance"] == 1500

    def test_single_level_merge_grand_total_only(self):
        """Single-level hierarchy produces only detail + grand total rows."""
        single = SubtotalHierarchy(
            hierarchy_name="Region",
            mdx_dim_name="Region",
            mdx_hier_name="Region",
            levels=[
                SubtotalLevel(name="Country", ordinal=0, dim_name="country_code"),
            ],
        )
        detail = GrainResult(
            query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=0,
                             level_name="detail", dim_cols=["country_code"]),
            columns=["country_code", "amount"],
            rows=[
                {"country_code": "US", "amount": 100},
                {"country_code": "UK", "amount": 200},
            ],
        )
        subtotals = [
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=-1,
                                 level_name="Grand Total", dim_cols=[]),
                columns=["amount"],
                rows=[{"amount": 300}],
            ),
        ]
        columns, rows = merge_grain_results(detail, subtotals, single)
        levels = [r[SUBTOTAL_LEVEL_KEY] for r in rows]
        assert "Grand Total" in levels
        assert levels.count("detail") == 2
        assert len(rows) == 3


# ---- Multi-hierarchy fixtures ----

def _make_calendar_hierarchy():
    return SubtotalHierarchy(
        hierarchy_name="Calendar",
        mdx_dim_name="Business Date",
        mdx_hier_name="Calendar",
        levels=[
            SubtotalLevel(name="Year", ordinal=0, dim_name="cal_year", time_unit="year"),
            SubtotalLevel(name="Month", ordinal=1, dim_name="cal_month", time_unit="month"),
            SubtotalLevel(name="Day", ordinal=2, dim_name="cal_day", time_unit="day"),
        ],
    )


def _make_region_hierarchy():
    return SubtotalHierarchy(
        hierarchy_name="Region",
        mdx_dim_name="Region",
        mdx_hier_name="Region",
        levels=[
            SubtotalLevel(name="Country", ordinal=0, dim_name="country"),
            SubtotalLevel(name="City", ordinal=1, dim_name="city"),
        ],
    )


def _two_hierarchies():
    return [_make_calendar_hierarchy(), _make_region_hierarchy()]


def _multi_measures_meta():
    return [
        {"name": "revenue", "default_agg": "sum"},
        {"name": "balance", "default_agg": "last_non_empty"},
    ]


def _multi_canonical():
    return {"revenue": "revenue", "balance": "balance"}


def _multi_detail_rows():
    """6 detail rows: 2 countries × 3 dates."""
    return [
        {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
         "country": "US", "city": "NYC", "revenue": 100, "balance": 1000},
        {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-31",
         "country": "US", "city": "NYC", "revenue": 200, "balance": 1100},
        {"cal_year": "2024", "cal_month": "2024-02", "cal_day": "2024-02-15",
         "country": "US", "city": "NYC", "revenue": 150, "balance": 1200},
        {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
         "country": "DE", "city": "Berlin", "revenue": 50, "balance": 500},
        {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-31",
         "country": "DE", "city": "Berlin", "revenue": 80, "balance": 550},
        {"cal_year": "2024", "cal_month": "2024-02", "cal_day": "2024-02-15",
         "country": "DE", "city": "Berlin", "revenue": 70, "balance": 600},
    ]


# ---- Multi-hierarchy query generation tests ----

class TestBuildMultiSubtotalQueries:

    def test_two_hierarchies_produce_correct_query_count(self):
        """Calendar 4 options (detail,Month,Year,All) × Region 3 (detail,Country,All) = 12 - 1 = 11."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        assert len(queries) == 11

    def test_detail_combo_is_skipped(self):
        """The all-detail combination (finest grain on both hierarchies) is not generated."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        for q in queries:
            gph = q.grain_per_hierarchy
            assert gph is not None
            is_all_detail = (gph.get("Calendar") == 2 and gph.get("Region") == 1)
            assert not is_all_detail, "All-detail combo should be skipped"

    def test_grand_total_combo_exists(self):
        """All-All combo (both hierarchies at 'All' grain) produces 'Grand Total'."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        gt = [q for q in queries if q.level_name == "Grand Total"]
        assert len(gt) == 1
        assert gt[0].grain_per_hierarchy == {"Calendar": -1, "Region": -1}
        assert gt[0].dim_cols == []

    def test_grain_per_hierarchy_set_on_every_query(self):
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        for q in queries:
            assert q.grain_per_hierarchy is not None
            assert "Calendar" in q.grain_per_hierarchy
            assert "Region" in q.grain_per_hierarchy

    def test_calendar_year_region_detail_dims(self):
        """Calendar=Year, Region=detail should have [cal_year, country, city]."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        match = [q for q in queries
                 if q.grain_per_hierarchy == {"Calendar": 0, "Region": 1}]
        assert len(match) == 1
        assert set(match[0].dim_cols) == {"cal_year", "country", "city"}

    def test_calendar_all_region_country_dims(self):
        """Calendar=All, Region=Country should have [country]."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        match = [q for q in queries
                 if q.grain_per_hierarchy == {"Calendar": -1, "Region": 0}]
        assert len(match) == 1
        assert match[0].dim_cols == ["country"]

    def test_non_hierarchy_dims_preserved(self):
        """A non-hierarchy dim like 'product' appears in all queries."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["product", "cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        for q in queries:
            assert "product" in q.dim_cols

    def test_sql_contains_correct_group_by(self):
        """Each query's SQL has GROUP BY matching its dim_cols."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        for q in queries:
            if q.dim_cols:
                assert "GROUP BY" in q.sql
                for dim in q.dim_cols:
                    assert f'"{dim}"' in q.sql
            else:
                assert "GROUP BY" not in q.sql

    def test_where_clauses_propagated(self):
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=['"status" = \'active\''],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        for q in queries:
            assert '"status"' in q.sql

    def test_lne_measure_uses_sum_proxy(self):
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["balance"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "balance", "default_agg": "last_non_empty"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"balance": "balance"},
        )
        for q in queries:
            assert 'SUM("balance")' in q.sql

    def test_count_distinct_preserved(self):
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["unique_customers"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "unique_customers", "default_agg": "count_distinct"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"unique_customers": "unique_customers"},
        )
        for q in queries:
            assert 'COUNT(DISTINCT "unique_customers")' in q.sql

    def test_label_includes_hierarchy_names(self):
        """Non-detail, non-grand-total combos have labels like 'Calendar:Year x Region:Country'."""
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=_two_hierarchies(),
            measure_canonical={"revenue": "revenue"},
        )
        match = [q for q in queries
                 if q.grain_per_hierarchy == {"Calendar": 0, "Region": 0}]
        assert len(match) == 1
        assert "Calendar:Year" in match[0].level_name
        assert "Region:Country" in match[0].level_name


# ---- Multi-hierarchy LNE computation tests ----

class TestComputeMultiLneSubtotals:

    def test_lne_computed_per_grain_combination(self):
        hierarchies = _two_hierarchies()
        detail_rows = _multi_detail_rows()
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["balance"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=[{"name": "balance", "default_agg": "last_non_empty"}],
            hierarchies=hierarchies,
            measure_canonical={"balance": "balance"},
        )
        result = compute_multi_lne_subtotals(
            detail_rows, hierarchies, ["balance"], queries,
        )
        assert len(result) == len(queries)

    def test_lne_grand_total_takes_last_overall(self):
        hierarchies = _two_hierarchies()
        detail_rows = _multi_detail_rows()
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["balance"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=[{"name": "balance", "default_agg": "last_non_empty"}],
            hierarchies=hierarchies,
            measure_canonical={"balance": "balance"},
        )
        result = compute_multi_lne_subtotals(
            detail_rows, hierarchies, ["balance"], queries,
        )
        gt_q = next(q for q in queries if q.level_name == "Grand Total")
        gt_key = tuple(gt_q.dim_cols)
        gt_overrides = result[gt_key]
        assert gt_overrides[()]["balance"] == 1200

    def test_lne_year_region_detail_partitioned_by_country(self):
        """Calendar=Year, Region=detail: LNE partitioned by (cal_year, country, city)."""
        hierarchies = _two_hierarchies()
        detail_rows = _multi_detail_rows()
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["balance"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=[{"name": "balance", "default_agg": "last_non_empty"}],
            hierarchies=hierarchies,
            measure_canonical={"balance": "balance"},
        )
        result = compute_multi_lne_subtotals(
            detail_rows, hierarchies, ["balance"], queries,
        )
        q = next(q for q in queries
                 if q.grain_per_hierarchy == {"Calendar": 0, "Region": 1})
        overrides = result[tuple(q.dim_cols)]
        assert overrides[("2024", "US", "NYC")]["balance"] == 1200
        assert overrides[("2024", "DE", "Berlin")]["balance"] == 600

    def test_lne_empty_rows_returns_empty(self):
        hierarchies = _two_hierarchies()
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["balance"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=[{"name": "balance", "default_agg": "last_non_empty"}],
            hierarchies=hierarchies,
            measure_canonical={"balance": "balance"},
        )
        result = compute_multi_lne_subtotals([], hierarchies, ["balance"], queries)
        assert result == {}

    def test_lne_no_measures_returns_empty(self):
        hierarchies = _two_hierarchies()
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=[{"name": "revenue", "default_agg": "sum"}],
            hierarchies=hierarchies,
            measure_canonical={"revenue": "revenue"},
        )
        result = compute_multi_lne_subtotals(
            _multi_detail_rows(), hierarchies, [], queries,
        )
        assert result == {}

    def test_lne_calendar_all_region_country(self):
        """Calendar=All, Region=Country: groups by country only, takes last date per country."""
        hierarchies = _two_hierarchies()
        detail_rows = _multi_detail_rows()
        queries = build_multi_subtotal_queries(
            mdx_dims=["cal_year", "cal_month", "cal_day", "country", "city"],
            mdx_measures=["balance"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=[{"name": "balance", "default_agg": "last_non_empty"}],
            hierarchies=hierarchies,
            measure_canonical={"balance": "balance"},
        )
        result = compute_multi_lne_subtotals(
            detail_rows, hierarchies, ["balance"], queries,
        )
        q = next(q for q in queries
                 if q.grain_per_hierarchy == {"Calendar": -1, "Region": 0})
        overrides = result[tuple(q.dim_cols)]
        assert overrides[("US",)]["balance"] == 1200
        assert overrides[("DE",)]["balance"] == 600


# ---- Multi-hierarchy merge tests ----

class TestMergeMultiHierarchyResults:

    def _build_multi_scenario(self):
        """Build a complete multi-hierarchy scenario with detail + subtotal results."""
        hierarchies = _two_hierarchies()
        detail_rows = [
            {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
             "country": "US", "city": "NYC", "revenue": 100},
            {"cal_year": "2024", "cal_month": "2024-02", "cal_day": "2024-02-15",
             "country": "US", "city": "NYC", "revenue": 200},
            {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
             "country": "DE", "city": "Berlin", "revenue": 50},
        ]
        detail_result = GrainResult(
            query=GrainQuery(
                sql="", protocol="jdbc", grain_ordinal=3,
                level_name="detail",
                dim_cols=["cal_year", "cal_month", "cal_day", "country", "city"],
                grain_per_hierarchy={"Calendar": 2, "Region": 1},
            ),
            columns=["cal_year", "cal_month", "cal_day", "country", "city", "revenue"],
            rows=detail_rows,
        )

        subtotal_results = [
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=1,
                    level_name="Calendar:Year",
                    dim_cols=["cal_year", "country", "city"],
                    grain_per_hierarchy={"Calendar": 0, "Region": 1},
                ),
                columns=["cal_year", "country", "city", "revenue"],
                rows=[
                    {"cal_year": "2024", "country": "US", "city": "NYC", "revenue": 300},
                    {"cal_year": "2024", "country": "DE", "city": "Berlin", "revenue": 50},
                ],
            ),
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=0,
                    level_name="Region:Country",
                    dim_cols=["cal_year", "cal_month", "cal_day", "country"],
                    grain_per_hierarchy={"Calendar": 2, "Region": 0},
                ),
                columns=["cal_year", "cal_month", "cal_day", "country", "revenue"],
                rows=[
                    {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
                     "country": "US", "revenue": 100},
                    {"cal_year": "2024", "cal_month": "2024-02", "cal_day": "2024-02-15",
                     "country": "US", "revenue": 200},
                    {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
                     "country": "DE", "revenue": 50},
                ],
            ),
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=-1,
                    level_name="Calendar:Year x Region:Country",
                    dim_cols=["cal_year", "country"],
                    grain_per_hierarchy={"Calendar": 0, "Region": 0},
                ),
                columns=["cal_year", "country", "revenue"],
                rows=[
                    {"cal_year": "2024", "country": "US", "revenue": 300},
                    {"cal_year": "2024", "country": "DE", "revenue": 50},
                ],
            ),
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=-2,
                    level_name="Grand Total",
                    dim_cols=[],
                    grain_per_hierarchy={"Calendar": -1, "Region": -1},
                ),
                columns=["revenue"],
                rows=[{"revenue": 350}],
            ),
        ]

        return hierarchies, detail_result, subtotal_results

    def test_all_rows_preserved(self):
        hierarchies, detail, subtotals = self._build_multi_scenario()
        _, rows = merge_multi_hierarchy_results(detail, subtotals, hierarchies)
        expected_count = len(detail.rows) + sum(len(sr.rows) for sr in subtotals)
        assert len(rows) == expected_count

    def test_per_hierarchy_grain_tags_present(self):
        hierarchies, detail, subtotals = self._build_multi_scenario()
        _, rows = merge_multi_hierarchy_results(detail, subtotals, hierarchies)
        for row in rows:
            assert SUBTOTAL_GRAIN_PREFIX + "Calendar" in row
            assert SUBTOTAL_GRAIN_PREFIX + "Region" in row

    def test_detail_rows_tagged_with_finest_grain(self):
        hierarchies, detail, subtotals = self._build_multi_scenario()
        _, rows = merge_multi_hierarchy_results(detail, subtotals, hierarchies)
        detail_rows = [r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "detail"]
        for r in detail_rows:
            assert r[SUBTOTAL_GRAIN_PREFIX + "Calendar"] == 2
            assert r[SUBTOTAL_GRAIN_PREFIX + "Region"] == 1

    def test_grand_total_first_in_sort(self):
        hierarchies, detail, subtotals = self._build_multi_scenario()
        _, rows = merge_multi_hierarchy_results(detail, subtotals, hierarchies)
        assert rows[0][SUBTOTAL_LEVEL_KEY] == "Grand Total"

    def test_column_order(self):
        """Columns: non-hier dims + all hier dims (Calendar then Region) + measures."""
        hierarchies, detail, subtotals = self._build_multi_scenario()
        columns, _ = merge_multi_hierarchy_results(detail, subtotals, hierarchies)
        assert columns == [
            "cal_year", "cal_month", "cal_day", "country", "city", "revenue",
        ]

    def test_country_subtotal_precedes_its_cities(self):
        """Within the same calendar grain, country subtotal sorts before city details (top-of-group)."""
        hierarchies, detail, subtotals = self._build_multi_scenario()
        _, rows = merge_multi_hierarchy_results(detail, subtotals, hierarchies)

        us_detail_indices = [
            i for i, r in enumerate(rows)
            if r.get("country") == "US" and r[SUBTOTAL_LEVEL_KEY] == "detail"
        ]
        us_country_sub_indices = [
            i for i, r in enumerate(rows)
            if r.get("country") == "US"
            and r.get(SUBTOTAL_GRAIN_PREFIX + "Region") == 0
            and r.get(SUBTOTAL_GRAIN_PREFIX + "Calendar") == 2
        ]
        assert us_detail_indices and us_country_sub_indices
        for sub_idx in us_country_sub_indices:
            related_details = [
                d for d in us_detail_indices
                if rows[d].get("cal_day") == rows[sub_idx].get("cal_day")
            ]
            for d_idx in related_details:
                assert sub_idx < d_idx

    def test_year_subtotal_sorts_before_detail_months(self):
        """Calendar:Year subtotals sort before detail-grain rows (top-of-group)."""
        hierarchies, detail, subtotals = self._build_multi_scenario()
        _, rows = merge_multi_hierarchy_results(detail, subtotals, hierarchies)

        year_sub_indices = [
            i for i, r in enumerate(rows)
            if r.get(SUBTOTAL_GRAIN_PREFIX + "Calendar") == 0
            and r.get("cal_year") == "2024"
        ]
        detail_indices = [
            i for i, r in enumerate(rows)
            if r[SUBTOTAL_LEVEL_KEY] == "detail"
            and r.get("cal_year") == "2024"
        ]
        assert year_sub_indices and detail_indices
        assert max(year_sub_indices) < min(detail_indices)

    def test_lne_overrides_applied_via_dim_cols_key(self):
        hierarchies, detail, subtotals = self._build_multi_scenario()
        gt_q = next(sr for sr in subtotals
                    if sr.query.level_name == "Grand Total")
        lne_overrides = {
            tuple(gt_q.query.dim_cols): {(): {"revenue": 9999}},
        }
        _, rows = merge_multi_hierarchy_results(
            detail, subtotals, hierarchies, lne_overrides=lne_overrides,
        )
        gt_row = next(r for r in rows if r[SUBTOTAL_LEVEL_KEY] == "Grand Total")
        assert gt_row["revenue"] == 9999

    def test_empty_subtotals_returns_detail_only(self):
        hierarchies = _two_hierarchies()
        detail_rows = [
            {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
             "country": "US", "city": "NYC", "revenue": 100},
        ]
        detail_result = GrainResult(
            query=GrainQuery(
                sql="", protocol="jdbc", grain_ordinal=3,
                level_name="detail",
                dim_cols=["cal_year", "cal_month", "cal_day", "country", "city"],
                grain_per_hierarchy={"Calendar": 2, "Region": 1},
            ),
            columns=["cal_year", "cal_month", "cal_day", "country", "city", "revenue"],
            rows=detail_rows,
        )
        _, rows = merge_multi_hierarchy_results(detail_result, [], hierarchies)
        assert len(rows) == 1
        assert rows[0][SUBTOTAL_LEVEL_KEY] == "detail"


# ---- Multi-hierarchy sort key tests ----

class TestMultiHierarchySortKey:

    def test_grand_total_sorts_first(self):
        hierarchies = _two_hierarchies()
        gt_row = {
            "cal_year": "", "cal_month": "", "cal_day": "",
            "country": "", "city": "",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": -1,
            SUBTOTAL_GRAIN_PREFIX + "Region": -1,
        }
        detail_row = {
            "cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
            "country": "US", "city": "NYC",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 2,
            SUBTOTAL_GRAIN_PREFIX + "Region": 1,
        }
        assert _multi_hierarchy_sort_key(gt_row, hierarchies) < \
            _multi_hierarchy_sort_key(detail_row, hierarchies)

    def test_country_subtotal_precedes_city_detail(self):
        """US country subtotal at 2024-01-15 sorts before US/NYC detail (top-of-group)."""
        hierarchies = _two_hierarchies()
        detail_row = {
            "cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
            "country": "US", "city": "NYC",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 2,
            SUBTOTAL_GRAIN_PREFIX + "Region": 1,
        }
        country_sub = {
            "cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
            "country": "US", "city": "",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 2,
            SUBTOTAL_GRAIN_PREFIX + "Region": 0,
        }
        assert _multi_hierarchy_sort_key(country_sub, hierarchies) < \
            _multi_hierarchy_sort_key(detail_row, hierarchies)

    def test_year_subtotal_before_month_rows(self):
        """Year subtotal sorts before its month-level children (top-of-group)."""
        hierarchies = _two_hierarchies()
        month_row = {
            "cal_year": "2024", "cal_month": "2024-12", "cal_day": "2024-12-31",
            "country": "US", "city": "NYC",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 2,
            SUBTOTAL_GRAIN_PREFIX + "Region": 1,
        }
        year_sub = {
            "cal_year": "2024", "cal_month": "", "cal_day": "",
            "country": "US", "city": "NYC",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 0,
            SUBTOTAL_GRAIN_PREFIX + "Region": 1,
        }
        assert _multi_hierarchy_sort_key(year_sub, hierarchies) < \
            _multi_hierarchy_sort_key(month_row, hierarchies)

    def test_different_countries_sort_by_name(self):
        hierarchies = _two_hierarchies()
        de_row = {
            "cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
            "country": "DE", "city": "Berlin",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 2,
            SUBTOTAL_GRAIN_PREFIX + "Region": 1,
        }
        us_row = {
            "cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15",
            "country": "US", "city": "NYC",
            SUBTOTAL_GRAIN_PREFIX + "Calendar": 2,
            SUBTOTAL_GRAIN_PREFIX + "Region": 1,
        }
        assert _multi_hierarchy_sort_key(de_row, hierarchies) < \
            _multi_hierarchy_sort_key(us_row, hierarchies)


# ---- Single-hierarchy regression under multi-hierarchy API ----

class TestSingleHierarchyRegression:

    def test_single_hierarchy_via_multi_api_produces_same_count(self):
        """Single Calendar hierarchy through build_multi_subtotal_queries matches build_subtotal_queries."""
        calendar = _make_calendar_hierarchy()
        canonical = {"revenue": "revenue"}
        meta = [{"name": "revenue", "default_agg": "sum"}]
        dims = ["cal_year", "cal_month", "cal_day"]

        single_queries = build_subtotal_queries(
            mdx_dims=dims,
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=meta,
            hierarchy=calendar,
            measure_canonical=canonical,
        )
        multi_queries = build_multi_subtotal_queries(
            mdx_dims=dims,
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=meta,
            hierarchies=[calendar],
            measure_canonical=canonical,
        )
        assert len(multi_queries) == len(single_queries)

    def test_single_hierarchy_multi_merge_preserves_all_rows(self):
        calendar = _make_calendar_hierarchy()
        detail_rows = [
            {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15", "revenue": 100},
            {"cal_year": "2024", "cal_month": "2024-02", "cal_day": "2024-02-15", "revenue": 200},
        ]
        detail = GrainResult(
            query=GrainQuery(
                sql="", protocol="jdbc", grain_ordinal=2,
                level_name="detail",
                dim_cols=["cal_year", "cal_month", "cal_day"],
                grain_per_hierarchy={"Calendar": 2},
            ),
            columns=["cal_year", "cal_month", "cal_day", "revenue"],
            rows=detail_rows,
        )
        subtotals = [
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=1,
                    level_name="Calendar:Month",
                    dim_cols=["cal_year", "cal_month"],
                    grain_per_hierarchy={"Calendar": 1},
                ),
                columns=["cal_year", "cal_month", "revenue"],
                rows=[
                    {"cal_year": "2024", "cal_month": "2024-01", "revenue": 100},
                    {"cal_year": "2024", "cal_month": "2024-02", "revenue": 200},
                ],
            ),
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=0,
                    level_name="Calendar:Year",
                    dim_cols=["cal_year"],
                    grain_per_hierarchy={"Calendar": 0},
                ),
                columns=["cal_year", "revenue"],
                rows=[{"cal_year": "2024", "revenue": 300}],
            ),
            GrainResult(
                query=GrainQuery(
                    sql="", protocol="jdbc", grain_ordinal=-1,
                    level_name="Grand Total",
                    dim_cols=[],
                    grain_per_hierarchy={"Calendar": -1},
                ),
                columns=["revenue"],
                rows=[{"revenue": 300}],
            ),
        ]
        columns, rows = merge_multi_hierarchy_results(
            detail, subtotals, [calendar],
        )
        assert len(rows) == 6
        assert rows[0][SUBTOTAL_LEVEL_KEY] == "Grand Total"
        assert rows[1].get(SUBTOTAL_GRAIN_PREFIX + "Calendar") == 0

    def test_single_hierarchy_multi_merge_sort_matches_single(self):
        """Sort order from multi-merge with one hierarchy produces same relative ordering as single merge."""
        calendar = _make_calendar_hierarchy()
        detail_rows = [
            {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-15", "revenue": 100},
            {"cal_year": "2024", "cal_month": "2024-01", "cal_day": "2024-01-31", "revenue": 200},
            {"cal_year": "2024", "cal_month": "2024-02", "cal_day": "2024-02-15", "revenue": 300},
        ]

        detail_single = GrainResult(
            query=GrainQuery(
                sql="", protocol="jdbc", grain_ordinal=2,
                level_name="Day",
                dim_cols=["cal_year", "cal_month", "cal_day"],
            ),
            columns=["cal_year", "cal_month", "cal_day", "revenue"],
            rows=detail_rows,
        )
        detail_multi = GrainResult(
            query=GrainQuery(
                sql="", protocol="jdbc", grain_ordinal=2,
                level_name="detail",
                dim_cols=["cal_year", "cal_month", "cal_day"],
                grain_per_hierarchy={"Calendar": 2},
            ),
            columns=["cal_year", "cal_month", "cal_day", "revenue"],
            rows=detail_rows,
        )

        subtotal_rows_month = [
            {"cal_year": "2024", "cal_month": "2024-01", "revenue": 300},
            {"cal_year": "2024", "cal_month": "2024-02", "revenue": 300},
        ]
        subtotal_rows_year = [{"cal_year": "2024", "revenue": 600}]
        subtotal_rows_gt = [{"revenue": 600}]

        subs_single = [
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=1,
                                 level_name="Month", dim_cols=["cal_year", "cal_month"]),
                columns=["cal_year", "cal_month", "revenue"], rows=subtotal_rows_month,
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=0,
                                 level_name="Year", dim_cols=["cal_year"]),
                columns=["cal_year", "revenue"], rows=subtotal_rows_year,
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=-1,
                                 level_name="Grand Total", dim_cols=[]),
                columns=["revenue"], rows=subtotal_rows_gt,
            ),
        ]
        subs_multi = [
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=1,
                                 level_name="Calendar:Month", dim_cols=["cal_year", "cal_month"],
                                 grain_per_hierarchy={"Calendar": 1}),
                columns=["cal_year", "cal_month", "revenue"], rows=subtotal_rows_month,
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=0,
                                 level_name="Calendar:Year", dim_cols=["cal_year"],
                                 grain_per_hierarchy={"Calendar": 0}),
                columns=["cal_year", "revenue"], rows=subtotal_rows_year,
            ),
            GrainResult(
                query=GrainQuery(sql="", protocol="jdbc", grain_ordinal=-1,
                                 level_name="Grand Total", dim_cols=[],
                                 grain_per_hierarchy={"Calendar": -1}),
                columns=["revenue"], rows=subtotal_rows_gt,
            ),
        ]

        _, rows_single = merge_grain_results(detail_single, subs_single, calendar)
        _, rows_multi = merge_multi_hierarchy_results(
            detail_multi, subs_multi, [calendar],
        )

        levels_single = [r[SUBTOTAL_LEVEL_KEY] for r in rows_single]
        levels_multi = [r[SUBTOTAL_LEVEL_KEY] for r in rows_multi]

        normalized_single = [
            "detail" if l == "Day" else l for l in levels_single
        ]
        normalized_multi = [
            l.replace("Calendar:", "") if l.startswith("Calendar:") else l
            for l in levels_multi
        ]
        assert normalized_single == normalized_multi


# ---------------------------------------------------------------------------
# Bug-586 — Axis-aware subtotal hierarchy detection
# ---------------------------------------------------------------------------

class TestSubtotalHierarchyAxisDetection:

    HIER_META = [
        {
            "name": "Calendar",
            "levels": [
                {"name": "Year", "ordinal": 0},
                {"name": "Month", "ordinal": 1},
            ],
        },
        {
            "name": "Geography",
            "levels": [
                {"name": "Country", "ordinal": 0},
            ],
        },
    ]
    LEVEL_DIM_MAP = {
        "calendar": {"year": "year_dim", "month": "month_dim"},
        "geography": {"country": "country_dim"},
    }

    def test_both_on_row_axis(self):
        result = detect_subtotal_hierarchies(
            "", "[Calendar].[Calendar].Members CROSSJOIN [Geography].[Geography].Members",
            self.HIER_META, self.LEVEL_DIM_MAP,
        )
        assert len(result) == 2
        assert all(h.axis == 1 for h in result)

    def test_cross_axis_placement(self):
        result = detect_subtotal_hierarchies(
            "[Geography].[Geography].Members",
            "[Calendar].[Calendar].Members",
            self.HIER_META, self.LEVEL_DIM_MAP,
        )
        assert len(result) == 2
        geo = next(h for h in result if h.hierarchy_name == "Geography")
        cal = next(h for h in result if h.hierarchy_name == "Calendar")
        assert geo.axis == 0
        assert cal.axis == 1

    def test_single_on_columns(self):
        result = detect_subtotal_hierarchies(
            "[Calendar].[Calendar].Members", "",
            self.HIER_META, self.LEVEL_DIM_MAP,
        )
        assert len(result) == 1
        assert result[0].axis == 0

    def test_single_on_rows(self):
        result = detect_subtotal_hierarchies(
            "", "[Calendar].[Calendar].Members",
            self.HIER_META, self.LEVEL_DIM_MAP,
        )
        assert len(result) == 1
        assert result[0].axis == 1

    def test_default_axis_is_row(self):
        h = SubtotalHierarchy(
            hierarchy_name="Test", mdx_dim_name="D", mdx_hier_name="H",
            levels=[SubtotalLevel(name="L", ordinal=0, dim_name="col")],
        )
        assert h.axis == 1
