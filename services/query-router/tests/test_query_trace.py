"""
Automated query trace — parametrised issue detection.

Each query is parsed and inspected for known issue categories. The test
collects structured findings and compares them against expected issue IDs.

Add new queries to ``QUERIES`` at the bottom. Run::

    cd tessallite/services/query-router
    pytest tests/test_query_trace.py -v
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# Import real `shared` + `shared.config` before stubbing so that later imports
# of `shared.config.bootstrap` / `shared.config.resolver` (via pocket_matcher)
# still resolve against the real package path.
import shared  # noqa: F401
import shared.config  # noqa: F401
import shared.db.session  # noqa: F401
import shared.schemas  # noqa: F401
import shared.schemas.pydantic_models  # noqa: F401

# ---------------------------------------------------------------------------
# Ensure shared.db.models stubs exist so imports succeed without a live DB.
# ---------------------------------------------------------------------------
for _mod in (
    "shared", "shared.db", "shared.db.models",
    "shared.db.session", "shared.schemas", "shared.schemas.pydantic_models",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)
_models = sys.modules["shared.db.models"]
for _cls in (
    "AggregateColumn", "AggregateDefinition", "Measure", "Dimension",
    "UserDefinedAttribute", "Model", "HierarchyDefinition", "HierarchyLevel",
    "DataSource", "ProjectConnection", "Join", "ModelColumn", "ModelTable",
    "PocketDefinition", "PocketPredicate", "PocketRefreshRun",
    "SystemSetting", "SystemRestartPending",
    "TenantSetting", "ProjectSetting", "ModelSetting",
    "QueryLog", "QueryMissLog", "RouteLog",
):
    if not hasattr(_models, _cls):
        setattr(_models, _cls, type(_cls, (), {}))

import pytest

from src.parsing.sql_parser import parse_sql_to_ir
from src.ir.logical_query import LogicalQuery


# ---------------------------------------------------------------------------
# Issue dataclass
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    id: str
    summary: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Trace engine — runs all checks and returns detected issues.
# ---------------------------------------------------------------------------

def trace_query(sql: str, model_id: str = "model-1") -> tuple[LogicalQuery, list[Issue]]:
    """Parse *sql* and run every automated check. Return (IR, issues).

    Uses ``protocol="xmla"`` so the parser emits warnings instead of raising —
    the trace engine inspects the IR + warnings for issues, so a hard raise
    on the JDBC path would prevent the very checks these tests exercise.
    """
    issues: list[Issue] = []
    ir = parse_sql_to_ir(sql, model_id, protocol="xmla")

    _check_syntax_recovery(sql, issues)
    _check_group_by_mismatch(ir, issues)
    _check_grain_fabrication(ir, issues)
    _check_alias_preservation(ir, issues)
    _check_fingerprint_weakness(ir, model_id, issues)
    _check_multi_table_from(sql, issues)
    _check_select_star_subquery(sql, ir, issues)
    _check_passthrough_source_fallback(ir, issues)
    _check_qualified_star(sql, ir, issues)
    _check_distinct_preserved(sql, ir, issues)
    _check_having_preserved(ir, issues)
    _check_unresolvable_where(ir, issues)
    _check_stray_semicolon(ir, issues)
    _check_literal_as_passthrough(ir, issues)

    return ir, issues


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_syntax_recovery(sql: str, issues: list[Issue]) -> None:
    """Detect SQL that only parses under WARN (syntax error silently fixed).

    SQLGlot RAISE mode does not catch all syntax errors (e.g. double commas).
    So we also apply pattern-based detection for common malformations.
    """
    # 1. Try RAISE-level parse.
    try:
        sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as exc:
        issues.append(Issue(
            id="syntax_recovery",
            summary="SQL has syntax error silently recovered by WARN-level parser",
            detail=str(exc)[:200],
        ))
        return

    # 2. Pattern-based detection for errors SQLGlot silently fixes.
    import re
    # Double/triple commas in SELECT list or GROUP BY.
    if re.search(r",\s*,", sql):
        issues.append(Issue(
            id="syntax_recovery",
            summary="SQL contains consecutive commas (double comma) silently dropped by parser",
            detail="SQLGlot WARN and RAISE both accept this; the extra comma is silently removed.",
        ))
        return

    # 3. Compare token count: if WARN-parsed SQL has fewer SELECT items
    # than commas in the original SELECT list suggest, something was dropped.
    try:
        tree_warn = sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.WARN)
        tree_raise = sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE)
        sel_w = tree_warn if isinstance(tree_warn, exp.Select) else tree_warn.find(exp.Select)
        sel_r = tree_raise if isinstance(tree_raise, exp.Select) else tree_raise.find(exp.Select)
        if sel_w and sel_r and len(sel_w.expressions) != len(sel_r.expressions):
            issues.append(Issue(
                id="syntax_recovery",
                summary="WARN-level parse produced different expression count than RAISE-level",
                detail=f"WARN={len(sel_w.expressions)} exprs, RAISE={len(sel_r.expressions)} exprs",
            ))
    except Exception:
        pass


def _check_group_by_mismatch(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect bare SELECT columns missing from GROUP BY when aggregates exist."""
    if not ir.requested_measures:
        return
    has_agg = any(
        e.agg_function is not None
        for e in ir.select_expressions
    )
    if not has_agg:
        return

    grain_set = set(ir.grain)
    bare_not_in_grain = [
        d for d in ir.requested_dimensions if d not in grain_set
    ]
    if not bare_not_in_grain:
        return

    if not ir.grain:
        issues.append(Issue(
            id="group_by_fabrication",
            summary="No GROUP BY but SELECT has bare columns with aggregate functions",
            detail=f"dimensions={ir.requested_dimensions}, measures={ir.requested_measures}. "
                   f"Rewriter will fabricate GROUP BY on all {len(ir.requested_dimensions)} dimensions.",
        ))
    else:
        issues.append(Issue(
            id="group_by_inflation",
            summary="SELECT has bare columns not in GROUP BY alongside aggregates",
            detail=f"grain={ir.grain}, extra_columns={bare_not_in_grain}. "
                   f"Rewriter will inflate GROUP BY from {len(ir.grain)} to "
                   f"{len(ir.requested_dimensions)} columns.",
        ))


def _check_grain_fabrication(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect empty grain with non-empty dimensions (no aggregates case)."""
    if ir.requested_measures:
        return  # Handled by group_by_mismatch
    if ir.grain or not ir.requested_dimensions:
        return
    # Bare dimensions, no grain, no aggregates — valid SQL, but the
    # aggregate matcher will use dimensions as requested_grain.
    issues.append(Issue(
        id="matcher_grain_from_dimensions",
        summary="Aggregate matcher will use SELECT dimensions as grain (no GROUP BY, no aggregates)",
        detail=f"dimensions={ir.requested_dimensions}. Matcher requested_grain "
               f"will be {set(ir.requested_dimensions)} instead of empty set.",
    ))


def _check_alias_preservation(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Flag aggregate expressions whose alias would be lost in source rewrite."""
    for expr in ir.select_expressions:
        if expr.classification == "literal" and expr.agg_function:
            if expr.alias:
                issues.append(Issue(
                    id="alias_lost_source_rewrite",
                    summary=f"Alias '{expr.alias}' on {expr.agg_function}({expr.inner_literal}) "
                            f"will be replaced with '__row_count' in source rewrite",
                    detail=f"raw_text={expr.raw_text!r}",
                ))
            else:
                issues.append(Issue(
                    id="no_alias_engine_dependent",
                    summary=f"No alias on {expr.raw_text}; response column name is engine-dependent",
                    detail="Aggregate rewrite path does not add a default alias for literal counts.",
                ))


def _check_fingerprint_weakness(
    ir: LogicalQuery, model_id: str, issues: list[Issue]
) -> None:
    """Detect queries whose fingerprint collides with structurally different queries."""
    if ir.select_star:
        # All SELECT * queries have the same fingerprint (measures=[], dims=[], grain=[]).
        issues.append(Issue(
            id="fingerprint_select_star_collapse",
            summary="All SELECT * queries produce the same fingerprint regardless of subquery content",
            detail=f"fingerprint={ir.query_fingerprint[:16]}",
        ))
        return

    # Test for actual collision: build a structurally different query with
    # the same measures/grain but different dimensions and check if
    # fingerprints match.  After the dimensions-in-fingerprint fix, most
    # former collisions are resolved.
    if not ir.grain and not ir.requested_dimensions and ir.requested_measures:
        # Empty grain + empty dimensions: test against a minimal count query.
        minimal_ir = parse_sql_to_ir("SELECT COUNT(*) FROM __any_table__", model_id)
        if minimal_ir.query_fingerprint == ir.query_fingerprint:
            issues.append(Issue(
                id="fingerprint_collision",
                summary="Empty grain + empty dimensions: fingerprint collides with "
                        "all no-GROUP-BY queries using the same aggregate functions",
                detail=f"fingerprint={ir.query_fingerprint[:16]}, "
                       f"measures={ir.requested_measures}",
            ))


def _check_multi_table_from(sql: str, issues: list[Issue]) -> None:
    """Detect multiple tables in FROM clause (joins, cross joins, comma joins)."""
    try:
        tree = sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        return

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not select:
        return

    tables: list[str] = []

    # SQLGlot stores FROM under key "from_" with the primary table in .this
    from_clause = select.args.get("from_")
    if from_clause:
        for t in from_clause.find_all(exp.Table):
            tables.append(t.name)

    # Additional tables appear in the "joins" arg (includes CROSS JOIN from comma syntax)
    joins = select.args.get("joins") or []
    for join in joins:
        for t in join.find_all(exp.Table):
            tables.append(t.name)

    if len(tables) > 1:
        issues.append(Issue(
            id="multi_table_from",
            summary=f"FROM clause references {len(tables)} tables: {tables}",
            detail="Parser does not extract FROM tables. Aggregate route may return "
                   "wrong results (single-model count vs cross-join product). "
                   "Source passthrough sends unresolved table names to DB.",
        ))


def _check_select_star_subquery(
    sql: str, ir: LogicalQuery, issues: list[Issue]
) -> None:
    """Detect SELECT * FROM (subquery) where subquery structure is invisible."""
    if not ir.select_star:
        return

    try:
        tree = sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        return

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not select:
        return

    # SQLGlot stores FROM under key "from_"; the primary source is in .this
    from_clause = select.args.get("from_")
    if not from_clause:
        return

    source = from_clause.this
    if isinstance(source, exp.Subquery):
        inner = source.this
        if isinstance(inner, exp.Select):
            inner_cols = [
                e.sql() for e in inner.expressions
                if not isinstance(e, exp.Star)
            ]
            inner_group = inner.args.get("group")
            inner_grain = []
            if inner_group:
                inner_grain = [e.sql() for e in inner_group.expressions]

            issues.append(Issue(
                id="select_star_subquery_opaque",
                summary=f"SELECT * wraps subquery with {len(inner_cols)} columns "
                        f"and {len(inner_grain)} GROUP BY columns — all invisible to router",
                detail=f"subquery_columns={inner_cols}, subquery_grain={inner_grain}. "
                       f"Binder will expand to ALL model dims/measures. "
                       f"Aggregate routing bypassed. Raw SQL sent to DB.",
            ))


def _check_passthrough_source_fallback(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect when source rewrite will return raw SQL due to no resolvable columns."""
    if ir.select_star:
        return  # Already covered by select_star checks.

    has_passthrough = any(
        e.classification == "passthrough" and e.inner_column is None
        for e in ir.select_expressions
    )
    if has_passthrough:
        issues.append(Issue(
            id="passthrough_raw_sql",
            summary="has_passthrough_expressions=True; raw SQL sent to DB without "
                    "table/column name resolution",
            detail="Passthrough flag set by expression with classification=passthrough "
                   "and inner_column=None.",
        ))
        return

    # Check if __row_count is the only measure, no dimensions, and
    # multiple FROM tables.  Single-table counts are now handled by the
    # base-table fallback, but multi-table counts still fall through raw.
    only_row_count = (
        ir.requested_measures == ["__row_count"]
        and not ir.requested_dimensions
    )
    from_tables = getattr(ir, "from_tables", [])
    if only_row_count and len(from_tables) > 1:
        issues.append(Issue(
            id="count_only_raw_fallback",
            summary="Multi-table count-only query: source rewriter returns raw SQL "
                    "unchanged (base-table fallback only applies to single-table queries)",
            detail="The rewriter cannot determine the physical table from __row_count "
                   "alone. Raw SQL with semantic table names sent to DB.",
        ))


def _check_qualified_star(sql: str, ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect qualified star (alias.*) that was NOT recognised as SELECT *."""
    try:
        tree = sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        return
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not select:
        return
    has_qualified_star = any(
        isinstance(e, exp.Column) and isinstance(e.this, exp.Star)
        for e in select.expressions
    )
    if has_qualified_star and not ir.select_star:
        issues.append(Issue(
            id="qualified_star_not_detected",
            summary="Qualified star (alias.*) not recognised as SELECT *",
            detail="Parser should set select_star=True for Column(this=Star) expressions.",
        ))
    if has_qualified_star and "*" in ir.requested_dimensions:
        issues.append(Issue(
            id="star_as_column_name",
            summary="'*' extracted as a column name from qualified star",
            detail="Binder will raise 'Unknown column: *'. Parser should skip qualified star "
                   "in _extract_columns.",
        ))


def _check_distinct_preserved(sql: str, ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect SELECT DISTINCT that the parser failed to capture."""
    try:
        tree = sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        return
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not select:
        return
    sql_has_distinct = select.args.get("distinct") is not None
    ir_has_distinct = getattr(ir, "has_distinct", False)
    if sql_has_distinct and not ir_has_distinct:
        issues.append(Issue(
            id="distinct_not_detected",
            summary="SELECT DISTINCT not captured by parser",
            detail="Rewriter will emit plain SELECT, losing deduplication semantics.",
        ))


def _check_having_preserved(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Flag queries where HAVING is present but not captured."""
    # The parser now captures having_raw. If it's populated, HAVING is tracked.
    # This check flags cases where having_raw is unexpectedly empty despite
    # HAVING being in the raw SQL.
    raw_upper = ir.raw_query.upper()
    if " HAVING " in raw_upper and not getattr(ir, "having_raw", None):
        issues.append(Issue(
            id="having_not_captured",
            summary="HAVING clause present in raw SQL but not captured in IR",
            detail="having_raw is empty. Rewriter will drop the HAVING clause.",
        ))


def _check_unresolvable_where(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Flag queries with unresolvable WHERE predicates (EXISTS, OR, subqueries)."""
    if getattr(ir, "has_unresolvable_where", False):
        issues.append(Issue(
            id="unresolvable_where",
            summary="WHERE contains predicates not representable as LogicalFilter "
                    "(EXISTS, OR, subquery)",
            detail="Query forced to passthrough to preserve full WHERE clause.",
        ))


def _check_stray_semicolon(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect stray semicolons that truncate the query."""
    for w in getattr(ir, "syntax_warnings", []):
        if "semicolon" in w.lower():
            issues.append(Issue(
                id="stray_semicolon",
                summary="Stray semicolon splits query into multiple statements",
                detail=w,
            ))
            return


def _check_literal_as_passthrough(ir: LogicalQuery, issues: list[Issue]) -> None:
    """Detect deterministic literal functions (CURRENT_DATE, etc.) misclassified as passthrough."""
    literal_func_keys = {
        "currentdate", "currenttime", "currenttimestamp",
        "current_date", "current_time", "current_timestamp",
        "now", "getdate", "sysdate",
    }
    for e in ir.select_expressions:
        if e.classification == "passthrough" and e.inner_column is None:
            # Check if the raw text looks like a deterministic literal function
            raw_lower = e.raw_text.lower().replace(" ", "")
            for key in literal_func_keys:
                if key in raw_lower:
                    issues.append(Issue(
                        id="literal_as_passthrough",
                        summary=f"Deterministic literal '{e.raw_text}' classified as passthrough",
                        detail="Registry key mismatch or missing entry. "
                               "Query will be sent as raw SQL without table name resolution.",
                    ))
                    break


# ---------------------------------------------------------------------------
# Parametrised queries
# ---------------------------------------------------------------------------

@dataclass
class QueryCase:
    """One query to trace."""
    id: str
    sql: str
    expected_issues: set[str]
    model_id: str = "model-1"


QUERIES: list[QueryCase] = [
    # ------------------------------------------------------------------
    # Query 1: double comma + GROUP BY mismatch (4 SELECT, 2 GROUP BY)
    # ------------------------------------------------------------------
    QueryCase(
        id="q1_double_comma_group_by_mismatch",
        sql=(
            "select x.account_type_name, x.account_type_code, x.auth_method, "
            "x.auth_method_name, ,count(*) as cnt from modely x "
            "group by account_type_name, account_type_code"
        ),
        expected_issues={
            "syntax_recovery",
            "group_by_inflation",
            "alias_lost_source_rewrite",
        },
    ),
    # ------------------------------------------------------------------
    # Query 2: double comma + no GROUP BY at all
    # ------------------------------------------------------------------
    QueryCase(
        id="q2_no_group_by_with_aggregate",
        sql=(
            "select x.account_type_name, x.account_type_code, x.auth_method, "
            "x.auth_method_name, ,count(*) as cnt from modely x"
        ),
        expected_issues={
            "syntax_recovery",
            "group_by_fabrication",
            "alias_lost_source_rewrite",
        },
    ),
    # ------------------------------------------------------------------
    # Query 3: SELECT * FROM (subquery)
    # ------------------------------------------------------------------
    QueryCase(
        id="q3_select_star_subquery",
        sql=(
            "select * from ("
            "select x.account_type_name, x.account_type_code, x.auth_method, "
            "x.auth_method_name from modely x "
            "group by account_type_name, account_type_code, x.auth_method, "
            "x.auth_method_name)"
        ),
        expected_issues=set(),  # Subquery unwrapped: parser extracts dims/grain directly
    ),
    # ------------------------------------------------------------------
    # Query 4: cross-join COUNT(1)
    # ------------------------------------------------------------------
    QueryCase(
        id="q4_cross_join_count",
        sql="select count(1) from modelx,modely",
        expected_issues={
            "multi_table_from",
            "no_alias_engine_dependent",
            "count_only_raw_fallback",
            "fingerprint_collision",
        },
    ),
    # ------------------------------------------------------------------
    # Query 5: COUNT(1) with LIMIT, single table, no alias
    # ------------------------------------------------------------------
    QueryCase(
        id="q5_count1_limit",
        sql="select count(1) from modely m2  limit 3",
        expected_issues={
            "no_alias_engine_dependent",
            "fingerprint_collision",
        },
    ),
    # ------------------------------------------------------------------
    # Query 6: SELECT * with LIMIT, single table, no subquery
    # ------------------------------------------------------------------
    QueryCase(
        id="q6_select_star_limit",
        sql="select * from modexy m2  limit 3",
        expected_issues={
            "fingerprint_select_star_collapse",
        },
    ),
    # ------------------------------------------------------------------
    # Query 7: bare SELECT * from table, no LIMIT
    # ------------------------------------------------------------------
    QueryCase(
        id="q7_select_star_bare",
        sql="select * from modexy",
        expected_issues={
            "fingerprint_select_star_collapse",
        },
    ),
    # ------------------------------------------------------------------
    # Query 8: single UDA-like column from schema-qualified table
    # ------------------------------------------------------------------
    QueryCase(
        id="q8_uda_column_schema_qualified",
        sql="select base_amount_plus_commession_amount  from public.modelx m2",
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 9: same as q8 — single UDA-like column, schema-qualified table
    # ------------------------------------------------------------------
    QueryCase(
        id="q9_uda_column_schema_qualified_retest",
        sql="select base_amount_plus_commession_amount  from public.modelx m2",
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 10: UDA column with EXISTS subquery and LIMIT
    # ------------------------------------------------------------------
    QueryCase(
        id="q10_uda_exists_subquery",
        sql=(
            "select modely.base_amount_plus_commession_amount from modely "
            "where exists (select 1 from modelx limit 1) limit 3"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
            "unresolvable_where",
        },
    ),
    # ------------------------------------------------------------------
    # Query 11: dimension with scalar subquery comparison in WHERE
    # ------------------------------------------------------------------
    QueryCase(
        id="q11_scalar_subquery_where",
        sql=(
            "select modely.base_amount from modely "
            "where 1 < (select 2 from modelx m limit 1)"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
            "unresolvable_where",
        },
    ),
    # ------------------------------------------------------------------
    # Query 12: MAX + bare column, no GROUP BY, HAVING clause
    # ------------------------------------------------------------------
    QueryCase(
        id="q12_having_no_group_by",
        sql=(
            "select max(modely.base_amount),modely.account_type_code from modely m "
            "having max(modely.base_amount) < avg(m.chargeback_amount)"
        ),
        expected_issues={
            "group_by_fabrication",
        },
    ),
    # ------------------------------------------------------------------
    # Query 13: HAVING before GROUP BY (non-standard order)
    # ------------------------------------------------------------------
    QueryCase(
        id="q13_having_before_group_by",
        sql=(
            "select max(modely.base_amount),modely.account_type_code from modely m "
            "having max(modely.base_amount) < avg(m.chargeback_amount) "
            "group by modely.account_type_code"
        ),
        expected_issues=set(),  # All issues (HAVING drop, no alias) are beyond automation scope
    ),
    # ------------------------------------------------------------------
    # Query 14: arithmetic expression of two columns, no aggregate
    # ------------------------------------------------------------------
    QueryCase(
        id="q14_column_arithmetic",
        sql="select m.settlement_amount + m.tax_amount from modely m",
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 15: arithmetic + GROUP BY + HAVING (Issues 15.1-15.8)
    # ------------------------------------------------------------------
    QueryCase(
        id="q15_arithmetic_group_by_having",
        sql=(
            "select m.settlement_amount + m.tax_amount from modely m "
            "group by modely.account_type_code "
            "having max(modely.base_amount) < avg(m.chargeback_amount)"
        ),
        expected_issues=set(),  # All columns resolvable, HAVING captured, no issues at parser level
    ),
    # ------------------------------------------------------------------
    # Query 16: qualified star — DBeaver SELECT m.* pattern (Bug-032)
    # ------------------------------------------------------------------
    QueryCase(
        id="q16_qualified_star",
        sql="SELECT m.* FROM public.modelx AS m",
        expected_issues={
            "fingerprint_select_star_collapse",
        },
    ),
    # ------------------------------------------------------------------
    # Query 17: SELECT DISTINCT (Bug-034)
    # ------------------------------------------------------------------
    QueryCase(
        id="q17_select_distinct",
        sql="SELECT DISTINCT payment_scheme FROM modely ORDER BY payment_scheme",
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 18: SELECT DISTINCT with multiple columns
    # ------------------------------------------------------------------
    QueryCase(
        id="q18_select_distinct_multi",
        sql="SELECT DISTINCT customer_type, payment_method FROM modely",
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 19: WHERE EXISTS forces passthrough (Bug-023)
    # ------------------------------------------------------------------
    QueryCase(
        id="q19_where_exists",
        sql=(
            "SELECT base_amount FROM modely "
            "WHERE EXISTS (SELECT 1 FROM modelx LIMIT 1) LIMIT 3"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
            "unresolvable_where",
        },
    ),
    # ------------------------------------------------------------------
    # Query 20: WHERE with OR forces passthrough (Bug-023)
    # ------------------------------------------------------------------
    QueryCase(
        id="q20_where_or",
        sql="SELECT base_amount FROM modely WHERE base_amount > 100 OR base_amount < 10",
        expected_issues={
            "matcher_grain_from_dimensions",
            "unresolvable_where",
        },
    ),
    # ------------------------------------------------------------------
    # Query 21: HAVING with GROUP BY — HAVING should be captured (Bug-022)
    # ------------------------------------------------------------------
    QueryCase(
        id="q21_having_with_group_by",
        sql=(
            "SELECT account_type_code, MAX(base_amount) FROM modely "
            "GROUP BY account_type_code "
            "HAVING MAX(base_amount) > 1000"
        ),
        expected_issues=set(),  # HAVING captured, no issues
    ),
    # ------------------------------------------------------------------
    # Query 22: cross-table ORDER BY with dim columns (Bug-037, Bug-038)
    # ------------------------------------------------------------------
    QueryCase(
        id="q22_cross_table_order_by",
        sql=(
            "SELECT customer_type_code, customer_type_name, active_flag "
            "FROM modely m ORDER BY sort_order"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 23: CURRENT_DATE/CURRENT_TIMESTAMP literals (Bug-039)
    # ------------------------------------------------------------------
    QueryCase(
        id="q23_current_date_timestamp",
        sql=(
            "SELECT CURRENT_DATE AS current_date_value, "
            "CURRENT_TIMESTAMP AS current_timestamp_value "
            "FROM modely ORDER BY sort_order"
        ),
        expected_issues=set(),  # literals classified correctly, no passthrough
    ),
    # ------------------------------------------------------------------
    # Query 24: stray semicolon truncates query (Bug-040)
    # ------------------------------------------------------------------
    QueryCase(
        # Bug-7916 / Codex gate R2: multi-statement input is now rejected on
        # ALL protocols (including XMLA). The stray semicolon test is replaced
        # by test_multi_statement_rejected_on_xmla in the Bug-7916 guard tests.
        # id="q24_stray_semicolon" — REMOVED (raises SyntaxErrorInSQL before
        # the trace engine can inspect the IR).
        id="q24_stray_semicolon_trailing_only",
        sql=(
            "SELECT CURRENT_DATE AS current_date_value, "
            "CURRENT_TIMESTAMP AS current_timestamp_value "
            "FROM modely "
            "ORDER BY sort_order;"
        ),
        # Bug-7916 / Codex gate R2: the original mid-stream semicolon
        # query is now rejected by the multi-statement guard. This
        # trailing-only variant has no stray_semicolon issue (the trailing
        # semicolon is stripped during parsing -- valid SQL).
        expected_issues=set(),
    ),
    # ------------------------------------------------------------------
    # Query 26: OR in WHERE with IS NOT NULL (Bug-046)
    # ------------------------------------------------------------------
    QueryCase(
        id="q26_where_or_is_not_null",
        sql=(
            "SELECT payment_id, merchant_name, counterparty_name "
            "FROM modely "
            "WHERE merchant_name IS NOT NULL OR counterparty_name IS NOT NULL "
            "ORDER BY payment_id LIMIT 50"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
            "unresolvable_where",
        },
    ),
    # ------------------------------------------------------------------
    # Query 25: typed date literals in BETWEEN (Bug-044)
    # ------------------------------------------------------------------
    QueryCase(
        id="q25_typed_date_between",
        sql=(
            "SELECT payment_id, payment_reference, business_date, transaction_currency "
            "FROM modely "
            "WHERE business_date BETWEEN DATE '2024-01-01' AND DATE '2026-12-31' "
            "ORDER BY business_date, payment_id LIMIT 50"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 28: CASE expression (Bug-051, Bug-052)
    # ------------------------------------------------------------------
    QueryCase(
        id="q28_case_expression",
        sql=(
            "SELECT payment_id, transaction_amount, "
            "CASE WHEN transaction_amount >= 10000 THEN 'VERY_LARGE' "
            "WHEN transaction_amount >= 1000 THEN 'LARGE' "
            "WHEN transaction_amount >= 100 THEN 'MEDIUM' "
            "ELSE 'SMALL' END AS amount_band "
            "FROM modely ORDER BY payment_id LIMIT 50"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Query 27: IS NOT NULL + GTE (Bug-050)
    # ------------------------------------------------------------------
    QueryCase(
        id="q27_is_not_null_and_gte",
        sql=(
            "SELECT payment_id, risk_score, risk_decision "
            "FROM modely "
            "WHERE risk_score IS NOT NULL AND risk_score >= 75 "
            "ORDER BY risk_score DESC, payment_id LIMIT 50"
        ),
        expected_issues={
            "matcher_grain_from_dimensions",
        },
    ),
    # ------------------------------------------------------------------
    # Valid baseline: should produce zero issues
    # ------------------------------------------------------------------
    QueryCase(
        id="q_valid_baseline",
        sql=(
            "SELECT country_code, SUM(transaction_amount) AS total "
            "FROM payment_transaction GROUP BY country_code"
        ),
        expected_issues=set(),
    ),
]


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case",
    QUERIES,
    ids=[q.id for q in QUERIES],
)
def test_query_trace(case: QueryCase):
    ir, issues = trace_query(case.sql, case.model_id)
    found = {i.id for i in issues}

    missing = case.expected_issues - found
    unexpected = found - case.expected_issues

    lines: list[str] = []
    if missing:
        lines.append(f"Expected but not found: {missing}")
    if unexpected:
        lines.append(f"Found but not expected: {unexpected}")
    if issues:
        lines.append("All detected issues:")
        for i in issues:
            lines.append(f"  [{i.id}] {i.summary}")
            if i.detail:
                lines.append(f"    {i.detail}")

    assert found == case.expected_issues, "\n".join(lines)
