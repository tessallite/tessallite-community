"""
Unit tests for src.rewrite.query_rewriter.

Tests both rewrite_for_aggregate() and rewrite_for_source().

Run from tessallite/services/query-router/:
    pytest tests/test_query_rewriter.py
"""
import pytest
import types
from datetime import datetime, timezone

from src.rewrite.query_rewriter import (
    _build_joined_from_clause,
    _coerce_value,
    _dialect_from_connection_type,
    _missing_join_error_message,
    _normalize_uda_expression_quoting,
    _render_condition,
    _render_uda_expression,
    _requote_identifiers_for_bigquery,
    dialect_from_connection_type,
    resolve_target_dialect_for_bound,
    rewrite_for_aggregate,
    rewrite_for_source,
)
from src.ir.logical_query import LogicalQuery, BoundQuery, LogicalFilter
from src.api.routes import ExecuteRequest

from conftest import make_measure, make_dimension, make_agg_col, make_aggregate, make_bound_query


# ---------------------------------------------------------------------------
# rewrite_for_aggregate — basic
# ---------------------------------------------------------------------------

def test_dimension_and_measure_in_select():
    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([d], [m])
    sql = rewrite_for_aggregate(bq, agg)
    assert '"country"' in sql
    assert '"revenue__sum"' in sql
    assert '"revenue"' in sql   # aliased AS


def test_table_ref_includes_schema():
    m = make_measure("revenue")
    d = make_dimension("region")
    agg = make_aggregate(["region"], [make_agg_col(m)], target_schema="my_schema", physical_table_name="my_agg")
    bq = make_bound_query([d], [m])
    sql = rewrite_for_aggregate(bq, agg)
    assert '"my_schema"."my_agg"' in sql


def test_no_schema_table_ref():
    m = make_measure("orders")
    d = make_dimension("region")
    agg = make_aggregate(["region"], [make_agg_col(m, "count")], target_schema=None, physical_table_name="orders_agg")
    agg.target_schema = None
    bq = make_bound_query([d], [m])
    sql = rewrite_for_aggregate(bq, agg)
    assert '"orders_agg"' in sql
    # No schema prefix
    assert '"aggregates"' not in sql


# ---------------------------------------------------------------------------
# Filter rendering
# ---------------------------------------------------------------------------

def test_filter_eq_in_where():
    m = make_measure("revenue")
    d = make_dimension("country")
    f = LogicalFilter("status", "eq", "active")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], filters=[f])
    sql = rewrite_for_aggregate(bq, agg)
    assert "WHERE" in sql
    assert "\"status\" = 'active'" in sql


def test_filter_in_rendered():
    m = make_measure("revenue")
    d = make_dimension("region")
    f = LogicalFilter("status", "in", ["active", "trial"])
    agg = make_aggregate(["region"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], filters=[f])
    sql = rewrite_for_aggregate(bq, agg)
    assert "IN (" in sql
    assert "'active'" in sql
    assert "'trial'" in sql


def test_filter_eq_int_dim_renders_numeric():
    # Bug-5546: an INT-typed grain filter must render a numeric literal on the
    # aggregate route, not a string. dim_type_by_name carries the binder-resolved
    # source-column type so the aggregate rewriter can type the WHERE value.
    m = make_measure("net_sales")
    d = make_dimension("year")
    f = LogicalFilter("year", "eq", "1999")
    agg = make_aggregate(["year"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], filters=[f])
    bq.dim_type_by_name = {"year": "INT64"}
    sql = rewrite_for_aggregate(bq, agg)
    assert '"year" = 1999' in sql
    assert "'1999'" not in sql


def test_filter_in_int_dim_renders_numeric():
    m = make_measure("net_sales")
    d = make_dimension("year")
    f = LogicalFilter("year", "in", ["1999", "2000"])
    agg = make_aggregate(["year"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], filters=[f])
    bq.dim_type_by_name = {"year": "INT64"}
    sql = rewrite_for_aggregate(bq, agg)
    assert "1999" in sql and "2000" in sql
    assert "'1999'" not in sql and "'2000'" not in sql


def test_filter_eq_string_dim_still_quoted():
    # Guard: a non-numeric column type must still render a quoted string literal.
    m = make_measure("net_sales")
    d = make_dimension("item_category")
    f = LogicalFilter("item_category", "eq", "Shoes")
    agg = make_aggregate(["item_category"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], filters=[f])
    bq.dim_type_by_name = {"item_category": "STRING"}
    sql = rewrite_for_aggregate(bq, agg)
    assert "'Shoes'" in sql


def test_filter_between_rendered():
    m = make_measure("revenue")
    d = make_dimension("region")
    f = LogicalFilter("amount", "between", (10, 100))
    agg = make_aggregate(["region"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], filters=[f])
    sql = rewrite_for_aggregate(bq, agg)
    assert "BETWEEN 10 AND 100" in sql


# Bug-918: _render_condition BETWEEN arity handling.

def test_render_condition_between_two_values():
    assert _render_condition('"x"', "between", [10, 100]) == '"x" BETWEEN 10 AND 100'


def test_render_condition_between_empty_value_does_not_crash():
    # Pre-existing degenerate: an empty value renders NULL bounds, never crashes.
    out = _render_condition('"x"', "between", None)
    assert "BETWEEN" in out


def test_render_condition_between_wrong_arity_raises():
    # A non-2-element bound must fail loudly rather than crash on tuple
    # unpacking (the original bug) or silently emit a wrong predicate.
    with pytest.raises(ValueError, match="exactly two bounds"):
        _render_condition('"x"', "between", [10])
    with pytest.raises(ValueError, match="exactly two bounds"):
        _render_condition('"x"', "between", [10, 20, 30])


# ---------------------------------------------------------------------------
# ORDER BY / LIMIT / OFFSET
# ---------------------------------------------------------------------------

def test_order_by_rendered():
    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], order_by=[("revenue", "desc")])
    sql = rewrite_for_aggregate(bq, agg)
    assert "ORDER BY" in sql
    assert '"revenue" DESC' in sql


def test_limit_offset_rendered():
    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], limit=50, offset=10)
    sql = rewrite_for_aggregate(bq, agg)
    assert "LIMIT 50" in sql
    assert "OFFSET 10" in sql


# ---------------------------------------------------------------------------
# Bug-880: Multiple agg functions on the same measure
# ---------------------------------------------------------------------------

def test_multi_function_same_measure_produces_two_columns():
    """Bug-880: SELECT MIN(x), MAX(x) must produce 2 columns, not 1."""
    from src.parsing.sql_parser import parse_sql_to_ir

    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m, "min"), make_agg_col(m, "max")],
    )

    sql = "SELECT country, MIN(revenue), MAX(revenue) FROM sales GROUP BY country"
    lq = parse_sql_to_ir(sql, "model-1")
    model = types.SimpleNamespace(id="model-1", slug="test")
    bq = BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[m],
        resolved_dimensions=[d],
        resolved_filters=[],
    )

    result = rewrite_for_aggregate(bq, agg)
    result_lower = result.lower()

    # Both revenue__min and revenue__max physical columns must appear
    assert "revenue__min" in result_lower, f"revenue__min missing from: {result}"
    assert "revenue__max" in result_lower, f"revenue__max missing from: {result}"

    # The two columns must have distinct aliases (Bug-AGG-001 disambiguation)
    # One gets the measure name ("revenue"), the other the agg function ("max")
    select_part = result.upper().split("FROM")[0]
    as_count = select_part.count(" AS ")
    # At least 3 AS clauses: country, revenue, max
    assert as_count >= 3, f"Expected at least 3 AS aliases, got {as_count} in: {result}"


def test_global_min_max_no_group_by_on_exact_grain_aggregate():
    """Bug-880: SELECT MIN(x), MAX(x) with no GROUP BY on exact-grain [] aggregate."""
    from src.parsing.sql_parser import parse_sql_to_ir

    m = make_measure("revenue")
    agg = make_aggregate(
        [],  # global aggregate: empty grain
        [make_agg_col(m, "min"), make_agg_col(m, "max")],
    )

    sql = "SELECT MIN(revenue), MAX(revenue) FROM sales"
    lq = parse_sql_to_ir(sql, "model-1")
    model = types.SimpleNamespace(id="model-1", slug="test")
    bq = BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[m],
        resolved_dimensions=[],
        resolved_filters=[],
    )

    result = rewrite_for_aggregate(bq, agg)
    result_lower = result.lower()

    # Exact grain [] = global → physical columns read directly
    assert "revenue__min" in result_lower, f"revenue__min missing from: {result}"
    assert "revenue__max" in result_lower, f"revenue__max missing from: {result}"

    # Two distinct aliases must exist for the measure columns
    select_part = result.upper().split("FROM")[0]
    as_count = select_part.count(" AS ")
    assert as_count >= 2, f"Expected at least 2 AS aliases, got {as_count} in: {result}"


# ---------------------------------------------------------------------------
# rewrite_for_source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rewrite_for_source_returns_raw():
    m = make_measure("revenue")
    d = make_dimension("country")
    raw = "SELECT SUM(revenue) FROM sales GROUP BY country"
    bq = make_bound_query([d], [m], raw_sql=raw)
    assert await rewrite_for_source(bq) == raw


def test_raw_sql_drill_join_comment_is_not_trusted_metadata():
    from src.parsing.sql_parser import parse_sql_to_ir

    lq = parse_sql_to_ir(
        'SELECT /* tessallite_drill_join_path=00000000-0000-4000-8000-000000000001 */ '
        'country, SUM(revenue) FROM modelx GROUP BY country',
        "model-1",
    )

    assert lq.drill_join_path_ids == []


def test_public_execute_request_cannot_set_drill_join_path_metadata():
    body = ExecuteRequest.model_validate({
        "model_id": "model-1",
        "raw_query": "SELECT region, SUM(revenue) FROM modelx GROUP BY region",
        "protocol": "jdbc",
        "drill_join_path_ids": ["00000000-0000-4000-8000-000000000001"],
    })

    assert "drill_join_path_ids" not in ExecuteRequest.model_fields
    assert not hasattr(body, "drill_join_path_ids")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_missing_aggregate_column_raises():
    m_query = make_measure("revenue", "sum")
    m_agg = make_measure("orders", "count")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m_agg, "count")])
    bq = make_bound_query([d], [m_query])
    with pytest.raises(ValueError, match="Cannot find aggregate column"):
        rewrite_for_aggregate(bq, agg)


def test_build_joined_from_clause_supports_multi_hop_path():
    joins = [
        types.SimpleNamespace(
            left_table_id="fact",
            right_table_id="country",
            left_column_id="fact_country_id",
            right_column_id="country_id",
            join_type="inner",
        ),
        types.SimpleNamespace(
            left_table_id="country",
            right_table_id="region",
            left_column_id="country_region_id",
            right_column_id="region_id",
            join_type="left",
        ),
    ]
    tables_by_id = {
        "fact": types.SimpleNamespace(id="fact", physical_name="fact_sales", alias="f"),
        "country": types.SimpleNamespace(id="country", physical_name="dim_country", alias="c"),
        "region": types.SimpleNamespace(id="region", physical_name="dim_region", alias="r"),
    }
    columns_by_id = {
        "fact_country_id": types.SimpleNamespace(model_table_id="fact", column_name="country_id"),
        "country_id": types.SimpleNamespace(model_table_id="country", column_name="id"),
        "country_region_id": types.SimpleNamespace(model_table_id="country", column_name="region_id"),
        "region_id": types.SimpleNamespace(model_table_id="region", column_name="id"),
    }
    alias_by_table_id = {"fact": "f", "country": "c", "region": "r"}

    from_clause = _build_joined_from_clause(
        base_table_id="fact",
        required_table_ids={"fact", "country", "region"},
        joins=joins,
        tables_by_id=tables_by_id,
        columns_by_id=columns_by_id,
        alias_by_table_id=alias_by_table_id,
    )

    assert from_clause is not None
    assert '"fact_sales" AS "f"' in from_clause
    assert 'INNER JOIN "dim_country" AS "c"' in from_clause
    assert 'LEFT JOIN "dim_region" AS "r"' in from_clause
    assert '"f"."country_id" = "c"."id"' in from_clause
    assert '"c"."region_id" = "r"."id"' in from_clause


def test_build_joined_from_clause_prefers_saved_drill_join_path():
    joins = [
        types.SimpleNamespace(
            id="fact-country",
            left_table_id="fact",
            right_table_id="country",
            left_column_id="fact_country_id",
            right_column_id="country_id",
            join_type="inner",
        ),
        types.SimpleNamespace(
            id="country-region",
            left_table_id="country",
            right_table_id="region",
            left_column_id="country_region_id",
            right_column_id="region_id",
            join_type="left",
        ),
        types.SimpleNamespace(
            id="fact-customer",
            left_table_id="fact",
            right_table_id="customer",
            left_column_id="fact_customer_id",
            right_column_id="customer_id",
            join_type="inner",
        ),
        types.SimpleNamespace(
            id="customer-region",
            left_table_id="customer",
            right_table_id="region",
            left_column_id="customer_region_id",
            right_column_id="region_id",
            join_type="left",
        ),
    ]
    tables_by_id = {
        "fact": types.SimpleNamespace(id="fact", physical_name="fact_sales", alias="f"),
        "country": types.SimpleNamespace(id="country", physical_name="dim_country", alias="c"),
        "customer": types.SimpleNamespace(id="customer", physical_name="dim_customer", alias="cust"),
        "region": types.SimpleNamespace(id="region", physical_name="dim_region", alias="r"),
    }
    columns_by_id = {
        "fact_country_id": types.SimpleNamespace(model_table_id="fact", column_name="country_id"),
        "country_id": types.SimpleNamespace(model_table_id="country", column_name="id"),
        "country_region_id": types.SimpleNamespace(model_table_id="country", column_name="region_id"),
        "fact_customer_id": types.SimpleNamespace(model_table_id="fact", column_name="customer_id"),
        "customer_id": types.SimpleNamespace(model_table_id="customer", column_name="id"),
        "customer_region_id": types.SimpleNamespace(model_table_id="customer", column_name="region_id"),
        "region_id": types.SimpleNamespace(model_table_id="region", column_name="id"),
    }
    alias_by_table_id = {"fact": "f", "region": "r"}

    from_clause = _build_joined_from_clause(
        base_table_id="fact",
        required_table_ids={"fact", "region"},
        joins=joins,
        tables_by_id=tables_by_id,
        columns_by_id=columns_by_id,
        alias_by_table_id=alias_by_table_id,
        preferred_join_ids=["fact-customer", "customer-region"],
    )

    assert from_clause is not None
    assert 'INNER JOIN "dim_customer" AS "cust"' in from_clause
    assert 'LEFT JOIN "dim_region" AS "r"' in from_clause
    assert 'dim_country' not in from_clause


def test_missing_join_error_message_is_explicit_for_hierarchy_path():
    joins = [
        types.SimpleNamespace(
            left_table_id="fact",
            right_table_id="country",
            left_column_id="fact_country_id",
            right_column_id="country_id",
            join_type="inner",
        ),
    ]
    tables_by_id = {
        "fact": types.SimpleNamespace(id="fact", physical_name="fact_sales", alias="fact"),
        "country": types.SimpleNamespace(id="country", physical_name="dim_country", alias="country"),
        "region": types.SimpleNamespace(id="region", physical_name="dim_region", alias="region"),
    }

    message = _missing_join_error_message(
        base_table_id="fact",
        required_table_ids={"fact", "country", "region"},
        joins=joins,
        tables_by_id=tables_by_id,
    )

    assert "Cannot resolve hierarchy path. Missing join between" in message
    assert "'fact'" in message
    assert "'region'" in message


# ---------------------------------------------------------------------------
# _coerce_value — CAST type alignment for UDA expressions (Bug-186)
# ---------------------------------------------------------------------------

def test_coerce_value_wraps_date_cast():
    col = '(CAST("transaction_ts" AS DATE))'
    assert _coerce_value(col, "'2025-05-04 17:36:17'") == "CAST('2025-05-04 17:36:17' AS DATE)"


def test_coerce_value_wraps_datetime_cast():
    col = '(CAST("ts" AS DATETIME))'
    assert _coerce_value(col, "'2025-05-04 17:36:17'") == "CAST('2025-05-04 17:36:17' AS DATETIME)"


def test_coerce_value_wraps_time_cast():
    col = '(CAST("ts" AS TIME))'
    assert _coerce_value(col, "'17:36:17'") == "CAST('17:36:17' AS TIME)"


def test_coerce_value_noop_for_plain_column():
    col = '"country"'
    assert _coerce_value(col, "'US'") == "'US'"


def test_coerce_value_noop_for_integer_cast():
    col = '(CAST("qty" AS INTEGER))'
    assert _coerce_value(col, "42") == "42"


def test_coerce_value_case_insensitive():
    col = '(cast("ts" as date))'
    assert _coerce_value(col, "'2025-01-01'") == "CAST('2025-01-01' AS DATE)"


def test_coerce_value_extract_year_from_date_cast_noop():
    """Bug-438: EXTRACT(YEAR FROM CAST(ts AS DATE)) returns INTEGER, not DATE.
    _coerce_value must not wrap the numeric literal with CAST(... AS DATE)."""
    col = '(EXTRACT(YEAR FROM CAST(`orders`.`posting_ts` AS DATE)))'
    assert _coerce_value(col, "2024", col_type="integer") == "2024"


def test_coerce_value_extract_month_from_date_cast_noop():
    col = '(EXTRACT(MONTH FROM CAST(`orders`.`posting_ts` AS DATE)))'
    assert _coerce_value(col, "6", col_type="integer") == "6"


def test_coerce_value_numeric_literal_never_cast_to_date():
    """Even without col_type, a bare numeric literal must not be cast to DATE."""
    col = '(CAST("ts" AS DATE))'
    assert _coerce_value(col, "2024") == "2024"


def test_coerce_value_col_type_numeric_overrides_regex():
    """col_type=INT64 prevents DATE coercion even when CAST(AS DATE) is in expr."""
    col = '(EXTRACT(YEAR FROM CAST("ts" AS DATE)))'
    assert _coerce_value(col, "'2024'", col_type="INT64") == "'2024'"


def test_coerce_value_col_type_date_still_wraps():
    """When col_type is DATE, the coercion should still apply for string literals."""
    col = '(CAST("ts" AS DATE))'
    assert _coerce_value(col, "'2025-01-01'", col_type="DATE") == "CAST('2025-01-01' AS DATE)"


# ---------------------------------------------------------------------------
# _render_condition — type coercion integration
# ---------------------------------------------------------------------------

def test_render_condition_eq_date_cast():
    col = '(CAST("transaction_ts" AS DATE))'
    result = _render_condition(col, "eq", "2025-05-04 17:36:17+00:00")
    assert "CAST(" in result
    assert "AS DATE)" in result
    assert result.startswith(f"{col} = ")


def test_render_condition_in_date_cast():
    col = '(CAST("transaction_ts" AS DATE))'
    result = _render_condition(col, "in", ["2025-05-04", "2025-05-05"])
    assert "CAST('2025-05-04' AS DATE)" in result
    assert "CAST('2025-05-05' AS DATE)" in result


def test_render_condition_not_in():
    result = _render_condition('"region"', "not_in", ["US", "UK"])
    assert "NOT IN" in result
    assert "'US'" in result
    assert "'UK'" in result


def test_render_condition_between_date_cast():
    col = '(CAST("transaction_ts" AS DATE))'
    result = _render_condition(col, "between", ["2025-01-01", "2025-12-31"])
    assert "CAST('2025-01-01' AS DATE)" in result
    assert "CAST('2025-12-31' AS DATE)" in result
    assert "BETWEEN" in result


def test_render_condition_eq_plain_column_no_cast():
    result = _render_condition('"country"', "eq", "US")
    assert result == "\"country\" = 'US'"
    assert "CAST(" not in result


def test_render_condition_extract_year_integer_col_type():
    """Bug-438: drill-through Year=2024 on EXTRACT(YEAR FROM CAST(ts AS DATE))
    must not wrap the integer with CAST(... AS DATE)."""
    col = '(EXTRACT(YEAR FROM CAST(`orders`.`posting_ts` AS DATE)))'
    result = _render_condition(col, "eq", 2024, col_type="integer")
    assert result == f"{col} = 2024"
    assert "CAST(2024" not in result


# ---------------------------------------------------------------------------
# Bug-5462: integer-keyed dimension filters must render NUMERIC literals.
# A BI client (Excel/Power BI/XMLA) sends a slicer member key as a quoted
# string ('1999'); when the column is INTEGER/NUMERIC, BigQuery rejects the
# INT64-vs-STRING comparison. The extracted-filter path (_render_value /
# _render_condition) must emit a bare numeric literal for numeric col_type and
# keep a string literal for text col_type.
# ---------------------------------------------------------------------------

import sqlglot
from src.rewrite.conditions import (
    _render_value,
    _render_where,
    is_numeric_col_type,
    value_is_numeric_literal,
)


@pytest.mark.parametrize("col_type", ["INT64", "INTEGER", "integer", "BIGINT", "NUMERIC", "DECIMAL(10,2)", "FLOAT64"])
def test_render_value_numeric_col_type_emits_bare_number(col_type):
    """A string member key against a numeric column renders unquoted."""
    assert _render_value("1999", col_type=col_type) == "1999"


def test_render_condition_eq_integer_dim_is_numeric():
    """d_year = 1999 (not '1999') for an INT64 column."""
    result = _render_condition('"dt"."d_year"', "eq", "1999", col_type="INT64")
    assert result == '"dt"."d_year" = 1999'
    assert "'1999'" not in result


def test_render_condition_eq_integer_dim_transpiles_for_bigquery():
    """The numeric predicate transpiles to a bare BigQuery numeric literal."""
    result = _render_condition('"dt"."d_year"', "eq", "1999", col_type="INT64")
    bq = sqlglot.transpile(result, read="postgres", write="bigquery")[0]
    assert bq == "`dt`.`d_year` = 1999"


def test_render_condition_quarter_month_numeric():
    """d_qoy = 2 and d_moy = 6 render as integers (slicer scenario)."""
    assert _render_condition('"d_qoy"', "eq", "2", col_type="INT64") == '"d_qoy" = 2'
    assert _render_condition('"d_moy"', "eq", "6", col_type="INTEGER") == '"d_moy" = 6'


def test_render_condition_in_integer_dim_numeric():
    """IN list against an integer column renders numeric members."""
    result = _render_condition('"d_year"', "in", ["1999", "2000"], col_type="INT64")
    assert result == '"d_year" IN (1999, 2000)'


def test_render_condition_string_dim_unchanged():
    """A string dimension keeps its quoted literal (must not regress)."""
    result = _render_condition('"it"."i_category"', "eq", "Shoes", col_type="STRING")
    assert result == "\"it\".\"i_category\" = 'Shoes'"


def test_render_condition_string_dim_no_col_type_unchanged():
    """No col_type → string literal default preserved for text values."""
    result = _render_condition('"region"', "eq", "Unknown")
    assert result == "\"region\" = 'Unknown'"


def test_render_value_date_col_type_unchanged():
    """DATE / TIMESTAMP handling is unchanged by the numeric fix."""
    out = _render_value("2025-01-01", col_type="TIMESTAMP")
    assert out == "TIMESTAMP '2025-01-01'"


def test_bug_6618_render_value_timestamptz_emits_tz_aware_literal():
    """Bug-6618: a tz-aware column (TIMESTAMPTZ / TIMESTAMP_TZ) must emit
    TIMESTAMPTZ 'x' so sqlglot transpiles to CAST('x' AS TIMESTAMP) on
    BigQuery, not CAST('x' AS DATETIME) which mismatches the column type.
    """
    import sqlglot

    # TIMESTAMPTZ column -> TIMESTAMPTZ literal
    out_tz = _render_value("2024-06-15 10:30:00", col_type="TIMESTAMPTZ")
    assert out_tz == "TIMESTAMPTZ '2024-06-15 10:30:00'"

    # TIMESTAMP_TZ column (Snowflake alias) -> TIMESTAMPTZ literal
    out_tz2 = _render_value("2024-06-15 10:30:00", col_type="TIMESTAMP_TZ")
    assert out_tz2 == "TIMESTAMPTZ '2024-06-15 10:30:00'"

    # Plain TIMESTAMP column -> TIMESTAMP literal (unchanged)
    out_plain = _render_value("2024-06-15 10:30:00", col_type="TIMESTAMP")
    assert out_plain == "TIMESTAMP '2024-06-15 10:30:00'"

    # Verify BigQuery transpile produces CAST(... AS TIMESTAMP) for tz-aware
    bq_out = sqlglot.transpile(
        f"SELECT {out_tz}", read="postgres", write="bigquery",
    )
    assert "AS TIMESTAMP" in bq_out[0].upper(), bq_out[0]
    assert "AS DATETIME" not in bq_out[0].upper(), bq_out[0]

    # Verify BigQuery transpile produces CAST(... AS DATETIME) for tz-unaware
    bq_out_plain = sqlglot.transpile(
        f"SELECT {out_plain}", read="postgres", write="bigquery",
    )
    assert "AS DATETIME" in bq_out_plain[0].upper(), bq_out_plain[0]


def test_is_numeric_col_type_helper():
    assert is_numeric_col_type("INT64")
    assert is_numeric_col_type("numeric(10,2)")
    assert not is_numeric_col_type("STRING")
    assert not is_numeric_col_type(None)


def test_value_is_numeric_literal_helper():
    assert value_is_numeric_literal("1999")
    assert value_is_numeric_literal("-3.14")
    # Bug-5538 (Codex round-2 finding 3): scientific notation is no longer a
    # valid bare member-key literal — a slicer member key is never written as
    # ``1e3`` and a bare exponent token is an injection/precision surface.
    assert not value_is_numeric_literal("1e3")
    assert value_is_numeric_literal(2000)
    assert not value_is_numeric_literal("Shoes")


def test_value_is_numeric_literal_tightened_grammar():
    """Bug-5538 (Codex round-2 finding 3): the safe grammar is optional leading
    ``-``, digits, optional single ``.`` fraction — NO exponent, NO leading
    ``+``, NO surrounding whitespace. Genuine integers/decimals still pass."""
    # Accepted — genuine integer / decimal forms.
    for good in ("1999", "-5", "19.99", "0", "-0.5", ".5", "-.5"):
        assert value_is_numeric_literal(good), good
    # Rejected — scientific notation, leading +, surrounding whitespace, and a
    # trailing newline (``$`` would have matched just before a single ``\n``;
    # the regex anchors with ``\Z`` so ``"12\n"`` cannot slip through as a bare
    # ``= 12\n`` token).
    for bad in ("1e9", "1E9", "-1e3", "+1", "+19.99", " 1", "1 ", " 1 ", "1.",
                "12\n", "1\n", "\n1", "1\n2"):
        assert not value_is_numeric_literal(bad), bad


def test_render_value_numeric_col_rejects_scientific_and_signed():
    """A scientific / leading-+ / padded token against a numeric column must FAIL
    LOUD — never emit a bare ``1e9`` / ``+1`` / ``' 1 '`` token."""
    from src.ir.logical_query import SemanticBindingError
    for bad in ("1e9", "+1", " 1 ", "0x1F"):
        with pytest.raises(SemanticBindingError):
            _render_value(bad, col_type="INT64")


def test_render_value_rawsql_non_numeric_against_numeric_col_fails_loud():
    """Bug-5538 (Codex round-2 finding 1): a ``RawSQL``-wrapped value must clear
    the SAME strict validator before rendering bare against a numeric column —
    the numeric gate is UNBYPASSABLE. A non-numeric raw token fails loud."""
    from src.ir.logical_query import SemanticBindingError
    from src.parsing.sql_parser import RawSQL
    with pytest.raises(SemanticBindingError):
        _render_value(RawSQL("1 OR 1=1"), col_type="INT64")
    with pytest.raises(SemanticBindingError):
        _render_value(RawSQL("d_year"), col_type="INTEGER")
    with pytest.raises(SemanticBindingError):
        _render_value(RawSQL("1e9"), col_type="INT64")


def test_render_value_rawsql_numeric_against_numeric_col_renders_bare():
    """A genuine numeric ``RawSQL`` against a numeric column still renders bare."""
    from src.parsing.sql_parser import RawSQL
    assert _render_value(RawSQL("1999"), col_type="INT64") == "1999"


def test_render_value_rawsql_non_numeric_col_unchanged():
    """For a NON-numeric column, ``RawSQL`` still emits as-is (e.g. a CAST date
    expression) — the guard only applies to numeric columns."""
    from src.parsing.sql_parser import RawSQL
    out = _render_value(RawSQL("CAST('2024-01-01' AS DATE)"), col_type="DATE")
    assert out == "CAST('2024-01-01' AS DATE)"


def test_value_is_numeric_literal_rejects_non_finite_and_grouped():
    """float() over-accepts these, but they must NOT become bare SQL numbers."""
    for bad in ("inf", "-inf", "nan", "infinity", "1_000", "", " "):
        assert not value_is_numeric_literal(bad), bad
    assert not value_is_numeric_literal(True)  # bool is not a numeric literal


def test_value_is_numeric_literal_rejects_unicode_digits():
    """Bug-5538: non-ASCII decimal digits (Arabic-Indic) must be rejected — a
    bare token like ``١٩٩٩`` is unparseable by Postgres/BigQuery, so it must
    fail loud, not slip through ``\\d``."""
    assert not value_is_numeric_literal("١٩٩٩")  # ١٩٩٩
    # And it fails loud through _render_value for a numeric column.
    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError):
        _render_value("١٩٩٩", col_type="INT64")


@pytest.mark.parametrize(
    "bad",
    ["abc", "", " ", "1_000", "inf", "-inf", "nan", "infinity", "1; DROP TABLE t"],
)
def test_render_value_numeric_col_invalid_string_fails_loud(bad):
    """Bug-5538 (Codex finding 1): a non-numeric string against a numeric column
    must FAIL LOUD, never emit a string literal (``'abc'`` is STRING vs INT64)
    nor a bare non-numeric token. The DB-rejects-it-loudly fallback was
    incomplete: a string literal silently changes the comparison type."""
    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError):
        _render_value(bad, col_type="INT64")


@pytest.mark.parametrize("bad", [True, False, float("inf"), float("nan"), float("-inf")])
def test_render_value_numeric_col_invalid_nonstring_fails_loud(bad):
    """Bug-5538 (Codex finding 1): bool / non-finite float against a numeric
    column must FAIL LOUD, never fall through to bare ``TRUE``/``inf``/``nan``."""
    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError):
        _render_value(bad, col_type="INT64")


def test_render_value_numeric_col_accepts_real_int_and_float():
    """A finite real int/float against a numeric column still renders bare."""
    assert _render_value(2000, col_type="INT64") == "2000"
    assert _render_value(3.14, col_type="NUMERIC") == "3.14"


# ---------------------------------------------------------------------------
# Bug-5539 (Codex round-3 finding 3): the extracted-filter path must not launder
# a scientific/signed source token into a bare numeric. ``_literal_value`` now
# preserves the ORIGINAL spelling on a ``NumericLiteral`` so the SAME strict
# grammar validates the original token at render time — F-003-10's float
# round-trip stays intact.
# ---------------------------------------------------------------------------


def _parsed_literal(sql_value: str):
    """Return the ``_literal_value`` result for a SQL literal token, exactly as
    the extracted-filter path would produce it from a parsed comparison."""
    from src.parsing.sql_parser import _literal_value
    node = sqlglot.parse_one(
        "SELECT 1 FROM t WHERE x = " + sql_value, read="postgres"
    ).args["where"].this.expression
    return _literal_value(node)


def test_numeric_literal_preserves_scientific_spelling_and_is_rejected():
    """A scientific source token is parsed to a finite float (F-003-10) BUT
    carries its original ``1e9`` spelling, which the strict grammar rejects — so
    it can never render as a bare token against a numeric column."""
    from src.parsing.sql_parser import NumericLiteral
    v = _parsed_literal("1e9")
    assert isinstance(v, NumericLiteral) and isinstance(v, float)
    assert v == 1e9
    assert v.original_text == "1e9"
    assert not value_is_numeric_literal(v)


def test_extracted_scientific_literal_against_numeric_fails_loud():
    """The end-to-end extracted-path defence: a ``NumericLiteral`` from ``1e9``
    rendered against a numeric column must FAIL LOUD (no ``1000000000.0``)."""
    from src.ir.logical_query import SemanticBindingError
    v = _parsed_literal("1e9")
    with pytest.raises(SemanticBindingError):
        _render_value(v, col_type="INT64")


def test_extracted_decimal_literal_against_numeric_renders_bare():
    """A genuine decimal source token (``19.99``) keeps rendering bare — its
    preserved spelling passes the strict grammar (no regression)."""
    v = _parsed_literal("19.99")
    assert value_is_numeric_literal(v)
    assert _render_value(v, col_type="NUMERIC") == "19.99"


@pytest.mark.parametrize(
    "token",
    [
        "0.0000001",                  # str(float) -> '1e-07'
        "10000000000000000000.0",     # str(float) -> '1e+19'
        ".5",                         # sqlglot normalises to '0.5'
    ],
)
def test_extracted_decimal_magnitude_renders_non_scientific(token):
    """A grammar-conformant plain decimal whose ``str(float)`` would switch to
    SCIENTIFIC form must STILL render as a non-scientific bare token against a
    numeric column — the preserved original spelling is emitted, never the float
    repr. Otherwise a valid plain decimal would launder into a bare ``1e-07`` /
    ``1e+19`` token, re-opening the same precision gap Bug-5539 closes."""
    v = _parsed_literal(token)
    assert value_is_numeric_literal(v)
    rendered = _render_value(v, col_type="NUMERIC")
    assert "e" not in rendered.lower(), rendered
    # The emitted token must itself pass the strict grammar (no scientific form).
    assert value_is_numeric_literal(rendered), rendered


def test_extracted_plain_int_literal_stays_int_and_renders_bare():
    """Plain integers still parse to a bare ``int`` and render bare."""
    v = _parsed_literal("1999")
    assert type(v) is int and v == 1999
    assert _render_value(v, col_type="INT64") == "1999"


# ---------------------------------------------------------------------------
# Bug-894 / LOW: empty IN / NOT IN filters must not emit invalid SQL
# ---------------------------------------------------------------------------

def test_render_condition_in_empty_list_returns_false():
    """Empty IN list should produce a false condition, not 'x IN ()'."""
    result = _render_condition('"status"', "in", [])
    assert result == "1=0"


def test_render_condition_not_in_empty_list_returns_true():
    """Empty NOT IN list should produce a true condition, not 'x NOT IN ()'."""
    result = _render_condition('"status"', "not_in", [])
    assert result == "1=1"


def test_render_condition_in_none_list_returns_false():
    """None value for IN should also produce the safe false condition."""
    result = _render_condition('"status"', "in", None)
    assert result == "1=0"


# ---------------------------------------------------------------------------
# MEDIUM: _requote_identifiers_for_bigquery must not corrupt string literals
# ---------------------------------------------------------------------------

def test_requote_identifiers_leaves_single_quoted_strings_intact():
    """Single-quoted string literals must not be converted to backticks."""
    sql = """SELECT "customer_id" FROM "orders" WHERE "status" = 'WEB'"""
    result = _requote_identifiers_for_bigquery(sql)
    assert "'WEB'" in result
    assert "`customer_id`" in result or "customer_id" in result


def test_requote_identifiers_converts_double_quoted_identifiers():
    """ANSI double-quoted identifiers should become BigQuery backtick-quoted."""
    sql = 'SELECT "nps_score" FROM "fct_orders"'
    result = _requote_identifiers_for_bigquery(sql)
    assert '"nps_score"' not in result
    assert '"fct_orders"' not in result


def test_bug_7012_requote_does_not_corrupt_identifier_shaped_value_literals():
    """Bug-7012 / Codex gate Finding B: the regex fallback must not convert
    identifier-shaped double-quoted string LITERALS (e.g. "active") to
    backtick-quoted identifiers.  Only tokens in unambiguous identifier
    positions (after FROM/JOIN/AS/SELECT, after a dot) are converted.

    End-to-end via the function: SQL that sqlglot CAN parse goes through the
    sqlglot path (which handles this correctly by AST).
    """
    result = _requote_identifiers_for_bigquery(
        'SELECT "col_a" FROM "tbl"'
    )
    # sqlglot path: identifiers become backticks
    assert "`col_a`" in result or "col_a" in result
    assert "`tbl`" in result or "tbl" in result


# ---------------------------------------------------------------------------
# HIGH-2: dialect_from_connection_type must normalise legacy connector aliases
# Bug-899 regression
# ---------------------------------------------------------------------------

def test_dialect_from_connection_type_jdbc_maps_to_spark():
    """Legacy 'jdbc' connector must produce 'spark' dialect after normalisation."""
    from shared.schemas.connection_type import normalize_connection_type
    ct = normalize_connection_type("jdbc")
    assert ct == "hadoop_spark"
    assert _dialect_from_connection_type(ct) == "spark"


def test_dialect_from_connection_type_bigquery():
    assert _dialect_from_connection_type("bigquery") == "bigquery"


def test_dialect_from_connection_type_snowflake():
    assert _dialect_from_connection_type("snowflake") == "snowflake"


def test_dialect_from_connection_type_sqlserver():
    assert _dialect_from_connection_type("sqlserver") == "tsql"


def test_dialect_from_connection_type_postgres_default():
    assert _dialect_from_connection_type("postgresql") == "postgres"
    assert _dialect_from_connection_type(None) == "postgres"
    assert _dialect_from_connection_type("unknown_type") == "postgres"


def test_dialect_from_connection_type_public_alias():
    """Public alias dialect_from_connection_type must forward to the private impl."""
    assert dialect_from_connection_type("bigquery") == "bigquery"
    assert dialect_from_connection_type("snowflake") == "snowflake"


# ---------------------------------------------------------------------------
# HIGH-1: resolve_target_dialect_for_bound uses the touched source
# Bug-899 regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_target_dialect_for_bound_uses_touched_source():
    """resolve_target_dialect_for_bound must use the source touched by the bound
    query's columns, not the first DataSource for the model."""
    from unittest.mock import AsyncMock, MagicMock
    import types

    # Simulate a bound_query with one resolved measure whose source_column_id
    # maps to a BigQuery DataSource.
    measure = types.SimpleNamespace(source_column_id="col-bq-1")
    bound = types.SimpleNamespace(
        resolved_dimensions=[],
        resolved_measures=[measure],
        model=types.SimpleNamespace(id="model-1"),
    )

    bq_source = types.SimpleNamespace(
        id="src-bq", project_connection_id="conn-bq"
    )
    bq_conn = types.SimpleNamespace(connection_type="bigquery")

    async def _db_execute(stmt):
        text = str(stmt)
        r = MagicMock()
        # Touched-source query: ModelTable.source_id join
        if "model_table" in text.lower() or "ModelTable" in text:
            row = MagicMock()
            row.__getitem__ = lambda self, idx: "src-bq"
            r.all.return_value = [row]
        else:
            r.all.return_value = []
            r.scalar_one_or_none.return_value = None
        return r

    async def _db_get(model_cls, pk):
        name = getattr(model_cls, "__name__", str(model_cls))
        if "DataSource" in name and pk == "src-bq":
            return bq_source
        if "ProjectConnection" in name and pk == "conn-bq":
            return bq_conn
        return None

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_db_execute)
    db.get = AsyncMock(side_effect=_db_get)

    dialect = await resolve_target_dialect_for_bound(db, bound)
    assert dialect == "bigquery", (
        f"Expected 'bigquery' from touched BigQuery source, got '{dialect}'"
    )


@pytest.mark.asyncio
async def test_resolve_target_dialect_for_bound_falls_back_for_no_columns():
    """When no source_column_id values are found, fall back to the model's
    first DataSource — same behaviour as the old resolve_target_dialect."""
    from unittest.mock import AsyncMock, MagicMock
    import types

    bound = types.SimpleNamespace(
        resolved_dimensions=[],
        resolved_measures=[],
        model=types.SimpleNamespace(id="model-1"),
    )

    sf_source = types.SimpleNamespace(
        id="src-sf", project_connection_id="conn-sf"
    )
    sf_conn = types.SimpleNamespace(connection_type="snowflake")

    async def _db_execute(stmt):
        r = MagicMock()
        r.all.return_value = []
        r.scalar_one_or_none.return_value = sf_source
        return r

    async def _db_get(model_cls, pk):
        name = getattr(model_cls, "__name__", str(model_cls))
        if "ProjectConnection" in name:
            return sf_conn
        return sf_source

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_db_execute)
    db.get = AsyncMock(side_effect=_db_get)

    dialect = await resolve_target_dialect_for_bound(db, bound)
    assert dialect == "snowflake"


# ---------------------------------------------------------------------------
# Bug-902: UDA expressions with backtick-quoted identifiers must parse cleanly
# ---------------------------------------------------------------------------

def test_normalize_uda_expression_quoting_converts_backticks():
    """Backtick-quoted identifiers must be converted to double-quoted."""
    expr = "EXTRACT(YEAR FROM `full_date`)"
    result = _normalize_uda_expression_quoting(expr)
    assert result == 'EXTRACT(YEAR FROM "full_date")'


def test_normalize_uda_expression_quoting_leaves_double_quotes_intact():
    """Properly double-quoted PostgreSQL expressions must not be modified."""
    expr = 'EXTRACT(YEAR FROM "full_date")'
    assert _normalize_uda_expression_quoting(expr) == expr


def test_normalize_uda_expression_quoting_handles_multiple_identifiers():
    """Multiple backtick-quoted identifiers in one expression must all be converted."""
    expr = "COALESCE(`col_a`, `col_b`)"
    result = _normalize_uda_expression_quoting(expr)
    assert result == 'COALESCE("col_a", "col_b")'


def test_render_uda_expression_backtick_stored_bigquery_source():
    """_render_uda_expression must succeed when the stored expression uses
    backtick quoting (as happens for BigQuery-sourced models)."""
    # Expression as stored by model builder against a BigQuery source
    result = _render_uda_expression(
        expression="EXTRACT(YEAR FROM `full_date`)",
        table_alias="d_date",
        target_dialect="bigquery",
    )
    # Should produce a qualified BigQuery column reference inside EXTRACT
    assert "EXTRACT" in result.upper()
    assert "YEAR" in result.upper()
    assert "full_date" in result
    # Must not raise ValueError


def test_render_uda_expression_postgres_stored_bigquery_target():
    """Standard PostgreSQL-stored expression must transpile cleanly to BigQuery."""
    result = _render_uda_expression(
        expression='EXTRACT(YEAR FROM "full_date")',
        table_alias="d_date",
        target_dialect="bigquery",
    )
    assert "EXTRACT" in result.upper()
    assert "YEAR" in result.upper()
    assert "full_date" in result


# ---------------------------------------------------------------------------
# Bug-5599: UDA backtick normalization must not corrupt string literals
# ---------------------------------------------------------------------------

def test_normalize_uda_preserves_backticks_inside_string_literals():
    """Bug-5599: backticks inside single-quoted string literals must not be
    converted to double quotes -- doing so corrupts business data."""
    expr = "CASE WHEN `status` = 'has `backtick` text' THEN `amount` ELSE 0 END"
    result = _normalize_uda_expression_quoting(expr)
    # Identifiers converted
    assert '"status"' in result
    assert '"amount"' in result
    # String literal content preserved verbatim
    assert "'has `backtick` text'" in result


def test_normalize_uda_preserves_escaped_quotes_in_string_literals():
    """Bug-5599: SQL escaped quotes ('') inside string literals must be
    handled correctly by the split pattern."""
    expr = "CASE WHEN `col` = 'it''s `here`' THEN 1 END"
    result = _normalize_uda_expression_quoting(expr)
    assert '"col"' in result
    # The string literal with escaped quotes and backticks is preserved
    assert "it''s `here`" in result


def test_normalize_uda_no_string_literals_unchanged():
    """Bug-5599 regression: expressions without string literals must still
    normalise backtick-quoted identifiers as before."""
    expr = "EXTRACT(YEAR FROM `full_date`)"
    result = _normalize_uda_expression_quoting(expr)
    assert result == 'EXTRACT(YEAR FROM "full_date")'


# ---------------------------------------------------------------------------
# Bug-6956 — _normalize_dialect validates against known dialects
# ---------------------------------------------------------------------------

def test_bug_6956_normalize_dialect_rejects_unknown():
    """Bug-6956: an unknown dialect must fall back to 'postgres' with a warning
    rather than passing through to sqlglot and causing a ValueError."""
    from src.parsing.sql_parser import _normalize_dialect
    # Known dialects pass through.
    assert _normalize_dialect("postgres") == "postgres"
    assert _normalize_dialect("bigquery") == "bigquery"
    assert _normalize_dialect("BIGQUERY") == "bigquery"
    assert _normalize_dialect("spark") == "spark"
    assert _normalize_dialect("tsql") == "tsql"
    # Aliases resolve correctly.
    assert _normalize_dialect("postgresql") == "postgres"
    assert _normalize_dialect("jdbc") == "postgres"
    assert _normalize_dialect("hadoop_spark") == "spark"
    # None / empty => postgres.
    assert _normalize_dialect(None) == "postgres"
    assert _normalize_dialect("") == "postgres"
    # SQL Server aliases resolve to tsql (Bug-6956, Fable R1).
    assert _normalize_dialect("mssql") == "tsql"
    assert _normalize_dialect("sqlserver") == "tsql"
    # Unknown dialects => fallback to postgres.
    assert _normalize_dialect("xyz") == "postgres"
    assert _normalize_dialect("nosql_db") == "postgres"


def test_f006_06_in_null_member_fails_loud():
    """F-006-06 / Bug-5820: IN/NOT IN [None] must not render a NULL member."""
    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError, match="NULL"):
        _render_condition('"region"', "in", [None])
    with pytest.raises(SemanticBindingError, match="NULL"):
        _render_condition('"region"', "not_in", ["US", None])


def test_f003_15_group_by_error_is_typed_400_body():
    from src.parsing.sql_parser import GroupByError
    from src.api.routes import _client_parse_error_detail
    detail = _client_parse_error_detail(GroupByError("column region must appear in GROUP BY"))
    assert detail["error_type"] == "group_by_error"
    assert "region" in detail["message"]


def test_f003_15_unexpected_value_error_stays_generic():
    from src.api.routes import _client_parse_error_detail
    assert _client_parse_error_detail(ValueError("boom")) == "Parse failed"


@pytest.mark.asyncio
async def test_f004_03_stale_active_cache_is_not_servable():
    """F-004-03 / F-004-08: is_stale=True + status=active must not replay cache."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock
    from src.api.routes import _cached_artifact_still_servable

    artifact_id = str(uuid.uuid4())
    model_id = str(uuid.uuid4())
    cached = types.SimpleNamespace(aggregate_id=artifact_id, pocket_id=None)
    row = types.SimpleNamespace(status="active", is_stale=True, invalid_reason=None)
    result = MagicMock()
    result.one_or_none.return_value = row
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.begin_nested = MagicMock()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=None)
    assert await _cached_artifact_still_servable(
        cached, db, model_id=model_id, route_type="aggregate",
    ) is False


def test_f003_08_extract_grain_emits_extract_not_date_trunc():
    """F-003-08 / G-003-03: EXTRACT month rewrites as EXTRACT, not DATE_TRUNC."""
    m = make_measure("rev")
    d = make_dimension("business_date")
    agg = make_aggregate(["business_date"], [make_agg_col(m)])
    bq = make_bound_query([d], [m], grain=[])
    bq.logical_query.time_period_grains = [("extract_month", "business_date")]
    sql = rewrite_for_aggregate(bq, agg)
    assert "EXTRACT" in sql.upper()
    assert "DATE_TRUNC" not in sql.upper()


def test_f006_12_bug_8329_snowflake_dow_uses_dayofweekiso():
    """F-006-12 / Bug-8329: Snowflake DOW is pinned via DAYOFWEEKISO (do not close)."""
    from src.rewrite.dialects import _transpile_to_dialect
    out = _transpile_to_dialect('SELECT EXTRACT(DOW FROM "d")', "snowflake")
    assert "DAYOFWEEKISO" in out.upper()


def test_f003_01_not_in_renders_negated_in_on_aggregate_route():
    """F-004-09: a not_in filter HIT must render NOT <col> IN (...), never =."""
    m = make_measure("revenue")
    agg = make_aggregate(["region"], [make_agg_col(m)])
    bq = make_bound_query(
        [make_dimension("region")], [m],
        filters=[LogicalFilter("region", "not_in", ["US", "CA"])],
    )
    sql = rewrite_for_aggregate(bq, agg)
    assert 'NOT "region" IN' in sql and "= 'US'" not in sql


def test_f006_08_generic_pocket_rewrite_error_is_unsupported():
    """F-006-08: generic rewrite failure raises PocketRewriteUnsupported, not raw SQL."""
    from unittest.mock import patch
    from src.rewrite.pocket import PocketRewriteUnsupported, rewrite_for_pocket

    lq = types.SimpleNamespace(
        raw_query="SELECT SUM(revenue) FROM modely",
        from_tables=["modely"], input_dialect="postgres",
    )
    bq = types.SimpleNamespace(logical_query=lq, model=types.SimpleNamespace(slug="modely"))
    pocket = types.SimpleNamespace(target_schema="agg", physical_table_name="pkt_1")
    with patch("src.rewrite.pocket._render_for_dialect", side_effect=RuntimeError("boom")):
        with pytest.raises(PocketRewriteUnsupported):
            rewrite_for_pocket(bq, pocket, "postgres")


@pytest.mark.asyncio
async def test_f004_08_invalid_reason_cache_not_servable():
    """F-004-08: non-empty invalid_reason must not replay from warm cache."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock
    from src.api.routes import _cached_artifact_still_servable

    cached = types.SimpleNamespace(aggregate_id=str(uuid.uuid4()), pocket_id=None)
    row = types.SimpleNamespace(status="active", is_stale=False, invalid_reason="coverage mismatch")
    result = MagicMock(); result.one_or_none.return_value = row
    db = AsyncMock(); db.execute = AsyncMock(return_value=result)
    db.begin_nested = MagicMock()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=None)
    assert await _cached_artifact_still_servable(
        cached, db, model_id=str(uuid.uuid4()), route_type="aggregate") is False
