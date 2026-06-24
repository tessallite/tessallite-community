"""Regression tests for Bug-5339 / Bug-5340 — passthrough dialect transpile.

A free-form / passthrough query routed through ``rewrite_for_source`` →
``_substitute_table_names`` is kept PostgreSQL-canonical until the SINGLE
sqlglot transpile boundary (``_requote_identifiers_for_dialect``). Previously
the FROM table was pre-quoted in the *target* dialect (BigQuery backticks)
before transpile, mixing backtick-quoted tables with double-quoted columns. The
resulting statement could not be parsed as ``read="postgres"``, so the requote
helper fell back to a conservative regex that re-quotes identifiers but CANNOT
rewrite dialect-specific function syntax. PostgreSQL ``DATE_TRUNC('month', col)``
therefore reached BigQuery un-transpiled (HTTP 400 invalidQuery) — Bug-5339 —
and the KPI preview sparkline (Bug-5340) sent the same query through the same
path and silently rendered empty.

These tests pin:
1. A passthrough ``DATE_TRUNC`` query transpiles to the correct per-dialect
   function form with native identifier quoting (BigQuery ``TIMESTAMP_TRUNC``).
2. The same input against a PostgreSQL target is unchanged (no-regression).
3. The substituted statement parses as ``read="postgres"`` (proving the single
   transpile boundary runs, not the regex fallback).

The offline scaffold (FakeDB, ``_bound``, ``_fact_db``) is reused from the
golden render harness, whose physical fact table is ``demo.sales``.
"""
from __future__ import annotations

import sqlglot
import pytest

import test_render_golden as G
from src.rewrite.query_rewriter import rewrite_for_source
from src.rewrite.source_sql import _substitute_table_names


# Free-form passthrough query authored in PostgreSQL-canonical form: PG
# DATE_TRUNC('month', col) with ANSI double-quoted identifiers, against the
# model slug "golden" (substituted to physical demo.sales).
_DATE_TRUNC_RAW = (
    "SELECT DATE_TRUNC('month', \"order_date\") AS period, COUNT(*) AS n "
    "FROM golden GROUP BY DATE_TRUNC('month', \"order_date\")"
)


def _passthrough_bound():
    return G._bound(
        measures=[], dimensions=[], grain=[],
        from_tables=[G.MODEL_SLUG], raw_query=_DATE_TRUNC_RAW,
        has_passthrough_expressions=True,
    )


@pytest.mark.asyncio
async def test_passthrough_date_trunc_transpiles_for_bigquery():
    """Bug-5339: PG DATE_TRUNC must become BigQuery TIMESTAMP_TRUNC(col, MONTH)
    with backtick identifiers — produced by sqlglot, not the regex fallback."""
    bq = _passthrough_bound()
    db = G._fact_db()
    sql = await rewrite_for_source(bq, db, target_dialect="bigquery")

    # Correct BigQuery TRUNC form: function name + opposite argument order.
    assert "TIMESTAMP_TRUNC(" in sql, sql
    assert "MONTH" in sql, sql
    # PG DATE_TRUNC must NOT survive untouched (the regex-fallback symptom).
    assert "DATE_TRUNC('month'" not in sql, sql
    # BigQuery identifier quoting: backticks, never ANSI double quotes for idents.
    assert "`order_date`" in sql, sql
    assert "`demo`.`sales`" in sql, sql
    assert '"order_date"' not in sql, sql
    # The emission must re-parse cleanly in its own dialect.
    assert sqlglot.parse_one(sql, read="bigquery") is not None


@pytest.mark.asyncio
async def test_passthrough_date_trunc_unchanged_for_postgres():
    """No-regression pin: a PostgreSQL target keeps the statement PG-canonical
    (only the FROM table is substituted to its physical reference)."""
    bq = _passthrough_bound()
    db = G._fact_db()
    sql = await rewrite_for_source(bq, db, target_dialect="postgres")

    expected = (
        "SELECT DATE_TRUNC('month', \"order_date\") AS period, COUNT(*) AS n "
        'FROM "demo"."sales" GROUP BY DATE_TRUNC(\'month\', "order_date")'
    )
    assert sql == expected, sql


@pytest.mark.asyncio
async def test_substituted_statement_parses_as_postgres():
    """Prove the single transpile boundary runs (no regex fallback): the
    table-substituted statement must parse as ``read="postgres"``. If it did
    not, the requote helper would silently regex-fallback and leave dialect
    function syntax un-transpiled (the Bug-5339 root cause)."""
    bq = _passthrough_bound()
    db = G._fact_db()
    # The bigquery connector exercises the requote-for-dialect path; assert the
    # PG-canonical substitution it produces upstream parses as postgres.
    substituted = await _substitute_table_names(bq, db, connector="postgresql")
    assert substituted is not None
    # PG-canonical: physical table double-quoted, no backticks anywhere.
    assert '"demo"."sales"' in substituted, substituted
    assert "`" not in substituted, substituted
    # The crux: parses cleanly as postgres -> the downstream transpile runs.
    assert sqlglot.parse_one(substituted, read="postgres") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dialect,trunc_fragment,open_quote",
    [
        ("bigquery", "TIMESTAMP_TRUNC(", "`"),
        ("spark", "`", "`"),
        ("tsql", "DATETRUNC(", "["),
    ],
)
async def test_passthrough_no_regression_across_dialects(
    dialect, trunc_fragment, open_quote,
):
    """Every non-PG passthrough target transpiles via sqlglot (native quoting
    + dialect function syntax), never returns raw PG-canonical SQL."""
    bq = _passthrough_bound()
    db = G._fact_db()
    sql = await rewrite_for_source(bq, db, target_dialect=dialect)
    assert trunc_fragment in sql, sql
    assert open_quote in sql, sql
    # ANSI double-quoted identifiers must not leak into a backtick/bracket dialect.
    assert '"order_date"' not in sql, sql
    assert sqlglot.parse_one(sql, read=dialect) is not None
