"""
SQL → LogicalQuery IR using SQLGlot.

The caller specifies the **input dialect** the SQL was written in (Postgres,
BigQuery, etc.). The parser honours that dialect on the way in, so identifier
quoting (``"x"`` in Postgres, ```x``` in BigQuery) is interpreted the
same way the producer wrote it. Column names in the parsed query are treated
as semantic measure or dimension names — the binder resolves them against the
model.

The output IR is dialect-agnostic. The rewriter handles **target dialect** —
the SQL flavour the source DB speaks — separately at emit time.
"""
from __future__ import annotations

import logging
from typing import Any

import sqlglot
from sqlglot import exp

from shared.aggregate_stats import stat_type_for_sqlglot_key
from shared.pocket.fingerprint import fingerprint_shape
from src.ir.logical_query import (
    ExpressionOccurrence,
    LogicalFilter,
    LogicalQuery,
    SelectExpression,
    UnsupportedSQL,
)
from src.registry import (
    get_aggregate_func,
    get_scalar_func,
    is_deterministic_literal,
)

logger = logging.getLogger(__name__)


# Map API/wire dialect names → sqlglot's canonical name. Anything not
# listed here passes through unchanged so callers can opt in to a new
# sqlglot dialect without a code change.
_DIALECT_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "pg": "postgres",
    "jdbc": "postgres",       # JDBC = Postgres wire protocol in this stack
    "spark": "spark",
    "spark_sql": "spark",
    "hadoop_spark": "spark",
    "mssql": "tsql",          # Bug-6956 (Fable R1): common SQL Server alias
    "sqlserver": "tsql",      # Bug-6956 (Fable R1): connector name for SQL Server
}

# Bug-6956: allowlist of sqlglot dialect names accepted by this stack.
# Unknown dialects previously passed through to sqlglot unchanged, causing a
# ValueError (or silently producing incorrect AST behaviour).  Validate early
# and fall back to "postgres" with a warning rather than crashing mid-parse.
_KNOWN_DIALECTS: frozenset[str] = frozenset({
    "postgres", "bigquery", "spark", "redshift", "snowflake", "tsql",
    "mysql", "hive", "trino", "presto", "duckdb", "clickhouse",
    "databricks", "sqlite", "oracle", "teradata", "athena",
    "starrocks", "doris", "drill", "druid", "materialize",
})


def _normalize_dialect(dialect: str | None) -> str:
    if not dialect:
        return "postgres"
    resolved = _DIALECT_ALIASES.get(dialect.lower(), dialect.lower())
    if resolved not in _KNOWN_DIALECTS:
        logger.warning(
            "Bug-6956: unknown SQL dialect %r (resolved as %r); "
            "falling back to 'postgres'",
            dialect, resolved,
        )
        return "postgres"
    return resolved


class GroupByError(ValueError):
    """Raised when a JDBC (SQL) query mixes aggregates with bare columns
    that are not listed in GROUP BY — matches PostgreSQL semantics."""


class SyntaxErrorInSQL(ValueError):
    """Raised when a JDBC (SQL) query contains malformed syntax that real
    Postgres would reject (consecutive commas, stray semicolons, etc.) but
    sqlglot silently recovers from."""


def parse_sql_to_ir(
    raw_sql: str,
    model_id: str,
    protocol: str = "jdbc",
    input_dialect: str | None = None,
) -> LogicalQuery:
    """Parse SQL into a LogicalQuery IR.

    ``input_dialect`` is the SQL flavour the producer wrote, in either the
    API/wire form (``"postgresql"``, ``"jdbc"``, ``"bigquery"``, ``"spark"``,
    ``"mssql"``, ``"sqlserver"``) or sqlglot's canonical name (``"postgres"``,
    ``"bigquery"``...). Unknown values fall back to ``"postgres"`` with a
    warning (Bug-6956). Defaults to Postgres -- the canonical internal dialect
    for this stack.
    """
    dialect = _normalize_dialect(input_dialect)

    # Raw-syntax scan (consecutive commas, stray semicolons).  Run ONCE here
    # and reuse the result below for the XMLA/DAX warning trace — F-003-11:
    # previously this scan ran twice per JDBC parse.
    raw_syntax_warnings = _detect_raw_syntax_errors(raw_sql)

    # JDBC pre-parse checks: surface specific messages for cases that
    # sqlglot's parser silently recovers from by dropping tokens.  The
    # friendlier messages run first; the generic "Malformed SQL" fallback
    # below catches anything else the tokens-dropped path produces.
    # XMLA/DAX clients send syntactic quirks (consecutive commas, stray
    # semicolons) that are not meaning-changing, so the pre-scan stays
    # JDBC-only. The sqlglot_errors check below catches meaning-changing
    # recoveries (Bug-7916) on ALL protocols.
    if protocol == "jdbc":
        for w in raw_syntax_warnings:
            raise SyntaxErrorInSQL(w)

    tree, sqlglot_errors = _parse_with_errors(raw_sql, dialect=dialect)

    # Bug-7916 / Codex gate: strict syntax enforcement for ALL protocols.
    # Any remaining sqlglot parse error (token dropped during recovery)
    # means real Postgres would have rejected the input.  A recovered
    # tree can have altered semantics (WHERE x = 1 !! -> WHERE x = NOT 1)
    # and must never be silently routed on any protocol.  Previously only
    # JDBC escalated; XMLA/DAX retained the permissive behaviour and
    # could execute a meaning-changed tree.
    if sqlglot_errors:
        first = sqlglot_errors[0]
        raise SyntaxErrorInSQL(f"Malformed SQL: {first}")

    # Bug-086 carve-out: flatten identity-shape derived tables so
    # ``SELECT <proj> FROM (SELECT * FROM T WHERE <inner>) q [WHERE <outer>]``
    # is treated the same as ``SELECT <proj> FROM T WHERE <inner> AND <outer>``
    # for IR extraction + fingerprinting purposes. The pocket matcher can
    # then find a ``SELECT * FROM T WHERE <slice>`` pocket via the
    # filter-only serviceable fingerprint. Downstream rewriter still
    # works from the original raw_sql, so the subquery shape is
    # preserved at execution time.
    tree = _try_flatten_identity_derived_table(tree)

    # Resolve the top-level Select once. Using isinstance instead of
    # tree.find() avoids recursing into subqueries in the FROM clause.
    select_node = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)

    select_star = _has_select_star(select_node)

    # Unwrap SELECT * FROM (single_subquery) when the outer query has no
    # WHERE / GROUP BY / HAVING / ORDER BY.  The outer SELECT * is
    # semantically transparent — extract columns from the subquery instead.
    if select_star and select_node is not None:
        has_outer_clauses = any(
            select_node.args.get(k) for k in ("where", "group", "having", "order")
        )
        from_clause = select_node.args.get("from_")
        if from_clause and not has_outer_clauses:
            source = from_clause.this
            if (isinstance(source, exp.Subquery)
                    and isinstance(source.this, exp.Select)
                    and not isinstance(source.this, (exp.Union, exp.Intersect, exp.Except))):
                select_node = source.this
                select_star = _has_select_star(select_node)

    measures, dimensions, grain, select_expressions, bare_true, alias_to_col = _extract_columns(select_node)
    filters = _extract_filters(select_node)
    order_by, has_unresolvable_order, order_by_alias_names = _extract_order_by(select_node)
    limit = _extract_limit(select_node)
    offset = _extract_offset(select_node)

    having_raw, having_columns = _extract_having(select_node)
    has_unresolvable_where = _has_unresolvable_where(select_node)
    has_distinct = select_node.args.get("distinct") is not None if select_node else False
    # DISTINCT ON (col) cannot be reconstructed by the source rewriter —
    # force complex-SQL passthrough so the raw SQL is preserved with only
    # table name substitution (Bug-879-D).
    _distinct_on = False
    if has_distinct:
        _distinct_node = select_node.args.get("distinct")
        if _distinct_node and _distinct_node.args.get("on"):
            _distinct_on = True

    # Detect function-based GROUP BY (DATE_TRUNC, EXTRACT, etc.)
    # Bug-7359: also extract recognized time-period grains (DATE_TRUNC)
    # so the binder/matcher can evaluate aggregate eligibility.
    has_function_grain = False
    _time_period_grains: list[tuple[str, str]] = []
    _has_unrecognized_function_grain = False
    _group = select_node.args.get("group") if select_node else None
    if _group:
        _select_items = select_node.expressions or []
        for _gexpr in _group.expressions:
            _ginner = _gexpr.this if isinstance(_gexpr, exp.Alias) else _gexpr
            # Unwrap transparent Paren wrappers so GROUP BY (region) / GROUP BY (1)
            # are treated exactly as their unparenthesised forms. (Cast is left
            # as-is here: GROUP BY x::text remains function grain, unchanged.)
            while isinstance(_ginner, exp.Paren):
                _ginner = _ginner.this
            if isinstance(_ginner, exp.Literal):
                # Positional GROUP BY (Bug-6082 / F-003-16): a positional ref
                # to a bare-column SELECT item resolves to real grain (not
                # function grain); a ref to an expression/aggregate item — or
                # an out-of-range/non-integer literal — is function grain.
                _resolved_pos = _resolve_positional_select_column(_ginner, _select_items)
                if _resolved_pos is None:
                    # Check if the positional ref points to a DATE_TRUNC / EXTRACT
                    # select item (F-003-08).
                    _pos_tp = _resolve_positional_time_period(_ginner, _select_items)
                    if _pos_tp is not None:
                        has_function_grain = True
                        _time_period_grains.append(_pos_tp)
                    else:
                        has_function_grain = True
                        _has_unrecognized_function_grain = True
                        break
                continue
            if isinstance(_ginner, exp.Column):
                continue
            # Bug-7359: recognize DATE_TRUNC(<unit>, <column>) as a time-period
            # grain expression.  The full DATE_TRUNC identity (unit + column)
            # preserves year boundaries: DATE_TRUNC('month', d) yields
            # 2025-01-01 for Jan 2025 and 2026-01-01 for Jan 2026 — these are
            # distinct date values that are NEVER merged.
            _tp = _recognize_time_period_grain(_ginner)
            if _tp is not None:
                has_function_grain = True
                _time_period_grains.append(_tp)
            else:
                has_function_grain = True
                _has_unrecognized_function_grain = True
                break
    # If ANY group-by item is an unrecognized function expression, clear
    # the time_period_grains — we cannot partially accelerate. All or nothing.
    if _has_unrecognized_function_grain:
        _time_period_grains = []

    # F3 guard: reject if the raw SQL contains a 3-arg DATE_TRUNC (timezone
    # form).  sqlglot silently drops the 3rd arg, so the AST check in
    # _recognize_date_trunc_grain cannot detect it.  A raw-SQL scan catches
    # it before the aggregate route serves wrong timezone buckets.  This is
    # conservative (false positives are safe — the query falls to source
    # passthrough, which is correct).
    if _time_period_grains:
        if _has_three_arg_date_trunc(raw_sql):
            _time_period_grains = []

    # Detect complex SQL constructs that the source rewriter cannot safely
    # reconstruct: CTEs, derived tables (FROM subquery), window functions,
    # scalar subqueries in SELECT, correlated subqueries.  These queries
    # should go through passthrough-with-table-substitution.
    has_complex_sql = _detect_complex_sql(tree) or _distinct_on
    windows = list(tree.find_all(exp.Window))
    has_window_functions = bool(windows)
    has_window_aggregate = any(window.find(exp.AggFunc) is not None for window in windows)

    cte_aliases = _extract_cte_aliases(tree)

    # Extract tables from the FULL parse tree so that set-operation branches
    # (UNION, INTERSECT, EXCEPT) beyond the first are captured for the
    # binder's FROM-table allow-list (Bug-6958: previously only the first
    # branch's tables were extracted because ``tree`` was narrowed to
    # ``select_node`` for non-Select top-level nodes).
    from_tables = _extract_from_tables(tree)

    # Compute ungrouped bare columns for strict GROUP BY enforcement.
    # Only TRULY bare SELECT columns (case 4 in _extract_columns) count —
    # columns referenced inside expressions (CASE, EXTRACT, CAST, arithmetic)
    # are not bare in the PostgreSQL sense and may match a matching GROUP BY
    # expression instead of needing to be in GROUP BY as a column.
    # Fold GROUP BY grain to PostgreSQL semantics before the strict gate so
    # valid queries are not falsely rejected:
    #   Bug-6084 — GROUP BY may reference a SELECT output-column ALIAS
    #     (``SELECT region AS r … GROUP BY r``); resolve the alias to the
    #     underlying column so the aliased bare column reads as grouped.
    #   Bug-6085 — unquoted identifiers fold to lower-case, so
    #     ``SELECT Region … GROUP BY region`` groups correctly; compare
    #     case-insensitively.
    # Bias to leniency: PostgreSQL is the final arbiter downstream, so a false
    # negative here is harmless (PG rejects a genuinely-invalid query) whereas
    # a false positive rejects a query PG accepts.
    # PostgreSQL ambiguity rule: when a GROUP BY name matches BOTH an output
    # alias and an input column, the INPUT COLUMN wins. So an alias that
    # collides with a real (bare) column name must NOT resolve to its aliased
    # column here — otherwise ``SELECT a AS b, b, SUM(x) ... GROUP BY b`` would
    # falsely treat the ungrouped column ``a`` as grouped (PG rejects it).
    # F-2: reuse the single bare-column-only alias map produced by
    # ``_extract_columns`` (aggregate/expression aliases are excluded there), so
    # the strict GROUP BY gate and the grain normalization agree on which
    # aliases are group-by-bindable.
    _alias_to_col = alias_to_col
    grain_folded = set()
    for _g in grain:
        _gl = _g.lower()
        grain_folded.add(_gl)
        # GROUP BY on an output alias groups by that alias's underlying column.
        if _gl in _alias_to_col:
            grain_folded.add(_alias_to_col[_gl].lower())
    ungrouped_bare = [c for c in bare_true if c.lower() not in grain_folded]

    # Strict GROUP BY enforcement for SQL (JDBC) callers only.  XMLA/DAX
    # have no explicit GROUP BY — grouping is inferred from the axis /
    # dimension selection, so the implicit-grouping logic above is correct
    # for those protocols.  For JDBC we must match PostgreSQL semantics:
    # mixing aggregates with bare columns that are not in GROUP BY is a
    # syntax error, not a silently-grouped query.
    #
    # Skip the check for complex SQL (CTEs, window functions, derived tables,
    # UNION, etc.): those queries route through passthrough-with-table-
    # substitution, and Postgres will enforce its own GROUP BY semantics
    # downstream.  Our static analysis doesn't model these precisely enough
    # to avoid false positives.
    if protocol == "jdbc" and measures and ungrouped_bare and not has_complex_sql:
        cols = ", ".join(f'"{c}"' for c in ungrouped_bare)
        # F-003-11: pluralise the noun to match the column count so a
        # multi-column list does not read as "column "a", "b" must…".
        noun = "column" if len(ungrouped_bare) == 1 else "columns"
        raise GroupByError(
            f"{noun} {cols} must appear in the GROUP BY clause or be used "
            f"in an aggregate function"
        )

    # Raw-SQL warnings already surfaced (and for JDBC raised) before
    # parsing.  Here we only collect the grammar-level warnings that
    # require a parsed tree so XMLA/DAX traces still see them.  Reuse the
    # single scan computed above (F-003-11) rather than re-scanning.
    syntax_warnings = list(raw_syntax_warnings)
    syntax_warnings.extend(
        _detect_grammar_syntax_warnings(
            measures=measures, grain=grain, select_bare=ungrouped_bare,
        )
    )
    # Derived-grain routing (spec §5.1, Phase 1): capture non-column expression
    # occurrences and fold their canonical fingerprints into the query shape so a
    # DATE_TRUNC('month', …) query no longer collides with an EXTRACT(month …) one
    # on the expression-blind ``has_function_grain`` boolean (spec I11). For an
    # ordinary query this list is empty, and ``_compute_fingerprint`` then omits
    # the derived-expression key entirely — the hash is byte-identical to before.
    expression_occurrences = _extract_expression_occurrences(select_node, dialect)
    expr_fingerprints = _occurrence_fingerprints(expression_occurrences)
    fingerprint = _compute_fingerprint(
        measures, dimensions, grain, filters, having_columns=having_columns,
        expr_fingerprints=expr_fingerprints,
    )

    return LogicalQuery(
        model_id=model_id,
        protocol=protocol,
        raw_query=raw_sql,
        requested_measures=measures,
        requested_dimensions=dimensions,
        filters=filters,
        grain=grain,
        order_by=order_by,
        limit=limit,
        offset=offset,
        query_fingerprint=fingerprint,
        select_star=select_star,
        select_expressions=select_expressions,
        from_tables=from_tables,
        syntax_warnings=syntax_warnings,
        having_raw=having_raw,
        having_columns=having_columns,
        has_unresolvable_where=has_unresolvable_where,
        has_unresolvable_order=has_unresolvable_order,
        order_by_alias_names=order_by_alias_names,
        has_distinct=has_distinct,
        has_function_grain=has_function_grain,
        has_complex_sql=has_complex_sql,
        has_window_functions=has_window_functions,
        has_window_aggregate=has_window_aggregate,
        cte_aliases=cte_aliases,
        input_dialect=dialect,
        expression_occurrences=expression_occurrences,
        time_period_grains=_time_period_grains,
    )


def _has_select_star(select_node: exp.Select | None) -> bool:
    """Check if the query uses SELECT * or SELECT alias.* (qualified star)."""
    if select_node:
        for expr in select_node.expressions:
            if isinstance(expr, exp.Star):
                return True
            # Qualified star: m.* is parsed as Column(table='m', this=Star)
            if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
                return True
    return False


def _composable_aggregate(
    inner: exp.Expression,
) -> tuple[bool, list[str], list[tuple[str, str]]]:
    """Return (is_composable, agg_functions, inner_aggregates) for a SELECT expression.

    Composable = a scalar composition of simple, re-aggregatable aggregates
    (e.g. ``SUM(a)/SUM(b)``, ``CASE WHEN SUM(b)=0 THEN NULL ELSE SUM(a)/SUM(b) END``):
    every aggregate is ``AGG(plain column)`` (or a literal-aware ``COUNT(*)``)
    whose routing is mappable/derivable/exact_grain, every column lives inside an
    aggregate (no bare / non-grouped columns), and there is no subquery.

    Such expressions stay ``classification="passthrough"`` (so the source
    rewriter and security audit are unchanged) but are aggregate-ROUTABLE: each
    aggregate node maps to its own physical column. The matcher applies
    additivity / exact-grain gating to the returned ``agg_functions``, and
    requires the exact ``(column, function)`` stat column from the returned
    ``inner_aggregates`` — so e.g. ``MAX(a)/MAX(b)`` over sum-default measures
    only routes to an aggregate that actually stores ``a__max``/``b__max``.
    """
    aggs = list(inner.find_all(exp.AggFunc))
    if not aggs or list(inner.find_all(exp.Subquery)):
        return False, [], []
    funcs: list[str] = []
    pairs: list[tuple[str, str]] = []
    agg_col_ids: set[int] = set()
    for a in aggs:
        fn = a.key.lower() if hasattr(a, "key") else ""
        this = getattr(a, "this", None)
        if isinstance(this, exp.Distinct) and fn == "count":
            fn = "count_distinct"
        info = get_aggregate_func(fn)
        if not info or info.get("routing") not in ("mappable", "derivable", "exact_grain"):
            return False, [], []
        col_name: str | None = None
        if isinstance(this, exp.Distinct):
            exprs = this.expressions
            # Bug-6093: multi-column COUNT(DISTINCT a, b) counts distinct tuples
            # and cannot be served by a single stat column — not composable.
            ok = len(exprs) == 1 and isinstance(exprs[0], exp.Column)
            if ok:
                col_name = exprs[0].name
        elif isinstance(this, exp.Column):
            ok = True
            col_name = this.name
        elif isinstance(this, (exp.Literal, exp.Star, exp.Boolean, exp.Neg)):
            ok = bool(info.get("literal_aware"))   # COUNT(*), COUNT(1)
            if ok:
                col_name = "__row_count"           # mirrors the rewriter's _name_func
        else:
            ok = False                              # SUM(a*b), SUM(a+b) — not composable
        if not ok:
            return False, [], []
        funcs.append(fn)
        pairs.append((col_name, fn))
        for c in a.find_all(exp.Column):
            agg_col_ids.add(id(c))
    # Every column must be inside an aggregate (no bare / non-grouped columns).
    for c in inner.find_all(exp.Column):
        if id(c) not in agg_col_ids:
            return False, [], []
    return True, funcs, pairs


def recognize_ordered_set_percentile(
    node: exp.Expression,
) -> dict | None:
    """Recognise the ONE safe ordered-set percentile shape the router can serve.

    Bug-6969/5891 (spec §4.1). Returns a descriptor for exactly

        PERCENTILE_CONT(<literal fraction>) WITHIN GROUP (ORDER BY <single column> [ASC|DESC])
        PERCENTILE_DISC(<literal fraction>) WITHIN GROUP (ORDER BY <single column> [ASC|DESC])

    and None for anything else (multi-column ORDER BY, an expression order key,
    a non-literal fraction, an unrecognised shape) — those stay complex SQL and
    route to source (fail closed). This is the shared recogniser used by BOTH
    ``_extract_columns`` (to emit a routable pNN SelectExpression) and
    ``_detect_complex_sql`` (to NOT flag the recognised shape complex), so the
    two can never disagree — the historical F-003-07 dead-code trap (parser
    routes it, complex-SQL gate then discards it) is closed because a single
    predicate governs both.

    Descriptor keys: ``stat_suffix`` (pNN or None when the fraction is not a
    canonical column suffix — still recognised, but the matcher will find no
    pNN column and route to source), ``column``, ``method`` (continuous |
    discrete), ``direction`` (asc | desc), ``fraction_text`` (EXACT decimal
    text, never a float). ``stat_suffix`` drives the existing (measure, pNN)
    column-lookup machinery; ``method``/``direction``/``fraction_text`` feed the
    binder's QuantileRequest inventory and the coverage proof.
    """
    if not isinstance(node, exp.WithinGroup):
        return None
    fn = node.this
    if isinstance(fn, exp.PercentileCont):
        method = "continuous"
    elif isinstance(fn, exp.PercentileDisc):
        method = "discrete"
    else:
        return None
    # Fraction must be a numeric literal (never a parameter/expression — a
    # late-bound fraction cannot select coverage before the proof, §4.1).
    frac = getattr(fn, "this", None)
    if not isinstance(frac, exp.Literal) or frac.is_string:
        return None
    fraction_text = str(frac.this)
    # ORDER BY must be a single bare column.
    order = node.args.get("expression")
    if not isinstance(order, exp.Order):
        return None
    ordered = list(order.expressions)
    if len(ordered) != 1:
        return None
    o = ordered[0]
    col = getattr(o, "this", None)
    if not isinstance(col, exp.Column):
        return None
    direction = "desc" if o.args.get("desc") else "asc"
    # SERVING suffix: the pNN column the rewriter will read. Materialised
    # columns store the ASCENDING percentile (built as PERCENTILE_CONT(0.9) ->
    # col__p90). So the column that serves a request is the ASCENDING-fraction
    # column, NOT the authored fraction (Fable R1 CRITICAL): a
    # ``CONT(0.9) DESC`` request equals ascending p10 and must read ``col__p10``,
    # never ``col__p90`` (over [1,100] p90=90.1 but the correct DESC-0.9 = p10 =
    # 10.9). For CONTINUOUS the ascending fraction is 1-p under DESC (positionally
    # symmetric). For DISCRETE there is NO ascending equivalent, so the serving
    # suffix stays the raw fraction and the coverage proof requires
    # direction-identical coverage (a DESC discrete only serves from a
    # DESC-built column, matched by physical-column identity below).
    stat_suffix = _serving_suffix(method, fraction_text, direction)
    return {
        "stat_suffix": stat_suffix,
        "column": _col_name(col),
        "method": method,
        "direction": direction,
        "fraction_text": fraction_text,
    }


def _serving_suffix(method: str, fraction_text: str, direction: str) -> str | None:
    """The pNN column suffix the rewriter reads for this request.

    CONTINUOUS: direction-normalised to the ASCENDING fraction the column stores
    (``CONT(0.9) DESC`` -> ascending 0.1 -> ``p10``). DISCRETE: the raw fraction
    (no ascending equivalent); a DESC discrete request is only ever served from
    direction-matched coverage, enforced downstream by physical-column identity.
    Returns None for a non-canonical fraction (no column) -> source.
    """
    from decimal import Decimal, InvalidOperation

    try:
        frac = Decimal(fraction_text.strip())
    except (InvalidOperation, AttributeError):
        return None
    if method == "continuous" and direction == "desc":
        frac = Decimal(1) - frac
    return _fraction_text_to_suffix(str(frac))


def _fraction_text_to_suffix(fraction_text: str) -> str | None:
    """Map an EXACT decimal fraction string (e.g. '0.9') to a canonical pNN
    suffix ('p90'), or None when it is not a whole-percentile canonical value.

    Uses exact ``Decimal`` arithmetic (never a float) so '0.3333333333' does not
    round into a pNN suffix (§16.14). A non-canonical fraction returns None: the
    shape is still recognised and bound, but no pNN column exists to serve it,
    so it routes to source.
    """
    from decimal import Decimal, InvalidOperation

    try:
        frac = Decimal(fraction_text.strip())
    except (InvalidOperation, AttributeError):
        return None
    scaled = frac * 100
    if scaled != scaled.to_integral_value():
        return None
    pct = int(scaled)
    from shared.aggregate_quantiles import QUANTILE_PERCENTILES, quantile_suffix

    return quantile_suffix(pct) if pct in QUANTILE_PERCENTILES else None


def _detect_percentile(node: exp.Expression) -> tuple[str | None, str | None]:
    """Map a ``MEDIAN(col)`` projection to a materialised quantile column.

    ``MEDIAN(col)`` -> ('p50', col), which is routable to a materialised
    ``col__p50`` column at exact grain (gated in the matcher; percentiles are
    not re-aggregatable).

    Bug-6969/5891: the explicit ordered-set form
    ``PERCENTILE_CONT/DISC(frac) WITHIN GROUP (ORDER BY col)`` is now recognised
    separately by ``recognize_ordered_set_percentile`` (which also feeds the
    binder's QuantileRequest inventory), so it is NOT handled here. This helper
    stays MEDIAN-only; the ordered-set caller in ``_extract_columns`` runs the
    shared recogniser explicitly.
    """
    if isinstance(node, exp.Median):
        this = getattr(node, "this", None)
        if isinstance(this, exp.Column):
            return "p50", _col_name(this)
    return None, None


def _resolve_positional_select_column(
    literal: exp.Literal, select_items: list[exp.Expression]
) -> str | None:
    """Resolve a positional reference (``GROUP BY 1`` / ``ORDER BY 1``) to the
    bare column name of the matching SELECT item.

    Returns the column name when the 1-based position points at a bare-column
    SELECT item (optionally aliased); returns ``None`` when the literal is not
    an integer, is out of range, or points at an expression/aggregate item —
    in which case the caller treats it as function grain / unresolvable, never
    as a bare-column grain. Mirrors the positional-resolution ``_extract_order_by``
    already performs, so GROUP BY and ORDER BY treat ``n`` identically."""
    if not literal.is_int:
        return None
    pos = int(literal.this) - 1
    if 0 <= pos < len(select_items):
        item = select_items[pos]
        inner = item.this if isinstance(item, exp.Alias) else item
        # Unwrap transparent Paren SELECT items (``SELECT (region)``) so the
        # positional ref resolves to the underlying bare column, mirroring how
        # an explicit ``GROUP BY region`` already groups it.
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, exp.Column):
            return _col_name(inner)
    return None


_DATE_TRUNC_UNITS = frozenset({
    "microsecond", "microseconds", "millisecond", "milliseconds",
    "second", "seconds", "minute", "minutes",
    "hour", "hours", "day", "days",
    "week", "weeks", "month", "months",
    "quarter", "quarters", "year", "years",
    "decade", "decades", "century", "centuries",
    "millennium", "millennia",
})

# Units that are safe for aggregate routing across ALL supported dialects
# (PG + BigQuery).  BigQuery DATE_TRUNC supports: DAY, WEEK, ISOWEEK,
# MONTH, QUARTER, YEAR, ISOYEAR.  Sub-day units (hour/minute/second/...)
# are only supported by BigQuery's TIMESTAMP_TRUNC (not DATE_TRUNC), and
# DECADE/CENTURY/MILLENNIUM are PG-only.  Non-routable units are still
# recognized as function grain (has_function_grain=True) but NOT as
# routable time-period grains, so the query falls to source passthrough
# (correct on all dialects).
_DATE_TRUNC_ROUTABLE_UNITS = frozenset({
    "day", "week", "month", "quarter", "year",
})

# F-003-08 / G-003-03: EXTRACT units that are aggregate-matchable. Stored in
# time_period_grains as ('extract_month', col) so fingerprints stay distinct
# from DATE_TRUNC('month', col) (spec I11). Arbitrary EXTRACT (DOW, WEEK, …)
# stays unrecognized function grain → source.
_EXTRACT_ROUTABLE_UNITS = frozenset({"month", "year", "quarter"})
_EXTRACT_UNIT_PREFIX = "extract_"


def _has_three_arg_date_trunc(raw_sql: str) -> bool:
    """Return True if raw_sql contains a DATE_TRUNC call with 3+ arguments.

    F3 guard: sqlglot silently drops the 3rd timezone argument from
    ``DATE_TRUNC('day', ts, 'America/New_York')``, so the AST-level
    recognizer cannot detect it.  This function scans the raw SQL for
    ``DATE_TRUNC(`` and counts top-level commas inside the call to detect
    the 3-arg form.  Conservative: false positives (e.g. a 3-arg call
    inside a string literal) are safe -- the query falls to source.
    """
    import re
    for m in re.finditer(r"DATE_TRUNC\s*\(", raw_sql, re.IGNORECASE):
        start = m.end()  # position right after the '('
        depth = 1
        commas = 0
        i = start
        while i < len(raw_sql) and depth > 0:
            ch = raw_sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 1:
                commas += 1
            elif ch == "'" and depth == 1:
                # Skip string literals to avoid counting commas inside them
                i += 1
                while i < len(raw_sql) and raw_sql[i] != "'":
                    if raw_sql[i] == "'" and i + 1 < len(raw_sql) and raw_sql[i + 1] == "'":
                        i += 2  # escaped quote
                        continue
                    i += 1
            i += 1
        if commas >= 2:
            return True
    return False


def _recognize_date_trunc_grain(
    node: exp.Expression,
) -> tuple[str, str] | None:
    """Recognize DATE_TRUNC(<unit>, <bare_column>) as a time-period grain.

    Bug-7359 (re-implementation): returns (unit, column_name) when the node is
    a DATE_TRUNC call with a time unit and a bare column argument.  Returns
    None for any other shape (nested expressions, EXTRACT, UPPER, etc.).

    F3 guard (timezone form): sqlglot silently drops the third timezone
    argument from DATE_TRUNC('day', ts, 'America/New_York'), parsing it
    as a 2-arg TimestampTrunc.  If accepted, the aggregate rewrite would
    truncate in the wrong timezone (shifting rows across day/month
    boundaries = wrong numbers).  Rejected by checking the node's zone
    arg and by counting commas in the raw SQL as a defence-in-depth
    against future sqlglot parse changes.

    sqlglot parses ``DATE_TRUNC('month', col)`` differently per dialect:
    - Postgres dialect: ``exp.TimestampTrunc`` with ``args["unit"]`` as a
      ``Var`` node and ``args["this"]`` as the Column.
    - Some dialects: ``exp.DateTrunc`` with similar structure.
    - Fallback: generic ``Func`` with ``key="date_trunc"``.

    All three are handled.  The unit may be a ``Var`` (``MONTH``), a
    string ``Literal`` (``'month'``), or an ``Identifier``.

    The FULL DATE_TRUNC identity (unit + underlying column) preserves year
    boundaries: DATE_TRUNC('month', order_date) on 2025-01-15 yields 2025-01-01
    and on 2026-01-15 yields 2026-01-01 -- distinct date values that are NEVER
    merged.  This avoids the fatal flaw of the prior name-heuristic approach
    that bucketed Jan-2025 and Jan-2026 together.
    """
    func_key = getattr(node, "key", "").lower() if hasattr(node, "key") else ""
    # Recognize DateTrunc, TimestampTrunc, and generic date_trunc Func.
    _is_trunc = (
        isinstance(node, exp.DateTrunc)
        or isinstance(node, getattr(exp, "TimestampTrunc", type(None)))
        or func_key in ("date_trunc", "timestamptrunc")
    )
    if not _is_trunc:
        return None

    # Extract the unit and column arguments.
    unit_str: str | None = None
    col_node: exp.Expression | None = None

    # All three node types store the date expression in .this and the
    # time unit in .args["unit"] (Var/Literal).  DateTrunc may also
    # store the unit in .this with the column in .expression when no
    # explicit "unit" arg exists.
    unit_node = node.args.get("unit")
    if unit_node is not None:
        col_node = node.this
    else:
        # Fallback: first arg is unit, second is column
        unit_node = node.this
        col_node = node.expression

    if unit_node is not None:
        if isinstance(unit_node, exp.Literal) and unit_node.is_string:
            unit_str = unit_node.this.lower()
        elif isinstance(unit_node, exp.Var):
            unit_str = unit_node.name.lower()
        elif isinstance(unit_node, exp.Identifier):
            unit_str = unit_node.name.lower()

    if unit_str is None or unit_str not in _DATE_TRUNC_UNITS:
        return None
    # Normalize plural units to singular before the routable check.
    _unit_normalized = unit_str
    if _unit_normalized.endswith("s") and _unit_normalized[:-1] in _DATE_TRUNC_UNITS:
        _unit_normalized = _unit_normalized[:-1]
    # Only units safe across all supported dialects (PG + BigQuery) are
    # routable.  PG-only units (DECADE/CENTURY/MILLENNIUM) are still
    # recognized as function grain (has_function_grain=True) but NOT as
    # routable time-period grains, so they route to source passthrough.
    if _unit_normalized not in _DATE_TRUNC_ROUTABLE_UNITS:
        return None
    if col_node is None or not isinstance(col_node, exp.Column):
        return None

    # F3 guard: reject 3-arg timezone form DATE_TRUNC('day', col, 'tz').
    # sqlglot silently drops the 3rd arg, so check the node's zone arg
    # (present in some sqlglot versions) AND count commas in the raw SQL
    # as defence-in-depth.
    if node.args.get("zone") is not None:
        return None
    # Raw-SQL comma count: DATE_TRUNC('unit', col) has 1 comma;
    # DATE_TRUNC('unit', col, 'tz') has 2 commas.  Use the node's own
    # SQL rendering (which includes the unit and column but may drop the
    # timezone) as a conservative check — if the ORIGINAL text has more
    # args than the rendered version, reject.
    try:
        _raw = node.sql(dialect="postgres")
        # Strip nested parens/function calls to count only top-level commas
        _depth = 0
        _commas = 0
        for _ch in _raw:
            if _ch == "(":
                _depth += 1
            elif _ch == ")":
                _depth -= 1
            elif _ch == "," and _depth == 1:
                _commas += 1
        # 2-arg form has 1 top-level comma; 3+ args => reject
        if _commas > 1:
            return None
    except Exception:
        pass  # If rendering fails, proceed with the AST-only check

    col_name = _col_name(col_node)
    if not col_name:
        return None

    return (_unit_normalized, col_name)


def _recognize_extract_grain(
    node: exp.Expression,
) -> tuple[str, str] | None:
    """Recognize EXTRACT(month|year|quarter FROM bare_column) as a time-period grain.

    F-003-08 / G-003-03: sqlglot parses ``EXTRACT(month FROM col)`` as
    ``exp.Extract(this=Var(MONTH), expression=Column)``. Stored as
    ``('extract_month', col)`` so the rewriter emits EXTRACT, not DATE_TRUNC
    (EXTRACT(month) collapses years; DATE_TRUNC('month') does not).
    """
    if not isinstance(node, exp.Extract):
        return None
    unit_node = node.this
    col_node = node.expression
    unit_str: str | None = None
    if isinstance(unit_node, exp.Var):
        unit_str = (unit_node.name or unit_node.this or "").lower()
    elif isinstance(unit_node, exp.Literal) and unit_node.is_string:
        unit_str = str(unit_node.this).lower()
    elif isinstance(unit_node, exp.Identifier):
        unit_str = (unit_node.name or "").lower()
    if not unit_str or unit_str not in _EXTRACT_ROUTABLE_UNITS:
        return None
    if col_node is None or not isinstance(col_node, exp.Column):
        return None
    col_name = _col_name(col_node)
    if not col_name:
        return None
    return (f"{_EXTRACT_UNIT_PREFIX}{unit_str}", col_name)


def _recognize_time_period_grain(
    node: exp.Expression,
) -> tuple[str, str] | None:
    """DATE_TRUNC or EXTRACT(month|year|quarter FROM col) → (unit, column)."""
    return _recognize_date_trunc_grain(node) or _recognize_extract_grain(node)


def _resolve_positional_time_period(
    literal: exp.Literal, select_items: list[exp.Expression],
) -> tuple[str, str] | None:
    """Resolve a positional GROUP BY ref to a DATE_TRUNC / EXTRACT SELECT item."""
    if not literal.is_int:
        return None
    pos = int(literal.this) - 1
    if 0 <= pos < len(select_items):
        item = select_items[pos]
        inner = item.this if isinstance(item, exp.Alias) else item
        while isinstance(inner, exp.Paren):
            inner = inner.this
        return _recognize_time_period_grain(inner)
    return None


def _resolve_positional_date_trunc(
    literal: exp.Literal, select_items: list[exp.Expression],
) -> tuple[str, str] | None:
    """Backward-compatible alias for ``_resolve_positional_time_period``."""
    return _resolve_positional_time_period(literal, select_items)


def _extract_columns(select_node: exp.Select | None) -> tuple[list[str], list[str], list[str], list[SelectExpression], list[str], dict[str, str]]:
    """
    Distinguish measure columns (wrapped in aggregate functions) from
    dimension columns (bare columns or expressions in GROUP BY).

    Returns: (measures, dimensions, grain, select_expressions, bare_true, alias_to_col)
    - measures: column names inside aggregate calls (SUM, COUNT, AVG, MAX, MIN, COUNT DISTINCT)
    - grain: column names in GROUP BY (Cast-unwrapped)
    - dimensions: grain + any bare SELECT columns not in an aggregate
    - select_expressions: detailed metadata for each select list item
    - bare_true: SELECT items that are TRULY bare columns (exp.Column at the
      top level, not wrapped in any expression).  Used for PG-style strict
      GROUP BY enforcement — expressions like CASE/EXTRACT/CAST don't count
      as bare even though they reference columns.
    - alias_to_col: GROUP-BY output-alias -> underlying column, built from
      BARE-COLUMN select items ONLY (F-2). An aggregate/expression alias is not
      a valid GROUP BY target, so it is deliberately absent.
    """
    measures = []
    select_bare = []
    bare_true = []
    select_expressions = []
    # F-2 (Fable sensitive-worktree review): the GROUP-BY output-alias
    # normalization (Bug-6084) must apply ONLY to aliases over a BARE column
    # (``region AS r``). An alias over an aggregate or expression
    # (``SUM(amount) AS total``, ``UPPER(region) AS r``) is NOT group-by-bindable
    # in PostgreSQL, so it must NOT resolve to its inner column — doing so let an
    # invalid ``GROUP BY <aggregate-alias>`` bind the measure column as a
    # dimension and execute garbage grouping instead of the correct 422. Record
    # (alias -> column) pairs from the bare-column branch (case 4) only.
    bare_alias_pairs: dict[str, str] = {}

    if select_node:
        for expr in select_node.expressions:
            # Skip Star and qualified star (m.*) — handled by select_star flag.
            if isinstance(expr, exp.Star):
                continue
            if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
                continue

            # Unwrap aliases
            alias_name = expr.alias if isinstance(expr, exp.Alias) else None
            inner = expr.this if isinstance(expr, exp.Alias) else expr
            raw_text = expr.sql()

            # Unwrap redundant parentheses — a semantically-transparent grouping
            # node. Routing classification must be invariant under transparent
            # rewrites: `(SUM(x))` must be classified identically to `SUM(x)`.
            # Without this the Paren top-node hides the aggregate and the
            # expression is mis-classified as passthrough (→ forced to source).
            while isinstance(inner, exp.Paren):
                inner = inner.this

            # Ordered-set percentile: PERCENTILE_CONT/DISC(frac) WITHIN GROUP
            # (ORDER BY col [ASC|DESC]) -> routable pNN column (Bug-6969/5891).
            # Must run BEFORE the WITHIN GROUP unwrap below. The recogniser
            # returns None for any unsupported shape (multi-column/expression
            # order key, non-literal fraction), which stays complex -> source.
            # A recognised shape with a non-canonical fraction (stat_suffix None,
            # e.g. p33) is still bound as continuous/discrete but has no pNN
            # column, so the matcher routes it to source — never a wrong serve.
            _qos = recognize_ordered_set_percentile(inner)
            if _qos and _qos.get("column"):
                _q_meta = {
                    "method": _qos["method"],
                    "direction": _qos["direction"],
                    "fraction_text": _qos["fraction_text"],
                    # source_syntax distinguishes the explicit ordered-set form
                    # (new, gated behind quantile_routing.proof_mode=enforce) from
                    # MEDIAN (pre-existing p50 serving). When the feature is off,
                    # the matcher keeps serving MEDIAN via the existing path but
                    # routes ordered-set percentiles to source (no unproven new
                    # serve), so this push is a strict no-regression when disabled.
                    "source_syntax": "ordered_set",
                }
                measures.append(_qos["column"])
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="analytical",
                    # stat_suffix drives the existing (measure, pNN) column
                    # lookup; when None (non-canonical fraction) fall back to a
                    # sentinel so the matcher cannot match a pNN column.
                    agg_function=(_qos.get("stat_suffix") or "__quantile_unmapped__"),
                    inner_column=_qos["column"], inner_literal=None,
                    quantile_meta=_q_meta,
                ))
                continue

            # Median / percentile -> materialised quantile column (pNN). Must run
            # BEFORE the WITHIN GROUP unwrap below (which discards the ORDER BY
            # column). Routed exact-grain only (gated in the matcher). MEDIAN is
            # canonically continuous p50 ASC (Bug-6969: carry that as quantile_meta
            # so the binder inventories it identically to PERCENTILE_CONT(0.5)).
            _q_suffix, _q_col = _detect_percentile(inner)
            if _q_suffix and _q_col:
                measures.append(_q_col)
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="analytical", agg_function=_q_suffix,
                    inner_column=_q_col, inner_literal=None,
                    quantile_meta={
                        "method": "continuous",
                        "direction": "asc",
                        "fraction_text": "0.5",
                        "source_syntax": "median",
                    },
                ))
                continue

            # Unwrap aggregate wrappers: FILTER (WHERE …) and WITHIN GROUP (…).
            # The wrapped aggregate is the semantically relevant node; the
            # wrapper just carries extra clauses we don't need to classify.
            if isinstance(inner, (exp.Filter, exp.WithinGroup)):
                inner = inner.this

            func_name = ""
            if hasattr(inner, "key"):
                func_name = inner.key.lower()
            elif hasattr(inner, "name"):
                func_name = inner.name.lower()

            if isinstance(getattr(inner, "this", None), exp.Distinct) and func_name == "count":
                func_name = "count_distinct"

            # Dispersion stats: sqlglot keys (stddev/stddevpop/variance/...) differ
            # from the registry's canonical stat types (stddev_samp/var_pop/...).
            # Normalise so the exact_grain registry entry + rewriter column lookup
            # ({measure}__{stat}) apply uniformly.
            _stat_norm = stat_type_for_sqlglot_key(func_name)
            if _stat_norm:
                func_name = _stat_norm

            agg_info = get_aggregate_func(func_name)
            scalar_info = get_scalar_func(func_name)

            # Unregistered aggregates (STDDEV_POP, PERCENTILE_CONT, STRING_AGG,
            # VAR_POP etc. — sqlglot normalises their keys differently than
            # the registry).  Treat any exp.AggFunc as an aggregate so its
            # inner columns are not leaked to select_bare.
            if not agg_info and isinstance(inner, exp.AggFunc):
                cols_in_expr = list(inner.find_all(exp.Column))
                primary = _col_name(cols_in_expr[0]) if len(cols_in_expr) == 1 else None
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="passthrough", agg_function=func_name or None,
                    inner_column=primary, inner_literal=None,
                ))
                continue

            # 2. Aggregate function call
            if agg_info:
                col = None
                literal = None
                inner_this = getattr(inner, "this", None)
                
                if isinstance(inner_this, exp.Distinct):
                    exprs = inner_this.expressions
                    # Bug-6093: COUNT(DISTINCT a, b) counts distinct (a, b)
                    # TUPLES — no single stat column can serve it. Only a
                    # single-column distinct is aggregate-routable; a
                    # multi-column distinct must fall through to the
                    # passthrough branch below and force a source read rather
                    # than collapse to the first column and serve a wrong number.
                    if len(exprs) == 1 and isinstance(exprs[0], exp.Column):
                        col = exprs[0]
                elif isinstance(inner_this, exp.Column):
                    col = inner_this
                elif isinstance(inner_this, (exp.Literal, exp.Star, exp.Boolean, exp.Neg)):
                    literal = inner_this

                if col and agg_info.get("routing") in ("mappable", "derivable", "exact_grain"):
                    measures.append(_col_name(col))
                    select_expressions.append(SelectExpression(
                        raw_text=raw_text, alias=alias_name,
                        classification="analytical", agg_function=func_name,
                        inner_column=_col_name(col), inner_literal=None
                    ))
                elif literal and agg_info.get("literal_aware"):
                    lit_val = _extract_literal_value(literal)
                    measures.append("__row_count")
                    select_expressions.append(SelectExpression(
                        raw_text=raw_text, alias=alias_name,
                        classification="literal", agg_function=func_name,
                        inner_column=None, inner_literal=lit_val
                    ))
                else:
                    # Complex expression inside aggregate (e.g. SUM(col * 2), SUM(col1 + col2))
                    cols_in_expr = list(inner_this.find_all(exp.Column)) if inner_this else []
                    if len(cols_in_expr) == 1:
                        # Single column with constant/expression (e.g. SUM(base_amount * 2)):
                        # extract the column as a measure so the source rewriter can
                        # qualify it, but mark the expression as passthrough.
                        primary = _col_name(cols_in_expr[0])
                        measures.append(primary)
                    else:
                        # Multi-column expression (e.g. SUM(price * qty)):
                        # cannot be served by any aggregate — true passthrough.
                        primary = None
                    select_expressions.append(SelectExpression(
                        raw_text=raw_text, alias=alias_name,
                        classification="passthrough", agg_function=func_name,
                        inner_column=primary, inner_literal=None
                    ))

            # 3. Scalar function wrapping an aggregate
            elif scalar_info == "transparent":
                inner_aggs = []
                _seen_agg_ids: set[int] = set()
                for node in inner.find_all(exp.Expression):
                    if id(node) in _seen_agg_ids:
                        continue
                    node_func = ""
                    if hasattr(node, "key"):
                        node_func = node.key.lower()
                    elif hasattr(node, "name"):
                        node_func = node.name.lower()

                    if get_aggregate_func(node_func):
                        inner_aggs.append((node, node_func))
                        # Mark all descendants so nested matches are skipped
                        for child in node.walk():
                            _seen_agg_ids.add(id(child[0]) if isinstance(child, tuple) else id(child))

                if len(inner_aggs) == 1:
                    agg_node, agg_name = inner_aggs[0]
                    agg_info_inner = get_aggregate_func(agg_name)
                    col = agg_node.this if isinstance(getattr(agg_node, "this", None), exp.Column) else None

                    if col and agg_info_inner and agg_info_inner.get("routing") in ("mappable", "derivable", "exact_grain"):
                        measures.append(_col_name(col))
                        select_expressions.append(SelectExpression(
                            raw_text=raw_text, alias=alias_name,
                            classification="analytical", agg_function=agg_name,
                            inner_column=_col_name(col), inner_literal=None,
                        ))
                    else:
                        select_expressions.append(SelectExpression(
                            raw_text=raw_text, alias=alias_name,
                            classification="passthrough", agg_function=None,
                            inner_column=None, inner_literal=None,
                        ))
                elif inner_aggs:
                    # Multiple aggregates (e.g. SUM(a)/SUM(b)) — register
                    # every inner column as a measure so the aggregate
                    # matcher knows ALL required columns.  Classify as
                    # passthrough with inner_column=None so the binder
                    # sets has_passthrough_expressions=True and the source
                    # rewriter preserves the raw SQL (table-name substitution
                    # only) instead of reconstructing standalone columns.
                    for _agg_node, _agg_name in inner_aggs:
                        _agg_info = get_aggregate_func(_agg_name)
                        _inner_col = _agg_node.this if isinstance(getattr(_agg_node, "this", None), exp.Column) else None
                        if _inner_col and _agg_info and _agg_info.get("routing") in ("mappable", "derivable", "exact_grain"):
                            cn = _col_name(_inner_col)
                            measures.append(cn)
                    _composable, _comp_funcs, _comp_pairs = _composable_aggregate(inner)
                    select_expressions.append(SelectExpression(
                        raw_text=raw_text, alias=alias_name,
                        classification="passthrough", agg_function=None,
                        inner_column=None, inner_literal=None,
                        composable=_composable, agg_functions=_comp_funcs,
                        inner_aggregates=_comp_pairs,
                    ))
                else:
                    cols = list(inner.find_all(exp.Column))
                    for c in cols:
                        cn = _col_name(c)
                        if cn not in select_bare:
                            select_bare.append(cn)
                    has_sq = bool(list(inner.find_all(exp.Subquery)))
                    select_expressions.append(SelectExpression(
                        raw_text=raw_text, alias=alias_name,
                        classification="passthrough", agg_function=None,
                        inner_column=_col_name(cols[0]) if cols and not has_sq else None,
                        inner_literal=None
                    ))

            # 4. Bare column
            elif isinstance(inner, exp.Column):
                col_name = _col_name(inner)
                select_bare.append(col_name)
                bare_true.append(col_name)
                if alias_name:
                    # Only a bare-column alias is a valid GROUP BY output-alias
                    # target (F-2). The ambiguity filter (alias colliding with a
                    # real bare column) is applied after the loop.
                    bare_alias_pairs[alias_name.lower()] = col_name
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="passthrough", agg_function=None,
                    inner_column=col_name, inner_literal=None
                ))

            # 5. Deterministic literal or absolute literal
            elif isinstance(inner, (exp.Literal, exp.Boolean, exp.Null)) or is_deterministic_literal(func_name):
                lit_val = "*" if isinstance(inner, exp.Star) else str(inner.this if hasattr(inner, "this") else inner)
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="literal", agg_function=None,
                    inner_column=None, inner_literal=lit_val
                ))

            # 6. Anything else (arithmetic, function calls, CASE, etc.)
            else:
                # If the expression contains aggregate functions (e.g.
                # CASE WHEN SUM(x)>1000 THEN 'HIGH' END, or COUNT(*) > 0),
                # extract the measures from inside the aggregates and classify
                # as passthrough so the rewriter preserves the raw SQL.
                contained_aggs = list(inner.find_all(exp.AggFunc))
                if contained_aggs:
                    for agg_node in contained_aggs:
                        agg_func = agg_node.key.lower() if hasattr(agg_node, "key") else ""
                        _ai = get_aggregate_func(agg_func)
                        _ic = agg_node.this if isinstance(getattr(agg_node, "this", None), exp.Column) else None
                        if _ic and _ai and _ai.get("routing") in ("mappable", "derivable", "exact_grain"):
                            measures.append(_col_name(_ic))
                    _composable, _comp_funcs, _comp_pairs = _composable_aggregate(inner)
                    select_expressions.append(SelectExpression(
                        raw_text=raw_text, alias=alias_name,
                        classification="passthrough", agg_function=None,
                        inner_column=None, inner_literal=None,
                        composable=_composable, agg_functions=_comp_funcs,
                        inner_aggregates=_comp_pairs,
                    ))
                    continue

                cols = list(inner.find_all(exp.Column))
                all_col_names = [_col_name(c) for c in cols]
                for cn in all_col_names:
                    if cn not in select_bare:
                        select_bare.append(cn)
                # Classify: the expression is resolvable (column names can be
                # substituted) if it references at least one column and does
                # not contain subqueries.  This covers CASE, COALESCE, IF,
                # arithmetic, string functions, etc.
                has_subquery = bool(list(inner.find_all(exp.Subquery)))
                is_resolvable = bool(cols) and not has_subquery
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="passthrough", agg_function=None,
                    inner_column=all_col_names[0] if is_resolvable and all_col_names else None,
                    inner_literal=None,
                ))

    # GROUP BY defines the grain (accessed directly from the Select node
    # to avoid recursing into subqueries in the FROM clause).
    #
    # Bug-6084: PostgreSQL allows GROUP BY to reference a SELECT output alias
    # (SELECT region AS r ... GROUP BY r). The source rewriter groups by
    # resolved dimension names, so normalize an unambiguous output alias to
    # the underlying column here. Preserve PostgreSQL's ambiguity rule: if the
    # GROUP BY name also matches a real input column selected bare, it means
    # the input column, not the output alias.
    # F-2: build the GROUP-BY alias map from BARE-COLUMN select items only
    # (``bare_alias_pairs``), never from aggregate/expression aliases. Apply the
    # PostgreSQL ambiguity rule: an alias that collides with a real bare-selected
    # column name means the INPUT column, not the alias, so it must not resolve.
    bare_true_folded = {c.lower() for c in bare_true}
    # Bug-6859 [DOCUMENTED DIVERGENCE]: the ambiguity rule checks
    # ``alias not in bare_true_folded`` — i.e. against bare-SELECTED columns
    # only, not all table columns. PostgreSQL checks against all INPUT columns
    # (table + subquery columns). The divergence is contrived: it requires a
    # non-selected table column that collides with a SELECT alias, and the
    # binder + PostgreSQL's own disambiguation catch truly ambiguous references
    # downstream. Accepted — no code change needed.
    #
    # Bug-6838 [DOCUMENTED DIVERGENCE]: the case-fold comparison
    # (``alias != col.lower()`` and ``alias not in bare_true_folded``)
    # deliberately ignores quoted-identifier case sensitivity. PostgreSQL
    # treats ``"Region"`` and ``"region"`` as different columns, so a quoted
    # mismatch GROUP BY should fail. Our leniency lets it pass the gate,
    # but the mismatch fails loud at the source (PG rejects the query with
    # a genuine GROUP BY violation). Accepted as fail-loud downstream.
    alias_to_col = {
        alias: col
        for alias, col in bare_alias_pairs.items()
        if col
        and alias != col.lower()
        and alias not in bare_true_folded
    }
    # Bug-6858: collect aliases of aggregate / expression select items so the
    # GROUP BY loop can reject ``GROUP BY <agg-alias>`` loudly instead of
    # silently treating the alias as a dimension name and executing garbage
    # grouping.  An alias is aggregate/expression when its SelectExpression
    # has an agg_function, is classified as "analytical", or is a composable
    # aggregate expression (e.g. SUM(a)/SUM(b) AS ratio — classification
    # stays "passthrough" but composable=True).
    #
    # [DOCUMENTED DIVERGENCE]: if a query has both an input column and an
    # aggregate alias with the same name (e.g. ``SELECT total, SUM(amount)
    # AS total ... GROUP BY total``), PostgreSQL resolves ``GROUP BY total``
    # to the INPUT column, not the alias. Our guard rejects unconditionally,
    # which is stricter (fail-loud). This edge case requires a column name
    # that collides with an aggregate alias — contrived enough to accept.
    _agg_expr_aliases: set[str] = set()
    for se in select_expressions:
        if se.alias and (
            se.agg_function
            or se.classification == "analytical"
            or se.composable
        ):
            _agg_expr_aliases.add(se.alias.lower())
    grain = []
    group = select_node.args.get("group") if select_node else None
    if group:
        for expr in group.expressions:
            inner = expr.this if isinstance(expr, exp.Alias) else expr
            # Unwrap transparent Paren wrappers: GROUP BY (region) / GROUP BY (1)
            # must group exactly as the unparenthesised form does.
            while isinstance(inner, exp.Paren):
                inner = inner.this
            # Unwrap Cast: GROUP BY success_flag::text should still add
            # success_flag to the grain so SELECT success_flag matches.
            if isinstance(inner, exp.Cast):
                inner = inner.this
                while isinstance(inner, exp.Paren):
                    inner = inner.this
            if isinstance(inner, exp.Column):
                name = _col_name(inner)
                # Bug-6858: reject GROUP BY on an aggregate/expression alias
                # (e.g. ``SELECT SUM(amount) AS total … GROUP BY total``).
                # PostgreSQL rejects this with "aggregate functions are not
                # allowed in GROUP BY"; we must fail loud here too, otherwise
                # the alias name leaks through as a dimension and the query
                # executes with garbage grouping.
                if name.lower() in _agg_expr_aliases:
                    raise GroupByError(
                        f'column "{name}" is an aggregate or expression alias '
                        f"and cannot appear in GROUP BY"
                    )
                grain.append(alias_to_col.get(name.lower(), name))
            elif isinstance(inner, exp.Literal):
                # Positional GROUP BY (e.g. GROUP BY 1,2): resolve the position
                # against the SELECT list exactly as ORDER BY positional refs
                # are resolved (Bug-6082 / F-003-16). A positional reference to
                # a bare-column SELECT item adds that column to the grain so the
                # source rewriter renders GROUP BY and the strict gate sees the
                # column as grouped. A reference to an expression/aggregate item
                # is function grain (handled via has_function_grain in
                # parse_sql_to_ir) and contributes no bare-column grain here.
                resolved = _resolve_positional_select_column(
                    inner, select_node.expressions or []
                )
                if resolved is not None:
                    grain.append(resolved)
            # Function-based GROUP BY (DATE_TRUNC, EXTRACT, etc.) — detected
            # separately via has_function_grain in parse_sql_to_ir.

        # GROUPING SETS / ROLLUP / CUBE store their columns in sibling args,
        # not in group.expressions.  Harvest them so PG-bare columns in the
        # SELECT are recognised as grouped.
        for construct_key in ("grouping_sets", "rollup", "cube"):
            construct = group.args.get(construct_key)
            if not construct:
                continue
            items = construct if isinstance(construct, list) else [construct]
            for item in items:
                for col in item.find_all(exp.Column):
                    name = _col_name(col)
                    if name and name not in grain:
                        grain.append(name)

    # dimensions = grain + bare selects not already in grain
    grain_set = set(grain)
    dimensions = list(grain)
    for col in select_bare:
        if col not in grain_set:
            dimensions.append(col)

    return _dedup(measures), _dedup(dimensions), _dedup(grain), select_expressions, _dedup(bare_true), alias_to_col


_COMPARISON_OPS: tuple[tuple[type, str, str], ...] = (
    # (sqlglot class, op-when-column-on-LHS, op-when-column-on-RHS)
    (exp.EQ, "eq", "eq"),
    (exp.NEQ, "neq", "neq"),
    (exp.GT, "gt", "lt"),
    (exp.GTE, "gte", "lte"),
    (exp.LT, "lt", "gt"),
    (exp.LTE, "lte", "gte"),
)


def _flatten_top_level_and(node: exp.Expression | None) -> list[exp.Expression]:
    """Return the top-level AND'd conjuncts of a WHERE body.

    Skips ``Paren`` wrappers. Stops at ``Or`` — disjunctions are not
    AND'd conjuncts and treating them as such (the pre-Bug-102 walker
    did) yields phantom filters that misrepresent the query.
    """
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _flatten_top_level_and(node.this)
    if isinstance(node, exp.And):
        return _flatten_top_level_and(node.this) + _flatten_top_level_and(node.expression)
    return [node]


def _unwrap_not_inner(node: exp.Expression) -> exp.Expression:
    """Inner expression of ``NOT …``, unwrapping a single layer of parens.

    sqlglot 30.8 emits ``Not(In)`` for ``col NOT IN (…)`` and
    ``Not(Paren(In))`` for ``NOT (col IN (…))``.
    """
    inner = node.this
    while isinstance(inner, exp.Paren):
        inner = inner.this
    return inner


def _conjunct_to_filter(node: exp.Expression) -> LogicalFilter | None:
    """Translate a single top-level conjunct into a ``LogicalFilter`` if
    and only if it is faithfully representable. Anything that isn't —
    function calls, arithmetic, subqueries, column-vs-column,
    ``OR``/``EXISTS``/``NOT`` other than ``IS NOT NULL`` / extractable
    ``NOT IN`` — returns None so the rewriter's raw-WHERE preservation
    path takes over instead of a phantom filter contaminating routing
    decisions."""
    for cls, op_lhs, op_rhs in _COMPARISON_OPS:
        if isinstance(node, cls):
            if not _comparison_is_extractable(node):
                return None
            lhs, rhs = node.this, node.expression
            if _is_bare_column(lhs):
                return LogicalFilter(_col_name(lhs), op_lhs, _literal_value(rhs))
            return LogicalFilter(_col_name(rhs), op_rhs, _literal_value(lhs))

    if isinstance(node, exp.In):
        if not _in_is_extractable(node):
            return None
        # F-003-01 / F-004-01 / F-102-04: honour In.negate if a dialect
        # ever emits it (sqlglot 30.8 uses Not(In) instead).
        operator = "not_in" if _in_is_negated(node) else "in"
        return LogicalFilter(
            _col_name(node.this),
            operator,
            [_literal_value(v) for v in node.expressions],
        )

    if isinstance(node, exp.Between):
        if not _between_is_extractable(node):
            return None
        return LogicalFilter(
            _col_name(node.this),
            "between",
            (_literal_value(node.args.get("low")), _literal_value(node.args.get("high"))),
        )

    if isinstance(node, exp.Like) and not isinstance(node, exp.ILike):
        if not _like_is_extractable(node):
            return None
        # Bug-5326 (F-P4ac-01): sqlglot 30.8 parses ``col NOT LIKE '%x%'`` as a
        # single ``Like`` node carrying ``negate=True`` (NOT ``Not(Like(...))``
        # as older versions did). Honour that polarity flag and emit ``not_like``
        # so the rewriter renders ``NOT LIKE`` — the render path supports
        # ``not_like`` (Bug-3609). Without this, a negated Like was extracted as
        # a POSITIVE ``like`` filter, silently inverting the predicate and
        # returning the complement of the intended rows. (ILIKE, case-insensitive,
        # is handled separately below — it stays on the raw-WHERE preservation
        # path so case-insensitivity is preserved verbatim.)
        operator = "not_like" if _like_is_negated(node) else "like"
        return LogicalFilter(
            _col_name(node.this), operator, _literal_value(node.expression)
        )

    if isinstance(node, exp.Not):
        inner = _unwrap_not_inner(node)
        if isinstance(inner, exp.In):
            # F-003-01 / F-004-01 / F-005-01 / F-102-04 / G-003 / G-004 / G-005:
            # sqlglot 30.8 parses ``col NOT IN (lits)`` as ``Not(In)``. Extract
            # ``not_in`` iff the IN is a bare column over literals. Subquery
            # ``NOT IN (SELECT …)`` has empty In.expressions → not extractable.
            if not _in_is_extractable(inner):
                return None
            return LogicalFilter(
                _col_name(inner.this),
                "not_in",
                [_literal_value(v) for v in inner.expressions],
            )
        if isinstance(inner, exp.Is):
            if _is_null_check_extractable(inner):
                return LogicalFilter(_col_name(inner.this), "is_not_null", None)
            return None
        return None

    if isinstance(node, exp.Is):
        if not _is_null_check_extractable(node):
            return None
        return LogicalFilter(_col_name(node.this), "is_null", None)

    return None


def _extract_filters(select_node: exp.Select | None) -> list[LogicalFilter]:
    """Extract WHERE filters as ``LogicalFilter`` rows.

    Walks ONLY the top-level AND'd conjuncts of the SELECT's WHERE
    clause (no descent into subqueries, function calls, arithmetic, or
    OR subtrees). Each conjunct is translated only if it is faithfully
    representable as ``column op literal`` — any other shape is left
    out, the parser's ``has_unresolvable_where`` flag takes care of
    flagging it, and the rewriter preserves the raw WHERE.

    Bug-102 hardening: the previous implementation used recursive
    ``node.find(Column)`` / ``node.find(Literal)`` which fabricated
    phantom filters from anywhere inside a comparison's subtree —
    e.g. ``(SELECT MAX(x) FROM u) > 100`` would extract ``x > 100``,
    and ``CONCAT('1', a) = '1foo'`` would extract ``a = '1foo'``.
    Phantom filters then contaminated routing, miss-log
    fingerprinting, and pocket matching. Strict per-conjunct
    extraction defuses the landmine."""
    filters: list[LogicalFilter] = []
    if not select_node:
        return filters
    where = select_node.args.get("where")
    if not where:
        return filters
    body = where.this if isinstance(where, exp.Where) else where
    for conjunct in _flatten_top_level_and(body):
        f = _conjunct_to_filter(conjunct)
        if f is not None:
            filters.append(f)
    return filters


def _extract_order_by(
    select_node: exp.Select | None,
) -> tuple[list[tuple[str, str]], bool, set[str]]:
    """Extract ORDER BY as ``(field_name, "asc"|"desc")`` rows.

    Returns ``(order_by, has_unresolvable_order, order_by_alias_names)``.
    F-003-11: a SELECT alias of a non-column (aggregate / expression) is
    recorded in ``order_by_alias_names`` so the aggregate rewriter ORDER BYs
    the output alias. A name that is BOTH an aggregate alias and a bare
    SELECT column is unresolvable (do not shadow grain).
    """
    if not select_node:
        return [], False, set()
    order = select_node.args.get("order")
    if not order:
        return [], False, set()
    select_items = select_node.expressions or []
    expr_aliases: set[str] = set()
    bare_select_names: set[str] = set()
    for item in select_items:
        if isinstance(item, exp.Alias):
            alias = item.alias or ""
            inner = item.this
            while isinstance(inner, exp.Paren):
                inner = inner.this
            if isinstance(inner, exp.Column):
                if alias:
                    bare_select_names.add(alias)
                col = _col_name(inner)
                if col:
                    bare_select_names.add(col)
            elif alias:
                expr_aliases.add(alias)
        elif isinstance(item, exp.Column):
            col = _col_name(item)
            if col:
                bare_select_names.add(col)

    def _in_names(name: str, names: set[str]) -> bool:
        nl = name.lower()
        return any(n.lower() == nl for n in names)

    result: list[tuple[str, str]] = []
    unresolvable = False
    alias_names: set[str] = set()
    for ordered in order.expressions:
        direction = "desc" if ordered.args.get("desc") else "asc"
        key = ordered.this
        if isinstance(key, (exp.Column, exp.Identifier)):
            name = _col_name(key) if isinstance(key, exp.Column) else (key.name or "")
            if not name:
                unresolvable = True
                continue
            if _in_names(name, expr_aliases) and _in_names(name, bare_select_names):
                unresolvable = True
                continue
            if _in_names(name, expr_aliases):
                result.append((name, direction))
                alias_names.add(name)
                continue
            result.append((name, direction))
            continue
        if isinstance(key, exp.Literal) and key.is_int:
            pos = int(key.this) - 1
            if 0 <= pos < len(select_items):
                item = select_items[pos]
                inner = item.this if isinstance(item, exp.Alias) else item
                while isinstance(inner, exp.Paren):
                    inner = inner.this
                if isinstance(inner, exp.Column):
                    result.append((_col_name(inner), direction))
                    continue
                if isinstance(item, exp.Alias) and item.alias:
                    result.append((item.alias, direction))
                    alias_names.add(item.alias)
                    continue
            unresolvable = True
            continue
        unresolvable = True
    return result, unresolvable, alias_names


def _extract_limit(select_node: exp.Select | None) -> int | None:
    if not select_node:
        return None
    limit = select_node.args.get("limit")
    if limit is None:
        return None
    # ANSI ``FETCH FIRST/NEXT n ROWS ONLY`` (Bug-6083 / F-003-17): sqlglot
    # parses this into an ``exp.Fetch`` node under ``args["limit"]`` — the row
    # count is in ``args["count"]`` and ``limit.expression`` is absent, so the
    # plain ``Limit`` path below would silently return None and hand back an
    # UNBOUNDED result where the user asked for n rows. ``FETCH FIRST n ROWS
    # ONLY`` is exactly ``LIMIT n``; map it. ``WITH TIES`` and ``PERCENT``
    # change the row semantics (they can return more/fewer than n rows) and
    # are not representable as a plain LIMIT — reject them loudly rather than
    # emitting a wrong bound.
    if isinstance(limit, exp.Fetch):
        options = limit.args.get("limit_options")
        with_ties = bool(getattr(options, "args", {}).get("with_ties")) if options else bool(limit.args.get("with_ties"))
        percent = bool(getattr(options, "args", {}).get("percent")) if options else bool(limit.args.get("percent"))
        if with_ties or percent:
            variant = "WITH TIES" if with_ties else "PERCENT"
            raise UnsupportedSQL(
                f"FETCH FIRST ... {variant} is not supported; use FETCH FIRST n "
                f"ROWS ONLY or LIMIT n"
            )
        count = limit.args.get("count")
        # ``FETCH FIRST ROW ONLY`` (no explicit count) means one row — the
        # ANSI/Postgres default; sqlglot leaves ``count`` absent.
        if count is None:
            return 1
        # The count must be a plain integer literal. A decimal (``2.5``), a bind
        # parameter (``$1``), or an arithmetic expression is NOT representable as
        # a plain ``LIMIT n`` — fail CLOSED with a typed error rather than
        # silently returning None (unbounded rows) or raising an uncaught
        # TypeError (500). (Bug-6083 hardening.)
        if isinstance(count, exp.Literal) and not count.is_string and str(count.this).isdigit():
            return int(count.this)
        raise UnsupportedSQL(
            "FETCH FIRST/NEXT requires a literal integer row count; use "
            "FETCH FIRST n ROWS ONLY or LIMIT n"
        )
    if limit.expression is not None:
        expr = limit.expression
        # A plain integer literal is the only form representable as ``LIMIT n``.
        if (
            isinstance(expr, exp.Literal)
            and not expr.is_string
            and str(expr.this).isdigit()
        ):
            return int(expr.this)
        # ``LIMIT ALL`` (Postgres) is an explicit request for unbounded rows;
        # sqlglot parses ``ALL`` as a bare column reference. Treat as no limit.
        if isinstance(expr, exp.Column) and (expr.name or "").upper() == "ALL":
            return None
        # ``LIMIT NULL`` (Postgres) is also an explicit request for no limit.
        if isinstance(expr, exp.Null):
            return None
        # Bug-6569: a decimal (``2.5``), quoted string, bind parameter (``$1``),
        # or arithmetic expression is NOT representable as a plain ``LIMIT n``.
        # The old ``int(limit.expression.this)`` silently returned None for a
        # decimal (ValueError swallowed -> UNBOUNDED where the user asked to
        # cap rows) and raised an uncaught TypeError (500) for a parameter /
        # ALL. Fail CLOSED with a typed error, mirroring the FETCH branch
        # above (Bug-6083 hardening).
        raise UnsupportedSQL(
            "LIMIT requires a literal integer row count; use LIMIT n "
            "(or LIMIT ALL for no limit)"
        )
    return None


def _extract_offset(select_node: exp.Select | None) -> int | None:
    """Extract a literal integer OFFSET from the SELECT node.

    Bug-6661: mirrors the LIMIT branch's fail-closed hardening. A non-integer
    OFFSET (decimal ``2.5``, bind parameter ``$1``, expression) must raise
    ``UnsupportedSQL`` instead of silently returning None (no offset) —
    ``LIMIT 100 OFFSET 2.5`` would otherwise execute with NO offset.
    """
    if not select_node:
        return None
    offset = select_node.args.get("offset")
    if not offset:
        return None
    expr = getattr(offset, "expression", None)
    if expr is None:
        return None
    # A plain integer literal is the only form representable as OFFSET n.
    if (
        isinstance(expr, exp.Literal)
        and not expr.is_string
        and str(expr.this).isdigit()
    ):
        return int(expr.this)
    # Bug-6661: anything else (decimal, parameter, expression, string) is
    # not representable as a plain OFFSET n — fail closed, matching LIMIT.
    raise UnsupportedSQL(
        "OFFSET requires a literal integer; use OFFSET n"
    )


def _extract_expression_occurrences(
    select_node: exp.Select | None, dialect: str,
) -> list[ExpressionOccurrence]:
    """Capture non-column expression occurrences for derived-grain routing.

    Spec §5.1 / §11.2 / §18: capture the AST nodes for GROUP BY, relevant SELECT,
    WHERE, HAVING, and ORDER roles DURING the parser's traversal — never re-parse
    GROUP BY from ``raw_query`` later in the matcher. This is purely additive and
    diagnostic in Phase 1: it does not change ``has_function_grain`` or any route.
    For an ordinary query (no non-column expressions in these roles) it returns an
    empty list, so the query fingerprint and every downstream consumer are
    byte-identical to pre-feature behaviour.

    Only shape is captured here (raw SQL text + sqlglot dump + role + alias). The
    binder later binds each occurrence's lineage against the deployed snapshot and
    canonicalises it; the parser must not depend on the semantic model.
    """
    if select_node is None:
        return []

    occurrences: list[ExpressionOccurrence] = []
    counter = 0

    def _add(node: exp.Expression, role: str, alias: str | None) -> None:
        nonlocal counter
        # A bare column or a transparent paren-wrapped bare column is ordinary
        # grain, not a derived expression — skip it. A positional literal in
        # GROUP BY is resolved elsewhere; skip literals here too.
        inner = node.this if isinstance(node, exp.Alias) else node
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if inner is None or isinstance(inner, (exp.Column, exp.Literal, exp.Star)):
            return
        # A derived GROUP-KEY candidate is a pure scalar expression over row
        # columns. Any expression that CONTAINS an aggregate anywhere in its
        # subtree (e.g. the composable ``SUM(rev) / SUM(cost)`` or a
        # ``CASE WHEN SUM(b)=0 …`` SELECT item) is a measure / composable
        # aggregate handled by the existing measure path, NOT a derived group
        # key. Capturing it here would perturb the query fingerprint of an
        # ordinary aggregate-routable query and break the byte-identical additive
        # path (spec I10). Skip any aggregate-bearing expression.
        if inner.find(exp.AggFunc) is not None:
            return
        counter += 1
        occurrences.append(
            ExpressionOccurrence(
                occurrence_id=f"occ{counter}",
                role=role,
                raw_sql=inner.sql(dialect=dialect),
                input_dialect=dialect,
                ast_json=inner.dump(),
                output_alias=alias,
            )
        )

    _select_items = select_node.expressions or []

    # Build a SELECT output-alias -> expression map so a GROUP BY / ORDER BY that
    # references a derived SELECT expression BY ALIAS (``SELECT DATE_TRUNC(...) AS
    # m … GROUP BY m``) or BY ORDINAL (``… GROUP BY 1``) still records the
    # expression under its GROUP_KEY / ORDER_KEY role. Without this, an aliased or
    # positional reference would be a bare Column / Literal at the GROUP/ORDER
    # node and be skipped, leaving the proof stages blind to the expression's
    # actual role (spec §5.1 / §14.4: "GROUP BY expression … referenced by
    # alias/ordinal").
    _alias_to_select_expr: dict[str, exp.Expression] = {}
    for sitem in _select_items:
        if isinstance(sitem, exp.Alias):
            _alias_to_select_expr[sitem.alias_or_name.lower()] = sitem.this

    def _resolve_reference(node: exp.Expression) -> exp.Expression:
        """Resolve a bare-column alias ref or a positional ordinal to the
        underlying SELECT expression; otherwise return the node unchanged."""
        probe = node.this if isinstance(node, exp.Alias) else node
        while isinstance(probe, exp.Paren):
            probe = probe.this
        if isinstance(probe, exp.Column) and not probe.table:
            target = _alias_to_select_expr.get(probe.name.lower())
            if target is not None:
                return target
        if isinstance(probe, exp.Literal) and probe.is_int:
            pos = int(probe.this) - 1
            if 0 <= pos < len(_select_items):
                item = _select_items[pos]
                return item.this if isinstance(item, exp.Alias) else item
        return node

    # GROUP BY expressions (the primary derived-grain source).
    _group = select_node.args.get("group")
    if _group:
        for gexpr in _group.expressions:
            _add(_resolve_reference(gexpr), "GROUP_KEY", None)

    # SELECT expressions that are non-column scalar expressions (not aggregates).
    for sexpr in _select_items:
        alias = sexpr.alias_or_name if isinstance(sexpr, exp.Alias) else None
        inner = sexpr.this if isinstance(sexpr, exp.Alias) else sexpr
        # Skip plain aggregate SELECT items — they are measures, handled by the
        # existing measure path, not derived group keys.
        if isinstance(inner, exp.AggFunc):
            continue
        _add(sexpr, "SELECT", alias)

    # ORDER BY keys, resolving alias/ordinal references to the SELECT expression.
    # (WHERE_LEFT/WHERE_RIGHT and HAVING_KEY predicate capture are deliberately
    # deferred to the phase that introduces derived-predicate movement — spec
    # §8.5; those roles are declared in EXPRESSION_ROLES but the parser does not
    # yet emit them, so nothing depends on partial capture here.)
    _order = select_node.args.get("order")
    if _order is not None:
        for oexpr in _order.expressions:
            key = oexpr.this if isinstance(oexpr, exp.Ordered) else oexpr
            _add(_resolve_reference(key), "ORDER_KEY", None)

    return occurrences


def _occurrence_fingerprints(
    occurrences: list[ExpressionOccurrence],
) -> list[str]:
    """Role-tagged canonical expression fingerprints for the query-shape hash.

    Spec I11: two queries differing only in their inline expression must hash
    distinctly. Each entry is ``<role>:<canonical-fingerprint>`` so an expression
    in different roles does not collide — e.g. ``SELECT UPPER(region)`` and
    ``… ORDER BY UPPER(region)`` produce different shapes because projection vs
    sort semantics differ (spec §10.1: the shape fingerprint includes projection
    roles and derived predicate shapes). Occurrence order is preserved (position
    carries meaning). An expression that fails to canonicalise contributes its
    raw text so it still perturbs the shape rather than silently colliding.
    """
    from shared.semantic.derived_expression import canonicalise_sql

    fps: list[str] = []
    for occ in occurrences:
        ce = canonicalise_sql(occ.raw_sql, input_dialect=occ.input_dialect)
        core = ce.fingerprint if ce is not None else f"raw:{occ.raw_sql}"
        fps.append(f"{occ.role}:{core}")
    return fps


def _compute_fingerprint(
    measures: list[str], dimensions: list[str], grain: list[str],
    filters: list[LogicalFilter], having_columns: list[str] | None = None,
    expr_fingerprints: list[str] | None = None,
) -> str:
    return fingerprint_shape(
        measures=measures,
        dimensions=dimensions,
        grain=grain,
        filter_cols=[f.dimension_name for f in filters],
        having_cols=having_columns or [],
        expr_fingerprints=expr_fingerprints,
    )


def _extract_literal_value(literal: exp.Expression) -> str:
    """Extract a display string from a literal AST node."""
    if isinstance(literal, exp.Star):
        return "*"
    if isinstance(literal, exp.Boolean):
        return str(literal.this).lower()
    if isinstance(literal, exp.Neg):
        inner = literal.this
        return f"-{inner.this}" if hasattr(inner, "this") else str(literal)
    return str(literal.this if hasattr(literal, "this") else literal)


def _col_name(col: exp.Column) -> str:
    """Return the unqualified column name."""
    return col.name or str(col)


class RawSQL(str):
    """Marker subclass so _render_value can emit SQL expressions without quoting."""
    pass


class NumericLiteral(float):
    """A numeric filter value that REMEMBERS its original SQL spelling.

    Bug-5539 (Codex round-3 finding 3): the extracted-filter path coerces a
    numeric SQL literal to a Python ``float`` via ``float(text)``. Once the
    spelling is gone, ``value_is_numeric_literal`` accepts ANY finite float, so a
    scientific/signed source token (``int_dim = 1e9``) silently launders into a
    bare ``1000000000.0`` token against a numeric DIMENSION filter — the exact
    shape the strict grammar exists to reject on the quoted-string path.

    The fix preserves the ORIGINAL text alongside the float value:
      - It IS a ``float`` (``isinstance(value, float)`` and ``value == 1e5`` both
        hold), so F-003-10's scientific-literal parse round-trip is NOT regressed
        — ``WHERE b > 1e5`` still yields a numeric float filter value, and a
        ``>`` comparison against a measure transpiles to a bare numeric for
        strict-typed targets exactly as before.
      - It carries ``.original_text`` so the SAME strict grammar
        (``value_is_numeric_literal``) can validate the original spelling at
        render time. A scientific/leading-plus form is then rejected before it
        can emit a bare token against a numeric column — closing the laundering
        gap without losing the spelling to ``float()``.

    Plain integers keep returning a bare ``int`` (no spelling ambiguity: the
    canonical ``str(int)`` always passes the strict grammar), so only the
    decimal/scientific branch needs this wrapper.
    """

    original_text: str

    def __new__(cls, value: float, original_text: str) -> "NumericLiteral":
        obj = super().__new__(cls, value)
        obj.original_text = original_text
        return obj


def _literal_value(lit: Any) -> Any:
    if lit is None:
        return None
    if isinstance(lit, exp.Literal):
        if lit.is_number:
            text = lit.this
            try:
                # Plain integer (no decimal point and no exponent).
                if "." not in text and "e" not in text.lower():
                    return int(text)
                # Decimal or scientific notation (e.g. ``1e5``, ``2.5E-3``).
                # F-003-10: previously ``int("1e5")`` raised and the raw
                # string was returned, so strict-typed targets (BigQuery)
                # got a quoted string for a numeric comparison. Parse the
                # full float grammar — Python's float() handles exponents.
                #
                # Bug-5539 (Codex round-3 finding 3): wrap the float in a
                # ``NumericLiteral`` that remembers ``text`` so the original
                # spelling survives parsing. ``value_is_numeric_literal`` then
                # validates the ORIGINAL token at render time — a scientific /
                # leading-plus form (``1e9``) is rejected as a bare token
                # against a numeric column instead of being laundered to
                # ``1000000000.0``. The value still IS a float, so F-003-10's
                # parse round-trip (``b > 1e5`` -> finite float) is unchanged.
                return NumericLiteral(float(text), text)
            except ValueError:
                return text
        return lit.this
    # For non-Literal expressions (Cast, typed literals, etc.) return a
    # RawSQL marker so the rewriter renders them as SQL, not as quoted strings.
    return RawSQL(lit.sql(dialect="postgres"))


def _dedup(lst: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in lst:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _parse_with_errors(
    raw_sql: str, dialect: str = "postgres",
) -> tuple[exp.Expression, list[Exception]]:
    """Parse with WARN tolerance but return both the tree and any errors
    sqlglot recovered from.

    ``sqlglot.parse_one`` discards the Parser instance, so we drive the
    dialect's parser directly to keep access to ``parser.errors``.
    JDBC callers escalate any error to a hard failure via the
    ``sqlglot_errors`` check at the call site. XMLA/DAX callers also
    escalate errors (Bug-7916 / Codex gate) so a recovered
    meaning-changed tree is never silently routed.
    """
    sg_dialect = sqlglot.Dialect.get_or_raise(dialect)
    tokens = sg_dialect.tokenizer_class().tokenize(raw_sql)
    parser = sg_dialect.parser(error_level=sqlglot.ErrorLevel.WARN)
    trees = parser.parse(tokens, sql=raw_sql)
    # Bug-7916 / Codex gate R2: reject multi-statement input. A single
    # gateway query must be exactly one statement; silently taking trees[0]
    # and dropping the rest is meaning-changing truncation.
    _valid_trees = [t for t in (trees or []) if t is not None]
    if len(_valid_trees) > 1:
        raise SyntaxErrorInSQL(
            "Multi-statement SQL is not supported: only a single SELECT "
            "statement is allowed per query. Separate statements with "
            "individual query calls."
        )
    tree = trees[0] if trees else None
    if tree is None:
        # Fall back to parse_one so the existing error message format is
        # preserved when sqlglot cannot produce any tree at all.
        tree = sqlglot.parse_one(
            raw_sql, read=dialect, error_level=sqlglot.ErrorLevel.WARN,
        )
    return tree, list(parser.errors)


def _try_flatten_identity_derived_table(tree: exp.Expression) -> exp.Expression:
    """Flatten trivial ``SELECT * FROM (subquery)`` wrappers.

    Two cases are handled:

    Case 1 — *star-over-star*: ``SELECT <cols> FROM (SELECT * FROM T [WHERE p]) q``
    The inner subquery is an identity projection. Replace the subquery with
    the inner FROM table and AND inner WHERE onto the outer WHERE.

    Case 2 — *star-over-anything*: ``SELECT * FROM (SELECT expr... FROM T ...) q``
    The outer query is a pure ``SELECT *`` with no WHERE, GROUP BY, ORDER BY,
    JOINs, HAVING, LIMIT, or OFFSET.  ``SELECT *`` over a subquery is an
    identity operation — replace the entire tree with the inner query.

    Returns a new tree when flattening applies, the original tree otherwise.
    """
    if not isinstance(tree, exp.Select):
        return tree
    from_clause = tree.args.get("from_")
    if not from_clause:
        return tree
    if tree.args.get("joins"):
        return tree
    source = from_clause.this
    if not isinstance(source, exp.Subquery):
        return tree
    inner = source.this
    if not isinstance(inner, exp.Select):
        return tree
    if isinstance(inner, (exp.Union, exp.Intersect, exp.Except)):
        return tree

    # ------------------------------------------------------------------
    # Case 2: outer is pure SELECT * with no clauses → replace with inner
    # ------------------------------------------------------------------
    outer_is_pure_star = (
        _has_select_star(tree)
        and not tree.args.get("where")
        and not tree.args.get("group")
        and not tree.args.get("having")
        and not tree.args.get("distinct")
        and not tree.args.get("order")
        and not tree.args.get("limit")
        and not tree.args.get("offset")
    )
    if outer_is_pure_star and not _has_select_star(inner):
        # The outer SELECT * is a no-op wrapper. Return the inner query.
        return inner.copy()

    # ------------------------------------------------------------------
    # Case 1: inner is SELECT * from single table, no grouping etc.
    # ------------------------------------------------------------------
    if not _has_select_star(inner):
        return tree
    # Inner must be a pure slice: no GROUP/HAVING/DISTINCT/ORDER/LIMIT/OFFSET/JOINs
    for k in ("group", "having", "distinct", "order", "limit", "offset", "joins"):
        if inner.args.get(k):
            return tree
    inner_from = inner.args.get("from_")
    if not inner_from or not isinstance(inner_from.this, exp.Table):
        return tree
    if inner_from.this.find(exp.Subquery):
        return tree

    new_tree = tree.copy()
    new_from = new_tree.args.get("from_")
    new_from.set("this", inner_from.this.copy())

    outer_where = new_tree.args.get("where")
    inner_where = inner.args.get("where")
    if inner_where is not None and outer_where is not None:
        combined = exp.And(
            this=inner_where.this.copy(),
            expression=outer_where.this.copy(),
        )
        new_tree.set("where", exp.Where(this=combined))
    elif inner_where is not None:
        new_tree.set("where", inner_where.copy())
    # else: outer_where stays as-is (or None)
    return new_tree


def _detect_complex_sql(tree: exp.Expression) -> bool:
    """Detect SQL constructs that the source rewriter cannot safely reconstruct.

    Returns True if the query contains any of:
    - CTEs (WITH clauses)
    - Derived tables (FROM (subquery) alias)
    - Window functions (OVER clause)
    - Scalar subqueries in SELECT list
    - Subqueries in WHERE (scalar comparison, IN, NOT IN, EXISTS)
    - UNION / INTERSECT / EXCEPT

    When True, the query should go through passthrough-with-table-substitution
    rather than the full _build_source_sql rewrite.
    """
    # CTEs
    if tree.find(exp.With):
        return True

    select_node = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not select_node:
        return False

    # UNION / INTERSECT / EXCEPT
    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        return True

    # Derived tables in FROM: FROM (subquery) alias
    from_clause = select_node.args.get("from_")
    if from_clause:
        for node in from_clause.find_all(exp.Subquery):
            return True

    # F-003-02: user-authored JOINs and comma (implicit) joins.
    #
    # The IR has no join representation and the semantic source rewriter
    # reconstructs the FROM clause from the MODEL's own join graph
    # (``_build_joined_from_clause``) for a single virtual table — it cannot
    # honour an authored ``JOIN ... ON`` or a comma cross-join. A self-join
    # (``modelx a JOIN modelx b ON ...``) also collapses under the
    # ``from_tables`` dedup, defeating the aggregate matcher's MULTI_TABLE
    # guard, so it could match an aggregate and return ``SUM(__row_count)``
    # instead of the join cardinality — a silent wrong result.
    #
    # Treat any authored join as complex SQL: the query routes through
    # passthrough-with-table-substitution, which rewrites EACH model-table
    # reference (FROM and JOIN) to the same physical table while preserving
    # the join structure, aliases, and ON clause verbatim. Persona-scoped
    # callers hit the fail-closed ``PERSONA_COMPLEX_SQL_NOT_ALLOWED`` gate.
    #
    # Explicit JOIN nodes hang off the top-level SELECT's ``joins`` arg.
    if select_node.args.get("joins"):
        return True
    # Comma (implicit) join: more than one Table directly under the top-level
    # FROM (e.g. ``FROM modelx a, modelx b``). sqlglot models the first source
    # as ``from_.this`` and the rest as additional expressions on the From
    # node, so a multi-source comma FROM surfaces as >1 Table among the From's
    # direct expressions.
    if from_clause is not None:
        from_tables_direct = [
            e for e in [from_clause.this, *from_clause.expressions]
            if isinstance(e, exp.Table)
        ]
        if len(from_tables_direct) > 1:
            return True

    # Window functions: any expression with OVER
    for node in select_node.find_all(exp.Window):
        return True

    # FILTER clauses: COUNT(*) FILTER (WHERE ...)
    for node in select_node.find_all(exp.Filter):
        return True

    # WITHIN GROUP: PERCENTILE_CONT(...) WITHIN GROUP (ORDER BY ...).
    # Bug-6969/5891: the ONE safe ordered-set percentile shape
    # (PERCENTILE_CONT/DISC(literal) WITHIN GROUP (ORDER BY single-column)) is
    # routable and must NOT be flagged complex, or the binder would clear the
    # resolved measures and the pNN column could never be reached (the exact
    # F-003-07 dead-code trap). Every OTHER WITHIN GROUP shape (multi-column or
    # expression order key, non-literal fraction, other ordered-set aggregate)
    # is still complex -> source. One shared recogniser governs both this gate
    # and the SELECT-item classifier so they can never disagree.
    #
    # Fable R1 MEDIUM (I1 single-inventory): the exemption is restricted to a
    # WITHIN GROUP that is a DIRECT SELECT-list projection — the only position
    # the binder's QuantileRequest inventory covers. A recognised ordered-set
    # percentile in HAVING, ORDER BY, or nested inside a larger expression
    # (``PERCENTILE_CONT(...)/100``) stays COMPLEX -> source, so it can never be
    # served from an artifact without being inventoried (which would violate the
    # exact-grain obligation). This keeps the "if any quantile occurrence cannot
    # be inventoried, disable artifact routing for the whole query" rule true by
    # construction rather than relying on downstream gates.
    _select_projection_within_groups: set[int] = set()
    for _sexpr in select_node.expressions:
        _proj = _sexpr.this if isinstance(_sexpr, exp.Alias) else _sexpr
        while isinstance(_proj, exp.Paren):
            _proj = _proj.this
        if isinstance(_proj, exp.WithinGroup):
            _select_projection_within_groups.add(id(_proj))
    for node in select_node.find_all(exp.WithinGroup):
        # A SELECT-projection ordered-set percentile is exempt only if it is the
        # safe shape; any WITHIN GROUP elsewhere (HAVING/ORDER/nested) is complex.
        if id(node) in _select_projection_within_groups:
            if recognize_ordered_set_percentile(node) is None:
                return True
        else:
            return True

    # GROUPING SETS / ROLLUP / CUBE
    group = select_node.args.get("group")
    if group:
        for node in group.find_all(exp.Expression):
            if isinstance(node, (exp.Cube, exp.Rollup)):
                return True
            if type(node).__name__ == "GroupingSets":
                return True

    # VALUES clause (e.g. VALUES (1,'a'), (2,'b') used as a table)
    if tree.find(exp.Values):
        return True

    # LATERAL joins
    if select_node.find(exp.Lateral):
        return True

    # UNNEST / table-valued functions used as a FROM source.  These are not
    # model tables and cannot be reconstructed by the semantic source rewriter,
    # so they must route through passthrough-with-table-substitution like the
    # other table-valued constructs above (Bug-923).
    if tree.find(exp.Unnest):
        return True

    # Unrecognised aggregate functions: any AggFunc not in the registry
    # (e.g. STRING_AGG, BOOL_OR, BOOL_AND, PERCENTILE_CONT).
    for expr in select_node.expressions:
        inner = expr.this if isinstance(expr, exp.Alias) else expr
        if isinstance(inner, exp.AggFunc):
            func_key = inner.key.lower() if hasattr(inner, "key") else ""
            # Dispersion stats parse to keys (stddevsamp/variance/...) that differ
            # from the registry's canonical names — normalise before the lookup so
            # STDDEV/VAR are recognised (routable), not flagged complex.
            func_key = stat_type_for_sqlglot_key(func_key) or func_key
            if not get_aggregate_func(func_key):
                return True

    # Subqueries anywhere in the SELECT expressions
    for expr in select_node.expressions:
        if list(expr.find_all(exp.Subquery)):
            return True

    # Subqueries in WHERE (including EXISTS which wraps Select directly)
    where = select_node.args.get("where")
    if where:
        if list(where.find_all(exp.Subquery)):
            return True
        if list(where.find_all(exp.Exists)):
            return True
        if list(where.find_all(exp.In)):
            # IN (SELECT ...) — check if any In node has a subquery
            for in_node in where.find_all(exp.In):
                if list(in_node.find_all(exp.Select)):
                    return True
        # ALL (SELECT ...) / ANY (SELECT ...) — sqlglot wraps the Select
        # inside exp.All / exp.Any without a Subquery node.
        for cls in (exp.All, getattr(exp, "Any", None)):
            if cls is None:
                continue
            for node in where.find_all(cls):
                if list(node.find_all(exp.Select)):
                    return True

    return False


def _extract_having(select_node: exp.Select | None) -> tuple[str | None, list[str]]:
    """Extract the HAVING clause raw SQL and the column names it references.
    Returns (having_raw, having_columns)."""
    if not select_node:
        return None, []
    having = select_node.args.get("having")
    if not having:
        return None, []
    # Round-trip the HAVING text via Postgres syntax — the rewriter's
    # re-parse uses ``read="postgres"`` and the eventual emit target is
    # Postgres, so this avoids dialect drift between the two halves.
    having_raw = having.sql(dialect="postgres")
    cols: list[str] = []
    for col in having.find_all(exp.Column):
        name = _col_name(col)
        if name and name not in cols:
            cols.append(name)
    return having_raw, cols


_LITERAL_NODES: tuple[type, ...] = (exp.Literal, exp.Boolean, exp.Null)


def _is_bare_column(node: Any) -> bool:
    return isinstance(node, exp.Column)


def _is_safe_literal(node: Any) -> bool:
    if isinstance(node, _LITERAL_NODES):
        return True
    if isinstance(node, exp.Cast) and isinstance(node.this, _LITERAL_NODES):
        return True
    return False


def _comparison_is_extractable(node: exp.Expression) -> bool:
    """``EQ/NEQ/GT/GTE/LT/LTE`` is safely extractable iff exactly one
    side is a bare ``Column`` and the other side is a literal. Anything
    else (function calls, arithmetic, column-vs-column, subqueries) is
    not representable as a ``LogicalFilter``."""
    lhs = node.this
    rhs = node.expression
    if lhs is None or rhs is None:
        return False
    if _is_bare_column(lhs) and _is_safe_literal(rhs):
        return True
    if _is_bare_column(rhs) and _is_safe_literal(lhs):
        return True
    return False


def _in_is_extractable(node: exp.In) -> bool:
    """``col IN (lit, lit, ...)`` only — wrapped column, mixed values, or
    subquery RHS all break the LogicalFilter representation."""
    if not _is_bare_column(node.this):
        return False
    values = node.expressions or []
    if not values:
        return False
    return all(_is_safe_literal(v) for v in values)


def _between_is_extractable(node: exp.Between) -> bool:
    if not _is_bare_column(node.this):
        return False
    low = node.args.get("low")
    high = node.args.get("high")
    return _is_safe_literal(low) and _is_safe_literal(high)


def _in_is_negated(node: exp.In) -> bool:
    """True when an ``In`` node itself carries ``negate=True``.

    F-003-01: sqlglot 30.8 emits ``Not(In)`` for ``NOT IN`` (handled in
    ``_conjunct_to_filter``). Some dialects / versions may instead set
    ``In.negate``. Reading that flag keeps polarity correct either way.
    """
    return bool(node.args.get("negate") or getattr(node, "negate", False))


def _like_is_negated(node: exp.Like) -> bool:
    """True when a ``Like`` node represents ``NOT LIKE``.

    Bug-5326 (F-P4ac-01): sqlglot 30.8 collapses ``col NOT LIKE '%x%'`` into a
    single ``exp.Like`` carrying ``negate=True`` rather than wrapping a positive
    ``Like`` in ``exp.Not``. Reading that attribute is the only way to recover
    the polarity from this node shape. ``getattr``/``args.get`` is used so the
    check is robust across sqlglot versions that omit the attribute (older
    versions emit ``Not(Like(...))`` instead, handled by the ``Not`` branch)."""
    return bool(node.args.get("negate") or getattr(node, "negate", False))


def _like_is_extractable(node: exp.Like) -> bool:
    return _is_bare_column(node.this) and _is_safe_literal(node.expression)


def _is_null_check_extractable(node: exp.Is) -> bool:
    return _is_bare_column(node.this) and isinstance(node.expression, exp.Null)


def _has_unresolvable_where(select_node: exp.Select | None) -> bool:
    """Return True when *any* top-level WHERE conjunct is something the IR's
    ``LogicalFilter`` cannot faithfully represent.

    The filter extractor in this module is permissive: ``_extract_filters``
    walks the top-level AND'd conjuncts and translates each via
    ``_conjunct_to_filter``, silently dropping any conjunct that returns None.
    That lossy behaviour is safe ONLY when the rewriter has been told to
    preserve the raw WHERE instead. This audit decides which path the
    rewriter takes.

    Design (Bug-6081 / F-003-15): the audit FAILS CLOSED against the exact
    same decision function the extractor uses — a conjunct is unresolvable
    iff ``_conjunct_to_filter`` cannot represent it. There is a single source
    of truth, so a new WHERE shape can never be extractable-but-unflagged (a
    bare boolean column ``WHERE is_active``, ``WHERE FALSE``, or a boolean
    literal) or flagged-but-extracted. This replaces the previous
    enumerated-blacklist walk, whose omission of bare Column/Boolean/Literal
    conjuncts shipped a silent predicate drop (the third instance of the
    Bug-102/Bug-5110/Bug-5333 class).

    Note the audit operates on the SAME top-level conjuncts as extraction, so
    it neither descends into an extractable comparison's operands nor into OR
    subtrees: an OR (or any non-AND top-level node) is itself one conjunct and
    is faithfully unrepresentable, so it is flagged as a whole.
    """
    if not select_node:
        return False
    where = select_node.args.get("where")
    if not where:
        return False
    body = where.this if isinstance(where, exp.Where) else where

    unresolvable: list[str] = []
    for conjunct in _flatten_top_level_and(body):
        if _conjunct_to_filter(conjunct) is None:
            reason = type(conjunct).__name__.lower()
            if reason not in unresolvable:
                unresolvable.append(reason)

    if unresolvable:
        logger.warning(
            "WHERE conjunct(s) not representable as LogicalFilter — "
            "routing through raw-WHERE preservation. Shapes: %s",
            ",".join(sorted(unresolvable)),
        )
        return True
    return False


def _extract_cte_aliases(tree: exp.Expression) -> list[str]:
    """Extract CTE alias names from WITH clauses."""
    aliases: list[str] = []
    with_node = tree.find(exp.With)
    if with_node:
        for cte in with_node.expressions:
            if isinstance(cte, exp.CTE) and cte.alias:
                aliases.append(cte.alias)
    return aliases


def _extract_from_tables(node: exp.Expression | None) -> list[str]:
    """Extract table names from FROM, JOIN, and subquery clauses.

    Accepts the FULL parse tree (which may be a ``Union`` / ``Intersect``
    / ``Except`` for set-operation queries) so that tables in ALL branches
    are captured for the binder's allow-list (Bug-6958).  Also scans
    subqueries (EXISTS, IN, scalar) within each branch.
    """
    tables: list[str] = []
    if not node:
        return tables
    # Scan all Table nodes in the entire AST (including subqueries and
    # all set-operation branches).
    for t in node.find_all(exp.Table):
        if t.name and t.name not in tables:
            tables.append(t.name)
    return tables


def _detect_raw_syntax_errors(raw_sql: str) -> list[str]:
    """Pattern-based syntax checks that run on the raw SQL string.

    Surfaces cases that sqlglot would silently recover from by dropping
    tokens — consecutive commas and stray semicolons (which truncate
    parsing to the first statement). The friendly message produced here
    takes precedence over the generic ``Malformed SQL`` fallback for
    JDBC, so the user sees a specific diagnosis.
    """
    import re
    warnings: list[str] = []

    # Strip string literals and SQL comments first so neither the comma nor
    # the semicolon check fires on a comma/semicolon that lives INSIDE a
    # string literal or comment.  F-003-05: previously the consecutive-comma
    # regex ran against the raw SQL, so a valid query like
    # ``WHERE note = 'wait,, what'`` was wrongly rejected — real Postgres
    # accepts it.  Both checks now share the same literal-/comment-stripped
    # text the semicolon check already used.
    _noliterals = re.sub(r"'(?:''|[^'])*'", "''", raw_sql)
    _nocomments = re.sub(r"--[^\n]*", "", _noliterals)
    _nocomments = re.sub(r"/\*.*?\*/", "", _nocomments, flags=re.DOTALL)

    if re.search(r",\s*,", _nocomments):
        warnings.append("Consecutive commas detected in SQL")

    # Stray semicolons: if the SQL contains a semicolon before the end,
    # parse_one only sees the first statement.  We use the same comment-/
    # literal-stripped text so semicolons inside them do not trigger a
    # false positive.  Ignore a single trailing semicolon followed only
    # by comments/whitespace (psql/DBeaver habit).
    _stripped = _nocomments.strip().rstrip(";").strip()
    if ";" in _stripped:
        warnings.append(
            "Stray semicolon detected — query may be truncated. "
            "Only the first statement is parsed."
        )
    return warnings


def _detect_grammar_syntax_warnings(
    *,
    measures: list[str] | None = None,
    grain: list[str] | None = None,
    select_bare: list[str] | None = None,
) -> list[str]:
    """Warnings that require a parsed tree (GROUP BY completeness, etc.).

    For JDBC the caller raises ``GroupByError`` before this runs — this
    function exists so XMLA/DAX traces still surface the issue as a
    warning without failing the query.
    """
    warnings: list[str] = []
    if grain is not None and select_bare is not None and measures is not None:
        if measures and select_bare:
            grain_set = set(grain)
            ungrouped = [c for c in select_bare if c not in grain_set]
            if ungrouped:
                warnings.append(
                    f"Columns not in GROUP BY: {', '.join(ungrouped)}"
                )
    return warnings
