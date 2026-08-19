"""Tests for GROUP BY alias/case-fold improvements (Bugs 6858, 6837, 6838, 6859).

Bug-6858: GROUP BY aggregate-alias must raise GroupByError.
Bug-6837: end-to-end coverage for unblocked GROUP BY shapes.
Bug-6838: quoted-identifier case sensitivity divergence (documented + tested).
Bug-6859: ambiguity rule documentation guard.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_6858_6837_6838_6859.py
"""
import pytest

from src.parsing.sql_parser import GroupByError, parse_sql_to_ir


# ---------------------------------------------------------------------------
# Bug-6858: GROUP BY aggregate-alias rejection
# ---------------------------------------------------------------------------

class TestGroupByAggregateAliasRejection:
    def test_sum_alias_in_group_by_raises(self):
        with pytest.raises(GroupByError, match="aggregate or expression alias"):
            parse_sql_to_ir(
                "SELECT SUM(amount) AS total FROM sales GROUP BY total",
                "model-1",
            )

    def test_count_alias_in_group_by_raises(self):
        with pytest.raises(GroupByError, match="aggregate or expression alias"):
            parse_sql_to_ir(
                "SELECT COUNT(orders) AS cnt, region FROM sales GROUP BY cnt, region",
                "model-1",
            )

    def test_avg_alias_in_group_by_raises(self):
        with pytest.raises(GroupByError, match="aggregate or expression alias"):
            parse_sql_to_ir(
                "SELECT AVG(price) AS avg_price, category FROM products GROUP BY avg_price, category",
                "model-1",
            )

    def test_bare_column_alias_in_group_by_still_works(self):
        # Guard: bare-column aliases must still resolve (Bug-6084 path preserved).
        q = parse_sql_to_ir(
            "SELECT region AS r, SUM(amount) FROM sales GROUP BY r",
            "model-1",
        )
        assert q.grain == ["region"]

    def test_aggregate_alias_case_insensitive_rejection(self):
        # GROUP BY TOTAL where SELECT SUM(amount) AS total -- case-insensitive.
        with pytest.raises(GroupByError, match="aggregate or expression alias"):
            parse_sql_to_ir(
                "SELECT SUM(amount) AS total, region FROM sales GROUP BY TOTAL, region",
                "model-1",
            )

    def test_composable_aggregate_alias_in_group_by_raises(self):
        # SUM(a)/SUM(b) AS ratio is a composable aggregate (classification=passthrough,
        # composable=True). GROUP BY ratio must also raise.
        with pytest.raises(GroupByError, match="aggregate or expression alias"):
            parse_sql_to_ir(
                "SELECT SUM(revenue)/SUM(quantity) AS ratio, region "
                "FROM sales GROUP BY ratio, region",
                "model-1",
            )


# ---------------------------------------------------------------------------
# Bug-6837: end-to-end GROUP BY shapes execute correctly
# ---------------------------------------------------------------------------

class TestGroupByShapeExecution:
    def test_case_insensitive_group_by_produces_correct_grain(self):
        # SELECT Region ... GROUP BY region -- case-fold leniency.
        q = parse_sql_to_ir(
            "SELECT Region, SUM(amount) FROM sales GROUP BY region",
            "model-1",
        )
        assert "Region" in q.requested_dimensions or "region" in q.grain
        assert "amount" in q.requested_measures

    def test_output_alias_group_by_resolves_column(self):
        q = parse_sql_to_ir(
            "SELECT region AS r, SUM(revenue) FROM model GROUP BY r",
            "model-1",
        )
        assert "region" in q.grain
        assert "revenue" in q.requested_measures

    def test_parenthesised_group_by(self):
        q = parse_sql_to_ir(
            "SELECT region, SUM(amount) FROM sales GROUP BY (region)",
            "model-1",
        )
        assert "region" in q.grain

    def test_cast_group_by(self):
        q = parse_sql_to_ir(
            "SELECT success_flag, SUM(amount) FROM sales GROUP BY success_flag::text",
            "model-1",
        )
        assert "success_flag" in q.grain


# ---------------------------------------------------------------------------
# Bug-6838: quoted-identifier case sensitivity divergence
# ---------------------------------------------------------------------------
# PostgreSQL treats quoted identifiers as case-sensitive: "Region" != "region".
# The parser's case-fold leniency deliberately ignores quotes for GROUP BY
# validation, so a quoted mismatch passes the parser gate. This is documented
# as acceptable: PG itself will reject the query at execution ("column
# "Region" must appear in the GROUP BY clause"), so the mismatch fails loud
# at the source.

class TestQuotedIdentifierCaseFold:
    def test_quoted_mismatch_passes_parser(self):
        # "Region" in SELECT vs "region" in GROUP BY -- the parser is lenient
        # (case-fold ignores quotes). PostgreSQL will reject at execution.
        q = parse_sql_to_ir(
            'SELECT "Region", SUM(amount) FROM sales GROUP BY "region"',
            "model-1",
        )
        # Parser does NOT raise -- the mismatch is caught by PG downstream.
        assert "amount" in q.requested_measures

    def test_quoted_exact_match_works(self):
        q = parse_sql_to_ir(
            'SELECT "region", SUM(amount) FROM sales GROUP BY "region"',
            "model-1",
        )
        assert "region" in q.grain


# ---------------------------------------------------------------------------
# Bug-6859: ambiguity rule documentation guard
# ---------------------------------------------------------------------------
# The alias_to_col ambiguity rule checks against bare-selected columns only,
# not all table columns. This is a known divergence from PostgreSQL's full
# resolution order (which checks against ALL input columns, not just those
# in the SELECT list). The divergence is noted as contrived (requires a
# non-selected column that collides with a SELECT alias) and binder-
# disambiguable (the binder resolves the alias to a dimension or measure,
# and PostgreSQL's own disambiguation catches truly ambiguous references).

class TestAmbiguityRuleDivergence:
    def test_alias_colliding_with_bare_selected_column_is_not_resolved(self):
        # SELECT a AS b, b, SUM(x) ... GROUP BY b -- b is an input column,
        # so the alias "b" must NOT resolve to "a". PG rejects this because
        # "a" (aliased as b) is ungrouped.
        with pytest.raises(GroupByError):
            parse_sql_to_ir(
                "SELECT amount AS region, region, SUM(qty) FROM sales GROUP BY region",
                "model-1",
            )

    def test_non_colliding_alias_resolves_correctly(self):
        # No collision: alias "r" does not match any bare-selected column.
        q = parse_sql_to_ir(
            "SELECT region AS r, SUM(qty) FROM sales GROUP BY r",
            "model-1",
        )
        assert q.grain == ["region"]
