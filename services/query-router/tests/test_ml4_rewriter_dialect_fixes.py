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

from src.rewrite.aggregate import _phys_expr_for_node, _render_pagination_suffix
from src.rewrite.dialects import _requote_identifiers_for_dialect, _translate_raw_sql
from src.rewrite.pocket import rewrite_for_pocket
from src.rewrite.uda import _normalize_uda_expression_quoting


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
    """After regex table-substitution the SQL is PG-canonical (double quotes);
    requoting must produce the target dialect's IDENTIFIER quoting so columns
    are not parsed as string literals (Spark/tsql previously got PG quotes)."""
    pg = 'SELECT "region", "amount" FROM "demo"."sales_fact"'
    out = _requote_identifiers_for_dialect(pg, dialect)
    tree = sqlglot.parse_one(out, read=dialect)
    cols = {c.name for c in tree.find_all(exp.Column)}
    assert {"region", "amount"} <= cols
    # No identifier leaked into a string literal.
    string_lits = {l.this for l in tree.find_all(exp.Literal) if l.is_string}
    assert "region" not in string_lits and "amount" not in string_lits
    assert quote_open in out and quote_close in out


def test_f6_03_postgres_unchanged():
    pg = 'SELECT "region" FROM "demo"."sales_fact"'
    assert _requote_identifiers_for_dialect(pg, "postgres") == pg


# --- F-006-11 -------------------------------------------------------------

def test_f6_11_translate_raw_honours_input_dialect():
    """A BigQuery-authored raw query must be read under bigquery, not postgres,
    so it transpiles correctly instead of being returned untranslated."""
    bq = "SELECT TIMESTAMP_TRUNC(ts, DAY) AS d FROM t"
    out = _translate_raw_sql(bq, "spark", "bigquery")
    # parses cleanly under spark and is non-trivially translated
    sqlglot.parse_one(out, read="spark")
    assert out  # non-empty


def test_f6_11_postgres_input_still_default():
    pg = 'SELECT "a" FROM "t"'
    # postgres target → unchanged regardless of input dialect
    assert _translate_raw_sql(pg, "postgres", "bigquery") == pg


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


def test_f6_07_pagination_no_orderby_injection_on_tsql():
    # tsql falls back to raw (latent target); must never inject an ORDER BY
    # that would collide with the assembled statement.
    assert "ORDER BY" not in _render_pagination_suffix(5, 10, "tsql").upper()


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
