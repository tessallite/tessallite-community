"""Tests for shared.semantic.table_analyzer — measure-vs-dimension validation."""
import pytest
from unittest.mock import MagicMock

from shared.semantic.table_analyzer import (
    MeasureWarning,
    _classify_column,
    _has_low_cardinality,
    _validate_measure,
    validate_measures,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_col(
    name: str,
    data_type: str = "integer",
    cardinality: int | None = None,
    col_id: str = "col-1",
) -> MagicMock:
    col = MagicMock()
    col.id = col_id
    col.column_name = name
    col.data_type = data_type
    col.cardinality_estimate = cardinality
    return col


def _make_table(
    columns: list,
    row_count: int | None = None,
    table_type: str = "fact",
    table_id: str = "tbl-1",
) -> MagicMock:
    tbl = MagicMock()
    tbl.id = table_id
    tbl.table_type = table_type
    tbl.row_count_estimate = row_count
    tbl.columns = columns
    return tbl


# ---------------------------------------------------------------------------
# _has_low_cardinality
# ---------------------------------------------------------------------------

class TestHasLowCardinality:
    def test_none_cardinality(self):
        assert _has_low_cardinality(None, 10000) is False

    def test_none_row_count(self):
        assert _has_low_cardinality(50, None) is False

    def test_zero_row_count(self):
        assert _has_low_cardinality(50, 0) is False

    def test_low_ratio_low_absolute(self):
        assert _has_low_cardinality(50, 100000) is True

    def test_low_ratio_high_absolute(self):
        assert _has_low_cardinality(200, 100000) is False

    def test_high_ratio(self):
        assert _has_low_cardinality(500, 1000) is False

    def test_boundary_ratio(self):
        # 100 / 10000 = 1% — NOT less than 1%, so False
        assert _has_low_cardinality(100, 10000) is False

    def test_just_under_ratio_boundary(self):
        # 99 / 10000 = 0.99% — less than 1% and <= 100, so True
        assert _has_low_cardinality(99, 10000) is True


# ---------------------------------------------------------------------------
# _classify_column
# ---------------------------------------------------------------------------

class TestClassifyColumn:
    """Tests for the dual-signal column classifier."""

    # Date detection
    def test_date_type(self):
        role, _ = _classify_column("created_at", "timestamp", None, None, "fact")
        assert role == "date_key"

    def test_date_name_pattern(self):
        role, _ = _classify_column("order_date", "varchar", None, None, "fact")
        assert role == "date_key"

    # Numeric dimension patterns (_id, _key, _code, etc.)
    def test_id_column(self):
        role, _ = _classify_column("customer_id", "integer", None, None, "fact")
        assert role == "dimension"

    def test_key_column(self):
        role, _ = _classify_column("product_key", "bigint", None, None, "fact")
        assert role == "dimension"

    def test_code_column(self):
        role, _ = _classify_column("region_code", "integer", None, None, "fact")
        assert role == "dimension"

    def test_number_column(self):
        role, _ = _classify_column("order_number", "integer", None, None, "fact")
        assert role == "dimension"

    def test_fk_column(self):
        role, _ = _classify_column("store_fk", "bigint", None, None, "fact")
        assert role == "dimension"

    def test_zip_code(self):
        role, _ = _classify_column("zip_code", "integer", None, None, "fact")
        assert role == "dimension"

    def test_phone(self):
        role, _ = _classify_column("phone_number", "bigint", None, None, "fact")
        assert role == "dimension"

    # Measure patterns in fact tables
    def test_revenue_in_fact(self):
        role, _ = _classify_column("total_revenue", "numeric", None, None, "fact")
        assert role == "measure"

    def test_quantity_in_fact(self):
        role, _ = _classify_column("order_qty", "integer", None, None, "fact")
        assert role == "measure"

    # Measure patterns in NON-fact tables → should NOT be measure
    def test_revenue_in_dim_table(self):
        role, _ = _classify_column("total_revenue", "numeric", None, None, "dim_detail")
        assert role == "dimension"

    def test_amount_in_dim_table(self):
        role, _ = _classify_column("balance_amount", "decimal", None, None, "dim_aggregate")
        assert role == "dimension"

    # Ambiguous patterns
    def test_score_low_cardinality_in_fact(self):
        role, _ = _classify_column("customer_score", "integer", 10, 100000, "fact")
        assert role == "dimension"

    def test_score_high_cardinality_in_fact(self):
        role, _ = _classify_column("customer_score", "integer", 5000, 100000, "fact")
        assert role == "measure"

    def test_class_in_dim_table(self):
        role, _ = _classify_column("item_class", "integer", None, None, "dim_detail")
        assert role == "dimension"

    def test_rank_no_cardinality_in_fact(self):
        role, _ = _classify_column("sales_rank", "integer", None, None, "fact")
        assert role == "measure"

    # Unrecognised numeric columns
    def test_unknown_numeric_in_fact_high_cardinality(self):
        role, _ = _classify_column("xyzabc", "float", 50000, 100000, "fact")
        assert role == "measure"

    def test_unknown_numeric_in_fact_low_cardinality(self):
        role, _ = _classify_column("xyzabc", "integer", 5, 100000, "fact")
        assert role == "dimension"

    def test_unknown_numeric_in_dim_table(self):
        role, _ = _classify_column("xyzabc", "integer", None, None, "dim_detail")
        assert role == "dimension"

    # Text columns
    def test_text_column(self):
        role, _ = _classify_column("description", "varchar", None, None, "fact")
        assert role == "dimension"

    # Unknown type
    def test_boolean_column(self):
        role, _ = _classify_column("is_active", "boolean", None, None, "fact")
        assert role == "ignore"


# ---------------------------------------------------------------------------
# _validate_measure
# ---------------------------------------------------------------------------

class TestValidateMeasure:
    """Tests for the measure-vs-dimension warning generator."""

    def test_non_numeric_no_warning(self):
        w = _validate_measure("category", "varchar", None, None, "col-1")
        assert w is None

    # Signal 1: Name pattern
    def test_id_column_high_severity(self):
        w = _validate_measure("customer_id", "integer", None, None, "col-1")
        assert w is not None
        assert w.severity == "high"
        assert w.suggested_role == "dimension"

    def test_key_column_high_severity(self):
        w = _validate_measure("product_key", "bigint", None, None, "col-1")
        assert w is not None
        assert w.severity == "high"

    def test_code_column_high_severity(self):
        w = _validate_measure("region_code", "integer", None, None, "col-1")
        assert w is not None
        assert w.severity == "high"

    # Signal 2: Low cardinality
    def test_low_cardinality_medium_severity(self):
        w = _validate_measure("flag_value", "integer", 5, 100000, "col-1")
        assert w is not None
        assert w.severity == "medium"
        assert "5 distinct values" in w.reason

    def test_high_cardinality_no_warning(self):
        w = _validate_measure("unit_price", "numeric", 50000, 100000, "col-1")
        assert w is None

    # Signal 3: Ambiguous pattern with low-ish cardinality
    def test_score_ambiguous_low_cardinality(self):
        # 20 / 100k = 0.02% — Signal 2 (low cardinality) fires before Signal 3
        w = _validate_measure("risk_score", "integer", 20, 100000, "col-1")
        assert w is not None
        assert w.severity == "medium"

    def test_score_ambiguous_moderate_cardinality(self):
        # 200 / 100k = 0.2% — above 1% threshold for Signal 2, but under 5%
        # for Signal 3 and <= 500, so ambiguous pattern warning fires
        w = _validate_measure("risk_score", "integer", 200, 100000, "col-1")
        assert w is not None
        assert w.severity == "low"

    def test_score_ambiguous_high_cardinality(self):
        w = _validate_measure("risk_score", "float", 80000, 100000, "col-1")
        assert w is None

    def test_class_ambiguous_moderate_cardinality(self):
        w = _validate_measure("item_class", "integer", 300, 100000, "col-1")
        assert w is not None
        assert w.severity == "low"

    def test_class_ambiguous_over_threshold(self):
        w = _validate_measure("item_class", "integer", 600, 100000, "col-1")
        assert w is None

    # Name pattern takes priority over low cardinality
    def test_id_pattern_wins_over_cardinality(self):
        w = _validate_measure("store_id", "integer", 5, 100000, "col-1")
        assert w is not None
        assert w.severity == "high"

    # Normal measure — no warning
    def test_clean_measure(self):
        w = _validate_measure("total_revenue", "numeric", 45000, 100000, "col-1")
        assert w is None


# ---------------------------------------------------------------------------
# validate_measures (integration with ModelTable mock)
# ---------------------------------------------------------------------------

class TestValidateMeasures:
    def test_warns_on_id_column(self):
        cols = [
            _make_col("customer_id", "integer", col_id="c1"),
            _make_col("total_amount", "numeric", col_id="c2"),
        ]
        table = _make_table(cols, row_count=50000)
        warnings = validate_measures(table, ["customer_id", "total_amount"])
        assert len(warnings) == 1
        assert warnings[0].column_name == "customer_id"
        assert warnings[0].severity == "high"

    def test_no_warnings_for_clean_measures(self):
        cols = [
            _make_col("revenue", "numeric", cardinality=40000, col_id="c1"),
            _make_col("quantity", "integer", cardinality=500, col_id="c2"),
        ]
        table = _make_table(cols, row_count=50000)
        warnings = validate_measures(table, ["revenue", "quantity"])
        assert len(warnings) == 0

    def test_only_checks_listed_measures(self):
        cols = [
            _make_col("customer_id", "integer", col_id="c1"),
            _make_col("revenue", "numeric", col_id="c2"),
        ]
        table = _make_table(cols, row_count=50000)
        warnings = validate_measures(table, ["revenue"])
        assert len(warnings) == 0

    def test_case_insensitive_matching(self):
        cols = [
            _make_col("Store_ID", "integer", col_id="c1"),
        ]
        table = _make_table(cols, row_count=50000)
        warnings = validate_measures(table, ["store_id"])
        assert len(warnings) == 1

    def test_low_cardinality_warning(self):
        cols = [
            _make_col("status_flag", "integer", cardinality=3, col_id="c1"),
        ]
        table = _make_table(cols, row_count=100000)
        warnings = validate_measures(table, ["status_flag"])
        assert len(warnings) == 1
        assert warnings[0].severity == "medium"

    def test_empty_measure_list(self):
        cols = [_make_col("revenue", "numeric", col_id="c1")]
        table = _make_table(cols, row_count=50000)
        warnings = validate_measures(table, [])
        assert len(warnings) == 0

    def test_no_columns_on_table(self):
        table = _make_table([], row_count=50000)
        warnings = validate_measures(table, ["revenue"])
        assert len(warnings) == 0
