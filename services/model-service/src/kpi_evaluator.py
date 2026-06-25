"""KPI evaluation pipeline (v2).

Implements the 14-step evaluation pipeline from Section 7.1 of the KPI
requirements specification. Each step is a small function; the pipeline
orchestrator calls them in sequence.

This module evaluates a single KPI. Batch evaluation, ad-hoc evaluation,
and trend-series generation live in the endpoint layer (kpis.py) which
calls this pipeline per-KPI.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from shared.semantic.kpi_expression import (
    ASTNode,
    FunctionCall,
    StringLiteral,
    parse_kpi_expression,
)

from src.kpi_formatter import (
    format_value,
    format_variance,
    value_str_if_needed,
)
from src.kpi_threshold import (
    ThresholdResult,
    evaluate_threshold,
)
from src.kpi_trend import (
    TrendResult,
    evaluate_trend,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    1: "#388E3C",   # green
    0: "#F57C00",   # amber
    -1: "#D32F2F",  # red
}


@dataclass
class EvaluationContext:
    """Input context for a single KPI evaluation."""
    kpi_id: UUID
    kpi_name: str
    expression: Optional[str] = None
    kpi_type: Optional[str] = None

    # Target configuration
    target_type: Optional[str] = None
    target_value: Optional[float] = None
    target_measure_id: Optional[UUID] = None
    target_expression: Optional[str] = None

    # Direction and thresholds
    direction: str = "higher_is_better"
    presentation_type: Optional[str] = None
    presentation_meta: Optional[dict] = None

    # Aggregation
    calc_agg_mode: str = "automatic"

    # Trend
    trend_period: str = "month"
    trend_threshold: float = 0.01

    # Formatting
    format_token: Optional[str] = None
    format_custom: Optional[str] = None
    unit_label: Optional[str] = None
    null_display_value: str = "N/A"



@dataclass
class MeasureValueProvider:
    """Callback interface for resolving measure values.

    The evaluator does not execute SQL directly. Instead, it calls the
    provider to get measure values. This decouples the evaluator from
    the query-router transport layer.
    """
    get_measure_value: object = None  # async callable: (measure_name: str) -> float | None
    get_kpi_value: object = None      # async callable: (kpi_name: str) -> float | None
    # async callable: (node: FunctionCall) -> float | None — resolves a
    # time-intelligence subtree via decomposed query-router queries. When
    # absent, TI subtrees evaluate to None (no silently-wrong values).
    evaluate_time_intelligence: object = None


@dataclass
class EvaluationResult:
    """Full result of a KPI evaluation."""
    kpi_id: UUID
    value: Optional[float] = None
    value_str: Optional[str] = None
    target: Optional[float] = None
    status: Optional[int] = None
    status_label: Optional[str] = None
    status_color: Optional[str] = None
    trend: Optional[int] = None
    trend_label: Optional[str] = None
    trend_pct: Optional[float] = None
    formatted_value: Optional[str] = None
    formatted_target: Optional[str] = None
    formatted_variance: Optional[str] = None
    evaluation_ms: Optional[int] = None
    error: Optional[str] = None
    # Legacy — retained for API response backward compatibility
    goal: Optional[float] = None
    formatted_goal: Optional[str] = None


# ---------------------------------------------------------------------------
# Expression value resolution
# ---------------------------------------------------------------------------

async def _resolve_expression_value(
    expression: str,
    provider: MeasureValueProvider,
) -> Optional[float]:
    """Resolve a KPI expression to a scalar value.

    Walks the AST and evaluates measure() and kpi() references via the
    provider, then computes arithmetic in Python. This is the model-service
    side evaluation for simple cases. Complex expressions with time
    intelligence or aggregation modes are deferred to the query-router
    (Phase 3).
    """
    try:
        ast = parse_kpi_expression(expression)
    except Exception:
        return None

    return await _evaluate_ast(ast, provider)


async def _evaluate_ast(
    node: ASTNode,
    provider: MeasureValueProvider,
) -> Optional[float]:
    """Recursively evaluate an AST node to a float value."""
    from shared.semantic.kpi_expression import (
        BinaryOp,
        NumberLiteral,
        UnaryMinus,
    )

    if isinstance(node, NumberLiteral):
        return node.value

    if isinstance(node, FunctionCall):
        return await _evaluate_function(node, provider)

    if isinstance(node, BinaryOp):
        left = await _evaluate_ast(node.left, provider)
        right = await _evaluate_ast(node.right, provider)
        if left is None or right is None:
            return None
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        return None  # division blocked at parse time

    if isinstance(node, UnaryMinus):
        operand = await _evaluate_ast(node.operand, provider)
        return -operand if operand is not None else None

    return None


async def _evaluate_function(
    node: FunctionCall,
    provider: MeasureValueProvider,
) -> Optional[float]:
    """Evaluate a function call AST node."""
    fn_name = node.name

    # Reference functions
    if fn_name == "measure":
        if node.args and isinstance(node.args[0], StringLiteral):
            name = node.args[0].value
            if provider.get_measure_value:
                return await provider.get_measure_value(name)
        return None

    if fn_name == "kpi":
        if node.args and isinstance(node.args[0], StringLiteral):
            name = node.args[0].value
            if provider.get_kpi_value:
                return await provider.get_kpi_value(name)
        return None

    if fn_name == "literal":
        if node.args:
            return await _evaluate_ast(node.args[0], provider)
        return None

    # Safe division functions
    if fn_name in ("safe_div", "safe_ratio"):
        if len(node.args) >= 2:
            num = await _evaluate_ast(node.args[0], provider)
            den = await _evaluate_ast(node.args[1], provider)
            if num is None or den is None or den == 0:
                return None
            return num / den
        return None

    if fn_name == "div":
        if len(node.args) >= 3:
            num = await _evaluate_ast(node.args[0], provider)
            den = await _evaluate_ast(node.args[1], provider)
            fallback = await _evaluate_ast(node.args[2], provider)
            if num is None or den is None or den == 0:
                return fallback
            return num / den
        return None

    # Conditional functions
    if fn_name == "coalesce":
        for arg in node.args:
            val = await _evaluate_ast(arg, provider)
            if val is not None:
                return val
        return None

    if fn_name == "if_then_else":
        if len(node.args) >= 3:
            condition = await _evaluate_ast(node.args[0], provider)
            then_val = await _evaluate_ast(node.args[1], provider)
            else_val = await _evaluate_ast(node.args[2], provider)
            if condition is None:
                return None
            return then_val if condition != 0 else else_val
        return None

    # Arithmetic functions
    if fn_name == "abs":
        if node.args:
            val = await _evaluate_ast(node.args[0], provider)
            return abs(val) if val is not None else None
        return None

    if fn_name == "round":
        if len(node.args) >= 2:
            val = await _evaluate_ast(node.args[0], provider)
            decimals = await _evaluate_ast(node.args[1], provider)
            if val is None or decimals is None:
                return None
            return round(val, int(decimals))
        return None

    if fn_name == "min_of":
        if len(node.args) >= 2:
            a = await _evaluate_ast(node.args[0], provider)
            b = await _evaluate_ast(node.args[1], provider)
            if a is None or b is None:
                return None
            return min(a, b)
        return None

    if fn_name == "max_of":
        if len(node.args) >= 2:
            a = await _evaluate_ast(node.args[0], provider)
            b = await _evaluate_ast(node.args[1], provider)
            if a is None or b is None:
                return None
            return max(a, b)
        return None

    # Time intelligence functions — resolved through the provider's TI hook,
    # which decomposes the subtree into simple query-router queries (the
    # same machinery the SQL/business-builder paths use). Without a hook,
    # returning the inner expression's current value would be silently wrong
    # (e.g. prior_period would return current, not prior), so the value is
    # None and a warning is logged.
    if fn_name in (
        "prior_period", "period_to_date", "moving_avg", "trailing_sum",
        "lag", "lead", "cagr", "pct_change", "fiscal_period_to_date",
    ):
        if provider.evaluate_time_intelligence:
            return await provider.evaluate_time_intelligence(node)
        log.warning(
            "Time intelligence function '%s' cannot be evaluated in Python "
            "fallback path — returning None. Use the SQL compiler path for "
            "accurate time-shifted values.",
            fn_name,
        )
        return None

    return None


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

async def _resolve_target(
    ctx: EvaluationContext,
    provider: MeasureValueProvider,
) -> Optional[float]:
    """Resolve the target value based on target_type."""
    if ctx.target_type == "static":
        return ctx.target_value

    if ctx.target_type == "measure" and ctx.target_measure_id:
        # The target_measure_id lookup is handled by the endpoint layer,
        # which passes the measure name through the provider. For now,
        # return target_value if already resolved.
        return ctx.target_value

    if ctx.target_type == "expression" and ctx.target_expression:
        return await _resolve_expression_value(ctx.target_expression, provider)

    if ctx.target_type == "prior_period":
        # Deferred to Phase 3 (time intelligence via query-router)
        return None

    return ctx.target_value


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def _sanitize_float(value: Optional[float]) -> Optional[float]:
    """Convert Infinity/NaN to None."""
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def evaluate_kpi(
    ctx: EvaluationContext,
    provider: MeasureValueProvider,
    prior_value: Optional[float] = None,
) -> EvaluationResult:
    """Execute the 14-step evaluation pipeline for a single KPI.

    Parameters
    ----------
    ctx : EvaluationContext
        The KPI configuration and metadata.
    provider : MeasureValueProvider
        Callbacks for resolving measure and KPI values.
    prior_value : float | None
        The prior period value for trend evaluation. If None, trend
        will be "Insufficient Data".

    Returns
    -------
    EvaluationResult
    """
    start_ms = time.monotonic_ns()

    # --- Step 1-2: Parse + resolve expression value ---
    value: Optional[float] = None

    if ctx.expression:
        value = await _resolve_expression_value(ctx.expression, provider)
    else:
        # No expression configured — cannot evaluate
        return EvaluationResult(
            kpi_id=ctx.kpi_id,
            error="No expression configured for this KPI",
        )

    value = _sanitize_float(value)

    # --- Steps 3-9: Agg mode, time context, semi-additive, RLS,
    # compile, route — handled by the query-router (Phase 3).
    # Value is already resolved and sanitized above.

    # --- Step 11: Evaluate threshold ---
    target = await _resolve_target(ctx, provider)
    target = _sanitize_float(target)

    threshold_result: Optional[ThresholdResult] = None
    status: Optional[int] = None
    status_label: Optional[str] = None
    status_color: Optional[str] = None

    if ctx.presentation_meta and ctx.presentation_meta.get("bands"):
        # v2 threshold evaluation with custom bands
        threshold_result = evaluate_threshold(
            value=value,
            target=target,
            direction=ctx.direction,
            evaluation_type=ctx.presentation_meta.get("evaluation_type", "percentage_of_target"),
            bands=ctx.presentation_meta.get("bands"),
        )
        status = threshold_result.status
        status_label = threshold_result.status_label
        status_color = threshold_result.status_color
    elif value is not None and target is not None:
        # Default threshold evaluation
        threshold_result = evaluate_threshold(
            value=value,
            target=target,
            direction=ctx.direction,
        )
        status = threshold_result.status
        status_label = threshold_result.status_label
        status_color = threshold_result.status_color

    # --- Step 12: Evaluate trend ---
    trend_result: Optional[TrendResult] = None
    trend_int: Optional[int] = None
    trend_label_str: Optional[str] = None
    trend_pct: Optional[float] = None

    trend_result = evaluate_trend(
        current_value=value,
        prior_value=prior_value,
        threshold=ctx.trend_threshold,
        direction=ctx.direction,
        target=target,
    )
    trend_int = trend_result.trend
    trend_label_str = trend_result.trend_label
    trend_pct = trend_result.trend_pct

    # --- Step 13: Format output ---
    formatted = format_value(
        value,
        format_token=ctx.format_token,
        format_custom=ctx.format_custom,
        null_display_value=ctx.null_display_value,
        unit_label=ctx.unit_label,
    )
    formatted_target_obj = format_value(
        target,
        format_token=ctx.format_token,
        null_display_value=ctx.null_display_value,
    )
    abs_variance_str, _pct_variance_str = format_variance(
        value, target,
        format_token=ctx.format_token,
        direction=ctx.direction,
    )

    elapsed_ms = int((time.monotonic_ns() - start_ms) / 1_000_000)

    # --- Step 14: Compose response ---
    return EvaluationResult(
        kpi_id=ctx.kpi_id,
        value=value,
        value_str=value_str_if_needed(value),
        target=target,
        status=status,
        status_label=status_label,
        status_color=status_color,
        trend=trend_int,
        trend_label=trend_label_str,
        trend_pct=trend_pct,
        formatted_value=formatted.display,
        formatted_target=formatted_target_obj.display,
        formatted_variance=abs_variance_str,
        evaluation_ms=elapsed_ms,
        # Legacy
        goal=target,
        formatted_goal=formatted_target_obj.display,
    )
