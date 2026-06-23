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
