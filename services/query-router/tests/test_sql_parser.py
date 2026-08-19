"""
Unit tests for src.parsing.sql_parser — parse_sql_to_ir.

Run from tessallite/services/query-router/:
    pytest tests/test_sql_parser.py
"""
import pytest

from src.parsing.sql_parser import GroupByError, SyntaxErrorInSQL, parse_sql_to_ir
from src.ir.logical_query import LogicalQuery, UnsupportedSQL


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

def test_simple_sum_measure_extracted():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM orders GROUP BY country",
        "model-1",
    )
    assert "revenue" in q.requested_measures


def test_grain_from_group_by():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM orders GROUP BY country",
        "model-1",
    )
    assert "country" in q.grain


def test_multiple_measures_and_grain():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue), COUNT(orders) FROM sales GROUP BY region, month",
        "model-1",
    )
    assert "revenue" in q.requested_measures
    assert "orders" in q.requested_measures
    assert set(q.grain) == {"region", "month"}


def test_no_group_by_empty_grain():
    q = parse_sql_to_ir("SELECT SUM(revenue) FROM sales", "model-1")
    assert q.grain == []


# ---------------------------------------------------------------------------
# Filter extraction
# ---------------------------------------------------------------------------

def test_filter_eq():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM sales WHERE status = 'active' GROUP BY region",
        "model-1",
    )
    assert any(
        f.dimension_name == "status" and f.operator == "eq" and f.value == "active"
        for f in q.filters
    )


def test_filter_gt():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM sales WHERE amount > 100 GROUP BY region",
        "model-1",
    )
    assert any(f.operator == "gt" and f.dimension_name == "amount" for f in q.filters)


def test_filter_in():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM sales WHERE region IN ('US', 'EU') GROUP BY month",
        "model-1",
    )
    in_filters = [f for f in q.filters if f.operator == "in"]
    assert len(in_filters) == 1
    assert "US" in in_filters[0].value
    assert "EU" in in_filters[0].value


def test_filter_is_null():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM sales WHERE deleted_at IS NULL GROUP BY region",
        "model-1",
    )
    assert any(f.operator == "is_null" and f.dimension_name == "deleted_at" for f in q.filters)


# ---------------------------------------------------------------------------
# ORDER BY / LIMIT / OFFSET
# ---------------------------------------------------------------------------

def test_order_by_desc():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM sales GROUP BY region ORDER BY revenue DESC",
        "model-1",
    )
    assert len(q.order_by) == 1
    col, direction = q.order_by[0]
    assert col == "revenue"
    assert direction == "desc"


def test_order_by_positional():
    q = parse_sql_to_ir(
        "SELECT city, country FROM sales ORDER BY 1",
        "model-1",
    )
    assert len(q.order_by) == 1
    col, direction = q.order_by[0]
    assert col == "city"
    assert direction == "asc"


def test_order_by_positional_second_column():
    q = parse_sql_to_ir(
        "SELECT city, country FROM sales ORDER BY 2 DESC",
        "model-1",
    )
    assert len(q.order_by) == 1
    col, direction = q.order_by[0]
    assert col == "country"
    assert direction == "desc"


def test_limit_and_offset():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM sales GROUP BY region LIMIT 100 OFFSET 20",
        "model-1",
    )
    assert q.limit == 100
    assert q.offset == 20


# Bug-6083 / F-003-17: ``FETCH FIRST/NEXT n ROWS ONLY`` (ANSI, emitted by JDBC
# tooling) parses into an ``exp.Fetch`` node, not ``exp.Limit`` — the row count
# lives in ``args["count"]`` with ``limit.expression`` absent. The old
# extractor returned ``limit=None`` and the source rewriter emitted no LIMIT,
# returning EVERY row where the user asked for n.

def test_fetch_first_n_rows_only_maps_to_limit():
    q = parse_sql_to_ir(
        "SELECT region FROM sales FETCH FIRST 5 ROWS ONLY",
        "model-1",
    )
    assert q.limit == 5


def test_fetch_next_n_rows_only_with_offset():
    q = parse_sql_to_ir(
        "SELECT region FROM sales ORDER BY region OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
        "model-1",
    )
    assert q.offset == 5
    assert q.limit == 10


def test_fetch_first_with_ties_rejected_loudly():
    # WITH TIES can return more than n rows — not representable as LIMIT n;
    # must fail loud, never silently bound or drop.
    with pytest.raises(UnsupportedSQL):
        parse_sql_to_ir(
            "SELECT region FROM sales ORDER BY region FETCH FIRST 5 ROWS WITH TIES",
            "model-1",
        )


def test_fetch_first_percent_rejected_loudly():
    with pytest.raises(UnsupportedSQL):
        parse_sql_to_ir(
            "SELECT region FROM sales FETCH FIRST 5 PERCENT ROWS ONLY",
            "model-1",
        )


def test_fetch_first_row_only_no_count_defaults_to_one():
    # FETCH FIRST ROW ONLY == FETCH FIRST 1 ROW ONLY; a missing count must not
    # silently become an unbounded result.
    q = parse_sql_to_ir("SELECT region FROM sales FETCH FIRST ROW ONLY", "model-1")
    assert q.limit == 1


def test_fetch_next_row_only_no_count_defaults_to_one():
    q = parse_sql_to_ir(
        "SELECT region FROM sales ORDER BY region OFFSET 2 ROWS FETCH NEXT ROW ONLY",
        "model-1",
    )
    assert q.offset == 2
    assert q.limit == 1


def test_fetch_first_decimal_count_rejected_loudly():
    # A decimal count is not a valid LIMIT n; fail closed rather than silently
    # returning an unbounded result.
    with pytest.raises(UnsupportedSQL):
        parse_sql_to_ir(
            "SELECT region FROM sales FETCH FIRST 2.5 ROWS ONLY",
            "model-1",
        )


def test_fetch_first_parameter_count_rejected_loudly():
    # A bind-parameter count ($1) must fail closed (typed error), never raise
    # an uncaught TypeError (500) or return unbounded.
    with pytest.raises(UnsupportedSQL):
        parse_sql_to_ir(
            "SELECT region FROM sales FETCH FIRST $1 ROWS ONLY",
            "model-1",
        )


# Bug-6569: the plain ``LIMIT`` branch had the same silent-drop class as the
# FETCH branch (Bug-6083). A non-integer LIMIT literal (``LIMIT 2.5``) hit
# ``int("2.5")`` -> ValueError -> caught -> ``limit=None`` -> UNBOUNDED result
# where the user asked to cap the rows. Fail closed instead.

def test_limit_decimal_rejected_loudly():
    with pytest.raises(UnsupportedSQL):
        parse_sql_to_ir(
            "SELECT region FROM sales GROUP BY region LIMIT 2.5",
            "model-1",
        )


def test_limit_parameter_rejected_loudly():
    # A bind-parameter LIMIT ($1) previously raised an uncaught TypeError
    # (500). It must fail closed with a typed error instead.
    with pytest.raises(UnsupportedSQL):
        parse_sql_to_ir(
            "SELECT region FROM sales GROUP BY region LIMIT $1",
            "model-1",
        )


def test_limit_all_means_unbounded():
    # ``LIMIT ALL`` (Postgres) is an explicit request for no limit; it must
    # resolve to limit=None (not fail loud, not raise TypeError).
    q = parse_sql_to_ir(
        "SELECT region FROM sales GROUP BY region LIMIT ALL",
        "model-1",
    )
    assert q.limit is None


def test_limit_null_means_unbounded():
    # ``LIMIT NULL`` (Postgres) is also an explicit request for no limit; it
    # must resolve to limit=None, not a typed rejection.
    q = parse_sql_to_ir(
        "SELECT region FROM sales GROUP BY region LIMIT NULL",
        "model-1",
    )
    assert q.limit is None


def test_limit_integer_still_extracted():
    q = parse_sql_to_ir(
        "SELECT region FROM sales GROUP BY region LIMIT 25",
        "model-1",
    )
    assert q.limit == 25


# Bug-6082 / F-003-16: positional GROUP BY (``GROUP BY 1``) must resolve against
# the SELECT list exactly as ORDER BY positional refs do — otherwise the
# aggregate form falsely raises GroupByError and the non-aggregate form silently
# rebuilds without GROUP BY (duplicate rows).

def test_group_by_positional_resolves_to_column_with_aggregate():
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) FROM sales GROUP BY 1",
        "model-1",
    )
    assert q.grain == ["region"]
    assert "amount" in q.requested_measures
    assert q.has_function_grain is False


def test_group_by_positional_resolves_without_aggregate():
    q = parse_sql_to_ir(
        "SELECT region FROM sales GROUP BY 1",
        "model-1",
    )
    assert q.grain == ["region"]


def test_group_by_positional_multi_column():
    q = parse_sql_to_ir(
        "SELECT region, city, SUM(amount) FROM sales GROUP BY 1, 2",
        "model-1",
    )
    assert q.grain == ["region", "city"]


def test_group_by_positional_resolves_paren_wrapped_select_item():
    # SELECT (region) — a transparent Paren column — must resolve positionally
    # exactly as an explicit GROUP BY region groups it; no false GroupByError.
    q = parse_sql_to_ir(
        "SELECT (region), SUM(amount) FROM sales GROUP BY 1",
        "model-1",
    )
    assert q.grain == ["region"]
    assert q.has_function_grain is False


def test_group_by_parenthesised_positional_resolves():
    # GROUP BY (1) — the grouping item itself is Paren-wrapped — must resolve
    # positionally just like GROUP BY 1.
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) FROM sales GROUP BY (1)",
        "model-1",
    )
    assert q.grain == ["region"]
    assert q.has_function_grain is False


def test_group_by_parenthesised_column_resolves():
    # GROUP BY (region) must group exactly as GROUP BY region.
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) FROM sales GROUP BY (region)",
        "model-1",
    )
    assert q.grain == ["region"]
    assert q.has_function_grain is False


# Bug-6084: GROUP BY <output alias> is valid PostgreSQL (GROUP BY may reference
# a SELECT-list output column name). The strict JDBC GROUP BY gate must not
# reject it as a bare-column-not-in-GROUP-BY error.
def test_group_by_output_alias_not_rejected():
    q = parse_sql_to_ir(
        "SELECT region AS r, SUM(amount) FROM sales GROUP BY r",
        "model-1",
    )
    # No false GroupByError; region is recognised as grouped via its alias.
    assert q.grain == ["region"]
    assert "amount" in q.requested_measures


def test_group_by_alias_input_column_ambiguity_still_rejected():
    # PostgreSQL ambiguity rule: GROUP BY b binds to the INPUT column b, not the
    # alias of a. So a (aliased b) is ungrouped and PG rejects the query — the
    # alias-resolution leniency must NOT mask this.
    with pytest.raises(GroupByError) as exc:
        parse_sql_to_ir(
            "SELECT amount AS region, region, SUM(qty) FROM sales GROUP BY region",
            "model-1",
        )
    assert "amount" in str(exc.value)


# F-2 (Fable sensitive-worktree review): the Bug-6084 output-alias
# normalization must apply to BARE-COLUMN aliases only. An alias over an
# AGGREGATE is not a valid GROUP BY target in PostgreSQL — it must NOT resolve
# to the measure's inner column, otherwise the invalid query binds the measure
# column as a dimension and executes garbage grouping instead of failing.
def test_group_by_aggregate_alias_not_normalized_to_measure_column():
    # Bug-6858: GROUP BY on an aggregate alias (e.g. ``GROUP BY total`` where
    # ``total`` is ``SUM(amount)``) must fail at parse time with a GroupByError,
    # matching PostgreSQL semantics ("aggregate functions are not allowed in
    # GROUP BY"). Previously this silently injected the aggregate alias as a
    # grain dimension and executed garbage grouping.
    with pytest.raises(GroupByError, match="aggregate or expression alias"):
        parse_sql_to_ir(
            "SELECT SUM(amount) AS total, region FROM sales GROUP BY total, region",
            "model-1",
        )


def test_group_by_expression_alias_not_normalized_to_inner_column():
    # An alias over a scalar expression (UPPER/CASE/DATE_TRUNC) is likewise not a
    # bare-column GROUP BY target; the grain records the alias, not the inner
    # column. Results stay correct via the passthrough raw-preservation path;
    # this just stops the grain metadata from claiming a grain the query does not
    # actually have (F-6).
    q = parse_sql_to_ir(
        "SELECT UPPER(region) AS r, SUM(amount) FROM sales GROUP BY r",
        "model-1",
    )
    assert "region" not in q.grain
    assert "r" in q.grain
    assert "amount" in q.requested_measures


def test_group_by_bare_column_alias_still_normalized():
    # Guard against over-restriction: a BARE-column alias must STILL resolve so
    # ``SELECT region AS r ... GROUP BY r`` groups by ``region`` (Bug-6084 path
    # preserved).
    q = parse_sql_to_ir(
        "SELECT region AS r, SUM(amount) FROM sales GROUP BY r",
        "model-1",
    )
    assert q.grain == ["region"]
    assert "amount" in q.requested_measures


def test_group_by_output_alias_with_extra_bare_still_rejected():
    # Leniency must not mask a genuinely ungrouped column: city is neither
    # grouped nor aliased into the GROUP BY, so PostgreSQL rejects it too.
    with pytest.raises(GroupByError) as exc:
        parse_sql_to_ir(
            "SELECT region AS r, city, SUM(amount) FROM sales GROUP BY r",
            "model-1",
        )
    assert "city" in str(exc.value)


# Bug-6093: COUNT(DISTINCT a, b) counts distinct (a, b) tuples. It must NOT
# collapse to COUNT(DISTINCT a) — doing so lets it match an ``a__count_distinct``
# aggregate column and serve a wrong number. Multi-column distinct is
# passthrough (source-only), never an analytical/routable single-column stat.
def test_count_distinct_multi_column_not_collapsed_to_first():
    q = parse_sql_to_ir(
        "SELECT COUNT(DISTINCT customer_id, product_id) AS c FROM sales",
        "model-1",
    )
    se = q.select_expressions[0]
    assert se.classification == "passthrough"
    assert se.agg_function == "count_distinct"
    # No single physical measure/stat column serves distinct tuples.
    assert se.inner_column is None
    assert "customer_id" not in q.requested_measures
    assert getattr(se, "composable", False) is False


def test_count_distinct_single_column_still_routable():
    # Guard against over-correction: single-column distinct stays analytical.
    q = parse_sql_to_ir(
        "SELECT COUNT(DISTINCT customer_id) AS c FROM sales",
        "model-1",
    )
    se = q.select_expressions[0]
    assert se.classification == "analytical"
    assert se.agg_function == "count_distinct"
    assert se.inner_column == "customer_id"
    assert "customer_id" in q.requested_measures


# Bug-6085: unquoted identifiers fold to lower-case in PostgreSQL, so
# SELECT Region ... GROUP BY region groups correctly. The strict gate must
# compare case-insensitively rather than falsely rejecting the query.
def test_group_by_case_insensitive_fold_not_rejected():
    q = parse_sql_to_ir(
        "SELECT Region, SUM(amount) FROM sales GROUP BY region",
        "model-1",
    )
    assert "amount" in q.requested_measures


def test_group_by_cast_still_function_grain():
    # Regression guard: GROUP BY x::text keeps its existing behaviour — the
    # underlying column joins the grain but the cast marks function grain.
    q = parse_sql_to_ir(
        "SELECT success_flag, SUM(amount) FROM sales GROUP BY success_flag::text",
        "model-1",
    )
    assert "success_flag" in q.grain
    assert q.has_function_grain is True


def test_group_by_positional_to_expression_is_function_grain():
    # GROUP BY 1 pointing at an expression SELECT item is function grain,
    # not a bare-column grain (mirrors GROUP BY DATE_TRUNC(...)).
    q = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', ts) AS m, SUM(amount) FROM sales GROUP BY 1",
        "model-1",
    )
    assert q.grain == []
    assert q.has_function_grain is True


# Bug-6081 / F-003-15: a bare boolean column / boolean literal WHERE conjunct is
# not extractable, so it MUST flag has_unresolvable_where (fail-closed audit).

def test_bare_boolean_column_where_flags_unresolvable():
    q = parse_sql_to_ir("SELECT region FROM sales WHERE is_active", "model-1")
    assert q.has_unresolvable_where is True
    assert q.filters == []


def test_boolean_literal_where_flags_unresolvable():
    q = parse_sql_to_ir("SELECT region FROM sales WHERE FALSE", "model-1")
    assert q.has_unresolvable_where is True


def test_boolean_comparison_literal_where_flags_unresolvable():
    q = parse_sql_to_ir("SELECT region FROM sales WHERE 1=0", "model-1")
    assert q.has_unresolvable_where is True
    assert q.filters == []


def test_bare_boolean_and_comparison_keeps_flag_and_extracts_comparison():
    q = parse_sql_to_ir(
        "SELECT region FROM sales WHERE is_active AND region = 'EU'",
        "model-1",
    )
    # The region comparison is still extracted faithfully...
    assert any(f.dimension_name == "region" and f.operator == "eq" for f in q.filters)
    # ...but the unresolvable bare boolean forces raw-WHERE preservation.
    assert q.has_unresolvable_where is True


# ---------------------------------------------------------------------------
# COUNT DISTINCT / model_id / protocol / fingerprint
# ---------------------------------------------------------------------------

def test_count_distinct_extracted_as_measure():
    q = parse_sql_to_ir(
        "SELECT COUNT(DISTINCT user_id) FROM events GROUP BY date",
        "model-1",
    )
    assert "user_id" in q.requested_measures


def test_model_id_preserved():
    q = parse_sql_to_ir("SELECT SUM(x) FROM t GROUP BY y", "my-model-uuid")
    assert q.model_id == "my-model-uuid"


def test_protocol_defaults_to_jdbc():
    q = parse_sql_to_ir("SELECT SUM(x) FROM t GROUP BY y", "m1")
    assert q.protocol == "jdbc"


def test_fingerprint_is_deterministic():
    sql = "SELECT SUM(revenue) FROM sales GROUP BY region"
    q1 = parse_sql_to_ir(sql, "model-1")
    q2 = parse_sql_to_ir(sql, "model-1")
    assert q1.query_fingerprint == q2.query_fingerprint


def test_fingerprint_differs_for_different_queries():
    q1 = parse_sql_to_ir("SELECT SUM(revenue) FROM s GROUP BY region", "m1")
    q2 = parse_sql_to_ir("SELECT SUM(orders) FROM s GROUP BY month", "m1")
    assert q1.query_fingerprint != q2.query_fingerprint


def test_returns_logical_query_instance():
    q = parse_sql_to_ir("SELECT SUM(x) FROM t GROUP BY y", "m1")
    assert isinstance(q, LogicalQuery)


# ---------------------------------------------------------------------------
# GROUP BY enforcement (PostgreSQL semantics for JDBC; warning for others)
# ---------------------------------------------------------------------------

def test_jdbc_rejects_missing_group_by_entirely():
    # Mixing a bare column with an aggregate and no GROUP BY is invalid SQL.
    # JDBC callers get a Postgres-style error instead of silent auto-grouping.
    with pytest.raises(GroupByError) as exc:
        parse_sql_to_ir(
            "SELECT account_type, COUNT(1) FROM modelx",
            "m1",
            protocol="jdbc",
        )
    assert "account_type" in str(exc.value)
    assert "GROUP BY" in str(exc.value)


def test_jdbc_rejects_partial_group_by():
    # One bare column listed in GROUP BY, another not.
    with pytest.raises(GroupByError) as exc:
        parse_sql_to_ir(
            "SELECT region, country, SUM(revenue) FROM sales GROUP BY region",
            "m1",
            protocol="jdbc",
        )
    assert "country" in str(exc.value)


def test_jdbc_accepts_pure_aggregate_without_group_by():
    # SELECT only aggregates — no bare columns — is valid SQL.
    q = parse_sql_to_ir(
        "SELECT COUNT(1) FROM modelx",
        "m1",
        protocol="jdbc",
    )
    assert q.grain == []
    assert q.requested_measures == ["__row_count"]


def test_jdbc_accepts_all_bare_columns_in_group_by():
    q = parse_sql_to_ir(
        "SELECT region, SUM(revenue) FROM sales GROUP BY region",
        "m1",
        protocol="jdbc",
    )
    assert q.grain == ["region"]
    assert q.requested_measures == ["revenue"]
    assert "Columns not in GROUP BY" not in " ".join(q.syntax_warnings)


def test_jdbc_rejects_count_star_with_bare_column():
    # The exact form reported by the user: `count(1)` must be treated the
    # same as any other aggregate.
    with pytest.raises(GroupByError):
        parse_sql_to_ir(
            "select account_type, count(1) from modelx",
            "m1",
            protocol="jdbc",
        )


def test_xmla_allows_missing_group_by_but_warns():
    # XMLA has no explicit GROUP BY — grouping is inferred from the axis
    # selection.  We must not raise for this protocol, but we should still
    # emit a warning so the trace surfaces the implicit conversion.
    q = parse_sql_to_ir(
        "SELECT account_type, COUNT(1) FROM modelx",
        "m1",
        protocol="xmla",
    )
    assert q.requested_dimensions == ["account_type"]
    assert q.requested_measures == ["__row_count"]
    assert any("GROUP BY" in w for w in q.syntax_warnings)


def test_dax_allows_missing_group_by_but_warns():
    q = parse_sql_to_ir(
        "SELECT account_type, COUNT(1) FROM modelx",
        "m1",
        protocol="dax",
    )
    assert q.requested_dimensions == ["account_type"]
    assert any("GROUP BY" in w for w in q.syntax_warnings)


def test_jdbc_select_star_not_rejected():
    # SELECT * with an aggregate is handled by a different path (select_star).
    # It shouldn't trip the GROUP BY check, which operates on named columns.
    q = parse_sql_to_ir("SELECT * FROM sales", "m1", protocol="jdbc")
    assert q.select_star is True


def test_jdbc_no_aggregate_bare_columns_ok():
    # A pure SELECT without any aggregates must not be rejected — the check
    # only applies when aggregates and bare columns are mixed.
    q = parse_sql_to_ir(
        "SELECT region, country FROM sales",
        "m1",
        protocol="jdbc",
    )
    assert q.requested_measures == []
    assert set(q.requested_dimensions) == {"region", "country"}


# ---------------------------------------------------------------------------
# Parser false-positives surfaced by the strict GROUP BY check
# ---------------------------------------------------------------------------

def test_filter_clause_column_not_bare():
    # `COUNT(*) FILTER (WHERE created_at IS NOT NULL)` must not cause
    # `created_at` to leak into select_bare and trigger GroupByError.
    q = parse_sql_to_ir(
        "SELECT COUNT(*) FILTER (WHERE created_at IS NOT NULL) FROM orders",
        "m1",
        protocol="jdbc",
    )
    assert q.requested_measures == ["__row_count"] or "__row_count" in q.requested_measures or q.requested_measures == []
    # The key assertion: parse must succeed and not flag created_at as bare.
    assert "created_at" not in q.requested_dimensions


def test_within_group_clause_column_not_bare():
    # PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) — the WITHIN GROUP wrapper
    # must not leak `x` into select_bare.
    q = parse_sql_to_ir(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM sales",
        "m1",
        protocol="jdbc",
    )
    assert "amount" not in q.requested_dimensions


def test_unregistered_aggregate_name_not_leaked():
    # STDDEV_POP normalizes to `stddevpop` in sqlglot but the registry
    # uses `stddev_pop`.  We must still recognize it as an aggregate via
    # isinstance(inner, exp.AggFunc) so `x` is not treated as bare.
    q = parse_sql_to_ir(
        "SELECT STDDEV_POP(amount) FROM sales",
        "m1",
        protocol="jdbc",
    )
    assert "amount" not in q.requested_dimensions


def test_string_agg_column_not_leaked():
    # STRING_AGG normalizes to `groupconcat` in sqlglot — another name
    # mismatch that must be handled via AggFunc isinstance check.
    q = parse_sql_to_ir(
        "SELECT STRING_AGG(name, ',') FROM users",
        "m1",
        protocol="jdbc",
    )
    assert "name" not in q.requested_dimensions


def test_cast_in_group_by_recognized_as_grain():
    # GROUP BY success_flag::text — the Cast wrapper must be unwrapped so
    # success_flag ends up in grain, letting SELECT success_flag through.
    q = parse_sql_to_ir(
        "SELECT success_flag, COUNT(*) FROM events GROUP BY success_flag::text",
        "m1",
        protocol="jdbc",
    )
    assert "success_flag" in q.grain


def test_case_expression_column_not_bare():
    # CASE WHEN region = 'US' THEN 1 ELSE 0 END alongside COUNT(*) must not
    # treat `region` as bare — it's inside an expression, not a bare column.
    q = parse_sql_to_ir(
        "SELECT CASE WHEN region = 'US' THEN 1 ELSE 0 END, COUNT(*) FROM sales",
        "m1",
        protocol="jdbc",
    )
    # Parse must succeed (no GroupByError).  `region` appears in
    # requested_dimensions so the binder can resolve it, but it is not
    # counted as a PG-bare column for the strict check.
    assert "region" in q.requested_dimensions


def test_extract_function_column_not_bare():
    # EXTRACT(YEAR FROM order_date) alongside COUNT(*) — the function wraps
    # order_date, so it's not a PG-bare column.
    q = parse_sql_to_ir(
        "SELECT EXTRACT(YEAR FROM order_date), COUNT(*) FROM orders GROUP BY EXTRACT(YEAR FROM order_date)",
        "m1",
        protocol="jdbc",
    )
    assert "order_date" in q.requested_dimensions


def test_grouping_sets_columns_added_to_grain():
    # GROUPING SETS stores its columns in a sibling arg, not group.expressions.
    # Harvester must pick them up so SELECT bare columns are recognised.
    q = parse_sql_to_ir(
        "SELECT a, b, COUNT(*) FROM t GROUP BY GROUPING SETS ((a, b), (a), ())",
        "m1",
        protocol="jdbc",
    )
    assert "a" in q.grain
    assert "b" in q.grain


def test_rollup_columns_added_to_grain():
    q = parse_sql_to_ir(
        "SELECT a, b, c, COUNT(*) FROM t GROUP BY ROLLUP (a, b, c)",
        "m1",
        protocol="jdbc",
    )
    assert set(q.grain) >= {"a", "b", "c"}


def test_cube_columns_added_to_grain():
    q = parse_sql_to_ir(
        "SELECT a, b, COUNT(*) FROM t GROUP BY CUBE (a, b)",
        "m1",
        protocol="jdbc",
    )
    assert set(q.grain) >= {"a", "b"}


def test_window_function_allows_bare_columns():
    # SUM(x) OVER (PARTITION BY ...) is a window function, not a grouping
    # aggregate.  Bare columns alongside it must not trigger GroupByError.
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) OVER (PARTITION BY region) FROM sales",
        "m1",
        protocol="jdbc",
    )
    assert "region" in q.requested_dimensions


def test_jdbc_rejects_consecutive_commas():
    # `SELECT 1 AS x, 2,, ,account_type FROM modely` is invalid SQL —
    # real Postgres raises "syntax error at or near ','".  sqlglot silently
    # recovers, so we enforce the same rejection for JDBC callers.
    with pytest.raises(SyntaxErrorInSQL) as exc:
        parse_sql_to_ir(
            "SELECT 1 AS x, 2,, ,account_type FROM modely",
            "m1",
            protocol="jdbc",
        )
    assert "Consecutive commas" in str(exc.value)


def test_jdbc_accepts_consecutive_commas_inside_string_literal():
    # F-003-05: a comma sequence INSIDE a string literal is valid SQL —
    # real Postgres accepts `WHERE note = 'wait,, what'`. The consecutive-
    # comma check must run against literal-stripped text, not the raw SQL,
    # so this query must NOT raise.
    q = parse_sql_to_ir(
        "SELECT note FROM modelx WHERE note = 'wait,, what'",
        "m1",
        protocol="jdbc",
    )
    assert isinstance(q, LogicalQuery)


def test_jdbc_groupby_error_pluralises_for_multiple_columns():
    # F-003-11: the GROUP BY error noun must agree with the column count.
    with pytest.raises(GroupByError) as exc:
        parse_sql_to_ir(
            "SELECT region, country, SUM(revenue) FROM sales GROUP BY region",
            "m1",
            protocol="jdbc",
        )
    # `country` is the only ungrouped bare column -> singular.
    assert str(exc.value).startswith("column ")
    with pytest.raises(GroupByError) as exc2:
        parse_sql_to_ir(
            "SELECT region, country, SUM(revenue) FROM sales",
            "m1",
            protocol="jdbc",
        )
    # Both `region` and `country` ungrouped -> plural.
    assert str(exc2.value).startswith("columns ")


def test_scientific_notation_literal_parses_as_float():
    # F-003-10: `WHERE b > 1e5` must yield a numeric (float) filter value,
    # not the raw string "1e5" — strict-typed targets reject a quoted string
    # in a numeric comparison.
    q = parse_sql_to_ir("SELECT a FROM modelx WHERE b > 1e5", "m1")
    matches = [f for f in q.filters if f.dimension_name == "b"]
    assert len(matches) == 1
    assert matches[0].value == 100000.0
    assert isinstance(matches[0].value, float)


def test_scientific_literal_preserves_original_spelling():
    # Bug-5539 (Codex round-3 finding 3): the float value carries its ORIGINAL
    # spelling so the strict render-time grammar can reject the scientific form
    # as a bare token — without regressing F-003-10's float round-trip above.
    from src.parsing.sql_parser import NumericLiteral
    q = parse_sql_to_ir("SELECT a FROM modelx WHERE b > 1e5", "m1")
    value = [f for f in q.filters if f.dimension_name == "b"][0].value
    assert isinstance(value, NumericLiteral)
    assert value.original_text == "1e5"


def test_jdbc_rejects_stray_semicolon():
    # `SELECT a; FROM t` — stray semicolon truncates the parse; reject it.
    with pytest.raises(SyntaxErrorInSQL) as exc:
        parse_sql_to_ir(
            "SELECT a; extra garbage FROM t",
            "m1",
            protocol="jdbc",
        )
    assert "semicolon" in str(exc.value).lower()


def test_jdbc_rejects_extra_token_in_from_clause():
    # `FROM modely modelx m` — sqlglot parses `modely AS modelx` and
    # silently drops the trailing `m`. Real Postgres would reject. JDBC
    # must escalate the recovered parse error to SyntaxErrorInSQL.
    with pytest.raises(SyntaxErrorInSQL) as exc:
        parse_sql_to_ir(
            "SELECT * FROM modely modelx m",
            "m1",
            protocol="jdbc",
        )
    msg = str(exc.value).lower()
    assert "malformed sql" in msg or "unexpected token" in msg


def test_xmla_rejects_meaning_changing_recovery():
    # Bug-7916: XMLA protocol is now strict for meaning-changing parse
    # recoveries. When sqlglot produces errors during recovery (token
    # dropped with altered semantics), XMLA raises SyntaxErrorInSQL.
    # The "modelx m" after "modely" triggers a parse error.
    with pytest.raises(SyntaxErrorInSQL):
        parse_sql_to_ir(
            "SELECT * FROM modely modelx m",
            "m1",
            protocol="xmla",
        )


def test_xmla_keeps_consecutive_commas_as_warning():
    # XMLA protocol stays permissive for harmless syntactic quirks
    # (consecutive commas, stray semicolons) that XMLA/DAX clients
    # commonly send. These are not meaning-changing recoveries.
    # The pre-scan fires only for JDBC.
    q = parse_sql_to_ir(
        "SELECT a,, b FROM t",
        "m1",
        protocol="xmla",
    )
    assert any("Consecutive commas" in w for w in q.syntax_warnings)


def test_cte_with_mixed_columns_not_rejected():
    # CTEs trigger has_complex_sql and route to passthrough — the strict
    # check must be skipped so Postgres can enforce its own semantics.
    q = parse_sql_to_ir(
        "WITH x AS (SELECT a, b FROM t) SELECT a, SUM(b) FROM x",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql is True


def test_column_vs_column_where_marked_unresolvable():
    """Regression — a WHERE predicate comparing two columns has no
    Literal RHS, so the LogicalFilter extractor silently drops it.
    The parser must flag it as unresolvable so the rewriter preserves
    the raw WHERE through column-name substitution rather than
    rebuilding the SQL with no filter at all.
    """
    q = parse_sql_to_ir(
        "SELECT base_amount, base_amount_ytd FROM modely "
        "WHERE base_amount <> base_amount_ytd LIMIT 10",
        "m1",
    )
    assert q.has_unresolvable_where is True


def test_column_vs_literal_where_remains_resolvable():
    """Counter-test: a normal column-vs-literal WHERE is still
    representable as a LogicalFilter and must NOT be flagged as
    unresolvable (otherwise we lose all aggregate-matching for
    perfectly ordinary filters)."""
    q = parse_sql_to_ir(
        "SELECT SUM(amount) FROM t WHERE country = 'US' GROUP BY region",
        "m1",
    )
    assert q.has_unresolvable_where is False


# ---------------------------------------------------------------------------
# Invariant — silent-drop audit (Bug-102 follow-up)
#
# Every predicate in a parsed WHERE clause must be EITHER faithfully
# represented in ``filters`` OR caught by ``has_unresolvable_where`` /
# ``has_complex_sql``. Anything in between is a silent-drop bug like
# Bug-102. The matrix below pins each known dangerous shape.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "where_clause, why",
    [
        # Column-vs-column comparisons (Bug-102 itself)
        ("a <> b", "neq col vs col"),
        ("a = b", "eq col vs col"),
        ("a > b", "gt col vs col"),
        ("a >= b", "gte col vs col"),
        ("a < b", "lt col vs col"),
        ("a <= b", "lte col vs col"),
        # Function / arithmetic on either side — find(Column)+find(Literal)
        # would emit the inner column with the wrong literal.
        ("LOWER(name) = 'foo'", "function on lhs"),
        ("a + 1 > 5", "arith on lhs"),
        ("5 = a + 1", "arith on rhs"),
        ("ABS(a) < 10", "function on lhs"),
        # NOT(comparison) — descending into the inner EQ would emit the
        # opposite-polarity filter.
        ("NOT (a = 1)", "polarity flip via NOT"),
        ("NOT (b > 5)", "polarity flip via NOT"),
        # IN with column or function on lhs / mixed values / subquery
        ("LOWER(c) IN ('a', 'b')", "in with function lhs"),
        ("c IN ('a', other_col)", "in with column value"),
        # BETWEEN with column bounds
        ("a BETWEEN low_col AND 10", "between with column low"),
        ("a BETWEEN 1 AND high_col", "between with column high"),
        # LIKE with column on rhs / function on lhs
        ("a LIKE other_col", "like col vs col"),
        ("LOWER(a) LIKE 'x%'", "like with function lhs"),
        # OR / EXISTS / scalar subqueries (already covered, asserted here too)
        ("a = 1 OR b = 2", "or"),
        ("EXISTS (SELECT 1 FROM t WHERE t.a = 1)", "exists"),
        ("NOT EXISTS (SELECT 1 FROM t)", "not exists"),
        ("a > (SELECT MAX(x) FROM t)", "scalar subquery"),
    ],
)
def test_where_predicate_invariant(where_clause, why):
    """Invariant: any predicate the extractor can't faithfully represent
    must be flagged via has_unresolvable_where (or has_complex_sql for
    structural cases like subqueries). Anything else is a silent drop.
    """
    q = parse_sql_to_ir(
        f"SELECT a FROM t WHERE {where_clause}",
        "m1",
    )
    assert q.has_unresolvable_where or q.has_complex_sql, (
        f"WHERE {where_clause!r} ({why}) not flagged — extractor would "
        f"drop or misread the predicate. Filters: {q.filters!r}"
    )


@pytest.mark.parametrize(
    "where_clause",
    [
        "a = 1",
        "a <> 1",
        "a > 1",
        "a >= 1",
        "a < 1",
        "a <= 1",
        "a IS NULL",
        "a IS NOT NULL",
        "a IN (1, 2, 3)",
        "a BETWEEN 1 AND 10",
        "a LIKE 'x%'",
        "a = 1 AND b = 2",
        "a IN ('x','y') AND b BETWEEN 1 AND 10",
    ],
)
def test_simple_where_remains_extractable(where_clause):
    """Counter-side of the invariant: ordinary column-vs-literal
    predicates must NOT be flagged as unresolvable, otherwise every
    aggregate-matching path would be bypassed."""
    q = parse_sql_to_ir(
        f"SELECT a FROM t WHERE {where_clause}",
        "m1",
    )
    assert q.has_unresolvable_where is False, (
        f"WHERE {where_clause!r} wrongly flagged unresolvable — this "
        f"would push routine queries through the raw-WHERE path and "
        f"defeat aggregate matching."
    )


# ---------------------------------------------------------------------------
# Bug-5325 (F-P4ac-01) — NOT LIKE polarity must survive the parser.
#
# sqlglot 30.8 parses ``col NOT LIKE '%x%'`` as a single ``exp.Like`` node
# carrying ``negate=True`` (older versions emitted ``Not(Like(...))``). The
# extractor used to ignore that flag and emit a POSITIVE ``like`` filter,
# silently inverting the predicate and returning the complement of the rows
# the user asked for (CRITICAL, data-exposure-adjacent — reaches XMLA label
# filters). Same defect CLASS as Bug-5110 (a WHERE shape slipping past the
# audit into a wrong filter); the remedy (Option B / Bug-5110 pattern) flags a
# negated Like unresolvable so the rewriter preserves the raw ``NOT LIKE``.
# ---------------------------------------------------------------------------

def test_not_like_never_inverted_to_positive_like():
    """CRITICAL invariant (Bug-5333, AKA F-P4ac-01): a raw ``NOT LIKE`` predicate
    must NEVER be extracted as a positive ``like`` filter — that silently returns
    the complement of the intended rows (data-exposure adjacent).

    The merged fix is **Option A** (structured ``not_like``). Its observable shape
    is sqlglot-version-dependent:
      * sqlglot >=30.8 parses ``NOT LIKE`` as a single ``Like(negate=True)`` node,
        which the extractor turns into a ``not_like`` LogicalFilter (correct
        polarity), so ``has_unresolvable_where`` is False.
      * older sqlglot (<30.8, e.g. host 30.4.x) parses it as ``Not(Like(...))``,
        which the audit flags as unresolvable so the rewriter preserves the raw
        ``NOT LIKE`` verbatim.
    In BOTH shapes the predicate's polarity is preserved — it is either extracted
    as ``not_like`` or carried verbatim on the raw-WHERE path. The ONE thing that
    must NEVER happen is a positive ``like`` for ``col``."""
    q = parse_sql_to_ir(
        "SELECT a FROM t WHERE col NOT LIKE '%LOAN%'",
        "m1",
    )
    col_filters = [f for f in q.filters if f.dimension_name == "col"]
    # INVARIANT: a negated source must NEVER yield a positive 'like' phantom.
    assert not any(
        f.operator == "like" for f in col_filters
    ), "NOT LIKE must never be extracted as a positive 'like' — polarity inverted."
    # Polarity is preserved one of two ways depending on the sqlglot shape:
    if col_filters:
        # Option A on sqlglot >=30.8: extracted as a structured not_like.
        assert all(f.operator == "not_like" for f in col_filters)
        assert q.has_unresolvable_where is False
    else:
        # Legacy Not(Like) shape: flagged so raw NOT LIKE is preserved verbatim.
        assert q.has_unresolvable_where is True


def test_positive_like_still_extracted_as_like():
    """Counter-test: an ordinary positive LIKE must stay ``like`` — no
    regression from the NOT LIKE fix."""
    q = parse_sql_to_ir(
        "SELECT a FROM t WHERE col LIKE '%LOAN%'",
        "m1",
    )
    like_filters = [f for f in q.filters if f.dimension_name == "col"]
    assert len(like_filters) == 1
    assert like_filters[0].operator == "like"
    assert like_filters[0].value == "%LOAN%"
    assert q.has_unresolvable_where is False


def test_not_like_renders_not_like_sql():
    """End-to-end polarity: the ``not_like`` operator the extractor produces
    must render as ``NOT LIKE`` in the rewriter's condition path (not LIKE)."""
    from src.rewrite.conditions import _render_condition

    rendered = _render_condition('"col"', "not_like", "%LOAN%")
    assert "NOT LIKE" in rendered.upper()
    # Defend against a bare ``LIKE`` (the inverted form) leaking through.
    assert rendered.upper().count("LIKE") == 1
    assert "%LOAN%" in rendered


def test_not_ilike_preserves_polarity_via_raw_where():
    """``NOT ILIKE`` (case-insensitive negated) is not representable as a
    LogicalFilter without losing case-insensitivity, so it must be flagged
    unresolvable and preserved verbatim through the raw-WHERE path — never
    extracted as a positive (or case-sensitive) like."""
    q = parse_sql_to_ir(
        "SELECT a FROM t WHERE col NOT ILIKE '%LOAN%'",
        "m1",
    )
    # No positive 'like'/'ilike' phantom may be emitted.
    assert not any(
        f.operator in ("like", "ilike") for f in q.filters
    )
    assert q.has_unresolvable_where is True


@pytest.mark.parametrize(
    "where_clause, forbidden_op",
    [
        # A negated source must never yield a positive 'like'.
        ("col NOT LIKE '%x%'", "like"),
        # A positive source must never yield a negated 'not_like'.
        ("col LIKE '%x%'", "not_like"),
    ],
)
def test_like_polarity_invariant(where_clause, forbidden_op):
    """Audit invariant: a ``Like`` node is either extracted with its CORRECT
    polarity OR flagged unresolvable — it may NEVER be extracted with the
    OPPOSITE polarity of the source predicate."""
    q = parse_sql_to_ir(
        f"SELECT a FROM t WHERE {where_clause}",
        "m1",
    )
    cols = [f for f in q.filters if f.dimension_name == "col"]
    assert not any(f.operator == forbidden_op for f in cols), (
        f"WHERE {where_clause!r} extracted with inverted polarity "
        f"({forbidden_op!r}) — silently wrong filter."
    )
    if not cols:
        # If not extracted, it must be flagged so raw WHERE is preserved.
        assert q.has_unresolvable_where or q.has_complex_sql


# ---------------------------------------------------------------------------
# Bug-102 hardening — phantom-filter defuse.
#
# The pre-Bug-102 ``_extract_filters`` used recursive ``find(Column)`` /
# ``find(Literal)`` calls inside each comparison, which fabricated
# ``LogicalFilter`` rows from operands buried inside subqueries,
# function calls, and arithmetic. Those phantom filters then
# contaminated routing (pocket fingerprints, miss-log workload). The
# strict per-conjunct extractor MUST NOT produce a filter for any of
# these shapes — the raw-WHERE preservation path is the only thing
# that should carry the predicate downstream.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "where_clause, label",
    [
        ("a + b > a * b", "arith-vs-arith"),
        ("a + b > (SELECT MAX(x) FROM u)", "arith-vs-subquery"),
        ("a > b + (SELECT MAX(x) FROM u)", "col-vs-arith-plus-subquery"),
        ("(SELECT MAX(x) FROM u) > (SELECT MIN(y) FROM v)", "subquery-vs-subquery"),
        ("(SELECT MAX(x) FROM u) > 100", "subquery-vs-literal"),
        ("(SELECT MAX(x) FROM u) > a", "subquery-vs-col"),
        ("1 + a + b = 2 + c", "literal-plus-cols-eq-literal-plus-col"),
        ("(a, b) IN (SELECT x, y FROM u)", "tuple-in-subquery"),
        ("1 + a IN (SELECT x FROM u)", "arith-in-subquery"),
        ("CONCAT('1', a) = '1foo'", "function-call-eq-literal"),
        ("a = 1 OR b = 2", "or-conjunction"),
        ("EXISTS (SELECT 1 FROM u WHERE u.x = 5)", "exists-with-inner-where"),
        ("(SELECT MAX(x) FROM u WHERE x = 99) > 100", "subquery-with-inner-where"),
    ],
)
def test_extract_filters_produces_no_phantom_filter(where_clause, label):
    """Any predicate the extractor cannot represent faithfully must
    yield zero filters — never a ``find(...)``-fabricated phantom row."""
    q = parse_sql_to_ir(
        f"SELECT a FROM t WHERE {where_clause}",
        "m1",
    )
    assert q.filters == [], (
        f"{label}: WHERE {where_clause!r} produced phantom filters "
        f"{q.filters!r}. has_unresolvable_where={q.has_unresolvable_where}"
    )


def test_extract_filters_keeps_legitimate_predicate_in_mixed_where():
    """When a WHERE mixes an extractable conjunct with an unresolvable
    one, the extractable conjunct is preserved in ``filters`` and the
    unresolvable one is left to the raw-WHERE preservation path."""
    q = parse_sql_to_ir(
        "SELECT a FROM t WHERE country = 'US' AND a + b > a * b",
        "m1",
    )
    assert [(f.dimension_name, f.operator, f.value) for f in q.filters] == [
        ("country", "eq", "US"),
    ]
    assert q.has_unresolvable_where is True


def test_extract_filters_flips_operator_when_literal_is_on_left():
    """``100 > amount`` must extract as ``amount < 100`` — symmetric
    operator semantics. The pre-Bug-102 extractor used ``find()`` and
    kept the original operator class regardless of side, which gave
    the wrong inequality whenever the literal was on the left."""
    q = parse_sql_to_ir(
        "SELECT a FROM t WHERE 100 > amount AND status = 'active'",
        "m1",
    )
    triples = [(f.dimension_name, f.operator, f.value) for f in q.filters]
    assert ("amount", "lt", 100) in triples
    assert ("status", "eq", "active") in triples


# ---------------------------------------------------------------------------
# Compound aggregate expressions (Bug-875)
# ---------------------------------------------------------------------------

def test_compound_div_extracts_both_measures():
    """SUM(a)/SUM(b) must register both a and b as measures, not just a.
    inner_column must be None so the binder sets has_passthrough_expressions."""
    q = parse_sql_to_ir(
        "SELECT SUM(fee_amount)/SUM(base_amount) FROM modely",
        "m1",
    )
    assert "fee_amount" in q.requested_measures
    assert "base_amount" in q.requested_measures
    se = q.select_expressions[0]
    assert se.classification == "passthrough"
    assert se.inner_column is None


def test_compound_add_extracts_both_measures():
    """SUM(a)+SUM(b) must register both a and b as measures."""
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) + SUM(cost) FROM t",
        "m1",
    )
    assert "revenue" in q.requested_measures
    assert "cost" in q.requested_measures
    se = q.select_expressions[0]
    assert se.classification == "passthrough"
    assert se.inner_column is None


def test_compound_div_count_extracts_both_measures():
    """SUM(a)/COUNT(b) must register both as measures."""
    q = parse_sql_to_ir(
        "SELECT SUM(amount)/COUNT(customer_id) FROM t",
        "m1",
    )
    assert "amount" in q.requested_measures
    assert "customer_id" in q.requested_measures
    se = q.select_expressions[0]
    assert se.classification == "passthrough"
    assert se.inner_column is None


def test_compound_mul_with_literal_extracts_both_measures():
    """SUM(a)*SUM(b)*0.001 must register both a and b as measures.
    inner_column=None ensures source rewriter uses table-name substitution."""
    q = parse_sql_to_ir(
        "SELECT SUM(fee_amount)*SUM(base_amount)*0.001 FROM modely",
        "m1",
    )
    assert "fee_amount" in q.requested_measures
    assert "base_amount" in q.requested_measures
    se = q.select_expressions[0]
    assert se.classification == "passthrough"
    assert se.inner_column is None


def test_star_over_aggregate_subquery_flattened():
    """SELECT * FROM (SELECT SUM(x)/SUM(y) FROM t) must flatten to the inner
    query so it is NOT flagged as has_complex_sql.  Bug-878 regression."""
    q = parse_sql_to_ir(
        "SELECT * FROM (SELECT SUM(fee_amount)/SUM(base_amount) FROM modely) t",
        "m1",
    )
    # Flattening should have removed the subquery wrapper
    assert not q.has_complex_sql, (
        "SELECT * FROM (aggregate subquery) should be flattened, not complex"
    )
    assert "fee_amount" in q.requested_measures
    assert "base_amount" in q.requested_measures
    se = q.select_expressions[0]
    assert se.classification == "passthrough"
    assert se.inner_column is None


def test_star_over_aggregate_subquery_with_alias_flattened():
    """SELECT * FROM (SELECT SUM(x) AS total FROM t) should flatten."""
    q = parse_sql_to_ir(
        "SELECT * FROM (SELECT SUM(transaction_amount) AS total FROM modely) sub",
        "m1",
    )
    assert not q.has_complex_sql
    assert "transaction_amount" in q.requested_measures


def test_star_over_aggregate_subquery_no_alias_flattened():
    """SELECT * FROM (SELECT SUM(x)/SUM(y) FROM t) without alias should flatten.
    Bug-878 regression — PostgreSQL requires aliases on FROM subqueries, but
    after flattening the subquery is gone so the alias is irrelevant."""
    q = parse_sql_to_ir(
        "SELECT * FROM (SELECT SUM(fee_amount)/SUM(base_amount) FROM modely)",
        "m1",
    )
    assert not q.has_complex_sql
    assert "fee_amount" in q.requested_measures
    assert "base_amount" in q.requested_measures


def test_star_over_star_subquery_with_where_flattened():
    """SELECT * FROM (SELECT * FROM t WHERE x='a') should flatten and merge WHERE."""
    q = parse_sql_to_ir(
        "SELECT * FROM (SELECT * FROM modely WHERE status='ACTIVE') q",
        "m1",
    )
    assert not q.has_complex_sql
    assert "modely" in q.from_tables


def test_star_over_subquery_with_outer_where_not_flattened():
    """SELECT * FROM (SELECT SUM(x) FROM t) WHERE y>1 — outer has WHERE,
    cannot flatten case 2 (but case 1 doesn't apply either since inner
    is not SELECT *). Should remain complex."""
    q = parse_sql_to_ir(
        "SELECT * FROM (SELECT SUM(fee_amount) FROM modely) t WHERE fee_amount > 1",
        "m1",
    )
    # This has outer WHERE + non-star inner, so neither case applies
    assert q.has_complex_sql


def test_single_agg_in_transparent_scalar_stays_analytical():
    """ABS(SUM(a)) with a single inner aggregate stays analytical for
    optimal aggregate routing."""
    q = parse_sql_to_ir(
        "SELECT ABS(SUM(amount)) FROM t",
        "m1",
    )
    assert "amount" in q.requested_measures
    se = q.select_expressions[0]
    assert se.classification == "analytical"
    assert se.agg_function == "sum"
    assert se.inner_column == "amount"


# ---------------------------------------------------------------------------
# Complex-SQL detection (catalog shapes #79-#85) — these constructs cannot be
# safely reconstructed by the semantic source rewriter and must route through
# passthrough-with-table-substitution (has_complex_sql=True).
# ---------------------------------------------------------------------------

def test_union_is_complex_sql():
    # Shape #79: set operation UNION.
    q = parse_sql_to_ir(
        "SELECT SUM(amount) FROM sales GROUP BY region "
        "UNION SELECT SUM(amount) FROM archive GROUP BY region",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql


def test_intersect_is_complex_sql():
    # Shape #80: set operation INTERSECT.
    q = parse_sql_to_ir(
        "SELECT region FROM sales INTERSECT SELECT region FROM archive",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql


def test_except_is_complex_sql():
    # Shape #81: set operation EXCEPT.
    q = parse_sql_to_ir(
        "SELECT region FROM sales EXCEPT SELECT region FROM archive",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql


def test_values_table_construct_is_complex_sql():
    # Shape #82: VALUES used as a table source.
    q = parse_sql_to_ir(
        "SELECT x FROM (VALUES (1), (2), (3)) AS v(x)",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql


def test_lateral_join_is_complex_sql():
    # Shape #83: LATERAL join.
    q = parse_sql_to_ir(
        "SELECT a.region FROM sales a, LATERAL (SELECT region FROM archive) b",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql


def test_unnest_table_function_is_complex_sql():
    # Shape #84 (Bug-923): UNNEST / table-valued function in FROM is not a
    # model table and must route through passthrough, not the source rewriter.
    q = parse_sql_to_ir(
        "SELECT x FROM UNNEST(ARRAY[1, 2, 3]) AS x",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql


def test_aggregate_filter_where_is_complex_sql():
    # Shape #85: aggregate FILTER (WHERE ...) — distinct from WHERE/HAVING.
    q = parse_sql_to_ir(
        "SELECT COUNT(*) FILTER (WHERE created_at IS NOT NULL) FROM orders",
        "m1",
        protocol="jdbc",
    )
    assert q.has_complex_sql
    # And the FILTER predicate column must not leak as a bare grouping column.
    assert "created_at" not in q.requested_dimensions


def test_scalar_function_over_bare_dimension_not_bare():
    # Shape #46: UPPER(region) is a scalar over a bare dimension.  The wrapped
    # column resolves as a dimension and the parse succeeds (no GroupByError);
    # it is not treated as a PG-bare column needing its own GROUP BY entry.
    q = parse_sql_to_ir(
        "SELECT UPPER(region), COUNT(*) FROM sales GROUP BY UPPER(region)",
        "m1",
        protocol="jdbc",
    )
    assert "region" in q.requested_dimensions


# ---------------------------------------------------------------------------
# H15 / F-003-01 — ORDER BY faithfulness invariant
#
# Mirrors test_where_predicate_invariant: any ORDER BY sort key the extractor
# cannot faithfully represent as a bare (column, direction) MUST set
# has_unresolvable_order and MUST NOT fabricate a phantom bare-column sort.
# A silent phantom sort, combined with LIMIT, returns different ROWS.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "order_clause, why",
    [
        ("LOWER(region) DESC", "scalar function sort key"),
        ("UPPER(region)", "scalar function sort key (asc)"),
        ("CASE WHEN region = 'US' THEN 1 ELSE 2 END", "CASE sort key"),
        ("SUM(amount) / COUNT(*) DESC", "arithmetic over aggregates"),
        ("amount + 1", "arithmetic sort key"),
        ("ABS(amount) DESC", "function over column"),
        ("2 DESC", "positional ref to a non-bare SELECT item"),
    ],
)
def test_order_by_unresolvable_invariant(order_clause, why):
    """An inexpressible ORDER BY sort key must be flagged, never silently
    rewritten to a bare-column sort (F-003-01)."""
    q = parse_sql_to_ir(
        f"SELECT region, SUM(amount) FROM modelx GROUP BY region "
        f"ORDER BY {order_clause}",
        "m1",
        protocol="xmla",
    )
    assert q.has_unresolvable_order, (
        f"ORDER BY {order_clause!r} ({why}) not flagged — the extractor would "
        f"sort by a phantom bare column. order_by={q.order_by!r}"
    )
    # And no phantom bare-column item was fabricated FROM the expression: the
    # only items allowed are genuine bare columns appearing elsewhere in the
    # clause (none here), so the list must be empty for these single-key cases.
    if "," not in order_clause:
        assert q.order_by == [], (
            f"ORDER BY {order_clause!r} fabricated a phantom sort {q.order_by!r}"
        )


def test_order_by_phantom_column_not_extracted_from_lower():
    # The exact F-003-01 evidence case: LOWER(region) must NOT become region.
    q = parse_sql_to_ir(
        "SELECT region FROM modelx ORDER BY LOWER(region) DESC",
        "m1", protocol="xmla",
    )
    assert q.order_by == []
    assert q.has_unresolvable_order


def test_order_by_phantom_column_not_extracted_from_ratio_with_limit():
    # SUM(amount)/COUNT(*) DESC LIMIT 5 must not silently become amount DESC.
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) FROM modelx GROUP BY region "
        "ORDER BY SUM(amount) / COUNT(*) DESC LIMIT 5",
        "m1", protocol="xmla",
    )
    assert q.order_by == []
    assert q.has_unresolvable_order
    assert q.limit == 5


def test_order_by_bare_column_still_extracted_no_flag():
    # No-regression: a genuine bare column sort still extracts cleanly.
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) FROM modelx GROUP BY region ORDER BY region DESC",
        "m1", protocol="xmla",
    )
    assert q.order_by == [("region", "desc")]
    assert not q.has_unresolvable_order


def test_order_by_qualified_bare_column_extracted_no_flag():
    q = parse_sql_to_ir(
        "SELECT region FROM modelx m ORDER BY m.region ASC",
        "m1", protocol="xmla",
    )
    assert q.order_by == [("region", "asc")]
    assert not q.has_unresolvable_order


def test_order_by_positional_to_bare_column_no_flag():
    # No-regression: positional refs to bare-column SELECT items still resolve.
    q = parse_sql_to_ir(
        "SELECT city, country FROM modelx ORDER BY 2 DESC",
        "m1", protocol="xmla",
    )
    assert q.order_by == [("country", "desc")]
    assert not q.has_unresolvable_order


def test_order_by_mixed_bare_and_expression_keeps_bare_and_flags():
    # A clause mixing a bare column with an expression keeps the bare item for
    # fingerprinting but still flags unresolvable so the rewriter preserves the
    # full raw ORDER BY (partial extraction must never be emitted as-is).
    q = parse_sql_to_ir(
        "SELECT region FROM modelx ORDER BY region, LOWER(region) DESC",
        "m1", protocol="xmla",
    )
    assert q.order_by == [("region", "asc")]
    assert q.has_unresolvable_order


# ---------------------------------------------------------------------------
# H15 / F-003-02 — explicit and comma JOINs are complex SQL
# ---------------------------------------------------------------------------

def test_explicit_self_join_is_complex_sql():
    # F-003-02 evidence: a self-join must be flagged complex so it routes
    # through passthrough-with-substitution (which preserves the ON clause and
    # join cardinality), not the single-relation semantic rebuild.
    q = parse_sql_to_ir(
        "SELECT COUNT(*) FROM modelx a JOIN modelx b ON a.id = b.parent_id",
        "m1", protocol="jdbc",
    )
    assert q.has_complex_sql


def test_comma_join_is_complex_sql():
    q = parse_sql_to_ir(
        "SELECT COUNT(*) FROM modelx a, modelx b",
        "m1", protocol="jdbc",
    )
    assert q.has_complex_sql


def test_variant_join_is_complex_sql():
    q = parse_sql_to_ir(
        "SELECT COUNT(*) FROM modelx "
        "JOIN modelx_technical ON modelx.id = modelx_technical.id",
        "m1", protocol="xmla",
    )
    assert q.has_complex_sql
    assert "modelx" in q.from_tables
    assert "modelx_technical" in q.from_tables


def test_single_table_query_is_not_complex_sql():
    # No-regression: a plain single-virtual-table query (the model's internal
    # join graph is expanded by the rewriter, NOT authored by the user) stays
    # non-complex.
    q = parse_sql_to_ir(
        "SELECT region, SUM(amount) FROM modelx GROUP BY region",
        "m1", protocol="jdbc",
    )
    assert not q.has_complex_sql


def test_left_join_is_complex_sql():
    q = parse_sql_to_ir(
        "SELECT a.region FROM modelx a LEFT JOIN modelx b ON a.id = b.id",
        "m1", protocol="xmla",
    )
    assert q.has_complex_sql


# ---------------------------------------------------------------------------
# Bug-6958: from_tables must capture ALL set-operation branches
# ---------------------------------------------------------------------------

def test_union_from_tables_captures_all_branches():
    """Bug-6958: UNION ALL -- from_tables must include tables from the second
    branch, not just the first."""
    q = parse_sql_to_ir(
        "SELECT region FROM modelx UNION ALL SELECT c FROM secret_tbl",
        "m1", protocol="jdbc",
    )
    assert "modelx" in q.from_tables
    assert "secret_tbl" in q.from_tables


def test_intersect_from_tables_captures_both_branches():
    """Bug-6958: INTERSECT -- second branch tables must appear."""
    q = parse_sql_to_ir(
        "SELECT region FROM modelx INTERSECT SELECT region FROM other_tbl",
        "m1", protocol="jdbc",
    )
    assert "modelx" in q.from_tables
    assert "other_tbl" in q.from_tables


def test_except_from_tables_captures_both_branches():
    """Bug-6958: EXCEPT -- second branch tables must appear in from_tables."""
    q = parse_sql_to_ir(
        "SELECT region FROM modelx EXCEPT SELECT region FROM another_tbl",
        "m1", protocol="jdbc",
    )
    assert "modelx" in q.from_tables
    assert "another_tbl" in q.from_tables


# ---------------------------------------------------------------------------
# Derived-grain routing — typed expression capture (spec §5.1, §14.1)
# ---------------------------------------------------------------------------
# These go through the REAL parser producer path (spec §14.1: not a synthetic
# BoundQuery). They pin that capture is purely additive and diagnostic:
# ordinary queries capture nothing and keep their exact fingerprint, while a
# function-grain query captures occurrences and folds an expression-aware shape.


def test_ordinary_query_captures_no_expression_occurrences():
    q = parse_sql_to_ir(
        "SELECT region, SUM(rev) FROM modelx GROUP BY region",
        "m1", protocol="jdbc",
    )
    assert q.expression_occurrences == []
    assert q.has_function_grain is False


def test_ordinary_query_fingerprint_is_byte_identical_to_pre_feature():
    # An ordinary query must produce EXACTLY the expression-blind fingerprint,
    # i.e. omitting the derived-expression key. We reconstruct that hash directly
    # from fingerprint_shape with no expr_fingerprints and require equality.
    from shared.pocket.fingerprint import fingerprint_shape

    q = parse_sql_to_ir(
        "SELECT region, SUM(rev) FROM modelx WHERE country = 'GB' GROUP BY region",
        "m1", protocol="jdbc",
    )
    expected = fingerprint_shape(
        measures=q.requested_measures,
        dimensions=q.requested_dimensions,
        grain=q.grain,
        filter_cols=[f.dimension_name for f in q.filters],
        having_cols=q.having_columns or [],
    )
    assert q.query_fingerprint == expected


def test_function_grain_query_captures_group_and_select_occurrences():
    q = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date), SUM(rev) FROM modelx "
        "GROUP BY DATE_TRUNC('month', order_date)",
        "m1", protocol="jdbc",
    )
    roles = {o.role for o in q.expression_occurrences}
    assert "GROUP_KEY" in roles
    assert "SELECT" in roles
    assert q.has_function_grain is True


def test_date_trunc_and_extract_month_fingerprints_are_distinct():
    """The January-2025 / January-2026 collapse defence at the query-shape level
    (spec §13): DATE_TRUNC month and EXTRACT month must not collide despite both
    being function grain."""
    dt = parse_sql_to_ir(
        "SELECT DATE_TRUNC('month', order_date), SUM(rev) FROM modelx "
        "GROUP BY DATE_TRUNC('month', order_date)",
        "m1", protocol="jdbc",
    )
    ex = parse_sql_to_ir(
        "SELECT EXTRACT(month FROM order_date), SUM(rev) FROM modelx "
        "GROUP BY EXTRACT(month FROM order_date)",
        "m1", protocol="jdbc",
    )
    assert dt.query_fingerprint != ex.query_fingerprint


def test_composable_aggregate_select_does_not_perturb_fingerprint():
    """Codex R1 finding 1 regression: a composable aggregate SELECT item
    (SUM(a)/SUM(b)) has has_function_grain=False and is aggregate-routable. It
    must NOT be captured as a derived group-key expression, so an ordinary
    aggregate-routable composable query keeps its byte-identical, expression-blind
    fingerprint (spec I10)."""
    from shared.pocket.fingerprint import fingerprint_shape

    q = parse_sql_to_ir(
        "SELECT region, SUM(rev) / SUM(cost) FROM modelx GROUP BY region",
        "m1", protocol="jdbc",
    )
    # No derived-expression occurrence should be captured for the composable.
    assert q.expression_occurrences == []
    assert q.has_function_grain is False
    expected = fingerprint_shape(
        measures=q.requested_measures,
        dimensions=q.requested_dimensions,
        grain=q.grain,
        filter_cols=[f.dimension_name for f in q.filters],
        having_cols=q.having_columns or [],
    )
    assert q.query_fingerprint == expected


def test_same_expression_in_select_vs_order_does_not_collide():
    """Codex R1 finding 5 regression: role-tagged fingerprints mean a derived
    expression in SELECT vs ORDER BY produces distinct shapes (projection vs sort
    semantics differ)."""
    sel = parse_sql_to_ir(
        "SELECT UPPER(region), SUM(rev) FROM modelx GROUP BY UPPER(region)",
        "m1", protocol="jdbc",
    )
    sel_ord = parse_sql_to_ir(
        "SELECT UPPER(region), SUM(rev) FROM modelx GROUP BY UPPER(region) "
        "ORDER BY UPPER(region)",
        "m1", protocol="jdbc",
    )
    assert sel.query_fingerprint != sel_ord.query_fingerprint


# ---------------------------------------------------------------------------
# F-003-01 / F-003-08 / F-003-11 — NOT IN extract, EXTRACT grain, ORDER BY alias
# ---------------------------------------------------------------------------

def test_f003_01_sql_not_in_extracts_not_in_operator():
    """JDBC ``NOT IN ('US')`` must extract LogicalFilter.operator not_in."""
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM orders WHERE region NOT IN ('US') GROUP BY region",
        "model-1", protocol="jdbc",
    )
    assert q.has_unresolvable_where is False
    assert len(q.filters) == 1
    assert q.filters[0].operator == "not_in"
    assert q.filters[0].dimension_name == "region"
    assert q.filters[0].value == ["US"]


def test_f003_01_not_in_and_eq_both_extract():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM orders "
        "WHERE region NOT IN ('US') AND country = 'FR' GROUP BY region",
        "model-1", protocol="jdbc",
    )
    ops = {(f.dimension_name, f.operator) for f in q.filters}
    assert ("region", "not_in") in ops
    assert ("country", "eq") in ops
    assert q.has_unresolvable_where is False


def test_f003_01_subquery_not_in_stays_unresolvable():
    q = parse_sql_to_ir(
        "SELECT SUM(revenue) FROM orders "
        "WHERE region NOT IN (SELECT code FROM banned) GROUP BY region",
        "model-1", protocol="jdbc",
    )
    assert q.has_unresolvable_where is True
    assert all(f.operator != "not_in" for f in q.filters)


def test_f003_08_extract_month_year_quarter_are_time_period_grains():
    for unit in ("month", "year", "quarter"):
        q = parse_sql_to_ir(
            f"SELECT EXTRACT({unit} FROM business_date), SUM(rev) FROM modelx "
            f"GROUP BY EXTRACT({unit} FROM business_date)",
            "m1", protocol="jdbc",
        )
        assert q.time_period_grains == [(f"extract_{unit}", "business_date")], q.time_period_grains
        assert q.has_function_grain is True


def test_f003_11_order_by_aggregate_alias_is_tagged_not_grain():
    q = parse_sql_to_ir(
        "SELECT region, SUM(rev) AS n FROM orders GROUP BY region ORDER BY n DESC",
        "model-1", protocol="jdbc",
    )
    assert q.has_unresolvable_order is False
    assert "n" in q.order_by_alias_names
    assert q.order_by == [("n", "desc")]


def test_f003_11_order_by_alias_shadowing_grain_is_unresolvable():
    # GROUP BY 1 (not GROUP BY region): a colliding aggregate alias still
    # trips Bug-6858 if grouped by name. F-003-11 is the ORDER BY shadow.
    q = parse_sql_to_ir(
        "SELECT region, SUM(rev) AS region FROM orders GROUP BY 1 ORDER BY region",
        "model-1", protocol="jdbc",
    )
    assert q.has_unresolvable_order is True
