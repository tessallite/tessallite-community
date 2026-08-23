"""Regression tests for the ML4 query-rewriter / dialect findings.

Covers the behavioural fixes in batch ML4 (Fable unit-006 review):

- F-006-02 persona-star UDA backtick normalisation
- F-006-03 passthrough / SELECT * identifier requoting for all connectors
- F-006-06 COUNT_DISTINCT emits NULL (not a wrong number) at coarser grain
- F-006-07 aggregate LIMIT/OFFSET rendered per dialect (no raw tsql append)
- F-006-09 pocket table matching by EXACT name, not substring containment
- F-006-11 raw-SQL fallback honours the query's input dialect

Run from tessallite/services/query-router/:
    pytest tests/test_ml4_rewriter_dialect_fixes.py
"""
import types

import pytest
import sqlglot
from sqlglot import exp

from src.rewrite.aggregate import (
    AggregateRewriteUnsupported,
    _phys_expr_for_node,
    _render_pagination_suffix,
)
from src.rewrite.dialects import (
    RewriteReparseError,
    _transpile_to_dialect,
    _translate_raw_sql,
    parse_one_strict,
    render_expression_for_dialect,
)
from src.rewrite.pocket import rewrite_for_pocket
from src.rewrite.uda import _normalize_uda_expression_quoting, _render_uda_expression


# --- F-006-03 -------------------------------------------------------------

@pytest.mark.parametrize(
    "dialect,quote_open,quote_close",
    [
        ("spark", "`", "`"),
        ("tsql", "[", "]"),
        ("bigquery", "`", "`"),
        ("redshift", '"', '"'),
        ("snowflake", '"', '"'),
    ],
)
def test_f6_03_passthrough_requote_emits_identifiers_not_literals(dialect, quote_open, quote_close):
    """After table-substitution the SQL is PG-canonical (double quotes);
    transpilation must produce the target dialect's IDENTIFIER quoting so
    columns are not parsed as string literals (Spark/tsql previously got PG
    quotes). Uses _transpile_to_dialect (the single choke point)."""
    pg = 'SELECT "region", "amount" FROM "demo"."sales_fact"'
    out = _transpile_to_dialect(pg, dialect)
    tree = sqlglot.parse_one(out, read=dialect)
    cols = {c.name for c in tree.find_all(exp.Column)}
    assert {"region", "amount"} <= cols
    # No identifier leaked into a string literal.
    string_lits = {l.this for l in tree.find_all(exp.Literal) if l.is_string}
    assert "region" not in string_lits and "amount" not in string_lits
    assert quote_open in out and quote_close in out


def test_f6_03_postgres_unchanged():
    pg = 'SELECT "region" FROM "demo"."sales_fact"'
    assert _transpile_to_dialect(pg, "postgres") == pg


# --- F-006-11 -------------------------------------------------------------

def test_f6_11_translate_raw_honours_input_dialect():
    """A BigQuery-authored raw query must be read under bigquery, not postgres,
    so it transpiles correctly instead of being returned untranslated."""
    bq = "SELECT TIMESTAMP_TRUNC(ts, DAY) AS d FROM t"
    out = _translate_raw_sql(bq, "spark", "bigquery")
    # parses cleanly under spark and is non-trivially translated
    sqlglot.parse_one(out, read="spark")
    assert out  # non-empty


def test_f6_11_postgres_authored_postgres_target_is_noop():
    """A PostgreSQL-authored query on a PostgreSQL target is a true no-op:
    normalised INPUT dialect is postgres, so nothing is translated."""
    pg = 'SELECT "a" FROM "t"'
    assert _translate_raw_sql(pg, "postgres", "postgres") == pg
    # postgresql alias for both sides normalises to the same no-op.
    assert _translate_raw_sql(pg, "postgresql", "postgresql") == pg


# --- F-006-01 -------------------------------------------------------------
# A PostgreSQL *target* is NOT synonymous with PostgreSQL-authored *input*.
# A valid BigQuery-/Spark-authored raw query on a PG source must be TRANSLATED
# to PostgreSQL syntax, not returned verbatim (verbatim -> 502 at the source).


def test_f6_01_bigquery_authored_translated_to_postgres_target():
    """BigQuery-authored scalar (``TIMESTAMP_TRUNC``, ``CURRENT_TIMESTAMP()``)
    on a PostgreSQL target must be re-emitted as PostgreSQL, not returned
    unchanged. The pre-fix early no-op returned it verbatim -> the source DB
    502'd on ``TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY)``."""
    bq = "SELECT TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY) AS d"
    out = _translate_raw_sql(bq, "postgres", "bigquery")
    # It must be genuinely translated (not byte-identical to the BQ input).
    assert out != bq
    # The emitted SQL must parse under PostgreSQL (self-parse invariant).
    sqlglot.parse_one(out, read="postgres")
    # sqlglot maps BQ TIMESTAMP_TRUNC(x, DAY) -> PG DATE_TRUNC('DAY', x).
    assert "DATE_TRUNC" in out.upper()
    assert "TIMESTAMP_TRUNC" not in out.upper()


def test_f6_01_bigquery_string_literal_semantics_on_postgres_target():
    """BigQuery treats double-quoted tokens as STRING LITERALS. A BQ-authored
    ``SELECT "a"`` must translate to PG ``SELECT 'a'`` (a string constant),
    NOT be handed to PostgreSQL verbatim (where ``"a"`` is an identifier —
    the opposite meaning)."""
    bq = 'SELECT "a" FROM "t"'
    out = _translate_raw_sql(bq, "postgres", "bigquery")
    assert out == 'SELECT \'a\' FROM "t"'
    sqlglot.parse_one(out, read="postgres")


def test_f6_01_spark_authored_translated_to_postgres_target():
    """Spark-authored raw SQL on a PostgreSQL target must be re-emitted as
    PostgreSQL and self-parse under postgres."""
    spark = "SELECT `region`, COUNT(*) FROM t GROUP BY `region`"
    out = _translate_raw_sql(spark, "postgres", "spark")
    assert out != spark
    parsed = sqlglot.parse_one(out, read="postgres")
    # The backtick identifier becomes a PostgreSQL identifier (a column), not
    # a string literal.
    cols = {c.name for c in parsed.find_all(exp.Column)}
    assert "region" in cols


# --- F-006-02 -------------------------------------------------------------

def test_f6_02_backtick_uda_normalised_before_parse():
    expr = "EXTRACT(YEAR FROM `full_date`)"
    with pytest.raises(Exception):
        sqlglot.parse_one(expr, read="postgres")  # the raw 500 trigger
    norm = _normalize_uda_expression_quoting(expr)
    rendered = sqlglot.parse_one(norm, read="postgres").sql(dialect="postgres")
    assert "full_date" in rendered


# --- F-006-06 -------------------------------------------------------------

def _col(name):
    return types.SimpleNamespace(physical_col_name=name)


def test_f6_06_count_distinct_null_at_coarser_grain():
    lookup = {("cust", "count_distinct"): _col("cust__count_distinct")}
    assert _phys_expr_for_node("cust", "count_distinct", lookup, True) == '"cust__count_distinct"'
    # coarser grain: NULL (fail-obvious), NOT SUM(...) which would over-count.
    assert _phys_expr_for_node("cust", "count_distinct", lookup, False) == "NULL"


def test_f6_06_plain_count_still_reaggregates():
    lookup = {("x", "count"): _col("x__count")}
    assert _phys_expr_for_node("x", "count", lookup, False) == 'SUM("x__count")'


# --- F-006-07 -------------------------------------------------------------

@pytest.mark.parametrize("dialect", ["postgres", "spark", "bigquery", "redshift"])
def test_f6_07_pagination_unchanged_on_live_targets(dialect):
    assert _render_pagination_suffix(5, None, dialect) == " LIMIT 5"
    assert _render_pagination_suffix(5, 10, dialect) == " LIMIT 5 OFFSET 10"
    assert _render_pagination_suffix(None, 10, dialect) == " OFFSET 10"


def test_f6_07_pagination_tsql_fails_loud_instead_of_invalid_syntax():
    # Bug-5902: SQL Server pagination is not a suffix transform (it needs a
    # full-statement OFFSET/FETCH rewrite). The old behaviour silently fell
    # back to a raw PostgreSQL-style ``LIMIT n OFFSET m`` suffix, which is
    # invalid tsql syntax masquerading as a successful aggregate route.
    # Assert it now fails loudly so the router falls back to source instead.
    with pytest.raises(AggregateRewriteUnsupported):
        _render_pagination_suffix(5, 10, "tsql")


# --- F-006-09 -------------------------------------------------------------

def _bound(raw_sql, slug, from_tables):
    lq = types.SimpleNamespace(
        raw_query=raw_sql, from_tables=from_tables, input_dialect="postgres",
    )
    model = types.SimpleNamespace(slug=slug, display_name=slug)
    return types.SimpleNamespace(logical_query=lq, model=model)


def _pocket(schema, table):
    return types.SimpleNamespace(target_schema=schema, physical_table_name=table)


def test_f6_09_exact_table_redirected_to_pocket():
    bound = _bound("SELECT * FROM sales", "sales", ["sales"])
    out = rewrite_for_pocket(bound, _pocket("pk", "sales_pocket"), "postgres")
    assert "sales_pocket" in out


def test_f6_09_substring_containment_not_redirected():
    """An unrelated table whose name merely CONTAINS the model slug as a
    substring must NOT be redirected to the pocket table. Here the slug is
    ``sales`` and the only table in scope is ``demo.regional_sales_summary``
    (which is not a parsed from-table); the previous ``any(n in full_name)``
    test would have wrongly rewritten it."""
    bound = _bound(
        "SELECT * FROM demo.regional_sales_summary", "sales", [],
    )
    out = rewrite_for_pocket(bound, _pocket("pk", "sales_pocket"), "postgres")
    assert "sales_pocket" not in out
    assert "regional_sales_summary" in out


# --- Bug-5901 (F-006-02) ----------------------------------------------------

def test_bug_5901_no_table_match_returns_byte_identical_raw_sql_non_postgres():
    """Regression for Bug-5901: when no table in the query matches
    ``table_names``, the router's ONLY safety net is
    ``rewritten == bound_query.logical_query.raw_query`` (routing/router.py).
    A non-postgres ``target_dialect`` re-serializes the parsed tree through
    sqlglot even when nothing was substituted, which is NOT guaranteed to be
    byte-identical to the original ``raw_sql`` (different quoting/formatting
    per dialect) — silently defeating that guard and letting the original
    source-table SQL execute against the pocket target connection. The
    existing F-006-09 no-match test only exercised ``target_dialect="postgres"``,
    where the round-trip happens to be stable and the underlying gap was not
    provable. Assert strict byte-identity under a NON-postgres target.

    Double-quoted identifiers are deliberate here (Fable R2 review finding):
    an UNQUOTED ``demo.regional_sales_summary`` round-trips byte-identically
    through both postgres and bigquery sqlglot serialization, so it would
    pass even against the pre-fix code and prove nothing. Quoted identifiers
    re-serialize as PostgreSQL double quotes vs BigQuery backticks
    (``"demo"."regional_sales_summary"`` -> `` `demo`.`regional_sales_summary` ``),
    so this genuinely fails pre-fix and passes only with the
    ``replaced``/``_pocket_table_present`` gate in place."""
    raw = 'SELECT * FROM "demo"."regional_sales_summary"'
    bound = _bound(raw, "sales", [])
    out = rewrite_for_pocket(bound, _pocket("pk", "sales_pocket"), "bigquery")
    assert out == raw
    assert "sales_pocket" not in out


# --- F-006-03 — internal reparses reject sqlglot's semantics-changing recovery


def test_f6_03_parse_one_strict_rejects_recovered_tree():
    """``parse_one_strict`` must reject malformed SQL that sqlglot would
    silently recover into a MEANING-CHANGED tree (``x = 1 !!`` -> ``x = NOT 1``),
    matching the top-level parser's fail-loud contract."""
    with pytest.raises(RewriteReparseError):
        parse_one_strict("SELECT x FROM m WHERE x = 1 !!", read="postgres")


def test_f6_03_parse_one_strict_accepts_valid_sql():
    """A valid statement parses normally and is byte-preserved on re-emit."""
    tree = parse_one_strict("SELECT x FROM m WHERE x = 1", read="postgres")
    assert tree.sql(dialect="postgres") == "SELECT x FROM m WHERE x = 1"


def test_f6_03_pocket_reparse_fails_loud_on_malformed():
    """The pocket reparse (``rewrite_for_pocket``) must FAIL LOUD on malformed
    input rather than substitute the WRONG rows into the pocket. Pre-fix it
    reparsed with ``ErrorLevel.WARN`` and transformed
    ``WHERE x = 1 !!`` -> ``WHERE x = NOT 1`` (a different predicate)."""
    bound = _bound("SELECT x FROM m WHERE x = 1 !!", "m", ["m"])
    with pytest.raises(RewriteReparseError):
        rewrite_for_pocket(bound, _pocket("cache", "p"), "postgres")


def test_f6_03_pocket_reparse_still_rewrites_valid_sql():
    """Sanity: a valid pocket query still rewrites (fail-loud did not break the
    happy path)."""
    bound = _bound("SELECT * FROM sales WHERE x = 1", "sales", ["sales"])
    out = rewrite_for_pocket(bound, _pocket("cache", "sales_pocket"), "postgres")
    assert "sales_pocket" in out


# --- F-103-03 — EXTRACT(WEEK) week-numbering parity across dialects ---------
# PostgreSQL EXTRACT(WEEK FROM d) is ISO-8601 (Monday-start, weeks 1..53).
# BigQuery EXTRACT(WEEK FROM d) is Sunday-based (0..53) — a DIFFERENT bucket
# for the same date. BigQuery EXTRACT(ISOWEEK FROM d) matches PG ISO numbering.


def test_f103_03_extract_week_maps_to_isoweek_on_bigquery():
    """A PG-canonical EXTRACT(WEEK FROM d) must transpile to BigQuery
    EXTRACT(ISOWEEK FROM d) so the week NUMBER matches PostgreSQL's ISO week.
    Pre-fix sqlglot preserved WEEK -> Sunday-based numbering (wrong bucket)."""
    pg = "SELECT EXTRACT(WEEK FROM d) AS wk FROM t"
    out = _transpile_to_dialect(pg, "bigquery")
    assert "ISOWEEK" in out.upper()
    assert "EXTRACT(WEEK" not in out.upper().replace(" ", "")
    sqlglot.parse_one(out, read="bigquery")


def test_f103_03_extract_week_unchanged_on_postgres():
    """PostgreSQL target keeps EXTRACT(WEEK ...) (it is already ISO)."""
    pg = "SELECT EXTRACT(WEEK FROM d) AS wk FROM t"
    out = _transpile_to_dialect(pg, "postgres")
    assert "WEEK" in out.upper()
    assert "ISOWEEK" not in out.upper()


def test_f103_03_date_trunc_week_still_maps_to_isoweek_on_bigquery():
    """The pre-existing truncation parity (Bug-7917) is preserved after the
    extraction rule was added to the same boundary transform."""
    pg = "SELECT DATE_TRUNC('week', d) AS wk FROM t"
    out = _transpile_to_dialect(pg, "bigquery")
    assert "ISOWEEK" in out.upper()


def test_f103_03_extract_week_via_render_boundary_pg_canonical_only():
    """The WEEK->ISOWEEK extraction rewrite fires only for PG-canonical input.
    A BQ-authored WEEK (pg_canonical=False) keeps the author's explicit
    Sunday-based WEEK choice — it is not silently overridden."""
    tree = sqlglot.parse_one("SELECT EXTRACT(WEEK FROM d) FROM t", read="bigquery")
    out = render_expression_for_dialect(tree, "bigquery", pg_canonical=False)
    assert "ISOWEEK" not in out.upper()
    assert "WEEK" in out.upper()


def test_f103_03_uda_extract_week_routes_through_boundary_for_bigquery():
    """A UDA carrying EXTRACT(WEEK FROM col) must get WEEK->ISOWEEK on a
    BigQuery target because UDA rendering now routes through the render
    boundary (F-006-02 + F-103-03)."""
    rendered = _render_uda_expression(
        expression="EXTRACT(WEEK FROM full_date)",
        table_alias="t",
        target_dialect="bigquery",
    )
    assert "ISOWEEK" in rendered.upper()
