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
from src.ir.logical_query import LogicalFilter, LogicalQuery, SelectExpression
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
}


def _normalize_dialect(dialect: str | None) -> str:
    if not dialect:
        return "postgres"
    return _DIALECT_ALIASES.get(dialect.lower(), dialect.lower())


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
    API/wire form (``"postgresql"``, ``"jdbc"``, ``"bigquery"``, ``"spark"``)
    or sqlglot's canonical name (``"postgres"``, ``"bigquery"``…). Unknown
    values pass through to sqlglot unchanged. Defaults to Postgres — the
    canonical internal dialect for this stack.
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
    if protocol == "jdbc":
        for w in raw_syntax_warnings:
            raise SyntaxErrorInSQL(w)

    tree, sqlglot_errors = _parse_with_errors(raw_sql, dialect=dialect)

    # Strict syntax enforcement for JDBC: any remaining sqlglot parse
    # error (token dropped during recovery) means real Postgres would
    # have rejected the input — raise rather than silently routing a
    # rewritten query.  XMLA/DAX callers keep the recoverable behaviour.
    if protocol == "jdbc" and sqlglot_errors:
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

    measures, dimensions, grain, select_expressions, bare_true = _extract_columns(select_node)
    filters = _extract_filters(select_node)
    order_by, has_unresolvable_order = _extract_order_by(select_node)
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
    has_function_grain = False
    _group = select_node.args.get("group") if select_node else None
    if _group:
        for _gexpr in _group.expressions:
            _ginner = _gexpr.this if isinstance(_gexpr, exp.Alias) else _gexpr
            if not isinstance(_ginner, (exp.Column, exp.Literal)):
                has_function_grain = True
                break

    # Detect complex SQL constructs that the source rewriter cannot safely
    # reconstruct: CTEs, derived tables (FROM subquery), window functions,
    # scalar subqueries in SELECT, correlated subqueries.  These queries
    # should go through passthrough-with-table-substitution.
    has_complex_sql = _detect_complex_sql(tree) or _distinct_on
    windows = list(tree.find_all(exp.Window))
    has_window_functions = bool(windows)
    has_window_aggregate = any(window.find(exp.AggFunc) is not None for window in windows)

    cte_aliases = _extract_cte_aliases(tree)

    # Extract tables from the original tree (not the unwrapped select_node)
    # so subquery tables are captured.
    from_tables = _extract_from_tables(tree if isinstance(tree, exp.Select) else select_node)

    # Compute ungrouped bare columns for strict GROUP BY enforcement.
    # Only TRULY bare SELECT columns (case 4 in _extract_columns) count —
    # columns referenced inside expressions (CASE, EXTRACT, CAST, arithmetic)
    # are not bare in the PostgreSQL sense and may match a matching GROUP BY
    # expression instead of needing to be in GROUP BY as a column.
    grain_set = set(grain)
    ungrouped_bare = [c for c in bare_true if c not in grain_set]

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
    fingerprint = _compute_fingerprint(
        measures, dimensions, grain, filters, having_columns=having_columns,
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
        has_distinct=has_distinct,
        has_function_grain=has_function_grain,
        has_complex_sql=has_complex_sql,
        has_window_functions=has_window_functions,
        has_window_aggregate=has_window_aggregate,
        cte_aliases=cte_aliases,
        input_dialect=dialect,
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
            ok = bool(exprs) and isinstance(exprs[0], exp.Column)
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


def _detect_percentile(node: exp.Expression) -> tuple[str | None, str | None]:
    """Map a ``MEDIAN(col)`` projection to a materialised quantile column.

    ``MEDIAN(col)`` -> ('p50', col), which is routable to a materialised
    ``col__p50`` column at exact grain (gated in the matcher; percentiles are
    not re-aggregatable).

    F-003-07: ``PERCENTILE_CONT/DISC(frac) WITHIN GROUP (ORDER BY col)`` is
    deliberately NOT routed here. ``_detect_complex_sql`` flags every
    ``WITHIN GROUP`` ordered-set aggregate as complex SQL, so it always routes
    through passthrough-with-table-substitution — which is the contract the
    query-shape catalog records for shape #86 ("ordered-set aggregate /
    WITHIN GROUP -> source passthrough"). Classifying it as a routable pNN
    here produced a select-expression the binder then discarded (complex SQL
    empties resolved measures), i.e. dead, self-contradicting machinery.
    Aggregate acceleration of percentiles is reached via ``MEDIAN`` (p50).
    """
    if isinstance(node, exp.Median):
        this = getattr(node, "this", None)
        if isinstance(this, exp.Column):
            return "p50", _col_name(this)
    return None, None


def _extract_columns(select_node: exp.Select | None) -> tuple[list[str], list[str], list[str], list[SelectExpression], list[str]]:
    """
    Distinguish measure columns (wrapped in aggregate functions) from
    dimension columns (bare columns or expressions in GROUP BY).

    Returns: (measures, dimensions, grain, select_expressions, bare_true)
    - measures: column names inside aggregate calls (SUM, COUNT, AVG, MAX, MIN, COUNT DISTINCT)
    - grain: column names in GROUP BY (Cast-unwrapped)
    - dimensions: grain + any bare SELECT columns not in an aggregate
    - select_expressions: detailed metadata for each select list item
    - bare_true: SELECT items that are TRULY bare columns (exp.Column at the
      top level, not wrapped in any expression).  Used for PG-style strict
      GROUP BY enforcement — expressions like CASE/EXTRACT/CAST don't count
      as bare even though they reference columns.
    """
    measures = []
    select_bare = []
    bare_true = []
    select_expressions = []

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

            # Median / percentile -> materialised quantile column (pNN). Must run
            # BEFORE the WITHIN GROUP unwrap below (which discards the ORDER BY
            # column). Routed exact-grain only (gated in the matcher).
            _q_suffix, _q_col = _detect_percentile(inner)
            if _q_suffix and _q_col:
                measures.append(_q_col)
                select_expressions.append(SelectExpression(
                    raw_text=raw_text, alias=alias_name,
                    classification="analytical", agg_function=_q_suffix,
                    inner_column=_q_col, inner_literal=None,
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
                    if exprs and isinstance(exprs[0], exp.Column):
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
    grain = []
    group = select_node.args.get("group") if select_node else None
    if group:
        for expr in group.expressions:
            inner = expr.this if isinstance(expr, exp.Alias) else expr
            # Unwrap Cast: GROUP BY success_flag::text should still add
            # success_flag to the grain so SELECT success_flag matches.
            if isinstance(inner, exp.Cast):
                inner = inner.this
            if isinstance(inner, exp.Column):
                grain.append(_col_name(inner))
            elif isinstance(inner, exp.Literal):
                # Positional GROUP BY (e.g. GROUP BY 1,2) — skip
                pass
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

    return _dedup(measures), _dedup(dimensions), _dedup(grain), select_expressions, _dedup(bare_true)


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


def _conjunct_to_filter(node: exp.Expression) -> LogicalFilter | None:
    """Translate a single top-level conjunct into a ``LogicalFilter`` if
    and only if it is faithfully representable. Anything that isn't —
    function calls, arithmetic, subqueries, column-vs-column,
    ``OR``/``EXISTS``/``NOT`` other than ``IS NOT NULL`` — returns None
    so the rewriter's raw-WHERE preservation path takes over instead of
    a phantom filter contaminating routing decisions."""
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
        return LogicalFilter(
            _col_name(node.this),
            "in",
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

    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is):
        is_node = node.this
        if _is_null_check_extractable(is_node):
            return LogicalFilter(_col_name(is_node.this), "is_not_null", None)
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
) -> tuple[list[tuple[str, str]], bool]:
    """Extract ORDER BY as ``(field_name, "asc"|"desc")`` rows.

    Returns ``(order_by, has_unresolvable_order)``. Only faithfully
    representable items are translated:

    - a bare ``exp.Column`` (optionally table-qualified) with its direction;
    - a positional integer literal that resolves to a bare-column SELECT item.

    Anything else — a function call (``LOWER(region)``), arithmetic
    (``SUM(amount)/COUNT(*)``), a ``CASE`` expression, an aggregate, or a
    positional reference to a non-bare SELECT item — is NOT translated. Its
    presence sets ``has_unresolvable_order`` and no phantom bare-column sort
    is fabricated.

    Bug-102 discipline (mirrors ``_extract_filters``): the previous
    implementation used recursive ``ordered.find(exp.Column)`` /
    ``ordered.find(exp.Literal)``, which fabricated a bare-column sort from
    inside any complex sort key — e.g. ``ORDER BY LOWER(region) DESC`` became
    ``("region", "desc")`` and ``ORDER BY SUM(amount)/COUNT(*) DESC`` became
    ``("amount", "desc")``. Combined with ``LIMIT`` the rewriter then sorted by
    the bare column and returned DIFFERENT rows — a silent wrong result. Strict
    per-item extraction with an unresolvable flag defuses it: the rewriter
    preserves the raw ORDER BY when the flag is set."""
    if not select_node:
        return [], False
    order = select_node.args.get("order")
    if not order:
        return [], False
    select_items = select_node.expressions or []
    result: list[tuple[str, str]] = []
    unresolvable = False
    for ordered in order.expressions:
        direction = "desc" if ordered.args.get("desc") else "asc"
        key = ordered.this
        # Bare column (optionally qualified): ORDER BY region [DESC]
        if isinstance(key, exp.Column):
            result.append((_col_name(key), direction))
            continue
        # Positional reference: ORDER BY 2 — resolve only to a bare-column
        # SELECT item; a positional reference to an expression item is itself
        # an expression sort and must be preserved verbatim.
        if isinstance(key, exp.Literal) and key.is_int:
            pos = int(key.this) - 1
            if 0 <= pos < len(select_items):
                item = select_items[pos]
                inner = item.this if isinstance(item, exp.Alias) else item
                if isinstance(inner, exp.Column):
                    result.append((_col_name(inner), direction))
                    continue
            # positional ref to a non-bare item (or out of range)
            unresolvable = True
            continue
        # Any other sort key (function, arithmetic, CASE, aggregate, subquery)
        # is not representable as a bare-column sort.
        unresolvable = True
    return result, unresolvable


def _extract_limit(select_node: exp.Select | None) -> int | None:
    if not select_node:
        return None
    limit = select_node.args.get("limit")
    if limit and limit.expression:
        try:
            return int(limit.expression.this)
        except (ValueError, AttributeError):
            pass
    return None


def _extract_offset(select_node: exp.Select | None) -> int | None:
    if not select_node:
        return None
    offset = select_node.args.get("offset")
    if offset and offset.expression:
        try:
            return int(offset.expression.this)
        except (ValueError, AttributeError):
            pass
    return None


def _compute_fingerprint(
    measures: list[str], dimensions: list[str], grain: list[str],
    filters: list[LogicalFilter], having_columns: list[str] | None = None,
) -> str:
    return fingerprint_shape(
        measures=measures,
        dimensions=dimensions,
        grain=grain,
        filter_cols=[f.dimension_name for f in filters],
        having_cols=having_columns or [],
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
                return float(text)
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
    JDBC callers escalate any error to a hard failure; XMLA/DAX retain
    the permissive behaviour.
    """
    sg_dialect = sqlglot.Dialect.get_or_raise(dialect)
    tokens = sg_dialect.tokenizer_class().tokenize(raw_sql)
    parser = sg_dialect.parser(error_level=sqlglot.ErrorLevel.WARN)
    trees = parser.parse(tokens, sql=raw_sql)
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

    # WITHIN GROUP: PERCENTILE_CONT(...) WITHIN GROUP (ORDER BY ...)
    for node in select_node.find_all(exp.WithinGroup):
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
    """Return True when *any* predicate in WHERE is something the IR's
    ``LogicalFilter`` cannot faithfully represent.

    The filter extractor in this module is permissive: it pattern-matches
    on the simple ``column op literal`` shape and silently drops anything
    else. That lossy behaviour is acceptable only when the rewriter has
    been told to preserve the raw WHERE through column-name substitution
    instead. This predicate audit decides which path the rewriter takes.

    The audit must catch every case where the extractor would drop or
    misread a predicate — see Bug-102 for the reason this exists. Any
    new WHERE shape added to ``_extract_filters`` must also be reflected
    here, otherwise we ship another silent drop.
    """
    if not select_node:
        return False
    where = select_node.args.get("where")
    if not where:
        return False

    unhandled_summary: list[str] = []

    def _flag(reason: str) -> None:
        if reason not in unhandled_summary:
            unhandled_summary.append(reason)

    for node in where.find_all(exp.Expression):
        # Skip pure structural nodes — only predicates and direct
        # operands need auditing.
        if isinstance(node, (exp.Where, exp.And, exp.Paren)):
            continue
        if isinstance(node, exp.Exists):
            _flag("exists")
            continue
        if isinstance(node, exp.Or):
            _flag("or")
            continue
        if isinstance(node, exp.Not):
            inner = node.this
            if isinstance(inner, exp.Exists):
                _flag("not-exists")
                continue
            if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                # NOT IS NULL = IS NOT NULL — handled by extractor.
                continue
            # Every other NOT (e.g. NOT (col = 'x'), NOT BETWEEN, NOT LIKE,
            # NOT IN) is unsafe: the extractor descends into the inner
            # comparison and emits it with the OPPOSITE polarity.
            _flag("not")
            continue
        if isinstance(node, exp.Subquery):
            _flag("subquery")
            continue
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            if not _comparison_is_extractable(node):
                _flag(type(node).__name__.lower())
            continue
        if isinstance(node, exp.In):
            if not _in_is_extractable(node):
                _flag("in")
            continue
        if isinstance(node, exp.Between):
            if not _between_is_extractable(node):
                _flag("between")
            continue
        if isinstance(node, exp.Like) and not isinstance(node, exp.ILike):
            # Bug-5326 (F-P4ac-01): a negated ``Like`` (``negate=True``, i.e.
            # ``NOT LIKE``) IS faithfully extractable now — the extractor emits a
            # ``not_like`` LogicalFilter, so polarity survives without raw-WHERE
            # preservation. Only flag the shapes the extractor genuinely can't
            # represent (wrapped column / non-literal pattern). ILIKE
            # (case-insensitive, either polarity) is NOT a ``Like`` subclass and
            # is handled by the catch-all below — it stays unresolvable so the
            # raw WHERE is preserved verbatim (keeping case-insensitivity).
            if not _like_is_extractable(node):
                _flag("like")
            continue
        if isinstance(node, exp.Is):
            if not _is_null_check_extractable(node):
                _flag("is")
            continue
        # Predicates the extractor doesn't try to handle at all:
        # IS DISTINCT FROM, SIMILAR TO, REGEXP_LIKE, ANY/ALL, GLOB, ILIKE
        # (sqlglot maps ILIKE to its own node), comparison-as-bool, etc.
        for cls_name in (
            "ILike", "Glob", "SimilarTo", "RegexpLike", "RegexpILike",
            "Any", "All", "Distance",
        ):
            cls = getattr(exp, cls_name, None)
            if cls is not None and isinstance(node, cls):
                _flag(cls_name.lower())
                break

    if unhandled_summary:
        logger.warning(
            "WHERE predicate(s) not representable as LogicalFilter — "
            "routing through raw-WHERE preservation. Reasons: %s",
            ",".join(sorted(unhandled_summary)),
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


def _extract_from_tables(select_node: exp.Select | None) -> list[str]:
    """Extract table names from FROM, JOIN, and subquery clauses.
    Scans the entire query tree to capture cross-model references in
    EXISTS, IN (SELECT ...), and scalar subqueries."""
    tables: list[str] = []
    if not select_node:
        return tables
    # Scan all Table nodes in the entire AST (including subqueries).
    for t in select_node.find_all(exp.Table):
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
