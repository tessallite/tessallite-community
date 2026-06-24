"""KPI expression-to-SQL compiler (Phase 3 + Phase 13.b time intelligence).

Compiles a KPI expression AST into a SQL query that the query-router can
execute in a single round-trip.  This replaces the Python-side AST-walking
evaluator for expressions that reference measures, producing semantically
correct SQL that honours aggregation modes, safe division, and NULL handling.

The generated SQL uses semantic names (measure names as column identifiers)
and the model slug as the FROM table.  The query-router's binder and rewriter
resolve these to physical column expressions and aggregate/source routing.

Time intelligence functions are compiled using the VariantBinding
infrastructure from ``shared/semantic/time_variants_sql.py``.  When a time
function wraps a ``kpi()`` reference (which cannot be compiled to SQL),
the compiler returns ``_COMPILER_UNSUPPORTED`` and the evaluator falls back
to the Python-side evaluation path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import sqlglot

from shared.semantic.kpi_expression import (
    ASTNode,
    BinaryOp,
    FunctionCall,
    NumberLiteral,
    StringLiteral,
    UnaryMinus,
    parse_kpi_expression,
)
from shared.semantic.time_variants_sql import (
    VariantBinding,
    emit_variant_expression,
    VariantSqlError,
    rewrite_ignore_nulls_for_postgresql,
)

# Sentinel indicating the expression cannot be compiled to SQL and must
# fall back to the Python evaluator (e.g., kpi() refs inside time functions).
_COMPILER_UNSUPPORTED = object()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompiledQuery:
    """Result of compiling a KPI expression to SQL."""
    sql: str                         # the full SELECT ... FROM ... query
    select_expr: str                 # just the SELECT expression (no FROM)
    measure_names: list[str]         # measures referenced (for dependency tracking)
    kpi_names: list[str]             # KPIs referenced (for dependency tracking)
    has_time_intelligence: bool      # True if expression uses time intelligence functions
    agg_mode: Optional[str]          # detected aggregation mode
    # F-017-22: share_of_total()/rank_over() used outside the grouped share CTE
    # path produce a single-row degenerate window (always 1 / rank 1). The
    # caller treats this as unsupported and fails the value closed.
    has_ungrouped_window: bool = False


@dataclass(frozen=True)
class CompilerContext:
    """Configuration for the compiler."""
    model_slug: str = "Model"
    calc_agg_mode: str = "automatic"
    default_agg: str = "sum"
    # Measure name -> default aggregation function
    measure_aggs: Optional[dict[str, str]] = None
    # Time intelligence context
    time_column: Optional[str] = None
    calendar_type: Optional[str] = None
    fiscal_year_start_month: Optional[int] = None
    # Semi-additive
    at_grain: Optional[str] = None
    non_additive_agg: Optional[str] = None
    carry_forward: bool = False
    # Aggregate-of-aggregate
    inner_agg: Optional[str] = None
    inner_grain: Optional[str] = None
    outer_agg: Optional[str] = None
    # Dialect (canonical is postgresql; transpile for other targets).
    # When SQL goes through the query-router, leave as "postgresql" --
    # the router handles dialect translation downstream.  Set explicitly
    # only for direct-execution or export paths (e.g. XMLA KPI export).
    dialect: str = "postgresql"
    # Business-definition WHERE clause (filter + time window predicates).
    # Inserted into the base FROM scope before grouping/subquery wrapping.
    where_clause: Optional[str] = None
    # Separated filter/time predicates for time-intelligence subquery path.
    # When enable_ti_subquery is True and the expression has time intelligence:
    #   inner query uses filter_where_clause (dimension filters)
    #   outer query uses time_where_clause (time window predicates)
    # Non-TI expressions fall back to combining both into a single WHERE.
    filter_where_clause: Optional[str] = None
    time_where_clause: Optional[str] = None
    enable_ti_subquery: bool = False
    # CTE-based scalar KPI fields (business builder).
    # When set, compile_expression generates CTE scalar SQL instead of
    # the window-function subquery path.
    ti_type: Optional[str] = None
    ti_grain: Optional[str] = None
    ti_n_periods: Optional[int] = None
    base_expression: Optional[str] = None
    time_window_start_sql: Optional[str] = None
    time_window_end_sql: Optional[str] = None
    # Share/rank metadata for grouped scalar queries.
    share_type: Optional[str] = None
    share_dimension: Optional[str] = None
    share_n: Optional[int] = None
    # Individual filter predicates (pre-split) for SQL-safe share/rank
    # dimension separation. Avoids re-splitting assembled WHERE on " AND "
    # which corrupts BETWEEN predicates.
    filter_predicate_list: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

from shared.connector_qualify import safe_ident as _safe_ident


def _inject_where(sql: str, where_clause: str) -> str:
    """Insert a WHERE clause into the innermost FROM scope of a subquery SQL."""
    upper = sql.upper()
    group_idx = upper.find(" GROUP BY")
    if group_idx != -1:
        return sql[:group_idx] + " WHERE " + where_clause + sql[group_idx:]
    order_idx = upper.find(" ORDER BY")
    if order_idx != -1:
        return sql[:order_idx] + " WHERE " + where_clause + sql[order_idx:]
    return sql + " WHERE " + where_clause


# Grain keywords that must be rendered as DATE_TRUNC('<grain>', <col>) rather
# than treated as a literal column name (F-017-21). Anything else is taken as a
# real column name.
_GRAIN_KEYWORDS = frozenset({"day", "week", "month", "quarter", "year"})


def _wrap_agg(agg: str, expr: str) -> str:
    """Wrap an expression in an aggregation function.

    ``median`` is canonicalised to PostgreSQL's ordered-set form
    ``PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY <expr>)`` — PostgreSQL has no
    ``MEDIAN`` function, so the prior ``MEDIAN(...)`` emission failed at SQL
    (F-017-21). sqlglot transpiles PERCENTILE_CONT to each target dialect.
    """
    agg_upper = agg.upper()
    if agg_upper == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {expr})"
    if agg_upper == "MEDIAN":
        return f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {expr})"
    return f"{agg_upper}({expr})"


# ---------------------------------------------------------------------------
# Dialect transpilation
# ---------------------------------------------------------------------------

_SQLGLOT_DIALECT = {
    "postgresql": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "redshift": "redshift",
    "sqlserver": "tsql",
}


def _transpile_to_dialect(sql: str, dialect: str) -> str:
    """Transpile ANSI canonical SQL to the target dialect.

    The compiler always emits ANSI-standard SQL. Translation pipeline:
    1. Pre-sqlglot compatibility pass: for PostgreSQL < 16, rewrite
       ``IGNORE NULLS`` window functions to ``ARRAY_AGG ... FILTER`` form
       before sqlglot processes the SQL (sqlglot would otherwise strip
       ``IGNORE NULLS`` without providing a functional equivalent).
    2. sqlglot transpilation for all dialects (identifier quoting, function
       name mapping, syntax normalization).

    Raises ValueError if transpilation produces empty output (parse failure).
    """
    target = _SQLGLOT_DIALECT.get(dialect)
    if target is None:
        return sql

    # Pre-sqlglot compatibility: PG < 16 IGNORE NULLS -> ARRAY_AGG FILTER.
    # Must run before sqlglot because sqlglot strips IGNORE NULLS for PG
    # without providing the ARRAY_AGG workaround.
    prepared = sql
    if dialect == "postgresql":
        prepared = rewrite_ignore_nulls_for_postgresql(prepared)

    results = sqlglot.transpile(prepared, read="postgres", write=target)
    if not results:
        raise ValueError(f"sqlglot produced empty output for dialect '{dialect}'")
    return results[0]


# ---------------------------------------------------------------------------
# Semi-additive SQL builders
# ---------------------------------------------------------------------------

def _semi_additive_expr(non_additive_agg: str, col_expr: str, time_col: str) -> str:
    """Build a semi-additive aggregation expression.

    For min/max/sum, uses standard aggregate functions (portable).
    For first/last, returns None — handled by _build_semi_additive_sql
    using ORDER BY + LIMIT 1 (portable across all dialects via sqlglot).
    """
    agg = non_additive_agg.lower()
    if agg == "min":
        return f"MIN({col_expr})"
    if agg == "max":
        return f"MAX({col_expr})"
    # first/last handled by _build_semi_additive_sql directly
    # Fallback: SUM
    return f"SUM({col_expr})"


def _build_semi_additive_sql(
    select_expr: str, ctx: CompilerContext,
) -> str:
    """Wrap a compiled expression in a semi-additive subquery.

    The inner query groups by ``at_grain`` (a time grain column) and
    computes the base expression per grain bucket.  The outer query
    picks the correct value (last, first, min, max) across grain buckets.

    For first/last, uses ORDER BY + LIMIT 1 which is portable across
    all dialects (sqlglot transpiles LIMIT to TOP/FETCH as needed).
    """
    time_col = _safe_ident(ctx.time_column or "date")
    grain_col = _safe_ident(ctx.at_grain)
    agg = (ctx.non_additive_agg or "last").lower()

    inner_sql = (
        f"SELECT {grain_col}, {select_expr} AS inner_val, {time_col} "
        f"FROM {_safe_ident(ctx.model_slug)} "
        f"GROUP BY {grain_col}, {time_col}"
    )

    if agg in ("first", "last"):
        order_dir = "DESC" if agg == "last" else "ASC"
        return (
            f"SELECT inner_val AS value "
            f"FROM ({inner_sql}) sub "
            f"WHERE inner_val IS NOT NULL "
            f"ORDER BY {time_col} {order_dir} "
            f"LIMIT 1"
        )

    # min/max/sum use standard aggregate functions
    agg_expr = _semi_additive_expr(agg, "inner_val", time_col)
    return (
        f"SELECT {agg_expr} AS value "
        f"FROM ({inner_sql}) sub"
    )


def _build_carry_forward_expr(select_expr: str, ctx: CompilerContext) -> str:
    """Wrap a compiled expression with carry-forward NULL fill.

    Always emits the ANSI-standard ``LAST_VALUE(expr IGNORE NULLS)`` form.
    Dialect-specific translation (e.g. PostgreSQL ``ARRAY_AGG`` pattern) is
    handled by ``_transpile_to_dialect`` after compilation, keeping the
    compiler itself dialect-agnostic.
    """
    time_col = _safe_ident(ctx.time_column or "date")

    # Canonical ANSI form — sqlglot handles dialect conversion downstream.
    # PostgreSQL-specific rewriting is handled in _transpile_to_dialect.
    lag_fill = (
        f"LAST_VALUE({select_expr} IGNORE NULLS)"
        f" OVER (ORDER BY {time_col}"
        f" ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)"
    )

    return f"COALESCE({select_expr}, {lag_fill})"


# ---------------------------------------------------------------------------
# Aggregate-of-aggregate SQL builder
# ---------------------------------------------------------------------------

def _build_ti_subquery(select_expr: str, ctx: CompilerContext) -> str:
    """Legacy window-function subquery — kept for non-business-builder TI paths.

    Inner query groups by the time column so window functions (LAG, SUM
    OVER, etc.) operate on multiple rows instead of a single aggregate.
    Filter predicates (dimension constraints) go on the inner query.
    Time-window predicates go on the outer query so historical rows
    needed by window functions are not removed.
    """
    time_col = _safe_ident(ctx.time_column)
    model = _safe_ident(ctx.model_slug)

    inner_where = ""
    if ctx.filter_where_clause:
        inner_where = f" WHERE {ctx.filter_where_clause}"

    inner_sql = (
        f"SELECT {time_col}, {select_expr} AS value "
        f"FROM {model}{inner_where} "
        f"GROUP BY {time_col}"
    )

    outer_where = ""
    if ctx.time_where_clause:
        outer_where = f" WHERE {ctx.time_where_clause}"

    return (
        f"SELECT value FROM ({inner_sql}) _ti"
        f"{outer_where} ORDER BY {time_col} DESC LIMIT 1"
    )


# ---------------------------------------------------------------------------
# CTE-based scalar KPI SQL
# ---------------------------------------------------------------------------

_INTERVAL_MAP = {
    "day": "1 day",
    "week": "7 days",
    "month": "1 month",
    "quarter": "3 months",
    "year": "1 year",
}

# n-period interval composition: PostgreSQL has no 'quarter' interval unit
# and 'n week' is normalised to days for uniformity. Each grain maps to
# (unit, multiplier) so "2 quarters" becomes "6 months".
_INTERVAL_UNIT = {
    "day": ("day", 1),
    "week": ("day", 7),
    "month": ("month", 1),
    "quarter": ("month", 3),
    "year": ("year", 1),
}


def interval_literal(n: int, grain: str) -> str:
    """Compose a PostgreSQL-valid interval body for *n* periods of *grain*.

    ``interval_literal(2, "quarter")`` -> ``"6 months"`` (PostgreSQL has no
    quarter interval unit; ``INTERVAL '2 quarter'`` is a syntax error).
    """
    unit, mult = _INTERVAL_UNIT.get(grain, ("month", 1))
    total = n * mult
    return f"{total} {unit}" if total == 1 else f"{total} {unit}s"


def compile_scalar_kpi_sql(
    base_select_expr: str,
    ctx: CompilerContext,
    *,
    ti_type: str | None = None,
    ti_grain: str | None = None,
    ti_n_periods: int | None = None,
    time_window_start_sql: str | None = None,
    time_window_end_sql: str | None = None,
) -> str | None:
    """Generate CTE-based scalar SQL for a business-builder KPI.

    Returns the full SQL string, or None if the KPI shape doesn't need
    special CTE treatment (simple aggregation without time intelligence).
    """
    if not ti_type or not ctx.time_column:
        return None

    time_col = _safe_ident(ctx.time_column)
    model = _safe_ident(ctx.model_slug)

    filter_where = ""
    if ctx.filter_where_clause:
        filter_where = f" AND {ctx.filter_where_clause}"

    grain = ti_grain or "month"
    interval = _INTERVAL_MAP.get(grain, "1 month")

    if not time_window_start_sql:
        time_window_start_sql = f"DATE_TRUNC('{grain}', CURRENT_DATE)"
    if not time_window_end_sql:
        time_window_end_sql = "CURRENT_DATE"

    if ti_type == "prior_period":
        return (
            f"WITH current_p AS ("
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} "
            f"AND {time_col} < {time_window_end_sql}{filter_where}"
            f"), prior_p AS ("
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} - INTERVAL '{interval}' "
            f"AND {time_col} < {time_window_end_sql} - INTERVAL '{interval}'{filter_where}"
            f") SELECT value FROM prior_p"
        )

    if ti_type == "growth_pct":
        return (
            f"WITH current_p AS ("
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} "
            f"AND {time_col} < {time_window_end_sql}{filter_where}"
            f"), prior_p AS ("
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} - INTERVAL '{interval}' "
            f"AND {time_col} < {time_window_end_sql} - INTERVAL '{interval}'{filter_where}"
            f") SELECT CASE WHEN prior_p.value = 0 OR prior_p.value IS NULL "
            f"THEN NULL ELSE (current_p.value - prior_p.value) * 1.0 / ABS(prior_p.value) END AS value "
            f"FROM current_p, prior_p"
        )

    if ti_type == "period_to_date":
        ptd_grain = grain
        return (
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= DATE_TRUNC('{ptd_grain}', CURRENT_DATE) "
            f"AND {time_col} < CURRENT_DATE + INTERVAL '1 day'{filter_where}"
        )

    n = ti_n_periods or 3
    if ti_type == "moving_avg":
        return (
            f"WITH period_values AS ("
            f"SELECT DATE_TRUNC('{grain}', {time_col}) AS period, "
            f"{base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} - INTERVAL '{interval_literal(n, grain)}' "
            f"AND {time_col} < {time_window_end_sql}{filter_where} "
            f"GROUP BY DATE_TRUNC('{grain}', {time_col})"
            f") SELECT AVG(value) AS value FROM ("
            f"SELECT value FROM period_values ORDER BY period DESC LIMIT {n}"
            f") _recent"
        )

    if ti_type == "trailing_sum":
        return (
            f"WITH period_values AS ("
            f"SELECT DATE_TRUNC('{grain}', {time_col}) AS period, "
            f"{base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} - INTERVAL '{interval_literal(n, grain)}' "
            f"AND {time_col} < {time_window_end_sql}{filter_where} "
            f"GROUP BY DATE_TRUNC('{grain}', {time_col})"
            f") SELECT SUM(value) AS value FROM ("
            f"SELECT value FROM period_values ORDER BY period DESC LIMIT {n}"
            f") _recent"
        )

    if ti_type == "lead":
        return (
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} + INTERVAL '{interval}' "
            f"AND {time_col} < {time_window_end_sql} + INTERVAL '{interval}'{filter_where}"
        )

    if ti_type == "cagr":
        n_cagr = ti_n_periods or 1
        return (
            f"WITH current_p AS ("
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} "
            f"AND {time_col} < {time_window_end_sql}{filter_where}"
            f"), prior_p AS ("
            f"SELECT {base_select_expr} AS value "
            f"FROM {model} "
            f"WHERE {time_col} >= {time_window_start_sql} - INTERVAL '{n_cagr} year' "
            f"AND {time_col} < {time_window_end_sql} - INTERVAL '{n_cagr} year'{filter_where}"
            f") SELECT CASE WHEN prior_p.value IS NULL OR prior_p.value <= 0 THEN NULL "
            f"ELSE POWER(current_p.value * 1.0 / prior_p.value, 1.0 / {n_cagr}) - 1 END AS value "
            f"FROM current_p, prior_p"
        )

    return None


def _split_share_dim_filter(
    filter_clause: str | None,
    dim_col: str,
    predicate_list: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Separate predicates targeting the share dimension from other filters.

    Returns (peer_set_filter, member_filter) where member_filter contains
    only predicates on the share dimension column, and peer_set_filter
    contains everything else. This ensures the full peer set is preserved
    in the grouped CTE and the member filter selects from the ranked result.

    When ``predicate_list`` is provided (individual predicates before
    joining), each predicate is classified as a whole unit — this avoids
    corrupting BETWEEN predicates whose internal ``AND`` would be split
    by a naive string split on ``" AND "``.
    """
    if not filter_clause:
        return None, None
    unquoted = dim_col.strip('"')

    if predicate_list:
        parts = predicate_list
    else:
        parts = [p.strip() for p in filter_clause.split(" AND ") if p.strip()]

    def _is_share_dim(pred: str) -> bool:
        return (
            f'"{unquoted}"' in pred
            or pred.startswith(f"{unquoted} ")
            or pred.startswith(f"{unquoted}=")
        )

    peer_parts = [p for p in parts if not _is_share_dim(p)]
    member_parts = [p for p in parts if _is_share_dim(p)]
    peer = " AND ".join(peer_parts) if peer_parts else None
    member = " AND ".join(member_parts) if member_parts else None
    return peer, member


def compile_share_rank_sql(
    base_select_expr: str,
    ctx: CompilerContext,
) -> str | None:
    """Generate grouped scalar SQL for share/rank/top-N KPIs.

    Returns a single scalar value: the share of total, top-N contribution
    percentage, or rank of a specific member within a grouped dimension.

    Filters targeting the share dimension are separated from the peer-set
    WHERE so that the full comparison group is preserved. The member filter
    is applied after ranking/grouping to select the specific member.
    """
    if not ctx.share_type or not ctx.share_dimension:
        return None

    model = _safe_ident(ctx.model_slug)
    dim_col = _safe_ident(ctx.share_dimension)

    peer_filter, member_filter = _split_share_dim_filter(
        ctx.filter_where_clause, dim_col, ctx.filter_predicate_list,
    )

    where_parts = []
    if peer_filter:
        where_parts.append(peer_filter)
    if ctx.time_where_clause:
        where_parts.append(ctx.time_where_clause)
    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    member_where = f" WHERE {member_filter}" if member_filter else ""

    if ctx.share_type == "top_n_contribution":
        n = ctx.share_n or 10
        return (
            f"WITH grouped AS ("
            f"SELECT {dim_col}, {base_select_expr} AS dim_value "
            f"FROM {model}{where_sql} GROUP BY {dim_col}"
            f"), total AS ("
            f"SELECT SUM(dim_value) AS grand_total FROM grouped"
            f"), top_ranked AS ("
            f"SELECT dim_value FROM grouped ORDER BY dim_value DESC LIMIT {n}"
            f") SELECT CASE WHEN total.grand_total = 0 OR total.grand_total IS NULL "
            f"THEN NULL ELSE SUM(top_ranked.dim_value) * 1.0 / total.grand_total END AS value "
            f"FROM top_ranked, total GROUP BY total.grand_total"
        )

    if ctx.share_type == "rank":
        # Always return exactly one scalar: best (lowest) rank among matched members.
        return (
            f"WITH grouped AS ("
            f"SELECT {dim_col}, {base_select_expr} AS dim_value "
            f"FROM {model}{where_sql} GROUP BY {dim_col}"
            f"), ranked AS ("
            f"SELECT {dim_col}, RANK() OVER (ORDER BY dim_value DESC) AS rnk "
            f"FROM grouped"
            f") SELECT MIN(rnk) AS value FROM ranked{member_where}"
        )

    # share_of_total: combined share of selected member(s) against grand total.
    if member_filter:
        return (
            f"WITH grouped AS ("
            f"SELECT {dim_col}, {base_select_expr} AS dim_value "
            f"FROM {model}{where_sql} GROUP BY {dim_col}"
            f"), total AS ("
            f"SELECT SUM(dim_value) AS grand_total FROM grouped"
            f") SELECT SUM(grouped.dim_value) * 1.0 / NULLIF(total.grand_total, 0) AS value "
            f"FROM grouped, total{member_where} GROUP BY total.grand_total"
        )
    # No member filter: return largest member's share (market leader share).
    return (
        f"WITH grouped AS ("
        f"SELECT {dim_col}, {base_select_expr} AS dim_value "
        f"FROM {model}{where_sql} GROUP BY {dim_col}"
        f"), total AS ("
        f"SELECT SUM(dim_value) AS grand_total FROM grouped"
        f") SELECT MAX(dim_value) * 1.0 / NULLIF(grand_total, 0) AS value "
        f"FROM grouped, total GROUP BY grand_total"
    )


def _row_outer_agg(ctx: CompilerContext) -> str:
    """Resolve the outer aggregation for row_first / pre_aggregated modes:
    ctx.outer_agg when set, else the context default agg (F-017-24)."""
    return (ctx.outer_agg or ctx.default_agg or "sum")


def _build_agg_of_agg_sql(
    select_expr: str, ctx: CompilerContext,
) -> str:
    """Build a nested subquery for aggregate-of-aggregate mode.

    Inner query groups by ``inner_grain`` and applies ``inner_agg``.
    Outer query applies ``outer_agg`` to the inner result.
    """
    # F-017-21: inner_grain may be a grain keyword ("month") OR a real column.
    # A grain keyword must be DATE_TRUNC'd against the model's time column
    # (spec 5.4.3) — treating it as a raw column name produces invalid SQL.
    raw_grain = (ctx.inner_grain or ctx.time_column or "date")
    if raw_grain.lower() in _GRAIN_KEYWORDS:
        time_col = _safe_ident(ctx.time_column or "date")
        inner_grain_expr = f"DATE_TRUNC('{raw_grain.lower()}', {time_col})"
    else:
        inner_grain_expr = _safe_ident(raw_grain)
    inner_select = _wrap_agg(ctx.inner_agg, select_expr)
    outer_select = _wrap_agg(ctx.outer_agg, "inner_val")
    return (
        f"SELECT {outer_select} AS value "
        f"FROM ("
        f"SELECT {inner_grain_expr} AS grain_key, {inner_select} AS inner_val "
        f"FROM {_safe_ident(ctx.model_slug)} "
        f"GROUP BY {inner_grain_expr}"
        f") sub"
    )


# ---------------------------------------------------------------------------
# AST-to-SQL compiler
# ---------------------------------------------------------------------------

class _Compiler:
    """Walk a KPI expression AST and emit SQL."""

    def __init__(self, ctx: CompilerContext) -> None:
        self.ctx = ctx
        self.measure_names: list[str] = []
        self.kpi_names: list[str] = []
        self.has_time_intelligence = False
        # F-017-22: set when share_of_total()/rank_over() is compiled OUTSIDE
        # the builder's grouped CTE path (ctx.share_type unset). A windowed
        # aggregate over a single ungrouped row always yields share 1 / rank 1 —
        # a silently-wrong number. compile_expression returns
        # _COMPILER_UNSUPPORTED in that case so the value fails closed instead.
        self.has_ungrouped_window = False

    def compile_node(self, node: ASTNode) -> str:
        """Compile an AST node to a SQL expression string."""
        if isinstance(node, NumberLiteral):
            return self._compile_number(node)
        if isinstance(node, FunctionCall):
            return self._compile_function(node)
        if isinstance(node, BinaryOp):
            return self._compile_binary(node)
        if isinstance(node, UnaryMinus):
            return self._compile_unary(node)
        if isinstance(node, StringLiteral):
            return self._compile_string(node)
        return "NULL"

    def _compile_number(self, node: NumberLiteral) -> str:
        if node.value == int(node.value) and not math.isinf(node.value):
            return str(int(node.value))
        return str(node.value)

    def _compile_string(self, node: StringLiteral) -> str:
        escaped = node.value.replace("'", "''")
        return f"'{escaped}'"

    def _compile_binary(self, node: BinaryOp) -> str:
        left = self.compile_node(node.left)
        right = self.compile_node(node.right)
        # Parenthesize to preserve precedence
        return f"({left} {node.op} {right})"

    def _compile_unary(self, node: UnaryMinus) -> str:
        operand = self.compile_node(node.operand)
        return f"(-{operand})"

    def _compile_function(self, node: FunctionCall) -> str:
        fn = node.name

        # -- Reference functions --
        if fn == "measure":
            return self._compile_measure(node)
        if fn == "kpi":
            return self._compile_kpi_ref(node)
        if fn == "literal":
            if node.args:
                return self.compile_node(node.args[0])
            return "NULL"
        if fn == "dimension":
            return self._compile_dimension(node)

        # -- Safe division --
        if fn in ("safe_div", "safe_ratio"):
            return self._compile_safe_div(node)
        if fn == "div":
            return self._compile_div(node)

        # -- Conditional --
        if fn == "coalesce":
            return self._compile_coalesce(node)
        if fn == "if_then_else":
            return self._compile_if_then_else(node)
        if fn == "sla_condition":
            return self._compile_sla_condition(node)

        # -- Aggregation --
        if fn == "count":
            return self._compile_count(node)
        if fn == "count_distinct":
            return self._compile_count_distinct(node)
        if fn in ("sum", "avg", "min", "max"):
            return self._compile_agg_override(node, fn)

        # -- Windowed analytics --
        if fn == "share_of_total":
            return self._compile_share_of_total(node)
        if fn == "rank_over":
            return self._compile_rank_over(node)

        # -- Arithmetic --
        if fn == "abs":
            return self._compile_abs(node)
        if fn == "round":
            return self._compile_round(node)
        if fn == "min_of":
            return self._compile_min_of(node)
        if fn == "max_of":
            return self._compile_max_of(node)

        # -- Time intelligence --
        if fn in (
            "prior_period", "period_to_date", "moving_avg", "trailing_sum",
            "lag", "lead", "cagr", "pct_change", "fiscal_period_to_date",
        ):
            self.has_time_intelligence = True
            return self._compile_time_intelligence(node)

        return "NULL"

    def _compile_measure(self, node: FunctionCall) -> str:
        if not node.args or not isinstance(node.args[0], StringLiteral):
            return "NULL"
        name = node.args[0].value
        if name not in self.measure_names:
            self.measure_names.append(name)

        agg = self.ctx.default_agg
        if self.ctx.measure_aggs and name in self.ctx.measure_aggs:
            agg = self.ctx.measure_aggs[name]

        mode = self.ctx.calc_agg_mode
        if mode in ("row_first", "pre_aggregated"):
            # Row-first: use bare column, outer agg applied later
            return _safe_ident(name)
        # aggregate_first or automatic: wrap in aggregation
        return _wrap_agg(agg, _safe_ident(name))

    def _compile_kpi_ref(self, node: FunctionCall) -> str:
        """Compile a kpi() reference.

        KPI cross-references are resolved by the evaluation pipeline, not
        SQL.  We emit NULL here; the evaluator substitutes the pre-computed
        value before sending to the query-router.
        """
        if node.args and isinstance(node.args[0], StringLiteral):
            name = node.args[0].value
            if name not in self.kpi_names:
                self.kpi_names.append(name)
        return "NULL"

    def _compile_dimension(self, node: FunctionCall) -> str:
        if not node.args or not isinstance(node.args[0], StringLiteral):
            return "NULL"
        return _safe_ident(node.args[0].value)

    def _compile_count(self, node: FunctionCall) -> str:
        if node.args:
            inner = node.args[0]
            if isinstance(inner, FunctionCall) and inner.name == "measure":
                if inner.args and isinstance(inner.args[0], StringLiteral):
                    name = inner.args[0].value
                    if name not in self.measure_names:
                        self.measure_names.append(name)
                    return f"COUNT({_safe_ident(name)})"
            compiled = self.compile_node(inner)
            return f"COUNT({compiled})"
        return "COUNT(*)"

    def _compile_count_distinct(self, node: FunctionCall) -> str:
        if not node.args:
            return "NULL"
        inner = node.args[0]
        if isinstance(inner, FunctionCall) and inner.name == "measure":
            if inner.args and isinstance(inner.args[0], StringLiteral):
                name = inner.args[0].value
                if name not in self.measure_names:
                    self.measure_names.append(name)
                return f"COUNT(DISTINCT {_safe_ident(name)})"
        compiled = self.compile_node(inner)
        return f"COUNT(DISTINCT {compiled})"

    def _compile_share_of_total(self, node: FunctionCall) -> str:
        if not node.args:
            return "NULL"
        # F-017-22: a plain expression (no grouped share CTE context) makes this
        # window degenerate. Flag it so compile_expression fails closed.
        if not self.ctx.share_type:
            self.has_ungrouped_window = True
        inner = node.args[0]
        if isinstance(inner, FunctionCall) and inner.name == "measure":
            if inner.args and isinstance(inner.args[0], StringLiteral):
                name = inner.args[0].value
                if name not in self.measure_names:
                    self.measure_names.append(name)
                agg = self.ctx.default_agg
                if self.ctx.measure_aggs and name in self.ctx.measure_aggs:
                    agg = self.ctx.measure_aggs[name]
                agg_expr = _wrap_agg(agg, _safe_ident(name))
                return f"CAST({agg_expr} AS FLOAT) / NULLIF(SUM({agg_expr}) OVER (), 0)"
        compiled = self.compile_node(inner)
        return f"CAST({compiled} AS FLOAT) / NULLIF(SUM({compiled}) OVER (), 0)"

    def _compile_rank_over(self, node: FunctionCall) -> str:
        if not node.args:
            return "NULL"
        # F-017-22: see _compile_share_of_total — degenerate outside a grouped
        # share CTE (every row ranks 1).
        if not self.ctx.share_type:
            self.has_ungrouped_window = True
        inner = node.args[0]
        if isinstance(inner, FunctionCall) and inner.name == "measure":
            if inner.args and isinstance(inner.args[0], StringLiteral):
                name = inner.args[0].value
                if name not in self.measure_names:
                    self.measure_names.append(name)
                agg = self.ctx.default_agg
                if self.ctx.measure_aggs and name in self.ctx.measure_aggs:
                    agg = self.ctx.measure_aggs[name]
                agg_expr = _wrap_agg(agg, _safe_ident(name))
                return f"RANK() OVER (ORDER BY {agg_expr} DESC)"
        compiled = self.compile_node(inner)
        return f"RANK() OVER (ORDER BY {compiled} DESC)"

    def _compile_agg_override(self, node: FunctionCall, fn: str) -> str:
        if not node.args:
            return "NULL"
        inner = node.args[0]
        if isinstance(inner, FunctionCall) and inner.name == "measure":
            if inner.args and isinstance(inner.args[0], StringLiteral):
                name = inner.args[0].value
                if name not in self.measure_names:
                    self.measure_names.append(name)
                return f"{fn.upper()}({_safe_ident(name)})"
        compiled_inner = self.compile_node(inner)
        return f"{fn.upper()}({compiled_inner})"

    def _compile_safe_div(self, node: FunctionCall) -> str:
        if len(node.args) < 2:
            return "NULL"
        num = self.compile_node(node.args[0])
        den = self.compile_node(node.args[1])
        return f"CASE WHEN {den} = 0 OR {den} IS NULL THEN NULL ELSE ({num}) * 1.0 / ({den}) END"

    def _compile_div(self, node: FunctionCall) -> str:
        if len(node.args) < 3:
            return "NULL"
        num = self.compile_node(node.args[0])
        den = self.compile_node(node.args[1])
        fallback = self.compile_node(node.args[2])
        return f"CASE WHEN {den} = 0 OR {den} IS NULL THEN {fallback} ELSE ({num}) * 1.0 / ({den}) END"

    def _compile_coalesce(self, node: FunctionCall) -> str:
        if not node.args:
            return "NULL"
        args = ", ".join(self.compile_node(a) for a in node.args)
        return f"COALESCE({args})"

    def _compile_if_then_else(self, node: FunctionCall) -> str:
        if len(node.args) < 3:
            return "NULL"
        cond = self.compile_node(node.args[0])
        then_val = self.compile_node(node.args[1])
        else_val = self.compile_node(node.args[2])
        return f"CASE WHEN ({cond}) <> 0 THEN {then_val} ELSE {else_val} END"

    def _compile_sla_condition(self, node: FunctionCall) -> str:
        """sla_condition(measure_expr, comparator_str, threshold, then_val, else_val)"""
        if len(node.args) < 5:
            return "NULL"
        inner = node.args[0]
        if isinstance(inner, FunctionCall) and inner.name == "measure":
            if inner.args and isinstance(inner.args[0], StringLiteral):
                name = inner.args[0].value
                if name not in self.measure_names:
                    self.measure_names.append(name)
                measure_sql = _safe_ident(name)
            else:
                measure_sql = self.compile_node(inner)
        else:
            measure_sql = self.compile_node(inner)
        if not isinstance(node.args[1], StringLiteral):
            return "NULL"
        comparator = node.args[1].value
        valid_ops = {">", ">=", "<", "<=", "=", "!="}
        if comparator not in valid_ops:
            return "NULL"
        threshold = self.compile_node(node.args[2])
        then_val = self.compile_node(node.args[3])
        else_val = self.compile_node(node.args[4])
        return f"CASE WHEN {measure_sql} {comparator} {threshold} THEN {then_val} ELSE {else_val} END"

    def _compile_abs(self, node: FunctionCall) -> str:
        if not node.args:
            return "NULL"
        val = self.compile_node(node.args[0])
        return f"ABS({val})"

    def _compile_round(self, node: FunctionCall) -> str:
        if len(node.args) < 2:
            return "NULL"
        val = self.compile_node(node.args[0])
        decimals = self.compile_node(node.args[1])
        return f"ROUND({val}, {decimals})"

    def _compile_min_of(self, node: FunctionCall) -> str:
        if len(node.args) < 2:
            return "NULL"
        a = self.compile_node(node.args[0])
        b = self.compile_node(node.args[1])
        return f"LEAST({a}, {b})"

    def _compile_max_of(self, node: FunctionCall) -> str:
        if len(node.args) < 2:
            return "NULL"
        a = self.compile_node(node.args[0])
        b = self.compile_node(node.args[1])
        return f"GREATEST({a}, {b})"

    # -- Time intelligence compilation --

    _TIME_FUNC_TO_VARIANT: dict[str, str] = {
        "lag": "lag",
        "lead": "lead",
        "pct_change": "pct_change",
        "cagr": "cagr",
    }

    _GRAIN_TO_PRIOR: dict[str, str] = {
        "year": "prior_year",
        "quarter": "prior_quarter",
        "month": "prior_month",
        "week": "prior_week",
    }

    _GRAIN_TO_PTD: dict[str, str] = {
        "year": "ytd",
        "quarter": "qtd",
        "month": "mtd",
        "week": "wtd",
    }

    def _compile_time_intelligence(self, node: FunctionCall) -> str:
        """Compile a time intelligence function to SQL via VariantBinding.

        Falls back to NULL if:
        - No time_column in context
        - Inner expression contains kpi() references (can't be compiled to SQL)
        """
        fn = node.name

        # Check if inner expression contains kpi() refs — if so, can't compile.
        # But still collect the kpi names for dependency tracking.
        if self._has_kpi_ref_in_args(node):
            self._collect_refs_from_args(node)
            return "NULL"

        # Compile the inner expression (the measure/expression being time-shifted)
        if not node.args:
            return "NULL"
        inner_expr = self.compile_node(node.args[0])

        # Extract grain from second arg if present
        grain = self._extract_grain(node)

        # Extract n from third arg if present (for moving_avg, trailing_sum, cagr)
        n_val = self._extract_n(node)

        # Determine the variant name
        variant_name = self._resolve_variant_name(fn, grain)
        if variant_name is None:
            return "NULL"

        # Build the VariantBinding
        time_col = self.ctx.time_column or "date"
        # calendar_type defaults to "standard" so period-boundary variants
        # (prior_month, ytd, etc.) can use EXTRACT-based computation even
        # when no calendar table is present.
        cal_type = self.ctx.calendar_type or "standard"
        try:
            binding = VariantBinding(
                base_expression=inner_expr,
                fact_date_column=_safe_ident(time_col),
                calendar_type=cal_type,
                fiscal_year_start_month=self.ctx.fiscal_year_start_month,
                n=n_val,
            )
            result = emit_variant_expression(variant_name, binding)
            return result.sql
        except VariantSqlError:
            return "NULL"

    def _has_kpi_ref_in_args(self, node: FunctionCall) -> bool:
        """Check if any argument subtree contains a kpi() call."""
        for arg in node.args:
            if self._contains_kpi_ref(arg):
                return True
        return False

    def _collect_refs_from_args(self, node: FunctionCall) -> None:
        """Walk args to collect kpi/measure names without emitting SQL."""
        for arg in node.args:
            self._collect_refs(arg)

    def _collect_refs(self, node: ASTNode) -> None:
        """Recursively collect kpi and measure names from an AST subtree."""
        if isinstance(node, FunctionCall):
            if node.name == "kpi" and node.args and isinstance(node.args[0], StringLiteral):
                name = node.args[0].value
                if name not in self.kpi_names:
                    self.kpi_names.append(name)
            elif node.name == "measure" and node.args and isinstance(node.args[0], StringLiteral):
                name = node.args[0].value
                if name not in self.measure_names:
                    self.measure_names.append(name)
            for arg in node.args:
                self._collect_refs(arg)
        elif isinstance(node, BinaryOp):
            self._collect_refs(node.left)
            self._collect_refs(node.right)
        elif isinstance(node, UnaryMinus):
            self._collect_refs(node.operand)

    def _contains_kpi_ref(self, node: ASTNode) -> bool:
        if isinstance(node, FunctionCall):
            if node.name == "kpi":
                return True
            return any(self._contains_kpi_ref(a) for a in node.args)
        if isinstance(node, BinaryOp):
            return self._contains_kpi_ref(node.left) or self._contains_kpi_ref(node.right)
        if isinstance(node, UnaryMinus):
            return self._contains_kpi_ref(node.operand)
        return False

    def _extract_grain(self, node: FunctionCall) -> Optional[str]:
        if len(node.args) >= 2 and isinstance(node.args[1], StringLiteral):
            return node.args[1].value.lower()
        return None

    def _extract_n(self, node: FunctionCall) -> Optional[int]:
        fn = node.name
        if fn in ("moving_avg", "trailing_sum", "cagr", "lag", "lead"):
            # n can be 2nd or 3rd arg depending on function signature, and
            # callers may pass it bare (3) or wrapped (literal(3)).
            return _extract_n_periods(node)
        return None

    def _resolve_variant_name(self, fn: str, grain: Optional[str]) -> Optional[str]:
        # Direct mapping for simple functions
        if fn in self._TIME_FUNC_TO_VARIANT:
            return self._TIME_FUNC_TO_VARIANT[fn]

        # prior_period(expr, grain) -> prior_{grain}
        if fn == "prior_period":
            if grain and grain in self._GRAIN_TO_PRIOR:
                return self._GRAIN_TO_PRIOR[grain]
            return "prior_month"  # default

        # period_to_date(expr, grain) -> {grain}td
        if fn in ("period_to_date", "fiscal_period_to_date"):
            if grain and grain in self._GRAIN_TO_PTD:
                return self._GRAIN_TO_PTD[grain]
            return "ytd"  # default

        # moving_avg(expr, n, grain) -> moving_avg_n
        if fn == "moving_avg":
            return "moving_avg_n"

        # trailing_sum(expr, n, grain) -> trailing_n
        if fn == "trailing_sum":
            return "trailing_n"

        return None


# ---------------------------------------------------------------------------
# Time-intelligence decomposition derivation (wizard/formula path)
# ---------------------------------------------------------------------------
# Business-builder KPIs carry ti_type/ti_grain/ti_n_periods/base_expression
# metadata, which routes evaluation through decomposed simple queries the
# query-router can bind. Wizard and formula-editor KPIs only have the raw
# expression — these helpers derive the same metadata from the parsed AST so
# both paths share one evaluation machine (F-017-01 root cause fix).

# DSL time function -> decomposed ti_type vocabulary.
_TI_FN_TO_DECOMPOSED = {
    "pct_change": "growth_pct",
    "prior_period": "prior_period",
    "moving_avg": "moving_avg",
    "trailing_sum": "trailing_sum",
    "cagr": "cagr",
    "period_to_date": "period_to_date",
    "fiscal_period_to_date": "fiscal_period_to_date",
    "lag": "lag",
    "lead": "lead",
}


@dataclass(frozen=True)
class TIDecomposition:
    """TI metadata derived from a top-level time-intelligence expression."""
    ti_type: str
    ti_grain: Optional[str]
    ti_n_periods: Optional[int]
    base_expression: str


def ast_to_expression(node: ASTNode) -> str:
    """Serialize an AST subtree back to canonical KPI DSL text."""
    if isinstance(node, NumberLiteral):
        if node.value == int(node.value) and not math.isinf(node.value):
            return str(int(node.value))
        return str(node.value)
    if isinstance(node, StringLiteral):
        if '"' in node.value:
            raise ValueError(
                "Cannot serialize string literal containing a double quote"
            )
        return f'"{node.value}"'
    if isinstance(node, FunctionCall):
        args = ", ".join(ast_to_expression(a) for a in node.args)
        return f"{node.name}({args})"
    if isinstance(node, BinaryOp):
        return f"({ast_to_expression(node.left)} {node.op} {ast_to_expression(node.right)})"
    if isinstance(node, UnaryMinus):
        return f"(-{ast_to_expression(node.operand)})"
    raise ValueError(f"Cannot serialize AST node {type(node).__name__}")


def _subtree_has_kpi_ref(node: ASTNode) -> bool:
    if isinstance(node, FunctionCall):
        if node.name == "kpi":
            return True
        return any(_subtree_has_kpi_ref(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return _subtree_has_kpi_ref(node.left) or _subtree_has_kpi_ref(node.right)
    if isinstance(node, UnaryMinus):
        return _subtree_has_kpi_ref(node.operand)
    return False


def _subtree_has_time_intelligence(node: ASTNode) -> bool:
    if isinstance(node, FunctionCall):
        if node.name in _TI_FN_TO_DECOMPOSED:
            return True
        return any(_subtree_has_time_intelligence(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return (
            _subtree_has_time_intelligence(node.left)
            or _subtree_has_time_intelligence(node.right)
        )
    if isinstance(node, UnaryMinus):
        return _subtree_has_time_intelligence(node.operand)
    return False


def _extract_n_periods(node: FunctionCall) -> Optional[int]:
    """Extract the window-size argument, accepting both ``3`` and ``literal(3)``
    in any post-expression position (the wizard emits ``(expr, grain, literal(n))``,
    the spec documents ``(expr, n, grain)`` — both are honoured)."""
    for arg in node.args[1:]:
        if isinstance(arg, NumberLiteral):
            return int(arg.value)
        if (
            isinstance(arg, FunctionCall)
            and arg.name == "literal"
            and arg.args
            and isinstance(arg.args[0], NumberLiteral)
        ):
            return int(arg.args[0].value)
    return None


def _extract_grain_arg(node: FunctionCall) -> Optional[str]:
    for arg in node.args[1:]:
        if isinstance(arg, StringLiteral):
            return arg.value.lower()
    return None


def derive_ti_decomposition_from_node(node: ASTNode) -> Optional[TIDecomposition]:
    """Derive decomposed-TI metadata from a parsed AST node.

    Returns None when the node is not a time-intelligence function call,
    has no inner expression, or wraps a kpi() reference (which cannot be
    compiled to SQL and stays on the Python evaluation path).
    """
    if not isinstance(node, FunctionCall):
        return None
    ti_type = _TI_FN_TO_DECOMPOSED.get(node.name)
    if ti_type is None or not node.args:
        return None
    inner = node.args[0]
    if _subtree_has_kpi_ref(inner) or _subtree_has_time_intelligence(inner):
        return None
    return TIDecomposition(
        ti_type=ti_type,
        ti_grain=_extract_grain_arg(node),
        ti_n_periods=_extract_n_periods(node),
        base_expression=ast_to_expression(inner),
    )


def derive_ti_decomposition(expression: str) -> Optional[TIDecomposition]:
    """Derive decomposed-TI metadata from a raw KPI expression string.

    Only a top-level time-intelligence call is decomposable here; nested
    TI (e.g. ``pct_change(...) * 100``) is evaluated by the Python pipeline
    via the provider's time-intelligence hook.
    """
    try:
        ast = parse_kpi_expression(expression)
    except Exception:
        return None
    return derive_ti_decomposition_from_node(ast)


def expression_has_time_intelligence(expression: str) -> bool:
    """True when *expression* contains any time-intelligence function."""
    try:
        ast = parse_kpi_expression(expression)
    except Exception:
        return False
    return _subtree_has_time_intelligence(ast)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compile_expression(
    expression: str,
    ctx: Optional[CompilerContext] = None,
) -> CompiledQuery:
    """Compile a KPI expression string to a SQL query.

    Parameters
    ----------
    expression : str
        The KPI expression (e.g., ``safe_div(measure("Revenue"), measure("Headcount"))``).
    ctx : CompilerContext | None
        Compiler configuration.  If None, uses defaults.

    Returns
    -------
    CompiledQuery
        The compiled SQL query and metadata.

    Raises
    ------
    ValueError
        If the expression cannot be parsed.
    """
    if ctx is None:
        ctx = CompilerContext()

    ast = parse_kpi_expression(expression)
    compiler = _Compiler(ctx)
    select_expr = compiler.compile_node(ast)

    # F-017-24: row_first / pre_aggregated compute a per-row expression and then
    # apply the OUTER aggregation across rows. The outer agg was hard-coded to
    # the context default ("sum"), so the spec's flagship example — average
    # per-product margin (outer_agg=avg) — was inexpressible, and pre_aggregated
    # with measure() emitted a bare un-aggregated column (an arbitrary row).
    # Honour ctx.outer_agg when set, falling back to default_agg.
    outer = _row_outer_agg(ctx)
    if (
        ctx.calc_agg_mode in ("row_first", "pre_aggregated")
        and not compiler.has_time_intelligence
        and not (ctx.inner_agg and ctx.outer_agg)  # agg-of-agg builds its own
    ):
        select_expr = _wrap_agg(outer, select_expr)

    # Carry-forward: wrap expression with COALESCE NULL fill before
    # any subquery wrapping so the fill operates at the row level.
    if ctx.carry_forward:
        select_expr = _build_carry_forward_expr(select_expr, ctx)

    # Resolve effective WHERE clause for non-subquery paths.
    effective_where = ctx.where_clause
    if not effective_where and (ctx.filter_where_clause or ctx.time_where_clause):
        parts = [p for p in (ctx.filter_where_clause, ctx.time_where_clause) if p]
        effective_where = " AND ".join(parts) if parts else None
    where_suffix = f" WHERE {effective_where}" if effective_where else ""

    # Aggregate-of-aggregate: nested subquery with inner/outer agg.
    # Takes priority over the default single-SELECT path.
    if ctx.inner_agg and ctx.outer_agg:
        sql = _build_agg_of_agg_sql(select_expr, ctx)
        if where_suffix and " FROM " in sql:
            sql = _inject_where(sql, effective_where)
    # Semi-additive: subquery with GROUP BY at_grain.
    elif ctx.non_additive_agg and ctx.at_grain:
        sql = _build_semi_additive_sql(select_expr, ctx)
        if where_suffix and " FROM " in sql:
            sql = _inject_where(sql, effective_where)
    # Share/rank grouped scalar SQL for business-builder KPIs.
    elif ctx.share_type and ctx.share_dimension and ctx.base_expression:
        base_ast = parse_kpi_expression(ctx.base_expression)
        base_compiler = _Compiler(ctx)
        base_select = base_compiler.compile_node(base_ast)
        if ctx.calc_agg_mode in ("row_first", "pre_aggregated") and not base_compiler.has_time_intelligence:
            base_select = _wrap_agg(_row_outer_agg(ctx), base_select)
        share_sql = compile_share_rank_sql(base_select, ctx)
        if share_sql:
            sql = share_sql
        else:
            sql = f"SELECT {select_expr} AS value FROM {_safe_ident(ctx.model_slug)}{where_suffix}"
    # CTE-based scalar SQL for business-builder TI KPIs.
    elif ctx.ti_type and ctx.base_expression and ctx.time_column:
        base_ast = parse_kpi_expression(ctx.base_expression)
        base_compiler = _Compiler(ctx)
        base_select = base_compiler.compile_node(base_ast)
        if ctx.calc_agg_mode in ("row_first", "pre_aggregated") and not base_compiler.has_time_intelligence:
            base_select = _wrap_agg(_row_outer_agg(ctx), base_select)
        scalar_sql = compile_scalar_kpi_sql(
            base_select, ctx,
            ti_type=ctx.ti_type,
            ti_grain=ctx.ti_grain,
            ti_n_periods=ctx.ti_n_periods,
            time_window_start_sql=ctx.time_window_start_sql,
            time_window_end_sql=ctx.time_window_end_sql,
        )
        if scalar_sql:
            sql = scalar_sql
        else:
            sql = f"SELECT {select_expr} AS value FROM {_safe_ident(ctx.model_slug)}{where_suffix}"
    # Legacy window-function subquery for non-business-builder TI paths.
    elif compiler.has_time_intelligence and ctx.time_column and ctx.enable_ti_subquery:
        sql = _build_ti_subquery(select_expr, ctx)
    else:
        sql = f"SELECT {select_expr} AS value FROM {_safe_ident(ctx.model_slug)}{where_suffix}"

    # Dialect transpilation
    sql = _transpile_to_dialect(sql, ctx.dialect)

    return CompiledQuery(
        sql=sql,
        select_expr=select_expr,
        measure_names=compiler.measure_names,
        kpi_names=compiler.kpi_names,
        has_time_intelligence=compiler.has_time_intelligence,
        agg_mode=ctx.calc_agg_mode,
        has_ungrouped_window=compiler.has_ungrouped_window,
    )
