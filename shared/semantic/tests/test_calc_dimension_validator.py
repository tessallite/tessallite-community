"""Tests for calculated dimension expression validator."""
import pytest
import types

from shared.semantic.calc_dimension_validator import (
    CalcDimensionValidationError,
    validate_calc_expression,
)


def test_simple_case_expression():
    result = validate_calc_expression(
        "CASE WHEN amount > 0 THEN 'Credit' ELSE 'Debit' END"
    )
    assert len(result.column_refs) == 1
    assert result.column_refs[0].column == "amount"


def test_coalesce_expression():
    result = validate_calc_expression("COALESCE(city, 'Unknown')")
    assert len(result.column_refs) == 1
    assert result.column_refs[0].column == "city"


def test_arithmetic_expression():
    result = validate_calc_expression("price * quantity")
    assert len(result.column_refs) == 2


def test_concat_expression():
    result = validate_calc_expression("CONCAT(first_name, ' ', last_name)")
    assert len(result.column_refs) == 2


def test_empty_expression_raises():
    with pytest.raises(CalcDimensionValidationError, match="empty"):
        validate_calc_expression("")


def test_invalid_sql_raises():
    with pytest.raises(CalcDimensionValidationError, match="Invalid SQL"):
        validate_calc_expression("SELECT * FROM ;;;; INVALID")


def test_subquery_disallowed():
    with pytest.raises(CalcDimensionValidationError, match="Disallowed construct"):
        validate_calc_expression("(SELECT MAX(x) FROM t)")


def test_cross_table_column_refs():
    cols = [
        types.SimpleNamespace(column_name="amount", model_table_id="t1"),
        types.SimpleNamespace(column_name="region", model_table_id="t2"),
    ]
    tables = [
        types.SimpleNamespace(id="t1", alias="fact"),
        types.SimpleNamespace(id="t2", alias="dim_geo"),
    ]
    result = validate_calc_expression(
        "CASE WHEN amount > 100 THEN region ELSE 'Other' END",
        model_columns=cols,
        model_tables=tables,
    )
    assert "t1" in result.table_ids
    assert "t2" in result.table_ids


def test_single_table_expression():
    cols = [
        types.SimpleNamespace(column_name="price", model_table_id="t1"),
        types.SimpleNamespace(column_name="quantity", model_table_id="t1"),
    ]
    tables = [
        types.SimpleNamespace(id="t1", alias="orders"),
    ]
    result = validate_calc_expression(
        "price * quantity",
        model_columns=cols,
        model_tables=tables,
    )
    assert result.table_ids == ["t1"]


def test_allowed_functions_accepted():
    for expr in [
        "UPPER(col)", "LOWER(col)", "TRIM(col)",
        "COALESCE(col, 0)", "CAST(col AS TEXT)",
        "ABS(col)", "ROUND(col, 2)",
    ]:
        result = validate_calc_expression(expr)
        assert result is not None


def test_calc_expression_with_nullif():
    result = validate_calc_expression("NULLIF(status, 'deleted')")
    assert len(result.column_refs) == 1
