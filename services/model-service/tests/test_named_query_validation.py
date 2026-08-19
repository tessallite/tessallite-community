"""Unit tests for Named Query definition validation (pure, DB-free).

Covers ``src/named_query_validation.py``: DML scan, @-reference rejection,
single-statement enforcement, shape derivation (aggregated vs projection),
output-column type derivation, and the column cap (reject, never truncate).
"""
from __future__ import annotations

import pytest

from src.named_query_validation import (
    NamedQueryValidationError,
    check_column_cap,
    contains_parameter_reference,
    derive_definition_metadata,
    derive_shape,
    scan_dml_keywords,
)

pytestmark = pytest.mark.unit

_DIMENSION_TYPES = {"city_name": "string", "branch_id": "string", "order_date": "date"}
_MEASURE_NAMES = {"transaction_value", "transaction_usd_converted"}


def _derive(sql: str) -> dict:
    return derive_definition_metadata(
        sql,
        dimension_types=_DIMENSION_TYPES,
        measure_names=_MEASURE_NAMES,
    )


# ---------------------------------------------------------------------------
# Shape derivation
# ---------------------------------------------------------------------------

def test_projection_shape_for_plain_select() -> None:
    derived = _derive("SELECT * FROM modely WHERE branch_id = '3279863'")
    assert derived["shape"] == "projection"
    assert derived["output_columns"] == [{"name": "*", "type": "string"}]


def test_projection_shape_with_explicit_columns() -> None:
    derived = _derive("SELECT branch_id, city_name FROM modely")
    assert derived["shape"] == "projection"
    assert derived["output_columns"] == [
        {"name": "branch_id", "type": "string"},
        {"name": "city_name", "type": "string"},
    ]


def test_aggregated_shape_for_group_by() -> None:
    derived = _derive(
        "SELECT city_name, SUM(transaction_value) AS sum_trx_values "
        "FROM modely GROUP BY city_name ORDER BY sum_trx_values DESC LIMIT 3"
    )
    assert derived["shape"] == "aggregated"
    assert derived["output_columns"][0] == {"name": "city_name", "type": "string"}
    assert derived["output_columns"][1] == {
        "name": "sum_trx_values", "type": "number",
    }
    # COUNT(1) AS row_count -> number
    derived2 = _derive(
        "SELECT city_name, COUNT(1) AS row_count FROM modely GROUP BY city_name"
    )
    assert derived2["output_columns"][1] == {"name": "row_count", "type": "number"}


def test_aggregated_shape_for_projection_aggregate_without_group_by() -> None:
    derived = _derive("SELECT SUM(transaction_value) AS total FROM modely")
    assert derived["shape"] == "aggregated"


def test_measure_reference_output_is_number() -> None:
    derived = _derive("SELECT transaction_value FROM modely LIMIT 5")
    assert derived["output_columns"][0] == {"name": "transaction_value", "type": "number"}


def test_unresolvable_expression_defaults_to_string() -> None:
    derived = _derive("SELECT unknown_expr FROM modely")
    assert derived["output_columns"][0] == {"name": "unknown_expr", "type": "string"}


# ---------------------------------------------------------------------------
# Defence-in-depth scans
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, keyword",
    [
        ("DELETE FROM modely", "DELETE"),
        ("SELECT * FROM modely; DROP TABLE x", "DROP"),
        ("UPDATE modely SET x = 1", "UPDATE"),
        ("INSERT INTO modely SELECT 1", "INSERT"),
    ],
)
def test_dml_scan_rejects(sql: str, keyword: str) -> None:
    assert scan_dml_keywords(sql) == keyword


def test_dml_scan_ignores_literals_and_comments() -> None:
    assert scan_dml_keywords("SELECT 'DELETE FROM x' AS note FROM modely") is None
    assert scan_dml_keywords("SELECT * FROM modely -- DELETE everything\n") is None
    assert scan_dml_keywords("SELECT * FROM modely /* UPDATE */") is None


def test_parameter_reference_detection() -> None:
    assert contains_parameter_reference("SELECT * FROM @other")
    assert contains_parameter_reference("SELECT * FROM modely WHERE x IN (@p)")
    assert not contains_parameter_reference("SELECT '@not a ref' FROM modely")
    assert not contains_parameter_reference("SELECT * FROM modely")


def test_multi_statement_rejected() -> None:
    with pytest.raises(NamedQueryValidationError) as excinfo:
        _derive("SELECT * FROM modely; SELECT 1")
    assert "single statement" in str(excinfo.value)


def test_empty_definition_rejected() -> None:
    with pytest.raises(NamedQueryValidationError) as excinfo:
        _derive("   ")
    assert "must not be empty" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Column cap
# ---------------------------------------------------------------------------

def test_column_cap_rejects_never_truncates() -> None:
    with pytest.raises(NamedQueryValidationError) as excinfo:
        check_column_cap(
            [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            effective_column_cap=2,
        )
    assert "max_columns" in str(excinfo.value)


def test_column_cap_allows_star_unknown_width() -> None:
    # A star projection has unknown width at authoring time; the refresh
    # manifest is the authoritative check.
    check_column_cap([{"name": "*"}], effective_column_cap=2)
