"""KPI Business Builder — definition validation and scope compilation.

Converts a ``business_definition`` JSON (authored by the guided business
builder UI) into the technical artifacts the KPI evaluation pipeline needs:

- ``expression``: KPI DSL expression string
- ``filter_predicates``: list of SQL WHERE clause fragments (semantic names)
- ``time_window_predicates``: SQL WHERE clause fragments for the time window
- ``summary``: human-readable business sentence

All SQL fragments use PostgreSQL-style double-quoted semantic identifiers.
The query-router handles dialect translation downstream.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from shared.connector_qualify import safe_ident
from shared.type_family import is_numeric

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compiled output
# ---------------------------------------------------------------------------

@dataclass
class CompiledBusinessKpi:
    expression: str
    filter_predicates: list[str] = field(default_factory=list)
    time_window_predicates: list[str] = field(default_factory=list)
    summary: str = ""
    summary_tokens: dict = field(default_factory=dict)
    time_dimension_name: str | None = None
    kpi_type: str | None = None
    direction: str = "higher_is_better"
    target_expression: str | None = None
    target_value: float | None = None
    target_type: str | None = None
    # Base expression before time-intelligence wrapping (for CTE scalar SQL).
    base_expression: str | None = None
    # Time-intelligence metadata for CTE scalar query generation.
    ti_type: str | None = None
    ti_grain: str | None = None
    ti_n_periods: int | None = None
    # Structured time-window bounds (SQL expressions, not predicates).
    time_window_start_sql: str | None = None
    time_window_end_sql: str | None = None
    # Share/rank metadata for grouped scalar queries.
    share_type: str | None = None
    share_dimension: str | None = None
    share_n: int | None = None

    @property
    def where_clause(self) -> str | None:
        all_preds = self.filter_predicates + self.time_window_predicates
        if not all_preds:
            return None
        return " AND ".join(all_preds)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORMULA_TYPES = {
    "single_measure",
    "ratio",
    "count_records",
    "count_distinct",
    "moving_average",
    "compare_periods",
    "compare_measures",
    "target_comparison",
    "exception_sla",
    "share_rank",
    "composite_score",
}

TIME_CALCULATION_TYPES = {
    "current",
    "prior_period",
    "period_to_date",
    "trailing_sum",
    "moving_average",
    "lag",
    "lead",
    "percentage_change",
    "yoy_value",
    "yoy_growth_pct",
    "cagr",
}

TIME_WINDOW_PRESETS = {
    "today",
    "this_week",
    "last_week",
    "last_complete_week",
    "this_month",
    "last_month",
    "last_complete_month",
    "this_quarter",
    "last_quarter",
    "last_complete_quarter",
    "this_year",
    "last_year",
    "last_complete_year",
    "last_7_days",
    "last_14_days",
    "last_30_days",
    "last_60_days",
    "last_90_days",
    "last_3_months",
    "last_6_months",
    "last_12_months",
    "custom_range",
}

# Bug-5924: top_n/bottom_n were previously listed here and in the frontend
# BusinessFilterOp union, but the compiler always rejected them ("requires
# measure-based ranking" — never implemented end to end) and the UI never
# rendered them as an option. Removed from the public contract rather than
# left half-advertised; see docs/execution/execution_future-features.md if
# ranking filters are approved for a later phase.
FILTER_OPERATORS = {
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in",
    "between",
    "like", "not_like",
    "is_null", "is_not_null",
}

PERIOD_GRAINS = {"day", "week", "month", "quarter", "year"}

COMPARISON_TYPES = {
    "prior_period",
    "same_period_last_year",
    "yoy",
    "yoy_growth_pct",
    "mom",
    "qoq",
}

AGGREGATION_TYPES = {"sum", "avg", "min", "max", "count", "count_distinct"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_business_definition(
    defn: dict,
    measure_ids: set[str],
    dimension_ids: set[str],
    time_dimension_ids: set[str],
    parameter_names: set[str] | None = None,
) -> list[str]:
    """Validate a business_definition dict.

    Returns a list of error strings. Empty list means valid.
    """
    errors: list[str] = []

    if not isinstance(defn, dict):
        return ["business_definition must be a JSON object"]

    if defn.get("builder") != "business_kpi":
        errors.append("builder must be 'business_kpi'")

    version = defn.get("version")
    if version != 1:
        errors.append(f"unsupported version: {version}")

    formula = defn.get("formula")
    if not isinstance(formula, dict):
        errors.append("formula is required and must be an object")
        return errors

    formula_type = formula.get("type")
    if formula_type not in FORMULA_TYPES:
        errors.append(f"unknown formula type: {formula_type}")
        return errors

    errors.extend(_validate_formula(formula, formula_type, measure_ids, dimension_ids))

    time_calc = formula.get("time_calculation")
    if time_calc and isinstance(time_calc, dict):
        errors.extend(_validate_time_calculation(time_calc))

    time_window = defn.get("time_window")
    if time_window and isinstance(time_window, dict):
        errors.extend(_validate_time_window(time_window, dimension_ids, time_dimension_ids))

    filters = defn.get("filters")
    if filters:
        if not isinstance(filters, list):
            errors.append("filters must be an array")
        else:
            for i, f in enumerate(filters):
                errors.extend(_validate_filter(f, i, dimension_ids, time_dimension_ids, parameter_names))

    target = defn.get("target")
    if target and isinstance(target, dict):
        errors.extend(_validate_target(target, measure_ids))

    return errors


def _validate_formula(
    formula: dict,
    formula_type: str,
    measure_ids: set[str],
    dimension_ids: set[str],
) -> list[str]:
    errors: list[str] = []

    if formula_type == "single_measure":
        mid = formula.get("measure_id")
        if not mid:
            errors.append("formula.measure_id is required for single_measure")
        elif str(mid) not in measure_ids:
            errors.append(f"formula.measure_id {mid} does not belong to this model")
        agg = formula.get("aggregation")
        if agg and agg not in AGGREGATION_TYPES:
            errors.append(f"unknown aggregation: {agg}")

    elif formula_type == "ratio":
        for role in ("numerator_measure_id", "denominator_measure_id"):
            mid = formula.get(role)
            if not mid:
                errors.append(f"formula.{role} is required for ratio")
            elif str(mid) not in measure_ids:
                errors.append(f"formula.{role} {mid} does not belong to this model")

    elif formula_type == "count_records":
        pass

    elif formula_type == "count_distinct":
        did = formula.get("dimension_id")
        if not did:
            errors.append("formula.dimension_id is required for count_distinct")
        elif str(did) not in dimension_ids:
            errors.append(f"formula.dimension_id {did} does not belong to this model")

    elif formula_type == "moving_average":
        mid = formula.get("measure_id")
        if not mid:
            errors.append("formula.measure_id is required for moving_average")
        elif str(mid) not in measure_ids:
            errors.append(f"formula.measure_id {mid} does not belong to this model")
        window = formula.get("window_size")
        if not window or not isinstance(window, int) or window < 2:
            errors.append("formula.window_size must be an integer >= 2")
        grain = formula.get("grain")
        if grain and grain not in PERIOD_GRAINS:
            errors.append(f"unknown grain: {grain}")

    elif formula_type == "compare_periods":
        mid = formula.get("measure_id")
        if not mid:
            errors.append("formula.measure_id is required for compare_periods")
        elif str(mid) not in measure_ids:
            errors.append(f"formula.measure_id {mid} does not belong to this model")
        comp = formula.get("comparison")
        if comp and comp not in COMPARISON_TYPES:
            errors.append(f"unknown comparison type: {comp}")

    elif formula_type == "compare_measures":
        for role in ("measure_a_id", "measure_b_id"):
            mid = formula.get(role)
            if not mid:
                errors.append(f"formula.{role} is required for compare_measures")
            elif str(mid) not in measure_ids:
                errors.append(f"formula.{role} {mid} does not belong to this model")
        mode = formula.get("mode", "absolute")
        if mode not in ("absolute", "percentage"):
            errors.append(f"unknown compare_measures mode: {mode}")

    elif formula_type == "target_comparison":
        mid = formula.get("measure_id")
        if not mid:
            errors.append("formula.measure_id is required for target_comparison")
        elif str(mid) not in measure_ids:
            errors.append(f"formula.measure_id {mid} does not belong to this model")
        t_mid = formula.get("denominator_measure_id")
        if t_mid and str(t_mid) not in measure_ids:
            errors.append(f"formula.denominator_measure_id {t_mid} does not belong to this model")
        mode = formula.get("mode", "variance_pct")
        if mode not in ("variance_pct", "variance_abs", "attainment_pct"):
            errors.append(f"unknown target_comparison mode: {mode}")

    elif formula_type == "exception_sla":
        mid = formula.get("measure_id")
        did = formula.get("dimension_id")
        if not mid and not did:
            errors.append("formula.measure_id or formula.dimension_id is required for exception_sla")
        if mid and str(mid) not in measure_ids:
            errors.append(f"formula.measure_id {mid} does not belong to this model")
        if did and str(did) not in dimension_ids:
            errors.append(f"formula.dimension_id {did} does not belong to this model")
        sla_type = formula.get("sla_type", "compliance_pct")
        if sla_type not in ("compliance_pct", "exception_count", "backlog", "aging_avg", "aging_max", "aging_count"):
            errors.append(f"unknown sla_type: {sla_type}")
        comparator = formula.get("comparator")
        threshold_value = formula.get("threshold_value")
        if sla_type in ("compliance_pct", "exception_count"):
            if not comparator:
                errors.append(f"formula.comparator is required for sla_type '{sla_type}'")
            if threshold_value is None:
                errors.append(f"formula.threshold_value is required for sla_type '{sla_type}'")
        if comparator and comparator not in (">", ">=", "<", "<=", "=", "!="):
            errors.append(f"formula.comparator '{comparator}' is not valid (must be >, >=, <, <=, =, or !=)")

    elif formula_type == "share_rank":
        mid = formula.get("measure_id")
        if not mid:
            errors.append("formula.measure_id is required for share_rank")
        elif str(mid) not in measure_ids:
            errors.append(f"formula.measure_id {mid} does not belong to this model")
        share_type = formula.get("share_type", "share_of_total")
        if share_type not in ("share_of_total", "top_n_contribution", "rank"):
            errors.append(f"unknown share_type: {share_type}")
        by_dim = formula.get("by_dimension_id")
        if not by_dim:
            errors.append("formula.by_dimension_id is required for share_rank (scalar KPI needs a grouping dimension)")
        elif str(by_dim) not in dimension_ids:
            errors.append(f"formula.by_dimension_id {by_dim} does not belong to this model")
        if share_type == "top_n_contribution":
            n = formula.get("n")
            if not n or not isinstance(n, int) or n < 1:
                errors.append("formula.n must be a positive integer for top_n_contribution")

    elif formula_type == "composite_score":
        components = formula.get("components")
        if not components or not isinstance(components, list):
            errors.append("formula.components is required for composite_score")
        else:
            for i, comp in enumerate(components):
                if not isinstance(comp, dict):
                    errors.append(f"formula.components[{i}] must be an object")
                    continue
                cid = comp.get("measure_id") or comp.get("kpi_name")
                if not cid:
                    errors.append(f"formula.components[{i}] requires measure_id or kpi_name")

    return errors


def _validate_time_calculation(time_calc: dict) -> list[str]:
    errors: list[str] = []
    tc_type = time_calc.get("type")
    if tc_type not in TIME_CALCULATION_TYPES:
        errors.append(f"unknown time_calculation type: {tc_type}")
        return errors

    if tc_type in ("trailing_sum", "moving_average", "lag", "lead"):
        n = time_calc.get("periods")
        if not n or not isinstance(n, int) or n < 1:
            errors.append(f"time_calculation.periods must be a positive integer for {tc_type}")

    if tc_type == "period_to_date":
        ptd = time_calc.get("period")
        if ptd and ptd not in ("mtd", "qtd", "ytd", "wtd", "fiscal_ytd"):
            errors.append(f"unknown period_to_date period: {ptd}")

    grain = time_calc.get("grain")
    if grain and grain not in PERIOD_GRAINS:
        errors.append(f"unknown time_calculation grain: {grain}")

    return errors


def _validate_time_window(
    tw: dict,
    dimension_ids: set[str],
    time_dimension_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    did = tw.get("dimension_id")
    if did:
        sid = str(did)
        if sid not in dimension_ids:
            errors.append(f"time_window.dimension_id {did} does not belong to this model")
        elif sid not in time_dimension_ids:
            errors.append(f"time_window.dimension_id {did} is not a time/date dimension")

    preset = tw.get("preset")
    if preset and preset not in TIME_WINDOW_PRESETS:
        errors.append(f"unknown time_window preset: {preset}")

    if preset == "custom_range":
        start = tw.get("start")
        end = tw.get("end")
        if not start or not end:
            errors.append("custom_range requires start and end dates")
        else:
            # F-017-19: validate ISO date format at validation time so a value
            # is never interpolated raw into SQL. _compile_time_window also
            # escapes via _quote_val, but rejecting non-ISO here is the
            # authoritative guard and gives the user a clear error.
            for label, val in (("start", start), ("end", end)):
                if not _is_iso_date(val):
                    errors.append(
                        f"custom_range {label} must be an ISO date "
                        f"(YYYY-MM-DD), got: {val!r}"
                    )

    return errors


def _validate_filter(
    f: dict,
    idx: int,
    dimension_ids: set[str],
    time_dimension_ids: set[str],
    parameter_names: set[str] | None,
) -> list[str]:
    errors: list[str] = []
    prefix = f"filters[{idx}]"

    if not isinstance(f, dict):
        return [f"{prefix} must be an object"]

    did = f.get("dimension_id")
    if not did:
        errors.append(f"{prefix}.dimension_id is required")
    elif str(did) not in dimension_ids:
        errors.append(f"{prefix}.dimension_id {did} does not belong to this model")

    op = f.get("operator")
    if op not in FILTER_OPERATORS:
        errors.append(f"{prefix}.operator '{op}' is not supported")

    mode = f.get("mode", "fixed")
    if mode not in ("fixed", "relative", "parameter"):
        errors.append(f"{prefix}.mode '{mode}' is not supported")

    if mode == "parameter":
        param = f.get("parameter_name")
        if not param:
            errors.append(f"{prefix}: parameter mode requires parameter_name")
        elif parameter_names is not None and param not in parameter_names:
            errors.append(f"{prefix}: parameter '{param}' does not exist in this model")
        if f.get("default_value") is None:
            errors.append(f"{prefix}: parameter filters must have a default_value for scheduled snapshots")

    if mode == "relative":
        if did and str(did) not in time_dimension_ids:
            errors.append(f"{prefix}: relative mode is only valid for time dimensions")
        vals = f.get("values", [])
        if not vals or not isinstance(vals, list) or not vals[0]:
            errors.append(f"{prefix}: relative mode requires a preset in values[0]")
        elif vals[0] not in TIME_WINDOW_PRESETS:
            errors.append(f"{prefix}: unknown relative preset '{vals[0]}'")
    elif op in ("eq", "ne", "gt", "gte", "lt", "lte", "like", "not_like"):
        if mode != "parameter" and not f.get("values") and not f.get("value"):
            errors.append(f"{prefix}: operator '{op}' requires a value")
    elif op in ("in", "not_in"):
        if mode != "parameter":
            vals = f.get("values")
            if not vals or not isinstance(vals, list):
                errors.append(f"{prefix}: operator '{op}' requires a values array")
    elif op == "between":
        if mode != "parameter":
            vals = f.get("values")
            if not vals or not isinstance(vals, list) or len(vals) < 2:
                errors.append(f"{prefix}: operator 'between' requires values with 2 elements")

    return errors


def _validate_target(target: dict, measure_ids: set[str]) -> list[str]:
    errors: list[str] = []
    tt = target.get("type")
    if tt not in ("static", "measure", "expression"):
        errors.append(f"target.type '{tt}' is not supported")
    if tt == "static" and target.get("value") is None:
        errors.append("target.value is required for static target")
    if tt == "measure":
        mid = target.get("measure_id")
        if not mid:
            errors.append("target.measure_id is required for measure target")
        elif str(mid) not in measure_ids:
            errors.append(f"target.measure_id {mid} does not belong to this model")
    if tt == "expression" and not target.get("expression"):
        errors.append("target.expression is required for expression target")
    return errors


# ---------------------------------------------------------------------------
# Compilation — formula to expression
# ---------------------------------------------------------------------------

def compile_business_definition(
    defn: dict,
    measure_name_map: dict[str, str],
    dimension_name_map: dict[str, str],
    time_dim_name_map: dict[str, str] | None = None,
    parameter_defaults: dict[str, Any] | None = None,
    dimension_data_types: dict[str, str] | None = None,
) -> CompiledBusinessKpi:
    """Compile a validated business_definition into evaluation artifacts.

    Parameters
    ----------
    defn : dict
        The validated business_definition JSON.
    measure_name_map : dict
        Map of measure UUID string -> measure name.
    dimension_name_map : dict
        Map of dimension UUID string -> dimension name.
    time_dim_name_map : dict | None
        Map of time dimension UUID string -> column name.
    parameter_defaults : dict | None
        Map of parameter name -> default value.
    dimension_data_types : dict | None
        Map of dimension UUID string -> source column data type. Drives
        column-type-aware literal typing so numeric filters emit bare tokens
        (Bug-6253). When absent, every literal is quoted as a string.
    """
    formula = defn["formula"]
    formula_type = formula["type"]

    result = CompiledBusinessKpi(expression="", kpi_type=formula_type)

    base_expr = _compile_formula_expression(
        formula, formula_type, measure_name_map, dimension_name_map,
    )
    result.base_expression = base_expr
    result.expression = base_expr

    # Populate share/rank metadata — store raw measure as base_expression
    if formula_type == "share_rank":
        result.share_type = formula.get("share_type", "share_of_total")
        by_dim_id = formula.get("by_dimension_id")
        if by_dim_id:
            result.share_dimension = dimension_name_map.get(str(by_dim_id))
        result.share_n = formula.get("n", 10)
        mid = str(formula["measure_id"])
        m_name = measure_name_map.get(mid, mid)
        agg = formula.get("aggregation", "sum").lower()
        if agg not in ("sum", "avg", "min", "max", "count"):
            agg = "sum"
        result.base_expression = f'{agg}(measure("{m_name}"))'

    # Dedicated moving_average formula → populate TI metadata directly
    if formula_type == "moving_average":
        result.ti_type = "moving_avg"
        result.ti_grain = formula.get("grain", "month")
        result.ti_n_periods = formula.get("window_size", 3)
        mid = str(formula["measure_id"])
        m_name = measure_name_map.get(mid, mid)
        result.base_expression = f'measure("{m_name}")'

    # Dedicated compare_periods formula → populate TI metadata directly
    if formula_type == "compare_periods":
        comp = formula.get("comparison", "prior_period")
        comp_map: dict[str, tuple[str, str]] = {
            "prior_period": ("prior_period", "month"),
            "mom": ("growth_pct", "month"),
            "qoq": ("growth_pct", "quarter"),
            "yoy": ("prior_period", "year"),
            "same_period_last_year": ("prior_period", "year"),
            "yoy_growth_pct": ("growth_pct", "year"),
        }
        ti_type, ti_grain = comp_map.get(comp, ("prior_period", "month"))
        result.ti_type = ti_type
        result.ti_grain = ti_grain
        mid = str(formula["measure_id"])
        m_name = measure_name_map.get(mid, mid)
        result.base_expression = f'measure("{m_name}")'

    time_calc = formula.get("time_calculation")
    if time_calc and isinstance(time_calc, dict) and time_calc.get("type") != "current":
        result.expression = _wrap_time_calculation(base_expr, time_calc, formula, measure_name_map)
        _populate_ti_metadata(result, time_calc, formula)

    time_window = defn.get("time_window")
    if time_window and isinstance(time_window, dict):
        tw = _compile_time_window(time_window, dimension_name_map, time_dim_name_map)
        result.time_window_predicates = tw.predicates
        result.time_dimension_name = tw.col_name
        result.time_window_start_sql = tw.start_sql
        result.time_window_end_sql = tw.end_sql

    filters = defn.get("filters")
    if filters and isinstance(filters, list):
        result.filter_predicates = _compile_filters(
            filters, dimension_name_map, parameter_defaults,
            dimension_data_types,
        )

    target = defn.get("target")
    if target and isinstance(target, dict):
        t_type = target.get("type")
        result.target_type = t_type
        if t_type == "static":
            result.target_value = float(target["value"])
        elif t_type == "measure":
            t_mid = str(target["measure_id"])
            t_name = measure_name_map.get(t_mid, t_mid)
            result.target_expression = f'measure("{t_name}")'
        elif t_type == "expression":
            result.target_expression = target["expression"]

    direction = defn.get("direction", "higher_is_better")
    if direction in ("higher_is_better", "lower_is_better", "closer_is_better"):
        result.direction = direction

    result.summary, result.summary_tokens = _build_summary(defn, measure_name_map, dimension_name_map)

    return result


def _wrap_time_calc(expr: str, tc: dict) -> str:
    """Wrap an expression with a per-component time intelligence function."""
    tc_type = tc.get("type", "current")
    if tc_type == "current":
        return expr
    grain = tc.get("grain", "month")
    periods = tc.get("periods", 1)
    if tc_type == "prior_period":
        return f'prior_period({expr}, "{grain}")'
    if tc_type == "period_to_date":
        ptd = tc.get("period", "ytd")
        grain_map = {"ytd": "year", "qtd": "quarter", "mtd": "month", "wtd": "week"}
        return f'period_to_date({expr}, "{grain_map.get(ptd, "year")}")'
    if tc_type == "trailing_sum":
        return f'trailing_sum({expr}, "{grain}", literal({periods}))'
    if tc_type == "moving_average":
        return f'moving_avg({expr}, "{grain}", literal({periods}))'
    if tc_type == "lag":
        return f'lag({expr}, "{grain}", literal({periods}))'
    if tc_type == "lead":
        return f'lead({expr}, "{grain}", literal({periods}))'
    if tc_type == "percentage_change":
        prev = f'prior_period({expr}, "{grain}")'
        return f'safe_div({expr} - {prev}, abs({prev}))'
    if tc_type in ("yoy_value", "yoy_growth_pct"):
        prev = f'prior_period({expr}, "year")'
        if tc_type == "yoy_growth_pct":
            return f'safe_div({expr} - {prev}, abs({prev}))'
        return prev
    return expr


def _compile_formula_expression(
    formula: dict,
    formula_type: str,
    measure_names: dict[str, str],
    dimension_names: dict[str, str],
) -> str:
    """Build the KPI DSL expression from a formula dict."""

    def _mref(mid_key: str) -> str:
        mid = str(formula[mid_key])
        name = measure_names.get(mid, mid)
        return f'measure("{name}")'

    def _agg_mref(mid_key: str) -> str:
        mid = str(formula[mid_key])
        name = measure_names.get(mid, mid)
        agg = formula.get("aggregation", "sum").lower()
        if agg == "count_distinct":
            return f'count_distinct(measure("{name}"))'
        if agg in ("sum", "avg", "min", "max", "count"):
            return f'{agg}(measure("{name}"))'
        return f'measure("{name}")'

    if formula_type == "single_measure":
        return _agg_mref("measure_id")

    if formula_type == "ratio":
        num = _mref("numerator_measure_id")
        den = _mref("denominator_measure_id")
        num_tc = formula.get("numerator_time_calculation")
        den_tc = formula.get("denominator_time_calculation")
        if num_tc and isinstance(num_tc, dict) and num_tc.get("type") not in (None, "current"):
            num = _wrap_time_calc(num, num_tc)
        if den_tc and isinstance(den_tc, dict) and den_tc.get("type") not in (None, "current"):
            den = _wrap_time_calc(den, den_tc)
        return f"safe_div({num}, {den})"

    if formula_type == "count_records":
        return 'count(literal(1))'

    if formula_type == "count_distinct":
        did = str(formula["dimension_id"])
        name = dimension_names.get(did, did)
        return f'count_distinct(dimension("{name}"))'

    if formula_type == "moving_average":
        mid = str(formula["measure_id"])
        name = measure_names.get(mid, mid)
        grain = formula.get("grain", "month")
        window = formula.get("window_size", 3)
        return f'moving_avg(measure("{name}"), "{grain}", literal({window}))'

    if formula_type == "compare_periods":
        base = _mref("measure_id")
        comp = formula.get("comparison", "prior_period")
        if comp == "yoy_growth_pct":
            prev = f'prior_period({base}, "year")'
            return f'safe_div({base} - {prev}, abs({prev}))'
        if comp in ("yoy", "same_period_last_year"):
            return f'prior_period({base}, "year")'
        if comp == "mom":
            return f'prior_period({base}, "month")'
        if comp == "qoq":
            return f'prior_period({base}, "quarter")'
        return f'prior_period({base}, "month")'

    if formula_type == "compare_measures":
        a = _mref("measure_a_id")
        b = _mref("measure_b_id")
        mode = formula.get("mode", "absolute")
        if mode == "percentage":
            return f"safe_div({a} - {b}, abs({b}))"
        return f"({a} - {b})"

    if formula_type == "target_comparison":
        actual = _mref("measure_id")
        target_mid = formula.get("denominator_measure_id")
        mode = formula.get("mode", "variance_pct")
        if target_mid:
            target = f'measure("{measure_names.get(str(target_mid), str(target_mid))}")'
            if mode == "variance_abs":
                return f"({actual} - {target})"
            if mode == "attainment_pct":
                return f"safe_div({actual}, {target})"
            return f"safe_div({actual} - {target}, abs({target}))"
        return _agg_mref("measure_id")

    if formula_type == "exception_sla":
        sla_type = formula.get("sla_type", "compliance_pct")
        comparator = formula.get("comparator")
        threshold_value = formula.get("threshold_value")
        has_condition = comparator and threshold_value is not None
        if sla_type == "backlog":
            return 'count(literal(1))'
        if sla_type in ("aging_avg", "aging_max", "aging_count"):
            mid = formula.get("measure_id")
            if mid:
                name = measure_names.get(str(mid), str(mid))
                if sla_type == "aging_avg":
                    return f'avg(measure("{name}"))'
                if sla_type == "aging_max":
                    return f'max(measure("{name}"))'
                return f'count(measure("{name}"))'
        if has_condition and formula.get("measure_id"):
            mref = _mref("measure_id")
            case_expr = f'sla_condition({mref}, "{comparator}", literal({threshold_value}), literal(1), literal(0))'
            if sla_type == "exception_count":
                return f'sum({case_expr})'
            return f'safe_div(sum({case_expr}), count(literal(1)))'
        if sla_type == "exception_count":
            return 'count(literal(1))'
        if formula.get("measure_id"):
            total = _mref("measure_id")
            return f"safe_div({total}, count(literal(1)))"
        return 'safe_div(count(literal(1)), count(literal(1)))'

    if formula_type == "share_rank":
        share_type = formula.get("share_type", "share_of_total")
        mid = str(formula["measure_id"])
        name = measure_names.get(mid, mid)
        # Bug-5425 (Option A): emit a registry-valid 1-arg analytics expression.
        # by_dimension and n are NOT encoded in this display/validation string —
        # they flow as structured metadata (share_type / share_dimension /
        # share_n on CompiledBusinessKpi) and are applied at execution by
        # _build_share_sql in kpi_compiler.py, which builds the grouped CTE that
        # partitions by the dimension and limits to N rows. Emitting 2-arg or
        # top_n_contribution forms here would fail FUNCTION_REGISTRY validation
        # on KPI create/update: share_of_total / rank_over are arity 1, and
        # top_n_contribution is not a registered function.
        if share_type == "rank":
            return f'rank_over(measure("{name}"))'
        return f'share_of_total(measure("{name}"))'

    if formula_type == "composite_score":
        components = formula.get("components", [])
        parts = []
        for comp in components:
            w = comp.get("weight", 1.0)
            if comp.get("measure_id"):
                mid = str(comp["measure_id"])
                name = measure_names.get(mid, mid)
                parts.append(f'(literal({w}) * measure("{name}"))')
            elif comp.get("kpi_name"):
                parts.append(f'(literal({w}) * kpi("{comp["kpi_name"]}"))')
        if not parts:
            return "literal(0)"
        return " + ".join(parts)

    return 'literal(0)'


def _wrap_time_calculation(
    base_expr: str,
    time_calc: dict,
    formula: dict,
    measure_names: dict[str, str],
) -> str:
    """Wrap a base expression with a time intelligence function."""
    tc_type = time_calc["type"]
    grain = time_calc.get("grain", "month")
    periods = time_calc.get("periods", 1)

    mid = formula.get("measure_id")
    m_name = measure_names.get(str(mid), str(mid)) if mid else None

    if tc_type == "prior_period":
        if m_name:
            return f'prior_period(measure("{m_name}"), "{grain}")'
        return base_expr

    if tc_type == "period_to_date":
        ptd = time_calc.get("period", "ytd")
        grain_map = {"ytd": "year", "qtd": "quarter", "mtd": "month", "wtd": "week"}
        ptd_grain = grain_map.get(ptd, "year")
        if m_name:
            return f'period_to_date(measure("{m_name}"), "{ptd_grain}")'
        return base_expr

    if tc_type == "trailing_sum":
        if m_name:
            return f'trailing_sum(measure("{m_name}"), "{grain}", literal({periods}))'
        return base_expr

    if tc_type == "moving_average":
        if m_name:
            return f'moving_avg(measure("{m_name}"), "{grain}", literal({periods}))'
        return base_expr

    if tc_type == "lag":
        if m_name:
            return f'lag(measure("{m_name}"), "{grain}", literal({periods}))'
        return base_expr

    if tc_type == "lead":
        if m_name:
            return f'lead(measure("{m_name}"), "{grain}", literal({periods}))'
        return base_expr

    if tc_type == "percentage_change":
        if m_name:
            cur = f'measure("{m_name}")'
            prev = f'prior_period(measure("{m_name}"), "{grain}")'
            return f'safe_div({cur} - {prev}, abs({prev}))'
        return base_expr

    if tc_type == "yoy_value":
        if m_name:
            return f'prior_period(measure("{m_name}"), "year")'
        return base_expr

    if tc_type == "yoy_growth_pct":
        if m_name:
            cur = f'measure("{m_name}")'
            prev = f'prior_period(measure("{m_name}"), "year")'
            return f'safe_div({cur} - {prev}, abs({prev}))'
        return base_expr

    return base_expr


def _populate_ti_metadata(
    result: CompiledBusinessKpi,
    time_calc: dict,
    formula: dict,
) -> None:
    """Set ti_type, ti_grain, ti_n_periods on the compiled result."""
    tc_type = time_calc["type"]
    grain = time_calc.get("grain", "month")
    periods = time_calc.get("periods", 1)

    ti_type_map: dict[str, str] = {
        "prior_period": "prior_period",
        "yoy_value": "prior_period",
        "lag": "prior_period",
        "lead": "lead",
        "period_to_date": "period_to_date",
        "trailing_sum": "trailing_sum",
        "moving_average": "moving_avg",
        "percentage_change": "growth_pct",
        "yoy_growth_pct": "growth_pct",
        "cagr": "cagr",
    }

    result.ti_type = ti_type_map.get(tc_type)
    if not result.ti_type:
        return

    if tc_type == "yoy_value":
        result.ti_grain = "year"
    elif tc_type == "yoy_growth_pct":
        result.ti_grain = "year"
    elif tc_type == "period_to_date":
        ptd = time_calc.get("period", "ytd")
        grain_map = {"ytd": "year", "qtd": "quarter", "mtd": "month", "wtd": "week"}
        result.ti_grain = grain_map.get(ptd, "year")
    else:
        result.ti_grain = grain

    if tc_type in ("trailing_sum", "moving_average", "lag", "lead", "cagr"):
        result.ti_n_periods = int(periods)

    # For moving_average formula type (not time_calculation), pull from formula
    if formula.get("type") == "moving_average" and not result.ti_n_periods:
        result.ti_n_periods = formula.get("window_size", 3)
        result.ti_grain = formula.get("grain", "month")
        result.ti_type = "moving_avg"


# ---------------------------------------------------------------------------
# Compilation — time window to WHERE predicates
# ---------------------------------------------------------------------------

_PRESET_SQL: dict[str, str] = {}


def _tw_col(col: str) -> str:
    return safe_ident(col)


def _preset_to_predicates(preset: str, col: str) -> list[str]:
    c = _tw_col(col)
    if preset == "today":
        return [f"{c} = CURRENT_DATE"]
    if preset == "this_week":
        return [f"{c} >= DATE_TRUNC('week', CURRENT_DATE)", f"{c} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '7 days'"]
    if preset == "last_week":
        return [f"{c} >= DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '7 days'", f"{c} < DATE_TRUNC('week', CURRENT_DATE)"]
    if preset == "last_complete_week":
        return [f"{c} >= DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '7 days'", f"{c} < DATE_TRUNC('week', CURRENT_DATE)"]
    if preset == "this_month":
        return [f"{c} >= DATE_TRUNC('month', CURRENT_DATE)", f"{c} < DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '1 month'"]
    if preset == "last_month":
        return [f"{c} >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')", f"{c} < DATE_TRUNC('month', CURRENT_DATE)"]
    if preset == "last_complete_month":
        return [f"{c} >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')", f"{c} < DATE_TRUNC('month', CURRENT_DATE)"]
    if preset == "this_quarter":
        return [f"{c} >= DATE_TRUNC('quarter', CURRENT_DATE)", f"{c} < DATE_TRUNC('quarter', CURRENT_DATE) + INTERVAL '3 months'"]
    if preset == "last_quarter":
        return [f"{c} >= DATE_TRUNC('quarter', CURRENT_DATE - INTERVAL '3 months')", f"{c} < DATE_TRUNC('quarter', CURRENT_DATE)"]
    if preset == "last_complete_quarter":
        return [f"{c} >= DATE_TRUNC('quarter', CURRENT_DATE - INTERVAL '3 months')", f"{c} < DATE_TRUNC('quarter', CURRENT_DATE)"]
    if preset == "this_year":
        return [f"{c} >= DATE_TRUNC('year', CURRENT_DATE)", f"{c} < DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '1 year'"]
    if preset == "last_year":
        return [f"{c} >= DATE_TRUNC('year', CURRENT_DATE - INTERVAL '1 year')", f"{c} < DATE_TRUNC('year', CURRENT_DATE)"]
    if preset == "last_complete_year":
        return [f"{c} >= DATE_TRUNC('year', CURRENT_DATE - INTERVAL '1 year')", f"{c} < DATE_TRUNC('year', CURRENT_DATE)"]
    if preset == "last_7_days":
        return [f"{c} >= CURRENT_DATE - INTERVAL '7 days'"]
    if preset == "last_14_days":
        return [f"{c} >= CURRENT_DATE - INTERVAL '14 days'"]
    if preset == "last_30_days":
        return [f"{c} >= CURRENT_DATE - INTERVAL '30 days'"]
    if preset == "last_60_days":
        return [f"{c} >= CURRENT_DATE - INTERVAL '60 days'"]
    if preset == "last_90_days":
        return [f"{c} >= CURRENT_DATE - INTERVAL '90 days'"]
    if preset == "last_3_months":
        return [f"{c} >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '3 months')", f"{c} < DATE_TRUNC('month', CURRENT_DATE)"]
    if preset == "last_6_months":
        return [f"{c} >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '6 months')", f"{c} < DATE_TRUNC('month', CURRENT_DATE)"]
    if preset == "last_12_months":
        return [f"{c} >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '12 months')", f"{c} < DATE_TRUNC('month', CURRENT_DATE)"]
    return []


@dataclass
class _TimeWindowBounds:
    predicates: list[str]
    col_name: str | None
    start_sql: str | None = None
    end_sql: str | None = None


def _preset_to_bounds(preset: str) -> tuple[str | None, str | None]:
    """Return (start_sql, end_sql_exclusive) SQL expressions for a preset.

    end_sql is an EXCLUSIVE upper bound — CTE templates use ``< end_sql``
    so that timestamp columns are not truncated on the last day.
    """
    bounds: dict[str, tuple[str, str]] = {
        "today": ("CURRENT_DATE", "CURRENT_DATE + INTERVAL '1 day'"),
        "this_week": ("DATE_TRUNC('week', CURRENT_DATE)", "DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '7 days'"),
        "last_week": ("DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '7 days'", "DATE_TRUNC('week', CURRENT_DATE)"),
        "last_complete_week": ("DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '7 days'", "DATE_TRUNC('week', CURRENT_DATE)"),
        "this_month": ("DATE_TRUNC('month', CURRENT_DATE)", "DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '1 month'"),
        "last_month": ("DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')", "DATE_TRUNC('month', CURRENT_DATE)"),
        "last_complete_month": ("DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')", "DATE_TRUNC('month', CURRENT_DATE)"),
        "this_quarter": ("DATE_TRUNC('quarter', CURRENT_DATE)", "DATE_TRUNC('quarter', CURRENT_DATE) + INTERVAL '3 months'"),
        "last_quarter": ("DATE_TRUNC('quarter', CURRENT_DATE - INTERVAL '3 months')", "DATE_TRUNC('quarter', CURRENT_DATE)"),
        "last_complete_quarter": ("DATE_TRUNC('quarter', CURRENT_DATE - INTERVAL '3 months')", "DATE_TRUNC('quarter', CURRENT_DATE)"),
        "this_year": ("DATE_TRUNC('year', CURRENT_DATE)", "DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '1 year'"),
        "last_year": ("DATE_TRUNC('year', CURRENT_DATE - INTERVAL '1 year')", "DATE_TRUNC('year', CURRENT_DATE)"),
        "last_complete_year": ("DATE_TRUNC('year', CURRENT_DATE - INTERVAL '1 year')", "DATE_TRUNC('year', CURRENT_DATE)"),
        "last_7_days": ("CURRENT_DATE - INTERVAL '7 days'", "CURRENT_DATE + INTERVAL '1 day'"),
        "last_14_days": ("CURRENT_DATE - INTERVAL '14 days'", "CURRENT_DATE + INTERVAL '1 day'"),
        "last_30_days": ("CURRENT_DATE - INTERVAL '30 days'", "CURRENT_DATE + INTERVAL '1 day'"),
        "last_60_days": ("CURRENT_DATE - INTERVAL '60 days'", "CURRENT_DATE + INTERVAL '1 day'"),
        "last_90_days": ("CURRENT_DATE - INTERVAL '90 days'", "CURRENT_DATE + INTERVAL '1 day'"),
        "last_3_months": ("DATE_TRUNC('month', CURRENT_DATE - INTERVAL '3 months')", "DATE_TRUNC('month', CURRENT_DATE)"),
        "last_6_months": ("DATE_TRUNC('month', CURRENT_DATE - INTERVAL '6 months')", "DATE_TRUNC('month', CURRENT_DATE)"),
        "last_12_months": ("DATE_TRUNC('month', CURRENT_DATE - INTERVAL '12 months')", "DATE_TRUNC('month', CURRENT_DATE)"),
    }
    pair = bounds.get(preset)
    if pair:
        return pair
    return None, None


def _compile_time_window(
    tw: dict,
    dimension_names: dict[str, str],
    time_dim_names: dict[str, str] | None,
) -> _TimeWindowBounds:
    """Return predicates, dimension name, and structured start/end bounds."""
    did = tw.get("dimension_id")
    col_name: str | None = None
    if did:
        sid = str(did)
        col_name = (time_dim_names or {}).get(sid) or dimension_names.get(sid)

    if not col_name:
        return _TimeWindowBounds([], None)

    preset = tw.get("preset")
    include_incomplete = tw.get("include_incomplete_period", True)
    if preset and preset != "custom_range":
        preds = _preset_to_predicates(preset, col_name)
        if not include_incomplete and preds:
            c = _tw_col(col_name)
            preds.append(f"{c} < CURRENT_DATE")
        start_sql, end_sql = _preset_to_bounds(preset)
        return _TimeWindowBounds(preds, col_name, start_sql, end_sql)

    if preset == "custom_range":
        preds = []
        start = tw.get("start")
        end = tw.get("end")
        c = _tw_col(col_name)
        # F-017-19: escape the user-supplied bounds via _quote_val (validation
        # already rejects non-ISO dates; this is defence in depth so a stray
        # quote can never break out of the literal).
        start_sql = _quote_val(start) if start else None
        # Exclusive upper bound for CTE templates: add 1 day so < works for both DATE and TIMESTAMP
        end_sql = f"{_quote_val(end)}::date + INTERVAL '1 day'" if end else None
        if start:
            preds.append(f"{c} >= {_quote_val(start)}")
        if end:
            preds.append(f"{c} <= {_quote_val(end)}")
        return _TimeWindowBounds(preds, col_name, start_sql, end_sql)

    return _TimeWindowBounds([], col_name)


# ---------------------------------------------------------------------------
# Compilation — filters to WHERE predicates
# ---------------------------------------------------------------------------

def _quote_val(v: Any) -> str:
    s = str(v).replace("'", "''")
    return f"'{s}'"


# Bug-6253: a finite, *safe* integer/decimal literal — optional leading MINUS,
# ASCII digits, optional single ``.`` fraction. Mirrors the query-router
# ``_NUMERIC_LITERAL_RE`` (rewrite/conditions.py): NO exponent, NO leading
# ``+``, NO surrounding whitespace, ``\Z`` end-anchor, ``re.ASCII`` so
# non-ASCII digits fail closed rather than emitting an unparseable bare token.
_NUMERIC_LITERAL_RE = re.compile(r"^-?(\d+(\.\d+)?|\.\d+)\Z", re.ASCII)


def _numeric_render(v: Any) -> str | None:
    """Render *v* to the bare SQL token it would emit, or ``None`` if unsafe.

    Every candidate — native int/float or string — is reduced to a string and
    validated through the SAME strict grammar (``_NUMERIC_LITERAL_RE``) before
    it may be emitted bare. This mirrors the query-router and closes three
    edge cases a bare ``math.isfinite`` check would miss:
      - a very large int (``10**400``) whose ``float()`` conversion would raise
        ``OverflowError`` (a 500) — ``str()`` never overflows and the digit
        string still matches the integer grammar;
      - a float in scientific form (``1e-07`` -> ``'1e-07'``) — rejected by the
        exponent-free grammar, so it falls back to a quoted literal (fail-safe);
      - ``inf`` / ``nan`` floats — rejected.
    Booleans are rejected outright (``True`` must never render as ``1``).
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        rendered = str(v)
    elif isinstance(v, int):
        rendered = str(v)
    elif isinstance(v, str):
        rendered = v
    else:
        return None
    return rendered if _NUMERIC_LITERAL_RE.match(rendered) else None


def _quote_val_typed(v: Any, col_type: str | None) -> str:
    """Render a filter literal with column-type-aware typing.

    Against a numeric column a strict numeric literal is emitted bare so
    strictly-typed connectors (BigQuery, Snowflake, SQL Server) accept the
    comparison instead of rejecting ``col = '100'``. Every other case — a
    non-numeric column, or a value that is not a strict numeric literal —
    falls back to a quoted string literal (fail-safe: a stray value can
    never break out of the literal).
    """
    if is_numeric(col_type):
        rendered = _numeric_render(v)
        if rendered is not None:
            return rendered
    return _quote_val(v)


def _quote_like_val(v: Any) -> str:
    """Quote *v* as a business "contains" LIKE pattern.

    Bug-5925: the Business Builder summary describes ``like``/``not_like``
    as "contains" / "does not contain", but the compiler previously passed
    the user's literal straight through as the LIKE pattern with no
    wildcards — an exact match, not a contains match, unless the user
    happened to type ``%`` themselves. This wraps the (escaped) literal in
    ``%...%`` so the compiled SQL matches the advertised business
    semantics.

    Wildcard escaping uses a bare backslash (no ``ESCAPE`` clause) rather
    than ANSI ``LIKE 'pattern' ESCAPE '\\'``: this compiler builds a raw
    SQL string fragment ahead of the sqlglot-aware dialect pipeline (it is
    not an AST the query-router can re-transpile per connector — see the
    "SQL generation" rule against hand-rolled per-connector branching), and
    BigQuery's LIKE operator does not support the ``ESCAPE`` clause at all
    (it would be a hard syntax error there), while backslash is already the
    *implicit* default escape character on postgresql/redshift/bigquery/
    hadoop_spark. Snowflake and sqlserver do not default to backslash
    escaping, so a literal ``%``/``_`` inside the search text on those two
    connectors is not escaped — a narrow, documented edge case (Bug-6017).
    Because those two dialects have no default escape character, the
    literal backslash we emit is NOT stripped/interpreted — it stays in
    the pattern as a literal character the search text does not contain,
    so the search SILENTLY MATCHES NOTHING for any value containing
    a percent sign, underscore, or backslash (an under-match /
    false-empty-result, not an over-match). The primary defect this fixes
    — "contains" not matching
    a superstring at all — is fixed on every connector; only search text
    with those specific characters, on those two connectors, is affected.
    """
    s = str(v).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    s = s.replace("'", "''")
    return f"'%{s}%'"


def _is_iso_date(v: Any) -> bool:
    """True if *v* is an ISO date or datetime string (F-017-19).

    Accepts ``YYYY-MM-DD`` and ISO datetimes; rejects anything else so a
    custom_range bound can never break out of the SQL literal.
    """
    if not isinstance(v, str):
        return False
    from datetime import date, datetime

    s = v.strip()
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        pass
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def _compile_filters(
    filters: list[dict],
    dimension_names: dict[str, str],
    parameter_defaults: dict[str, Any] | None,
    dimension_data_types: dict[str, str] | None = None,
    *,
    strict: bool = False,
) -> list[str]:
    predicates: list[str] = []

    for f in filters:
        did = f.get("dimension_id")
        if not did:
            if strict:
                raise ValueError("filter is missing dimension_id")
            continue
        dim_name = dimension_names.get(str(did))
        if not dim_name:
            if strict:
                raise ValueError(f"unknown dimension_id {did}")
            continue

        col = safe_ident(dim_name)
        col_type = (dimension_data_types or {}).get(str(did))
        op = f.get("operator", "eq")
        mode = f.get("mode", "fixed")
        if mode not in {"fixed", "parameter", "relative"}:
            if strict:
                raise ValueError(f"unsupported filter mode {mode!r}")
            mode = "fixed"

        if mode == "parameter":
            param_name = f.get("parameter_name", "")
            default = f.get("default_value")
            val = default
            if parameter_defaults and param_name in parameter_defaults:
                val = parameter_defaults[param_name]
            if val is None:
                if strict:
                    raise ValueError(
                        f"parameter filter {param_name or '<unnamed>'} has no value"
                    )
                continue
            values = [val] if not isinstance(val, list) else val
        elif mode == "relative":
            raw_val = f.get("value")
            preset_values = f.get("values", [raw_val] if raw_val is not None else [])
            preset = preset_values[0] if preset_values else None
            if preset:
                rel_preds = _preset_to_predicates(preset, dim_name)
                if strict and not rel_preds:
                    raise ValueError(f"unknown relative preset {preset}")
                predicates.extend(rel_preds)
            elif strict:
                raise ValueError("relative filter is missing a preset")
            continue
        else:
            raw_val = f.get("value")
            values = f.get("values", [raw_val] if raw_val is not None else [])

        pred = _op_to_sql(col, op, values, f, col_type)
        if pred:
            predicates.append(pred)
        elif strict:
            raise ValueError(f"operator {op!r} cannot be compiled")

    if strict and filters and not predicates:
        raise ValueError("request filters produced no predicates")
    return predicates


def _op_to_sql(
    col: str, op: str, values: list, f: dict, col_type: str | None = None
) -> str | None:
    def q(v: Any) -> str:
        return _quote_val_typed(v, col_type)

    if op == "eq":
        if not values:
            return None
        return f"{col} = {q(values[0])}"
    if op == "ne":
        if not values:
            return None
        return f"{col} <> {q(values[0])}"
    if op == "gt":
        if not values:
            return None
        return f"{col} > {q(values[0])}"
    if op == "gte":
        if not values:
            return None
        return f"{col} >= {q(values[0])}"
    if op == "lt":
        if not values:
            return None
        return f"{col} < {q(values[0])}"
    if op == "lte":
        if not values:
            return None
        return f"{col} <= {q(values[0])}"
    if op == "in":
        if not values:
            return None
        return f"{col} IN ({', '.join(q(v) for v in values)})"
    if op == "not_in":
        if not values:
            return None
        return f"{col} NOT IN ({', '.join(q(v) for v in values)})"
    if op == "between":
        if len(values) < 2:
            return None
        return f"{col} BETWEEN {q(values[0])} AND {q(values[1])}"
    if op == "like":
        if not values:
            return None
        return f"{col} LIKE {_quote_like_val(values[0])}"
    if op == "not_like":
        if not values:
            return None
        return f"{col} NOT LIKE {_quote_like_val(values[0])}"
    if op == "is_null":
        return f"{col} IS NULL"
    if op == "is_not_null":
        return f"{col} IS NOT NULL"
    return None


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

# Every ``summary_tokens`` key whose STRING value is a bare measure NAME.
#
# THE producer/consumer contract for measure renames (Bug-9483). ``measure_rename``
# imports this set to decide which summary strings to rewrite; a private copy
# there drifted the moment ``compare_measures`` was added, leaving a shipped,
# wizard-reachable formula family whose summary still named a measure that no
# longer exists. One owner, exported — the Bug-6574 pattern.
#
# Deliberately EXCLUDED:
#   * ``dimension_name`` — a DIMENSION name; a measure rename must not touch it.
#   * ``filter_dimensions`` — rendered filter LABELS ("region in EMEA, APAC"),
#     not bare names; rewriting inside them would corrupt member values.
# Both exclusions are asserted by the contract test, so widening this set to
# "every *_name key" cannot happen by accident.
MEASURE_NAME_SUMMARY_TOKEN_KEYS = frozenset({
    "measure_name",
    "numerator_name",
    "denominator_name",
    "measure_a_name",
    "measure_b_name",
})


def _build_summary(
    defn: dict,
    measure_names: dict[str, str],
    dimension_names: dict[str, str],
) -> tuple[str, dict]:
    parts: list[str] = []
    formula = defn.get("formula", {})
    formula_type = formula.get("type", "")
    tokens: dict = {"formula_type": formula_type}

    if formula_type == "single_measure":
        mid = str(formula.get("measure_id", ""))
        name = measure_names.get(mid, mid)
        agg = formula.get("aggregation", "sum")
        tokens["aggregation"] = agg
        tokens["measure_name"] = name
        parts.append(f"{agg.title()} of {name}")
    elif formula_type == "ratio":
        n_id = str(formula.get("numerator_measure_id", ""))
        d_id = str(formula.get("denominator_measure_id", ""))
        n_name = measure_names.get(n_id, n_id)
        d_name = measure_names.get(d_id, d_id)
        tokens["numerator_name"] = n_name
        tokens["denominator_name"] = d_name
        parts.append(f"{n_name} / {d_name}")
    elif formula_type == "count_records":
        parts.append("Count of records")
    elif formula_type == "count_distinct":
        did = str(formula.get("dimension_id", ""))
        name = dimension_names.get(did, did)
        tokens["dimension_name"] = name
        parts.append(f"Distinct count of {name}")
    elif formula_type == "moving_average":
        mid = str(formula.get("measure_id", ""))
        name = measure_names.get(mid, mid)
        window = formula.get("window_size", 3)
        grain = formula.get("grain", "month")
        tokens["measure_name"] = name
        tokens["window_size"] = window
        tokens["grain"] = grain
        parts.append(f"{window}-{grain} moving average of {name}")
    elif formula_type == "compare_periods":
        mid = str(formula.get("measure_id", ""))
        name = measure_names.get(mid, mid)
        comp = formula.get("comparison", "prior_period")
        tokens["measure_name"] = name
        tokens["comparison"] = comp
        labels = {
            "prior_period": "vs prior period",
            "same_period_last_year": "vs same period last year",
            "yoy": "year over year",
            "yoy_growth_pct": "YoY growth %",
            "mom": "month over month",
            "qoq": "quarter over quarter",
        }
        parts.append(f"{name} {labels.get(comp, comp)}")
    elif formula_type == "compare_measures":
        a_id = str(formula.get("measure_a_id", ""))
        b_id = str(formula.get("measure_b_id", ""))
        a_name = measure_names.get(a_id, a_id)
        b_name = measure_names.get(b_id, b_id)
        mode = formula.get("mode", "absolute")
        tokens["measure_a_name"] = a_name
        tokens["measure_b_name"] = b_name
        tokens["mode"] = mode
        if mode == "percentage":
            parts.append(f"({a_name} - {b_name}) / {b_name}")
        else:
            parts.append(f"{a_name} - {b_name}")
    elif formula_type == "target_comparison":
        mid = str(formula.get("measure_id", ""))
        name = measure_names.get(mid, mid)
        tokens["measure_name"] = name
        parts.append(f"{name} vs target")
    elif formula_type == "exception_sla":
        sla_type = formula.get("sla_type", "compliance_pct")
        tokens["sla_type"] = sla_type
        parts.append(f"SLA ({sla_type})")
    elif formula_type == "share_rank":
        mid = str(formula.get("measure_id", ""))
        name = measure_names.get(mid, mid)
        share_type = formula.get("share_type", "share_of_total")
        tokens["measure_name"] = name
        tokens["share_type"] = share_type
        parts.append(f"{name} ({share_type})")
    elif formula_type == "composite_score":
        parts.append("Composite score")

    time_calc = formula.get("time_calculation")
    if time_calc and isinstance(time_calc, dict) and time_calc.get("type") != "current":
        tc_type = time_calc["type"]
        tokens["time_calc_type"] = tc_type
        tokens["time_calc_grain"] = time_calc.get("grain", "month")
        tokens["time_calc_periods"] = time_calc.get("periods", 1)
        tc_labels = {
            "prior_period": "prior period",
            "period_to_date": time_calc.get("period", "YTD").upper(),
            "trailing_sum": f"trailing {time_calc.get('periods', '')} {time_calc.get('grain', 'month')}s",
            "moving_average": f"{time_calc.get('periods', '')}-{time_calc.get('grain', 'month')} moving avg",
            "lag": f"lag {time_calc.get('periods', 1)}",
            "lead": f"lead {time_calc.get('periods', 1)}",
            "percentage_change": "% change",
            "yoy_value": "YoY",
            "yoy_growth_pct": "YoY growth %",
        }
        parts.append(tc_labels.get(tc_type, tc_type))

    tw = defn.get("time_window")
    if tw and isinstance(tw, dict):
        preset = tw.get("preset")
        if preset:
            tokens["time_window_preset"] = preset
            label = preset.replace("_", " ")
            parts.append(f"for {label}")

    _OP_DISPLAY = {
        "eq": "=",
        "ne": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "in": "is one of",
        "not_in": "is not one of",
        "is_null": "is blank",
        "is_not_null": "is not blank",
        "between": "between",
        "like": "contains",
        "not_like": "does not contain",
    }

    filters = defn.get("filters")
    if filters and isinstance(filters, list):
        filter_labels = []
        labels = []
        for f in filters:
            did = str(f.get("dimension_id", ""))
            name = f.get("label") or dimension_names.get(did, did)
            op = f.get("operator", "eq")
            op_display = _OP_DISPLAY.get(op, op)
            vals = f.get("values", [])
            if op in ("is_null", "is_not_null"):
                labels.append(f"{name} {op_display}")
            elif vals:
                labels.append(f"{name} {op_display} {', '.join(str(v) for v in vals[:3])}")
            else:
                labels.append(name)
            filter_labels.append(
                labels[-1] if vals or op in ("is_null", "is_not_null") else name,
            )
        if labels:
            parts.append(f"where {'; '.join(labels)}")
        if filter_labels:
            tokens["filter_dimensions"] = filter_labels

    return " | ".join(parts) if parts else "", tokens
