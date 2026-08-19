"""Tests for the KPI Business Builder — validation and compilation."""
import uuid

import pytest

from src.kpi_business_builder import (
    compile_business_definition,
    validate_business_definition,
    CompiledBusinessKpi,
)
from shared.semantic.kpi_expression import validate_expression


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ids(*names: str) -> dict[str, str]:
    return {str(uuid.uuid4()): n for n in names}


def _id_set(name_map: dict[str, str]) -> set[str]:
    return set(name_map.keys())


def _base_def(formula: dict, **extras) -> dict:
    d = {
        "builder": "business_kpi",
        "version": 1,
        "formula": formula,
    }
    d.update(extras)
    return d


MEASURES = _ids("revenue", "cost", "headcount", "nps_score", "days_to_deliver")
DIMS = _ids("country", "region", "product_line", "invoice_id")
TIME_DIMS = _ids("order_date", "delivery_date")
ALL_DIMS = {**DIMS, **TIME_DIMS}


# ---------------------------------------------------------------------------
# Validation — happy path
# ---------------------------------------------------------------------------

class TestValidationHappyPath:

    def test_single_measure_valid(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "single_measure", "measure_id": mid, "aggregation": "sum"})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_ratio_valid(self):
        ids = list(MEASURES.keys())
        defn = _base_def({"type": "ratio", "numerator_measure_id": ids[0], "denominator_measure_id": ids[1]})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_count_records_valid(self):
        defn = _base_def({"type": "count_records"})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_count_distinct_valid(self):
        did = list(DIMS.keys())[3]
        defn = _base_def({"type": "count_distinct", "dimension_id": did})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_moving_average_valid(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "moving_average", "measure_id": mid, "window_size": 6, "grain": "month"})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_compare_periods_valid(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "compare_periods", "measure_id": mid, "comparison": "yoy_growth_pct"})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_with_time_window(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_month"},
        )
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []

    def test_with_filters(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[
                {"dimension_id": did, "operator": "eq", "values": ["Germany"]},
            ],
        )
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert errors == []


# ---------------------------------------------------------------------------
# Validation — error cases
# ---------------------------------------------------------------------------

class TestValidationErrors:

    def test_missing_builder(self):
        defn = {"version": 1, "formula": {"type": "single_measure"}}
        errors = validate_business_definition(defn, set(), set(), set())
        assert any("builder" in e for e in errors)

    def test_wrong_version(self):
        defn = {"builder": "business_kpi", "version": 99, "formula": {"type": "single_measure"}}
        errors = validate_business_definition(defn, set(), set(), set())
        assert any("version" in e for e in errors)

    def test_unknown_formula_type(self):
        defn = _base_def({"type": "unknown_type"})
        errors = validate_business_definition(defn, set(), set(), set())
        assert any("unknown formula type" in e for e in errors)

    def test_single_measure_missing_id(self):
        defn = _base_def({"type": "single_measure"})
        errors = validate_business_definition(defn, set(), set(), set())
        assert any("measure_id" in e and "required" in e for e in errors)

    def test_single_measure_wrong_model(self):
        defn = _base_def({"type": "single_measure", "measure_id": str(uuid.uuid4())})
        errors = validate_business_definition(defn, set(), set(), set())
        assert any("does not belong" in e for e in errors)

    def test_ratio_missing_denominator(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "ratio", "numerator_measure_id": mid})
        errors = validate_business_definition(defn, _id_set(MEASURES), set(), set())
        assert any("denominator_measure_id" in e for e in errors)

    def test_filter_bad_operator(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "bad_op"}],
        )
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert any("not supported" in e for e in errors)

    def test_time_window_wrong_dimension(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": did, "preset": "last_month"},
        )
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert any("not a time/date dimension" in e for e in errors)

    def test_parameter_filter_missing_default(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[
                {"dimension_id": did, "operator": "eq", "mode": "parameter", "parameter_name": "region_param"},
            ],
        )
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
            parameter_names={"region_param"},
        )
        assert any("default_value" in e for e in errors)

    def test_moving_average_bad_window(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "moving_average", "measure_id": mid, "window_size": 1})
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert any("window_size" in e for e in errors)

    def test_time_calc_invalid_type(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "invalid_calc"},
        })
        errors = validate_business_definition(defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS))
        assert any("unknown time_calculation" in e for e in errors)

    def test_relative_filter_bypasses_operator_value_checks(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": tdid,
                "operator": "between",
                "mode": "relative",
                "values": ["last_30_days"],
            }],
        )
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
        )
        assert errors == [], f"Relative filter should pass validation: {errors}"

    def test_relative_filter_requires_preset(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": tdid,
                "operator": "between",
                "mode": "relative",
                "values": [],
            }],
        )
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
        )
        assert any("relative mode requires a preset" in e for e in errors)

    def test_relative_filter_rejects_non_time_dimension(self):
        mid = list(MEASURES.keys())[0]
        non_time_dim = list(DIMS.keys())[0]  # e.g. "country" — not a time dim
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": non_time_dim,
                "operator": "eq",
                "mode": "relative",
                "values": ["last_30_days"],
            }],
        )
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
        )
        assert any("only valid for time dimensions" in e for e in errors)

    def test_relative_filter_rejects_unknown_preset(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": tdid,
                "operator": "eq",
                "mode": "relative",
                "values": ["banana"],
            }],
        )
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
        )
        assert any("unknown relative preset" in e for e in errors)


# ---------------------------------------------------------------------------
# Compilation — expression generation
# ---------------------------------------------------------------------------

class TestCompilation:

    def test_single_measure_expression(self):
        mid = list(MEASURES.keys())[0]
        name = MEASURES[mid]
        defn = _base_def({"type": "single_measure", "measure_id": mid})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert f'measure("{name}")' in result.expression

    def test_ratio_expression(self):
        ids = list(MEASURES.keys())
        defn = _base_def({
            "type": "ratio",
            "numerator_measure_id": ids[0],
            "denominator_measure_id": ids[1],
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "safe_div(" in result.expression
        assert MEASURES[ids[0]] in result.expression
        assert MEASURES[ids[1]] in result.expression

    def test_count_records_expression(self):
        defn = _base_def({"type": "count_records"})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "count(" in result.expression

    def test_count_distinct_expression(self):
        did = list(DIMS.keys())[3]
        defn = _base_def({"type": "count_distinct", "dimension_id": did})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "count_distinct(" in result.expression

    def test_moving_average_expression(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "moving_average", "measure_id": mid, "window_size": 6, "grain": "month"})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "moving_avg(" in result.expression
        assert "6" in result.expression

    def test_yoy_growth_pct_expression(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "compare_periods", "measure_id": mid, "comparison": "yoy_growth_pct"})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "safe_div(" in result.expression
        assert 'prior_period(' in result.expression
        assert '"year"' in result.expression


# ---------------------------------------------------------------------------
# Compilation — time calculation wrapping
# ---------------------------------------------------------------------------

class TestTimeCalculation:

    def test_prior_period_wraps(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "prior_period", "grain": "month", "periods": 1},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "prior_period(" in result.expression

    def test_ytd_wraps(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "period_to_date", "period": "ytd"},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert 'period_to_date(' in result.expression
        assert '"year"' in result.expression

    def test_trailing_sum_wraps(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "trailing_sum", "grain": "month", "periods": 12},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "trailing_sum(" in result.expression
        assert "12" in result.expression

    def test_yoy_growth_wraps(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "yoy_growth_pct"},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "safe_div(" in result.expression
        assert 'prior_period(' in result.expression
        assert '"year"' in result.expression


# ---------------------------------------------------------------------------
# Compilation — time window predicates
# ---------------------------------------------------------------------------

class TestTimeWindow:

    def test_last_month_predicates(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_month"},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert len(result.time_window_predicates) == 2
        assert "DATE_TRUNC" in result.time_window_predicates[0]
        assert result.time_dimension_name == TIME_DIMS[tdid]

    def test_last_6_months_predicates(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_6_months"},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert len(result.time_window_predicates) == 2
        assert "6 months" in result.time_window_predicates[0]

    def test_custom_range_predicates(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "custom_range", "start": "2025-01-01", "end": "2025-06-30"},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert len(result.time_window_predicates) == 2
        assert "2025-01-01" in result.time_window_predicates[0]
        assert "2025-06-30" in result.time_window_predicates[1]


# ---------------------------------------------------------------------------
# Compilation — filter predicates
# ---------------------------------------------------------------------------

class TestFilters:

    def test_eq_filter(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["Germany"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert len(result.filter_predicates) == 1
        assert "Germany" in result.filter_predicates[0]
        assert "=" in result.filter_predicates[0]

    def test_in_filter(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "in", "values": ["Germany", "France"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert len(result.filter_predicates) == 1
        assert "IN" in result.filter_predicates[0]
        assert "Germany" in result.filter_predicates[0]
        assert "France" in result.filter_predicates[0]

    def test_not_in_filter(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "not_in", "values": ["Cancelled"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "NOT IN" in result.filter_predicates[0]

    def test_is_null_filter(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "is_null"}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "IS NULL" in result.filter_predicates[0]

    def test_between_filter(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "between", "values": ["100", "500"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "BETWEEN" in result.filter_predicates[0]

    def test_like_filter_wraps_value_as_contains_pattern(self):
        # Bug-5925: the Business Builder summary describes "like" as
        # "contains" — the compiled SQL must actually implement contains
        # semantics (wrap in %...%), not an exact-match LIKE.
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "like", "values": ["Acme"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "LIKE '%Acme%'" in result.filter_predicates[0]
        assert "NOT LIKE" not in result.filter_predicates[0]

    def test_not_like_filter_wraps_value_as_contains_pattern(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "not_like", "values": ["Test"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "NOT LIKE '%Test%'" in result.filter_predicates[0]

    def test_like_filter_escapes_wildcard_characters_in_value(self):
        # A literal % or _ typed by the user must not be misread as a
        # LIKE wildcard once wrapped.
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "like", "values": ["50%_off"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "LIKE '%50\\%\\_off%'" in result.filter_predicates[0]

    def test_like_filter_escapes_single_quote_in_value(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "like", "values": ["O'Brien"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "LIKE '%O''Brien%'" in result.filter_predicates[0]

    # Bug-6253: column-type-aware literal typing. Numeric columns must emit a
    # bare token so strictly-typed connectors accept the comparison; string
    # columns (and non-numeric literals) stay quoted.

    def test_numeric_column_emits_bare_literal(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["100"]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "integer"}
        )
        pred = result.filter_predicates[0]
        assert "= 100" in pred
        assert "'100'" not in pred

    def test_string_column_keeps_quoted_literal(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["100"]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "varchar"}
        )
        assert "'100'" in result.filter_predicates[0]

    def test_numeric_column_non_numeric_value_stays_quoted(self):
        # Fail-safe: a non-numeric value against a numeric column must never
        # be emitted as a bare token (injection surface).
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["10 OR 1=1"]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "numeric"}
        )
        assert "'10 OR 1=1'" in result.filter_predicates[0]

    def test_numeric_in_filter_emits_bare_tokens(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "in", "values": ["1", "2", "3"]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "bigint"}
        )
        pred = result.filter_predicates[0]
        assert "IN (1, 2, 3)" in pred

    def test_numeric_column_huge_int_does_not_crash(self):
        # Codex R1 finding: math.isfinite(10**400) raises OverflowError. A huge
        # integer value must render fail-safe (bare digit string, never a 500).
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        big = 10 ** 400
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": [big]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "numeric"}
        )
        assert str(big) in result.filter_predicates[0]

    def test_numeric_column_exponent_float_falls_back_to_quoted(self):
        # A native float in scientific form (1e-07) must not emit a bare
        # exponent token — the strict grammar rejects it, so it is quoted.
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": [1e-07]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "numeric"}
        )
        pred = result.filter_predicates[0]
        assert "'1e-07'" in pred

    def test_numeric_column_plain_float_emits_bare(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": [100.5]}],
        )
        result = compile_business_definition(
            defn, MEASURES, ALL_DIMS, dimension_data_types={did: "numeric"}
        )
        pred = result.filter_predicates[0]
        assert "= 100.5" in pred
        assert "'100.5'" not in pred

    def test_no_types_defaults_to_quoted(self):
        # Backward compatible: without dimension_data_types every literal is
        # quoted, exactly as before Bug-6253.
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["100"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "'100'" in result.filter_predicates[0]

    def test_parameter_filter_uses_default(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": did,
                "operator": "eq",
                "mode": "parameter",
                "parameter_name": "region_param",
                "default_value": "EMEA",
            }],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "EMEA" in result.filter_predicates[0]

    def test_relative_date_filter_resolves_preset(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": tdid,
                "operator": "between",
                "mode": "relative",
                "values": ["last_30_days"],
            }],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert len(result.filter_predicates) >= 1
        assert "CURRENT_DATE" in result.filter_predicates[0]
        assert "30 days" in result.filter_predicates[0]

    def test_relative_date_filter_last_month(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{
                "dimension_id": tdid,
                "operator": "between",
                "mode": "relative",
                "values": ["last_month"],
            }],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert len(result.filter_predicates) == 2
        assert "DATE_TRUNC" in result.filter_predicates[0]


# ---------------------------------------------------------------------------
# Compilation — where_clause property
# ---------------------------------------------------------------------------

class TestWhereClause:

    def test_combined_where(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_month"},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["Germany"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        wc = result.where_clause
        assert wc is not None
        assert "Germany" in wc
        assert "DATE_TRUNC" in wc

    def test_no_where_clause(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "single_measure", "measure_id": mid})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.where_clause is None


# ---------------------------------------------------------------------------
# Compilation — summary
# ---------------------------------------------------------------------------

class TestSummary:

    def test_single_measure_summary(self):
        mid = list(MEASURES.keys())[0]
        name = MEASURES[mid]
        defn = _base_def({"type": "single_measure", "measure_id": mid, "aggregation": "avg"})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert name in result.summary
        assert "Avg" in result.summary

    def test_summary_with_time_and_filter(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_month"},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["Germany"], "label": "Country"}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert "last month" in result.summary
        assert "Country = Germany" in result.summary

    def test_summary_filter_derives_operator_and_values_from_bare_label(self):
        """M-001 regression: frontend sets label to just the dimension name.
        The summary must still include operator and values."""
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "eq", "values": ["Germany"], "label": "Country"}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "Country = Germany" in result.summary
        assert result.summary_tokens["filter_dimensions"] == ["Country = Germany"]

    def test_summary_filter_with_no_label_derives_from_dimension_name(self):
        """Filter without label field still derives name from dimension_names map."""
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            filters=[{"dimension_id": did, "operator": "in", "values": ["DE", "FR", "UK"]}],
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "country is one of DE, FR, UK" in result.summary

    def test_summary_advanced_operators_humanized(self):
        """L-001: advanced operators (like, not_like, between)
        must display human-readable labels, not raw tokens.

        Bug-5924: top_n/bottom_n were removed from the public filter
        contract (FILTER_OPERATORS, BusinessFilterOp) — the compiler
        always rejected them at validation time ("requires measure-based
        ranking", never implemented) while this test asserted summary
        text as if they worked. Cases for those two operators are removed
        rather than left asserting behaviour the API no longer accepts.
        """
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        cases = [
            ("like", ["Acme"], "country contains Acme"),
            ("not_like", ["Test"], "country does not contain Test"),
            ("between", ["10", "20"], "country between 10, 20"),
        ]
        for op, vals, expected_fragment in cases:
            defn = _base_def(
                {"type": "single_measure", "measure_id": mid},
                filters=[{"dimension_id": did, "operator": op, "values": vals}],
            )
            result = compile_business_definition(defn, MEASURES, ALL_DIMS)
            assert expected_fragment in result.summary, (
                f"operator '{op}' should produce '{expected_fragment}', got: {result.summary}"
            )


# ---------------------------------------------------------------------------
# Compilation — target
# ---------------------------------------------------------------------------

class TestTarget:

    def test_static_target(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            target={"type": "static", "value": 42.0},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.target_type == "static"
        assert result.target_value == 42.0

    def test_measure_target(self):
        ids = list(MEASURES.keys())
        defn = _base_def(
            {"type": "single_measure", "measure_id": ids[0]},
            target={"type": "measure", "measure_id": ids[1]},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.target_type == "measure"
        assert 'measure("' in result.target_expression


# ---------------------------------------------------------------------------
# Compilation — all form families
# ---------------------------------------------------------------------------

class TestAllFormFamilies:

    def test_compare_measures(self):
        ids = list(MEASURES.keys())
        defn = _base_def({
            "type": "compare_measures",
            "measure_a_id": ids[0],
            "measure_b_id": ids[1],
            "mode": "percentage",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "safe_div(" in result.expression

    def test_target_comparison(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "target_comparison",
            "measure_id": mid,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert 'measure("' in result.expression

    def test_exception_sla(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "exception_sla",
            "measure_id": mid,
            "sla_type": "compliance_pct",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.expression

    def test_sla_compliance_pct_with_condition(self):
        mid = list(MEASURES.keys())[0]
        mname = MEASURES[mid]
        defn = _base_def({
            "type": "exception_sla",
            "measure_id": mid,
            "sla_type": "compliance_pct",
            "comparator": "<=",
            "threshold_value": 5,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "sla_condition(" in result.expression
        assert f'measure("{mname}")' in result.expression
        assert '"<="' in result.expression
        assert "literal(5)" in result.expression
        assert "safe_div(" in result.expression
        assert "count(literal(1))" in result.expression

    def test_sla_exception_count_with_condition(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "exception_sla",
            "measure_id": mid,
            "sla_type": "exception_count",
            "comparator": ">",
            "threshold_value": 100,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "sla_condition(" in result.expression
        assert "sum(" in result.expression
        assert "safe_div(" not in result.expression

    def test_sla_backlog(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "exception_sla",
            "measure_id": mid,
            "sla_type": "backlog",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.expression == 'count(literal(1))'

    def test_sla_aging_avg(self):
        mid = list(MEASURES.keys())[0]
        mname = MEASURES[mid]
        defn = _base_def({
            "type": "exception_sla",
            "measure_id": mid,
            "sla_type": "aging_avg",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.expression == f'avg(measure("{mname}"))'

    def test_sla_aging_max(self):
        mid = list(MEASURES.keys())[0]
        mname = MEASURES[mid]
        defn = _base_def({
            "type": "exception_sla",
            "measure_id": mid,
            "sla_type": "aging_max",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.expression == f'max(measure("{mname}"))'

    def test_share_rank(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        dim_name = ALL_DIMS[did]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "share_of_total",
            "by_dimension_id": did,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "share_of_total(" in result.expression
        # Bug-5425 (Option A): by_dimension flows as metadata, not in the
        # registry-valid 1-arg expression string.
        assert result.share_dimension == dim_name

    def test_rank_expression(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        dim_name = ALL_DIMS[did]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "rank",
            "by_dimension_id": did,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "rank_over(" in result.expression
        # Bug-5425 (Option A): by_dimension flows as metadata.
        assert result.share_dimension == dim_name

    def test_composite_score(self):
        ids = list(MEASURES.keys())
        defn = _base_def({
            "type": "composite_score",
            "components": [
                {"measure_id": ids[0], "weight": 0.6},
                {"measure_id": ids[1], "weight": 0.4},
            ],
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert "0.6" in result.expression
        assert "0.4" in result.expression


# ---------------------------------------------------------------------------
# CTE metadata population
# ---------------------------------------------------------------------------

class TestCteMetadata:

    def test_base_expression_set_without_ti(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({"type": "single_measure", "measure_id": mid})
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.base_expression is not None
        assert result.base_expression == result.expression
        assert result.ti_type is None

    def test_prior_period_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "prior_period", "grain": "month"},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "prior_period"
        assert result.ti_grain == "month"
        assert result.base_expression != result.expression
        assert "prior_period" not in result.base_expression

    def test_yoy_growth_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "yoy_growth_pct"},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "growth_pct"
        assert result.ti_grain == "year"

    def test_moving_average_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "moving_average", "grain": "month", "periods": 3},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "moving_avg"
        assert result.ti_grain == "month"
        assert result.ti_n_periods == 3

    def test_period_to_date_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "period_to_date", "period": "qtd"},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "period_to_date"
        assert result.ti_grain == "quarter"

    def test_time_window_bounds_preset(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_month"},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert result.time_window_start_sql is not None
        assert result.time_window_end_sql is not None
        assert "DATE_TRUNC" in result.time_window_start_sql

    def test_time_window_bounds_custom_range(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={
                "dimension_id": tdid,
                "preset": "custom_range",
                "start": "2025-01-01",
                "end": "2025-06-30",
            },
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert result.time_window_start_sql == "'2025-01-01'"
        assert "'2025-06-30'" in result.time_window_end_sql
        assert "INTERVAL '1 day'" in result.time_window_end_sql

    def test_trailing_sum_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "single_measure",
            "measure_id": mid,
            "time_calculation": {"type": "trailing_sum", "grain": "month", "periods": 6},
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "trailing_sum"
        assert result.ti_n_periods == 6

    def test_dedicated_moving_average_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        m_name = MEASURES[mid]
        defn = _base_def({
            "type": "moving_average",
            "measure_id": mid,
            "grain": "quarter",
            "window_size": 4,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "moving_avg"
        assert result.ti_grain == "quarter"
        assert result.ti_n_periods == 4
        assert result.base_expression == f'measure("{m_name}")'

    def test_dedicated_compare_periods_yoy_growth_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        m_name = MEASURES[mid]
        defn = _base_def({
            "type": "compare_periods",
            "measure_id": mid,
            "comparison": "yoy_growth_pct",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "growth_pct"
        assert result.ti_grain == "year"
        assert result.base_expression == f'measure("{m_name}")'

    def test_dedicated_compare_periods_mom_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "compare_periods",
            "measure_id": mid,
            "comparison": "mom",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "growth_pct"
        assert result.ti_grain == "month"

    def test_dedicated_compare_periods_qoq_ti_metadata(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "compare_periods",
            "measure_id": mid,
            "comparison": "qoq",
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.ti_type == "growth_pct"
        assert result.ti_grain == "quarter"

    def test_time_window_bounds_are_exclusive(self):
        mid = list(MEASURES.keys())[0]
        tdid = list(TIME_DIMS.keys())[0]
        defn = _base_def(
            {"type": "single_measure", "measure_id": mid},
            time_window={"dimension_id": tdid, "preset": "last_month"},
        )
        result = compile_business_definition(defn, MEASURES, ALL_DIMS, TIME_DIMS)
        assert result.time_window_end_sql is not None
        assert "- INTERVAL '1 day'" not in result.time_window_end_sql

    def test_share_rank_base_expression_is_raw_measure(self):
        mid = list(MEASURES.keys())[0]
        m_name = MEASURES[mid]
        did = list(DIMS.keys())[0]
        defn = _base_def({
            "type": "share_rank",
            "share_type": "share_of_total",
            "measure_id": mid,
            "aggregation": "sum",
            "by_dimension_id": did,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.base_expression == f'sum(measure("{m_name}"))'
        assert "share_of_total" not in result.base_expression


# ---------------------------------------------------------------------------
# Bug-5425: share_rank must honor by_dimension_id and n
# ---------------------------------------------------------------------------

class TestShareRankDimensionAndN:
    """Bug-5425: share_rank must honor ``by_dimension_id`` and ``n``.  Option A:
    they flow as structured metadata (share_type / share_dimension / share_n),
    consumed at execution by ``_build_share_sql`` in kpi_compiler.py.  The
    persisted ``expression`` string stays a registry-valid 1-arg analytics call
    so it passes ``validate_expression`` on KPI create/update — emitting 2-arg
    or ``top_n_contribution`` forms here would be rejected (HTTP 400)."""

    @staticmethod
    def _assert_expression_validates(expr: str) -> None:
        # The persisted expression must pass the same registry validation that
        # KPI create/update applies (kpis.py::_validate_kpi_expression).
        result = validate_expression(
            expr,
            model_measures=set(MEASURES.values()),
            model_kpis=set(),
            model_dimensions=set(ALL_DIMS.values()),
        )
        assert result.valid, [getattr(e, "message", str(e)) for e in result.errors]

    def test_share_of_total_metadata_and_valid_expression(self):
        mid = list(MEASURES.keys())[0]
        m_name = MEASURES[mid]
        did = list(DIMS.keys())[0]
        dim_name = ALL_DIMS[did]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "share_of_total",
            "by_dimension_id": did,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.expression == f'share_of_total(measure("{m_name}"))'
        self._assert_expression_validates(result.expression)
        # by_dimension honored via metadata, not the expression string.
        assert result.share_dimension == dim_name

    def test_top_n_contribution_metadata_and_valid_expression(self):
        mid = list(MEASURES.keys())[0]
        m_name = MEASURES[mid]
        did = list(DIMS.keys())[1]
        dim_name = ALL_DIMS[did]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "top_n_contribution",
            "by_dimension_id": did,
            "n": 5,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        # top_n_contribution is not a registered function; the display string
        # falls back to the valid share_of_total call, and n flows as metadata.
        assert result.expression == f'share_of_total(measure("{m_name}"))'
        self._assert_expression_validates(result.expression)
        assert result.share_dimension == dim_name
        assert result.share_n == 5

    def test_top_n_contribution_defaults_n_to_10(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "top_n_contribution",
            "by_dimension_id": did,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        # n is not encoded in the expression; it defaults to 10 in metadata.
        assert result.share_n == 10

    def test_rank_metadata_and_valid_expression(self):
        mid = list(MEASURES.keys())[0]
        m_name = MEASURES[mid]
        did = list(DIMS.keys())[2]
        dim_name = ALL_DIMS[did]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "rank",
            "by_dimension_id": did,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.expression == f'rank_over(measure("{m_name}"))'
        self._assert_expression_validates(result.expression)
        assert result.share_dimension == dim_name

    def test_share_rank_metadata_populated(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        dim_name = ALL_DIMS[did]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "top_n_contribution",
            "by_dimension_id": did,
            "n": 3,
        })
        result = compile_business_definition(defn, MEASURES, ALL_DIMS)
        assert result.share_type == "top_n_contribution"
        assert result.share_dimension == dim_name
        assert result.share_n == 3
        assert result.kpi_type == "share_rank"

    def test_validation_requires_by_dimension_id(self):
        mid = list(MEASURES.keys())[0]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "share_of_total",
        })
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
        )
        assert any("by_dimension_id" in e for e in errors)

    def test_validation_requires_positive_n_for_top_n(self):
        mid = list(MEASURES.keys())[0]
        did = list(DIMS.keys())[0]
        defn = _base_def({
            "type": "share_rank",
            "measure_id": mid,
            "share_type": "top_n_contribution",
            "by_dimension_id": did,
            "n": 0,
        })
        errors = validate_business_definition(
            defn, _id_set(MEASURES), _id_set(ALL_DIMS), _id_set(TIME_DIMS),
        )
        assert any("formula.n" in e for e in errors)
