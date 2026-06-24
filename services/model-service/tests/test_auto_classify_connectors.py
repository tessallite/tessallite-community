"""Auto-classify heuristics work on every connector, not just PostgreSQL (F-014-02).

Before this fix the type vocabulary only recognised PostgreSQL spellings, so a
BigQuery ``float64`` amount, a Snowflake ``timestamp_ntz`` order date, or a SQL
Server ``bit`` flag were silently misclassified — measures and time dimensions
were lost. These tests pin the corrected behaviour using each connector's native
type spellings, plus the cardinality SQL builder shared across connectors.

Pure-function tests: no FastAPI, no DB.
"""
from __future__ import annotations

import pytest

from src.api.connections import (
    _apply_role_suggestions,
    _classify_table,
    _suggest_agg,
    _suggest_role,
)


def _col(name, dtype, distinct=None):
    return {
        "column_name": name,
        "data_type": dtype,
        "is_nullable": True,
        "approx_distinct": distinct,
    }


# ---------------------------------------------------------------------------
# _suggest_role: native spellings per connector resolve to the right role
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dtype", [
    "timestamp without time zone",  # PG
    "datetime",                      # BigQuery / SQL Server
    "timestamp_ntz",                 # Snowflake
    "timestamp",                     # Spark
])
def test_date_columns_are_time_dimensions_on_every_connector(dtype):
    assert _suggest_role("order_date", dtype, "fact", 365, 100_000) == "time_dimension"


@pytest.mark.parametrize("dtype", [
    "boolean",  # PG / Snowflake / Spark
    "bool",     # BigQuery
    "bit",      # SQL Server
])
def test_boolean_columns_are_dimensions_on_every_connector(dtype):
    assert _suggest_role("is_active", dtype, "fact", 2, 100_000) == "dimension"


@pytest.mark.parametrize("dtype", [
    "double precision",  # PG
    "float64",           # BigQuery
    "number",            # Snowflake
    "decimal",           # SQL Server
    "double",            # Spark
])
def test_numeric_measure_name_is_measure_on_every_connector(dtype):
    # The headline regression: a numeric amount column in a fact table must
    # become a measure regardless of how the connector spells the type.
    assert _suggest_role("total_amount", dtype, "fact", 90_000, 100_000) == "measure"


@pytest.mark.parametrize("dtype", ["int64", "number", "int", "bigint", "integer"])
def test_fk_columns_stay_dimensions_regardless_of_numeric_spelling(dtype):
    assert _suggest_role("customer_id", dtype, "fact", 5000, 100_000) == "dimension"


def test_non_pg_numeric_no_name_hint_in_fact_defaults_to_measure():
    # Snowflake NUMBER with no name hint, high cardinality, fact table -> measure.
    assert _suggest_role("val", "number", "fact", 80_000, 100_000) == "measure"


# ---------------------------------------------------------------------------
# _classify_table: a fact-shaped table on Snowflake classifies as fact
# ---------------------------------------------------------------------------

def test_snowflake_fact_table_classifies_as_fact():
    cols = [
        _col("order_id", "number", 100_000),
        _col("customer_id", "number", 5_000),
        _col("order_ts", "timestamp_ntz", 90_000),
        _col("ship_ts", "timestamp_ntz", 90_000),
        _col("amount", "number", 70_000),
        _col("quantity", "number", 200),
    ]
    # Without F-014-02, number/timestamp_ntz were unrecognised: numeric_ratio and
    # date-presence signals collapsed and this fact table mis-scored.
    assert _classify_table("fact_orders", cols, 1_000_000) == "fact"


def test_bigquery_fact_table_emits_measures():
    cols = [
        _col("event_id", "int64", 500_000),
        _col("user_id", "int64", 20_000),
        _col("event_time", "datetime", 400_000),
        _col("revenue", "float64", 300_000),
        _col("clicks", "int64", 100),
    ]
    classification = _classify_table("fact_events", cols, 2_000_000)
    _apply_role_suggestions(cols, classification, 2_000_000)
    by_name = {c["column_name"]: c for c in cols}
    assert classification == "fact"
    assert by_name["revenue"]["suggested_role"] == "measure"
    assert by_name["event_time"]["suggested_role"] == "time_dimension"
    assert by_name["user_id"]["suggested_role"] == "dimension"  # FK


def test_sqlserver_dimension_table_no_false_measures():
    cols = [
        _col("country_id", "int", 200),
        _col("country_name", "nvarchar", 200),
        _col("region", "nvarchar", 8),
        _col("is_eu", "bit", 2),
    ]
    classification = _classify_table("dim_country", cols, 200)
    _apply_role_suggestions(cols, classification, 200)
    assert classification.startswith("dim")
    # No numeric measure-named column -> no measures invented.
    assert all(c["suggested_role"] != "measure" for c in cols)


# ---------------------------------------------------------------------------
# Degradation: when cardinality is absent the heuristics still run on type/name
# ---------------------------------------------------------------------------

def test_suggestions_still_produced_without_cardinality():
    # approx_distinct=None (cardinality probe unavailable) must not crash and the
    # name/type signals still classify a measure.
    cols = [
        _col("sale_id", "number", None),
        _col("sale_ts", "timestamp_ntz", None),
        _col("amount", "number", None),
    ]
    classification = _classify_table("fact_sales", cols, 50_000)
    _apply_role_suggestions(cols, classification, 50_000)
    by_name = {c["column_name"]: c for c in cols}
    assert by_name["amount"]["suggested_role"] == "measure"
    assert by_name["sale_ts"]["suggested_role"] == "time_dimension"
    assert by_name["amount"]["cardinality_ratio"] is None


# ---------------------------------------------------------------------------
# _suggest_agg
# ---------------------------------------------------------------------------

def test_suggest_agg_defaults_sum_for_non_pg_spellings():
    assert _suggest_agg("amount", "number") == "sum"
    assert _suggest_agg("unit_price", "float64") == "avg"
    assert _suggest_agg("conversion_rate", "double") == "avg"
    assert _suggest_agg("item_count", "int64") == "sum"
