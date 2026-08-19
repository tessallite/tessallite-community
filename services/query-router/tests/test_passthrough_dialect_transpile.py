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

from conftest import attach_fixture_deployed_shape

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
    await attach_fixture_deployed_shape(bq, db)
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
    await attach_fixture_deployed_shape(bq, db)
    sql = await rewrite_for_source(bq, db, target_dialect="postgres")

    # Bug-7915: AST-based table substitution normalizes unit casing
    # ('month' -> 'MONTH') via the sqlglot round-trip. Both forms are
    # valid PG SQL; the semantic is identical.
    assert '"demo"."sales"' in sql, f"Table not substituted: {sql}"
    assert '"order_date"' in sql, f"Column ident missing: {sql}"
    assert "DATE_TRUNC(" in sql, f"DATE_TRUNC missing: {sql}"
    assert "COUNT(*)" in sql, f"COUNT(*) missing: {sql}"
    assert "GROUP BY" in sql, f"GROUP BY missing: {sql}"
    # Must NOT contain backticks (PG target).
    assert "`" not in sql, f"Backticks leaked into PG output: {sql}"


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
    await attach_fixture_deployed_shape(bq, db)
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
    await attach_fixture_deployed_shape(bq, db)
    sql = await rewrite_for_source(bq, db, target_dialect=dialect)
    assert trunc_fragment in sql, sql
    assert open_quote in sql, sql
    # ANSI double-quoted identifiers must not leak into a backtick/bracket dialect.
    assert '"order_date"' not in sql, sql
    assert sqlglot.parse_one(sql, read=dialect) is not None


# --- Bug-6035: passthrough AUTHORED in BigQuery syntax against a BigQuery model.
# The prior helpers hardcoded read="postgres"; a BQ-authored passthrough
# (backtick identifiers + BQ-native TIMESTAMP_TRUNC(col, MONTH)) does not parse
# under postgres, so the requote helper dropped to a regex that re-quotes
# identifiers but CANNOT transpile/validate dialect FUNCTION syntax. Threading
# input_dialect through the read ladder parses it under bigquery first.
_BQ_AUTHORED_RAW = (
    "SELECT `region`, TIMESTAMP_TRUNC(`order_date`, MONTH) AS period "
    "FROM golden WHERE `region` = 'EMEA' GROUP BY `region`, "
    "TIMESTAMP_TRUNC(`order_date`, MONTH)"
)


def _bq_authored_bound():
    return G._bound(
        measures=[], dimensions=[], grain=[],
        from_tables=[G.MODEL_SLUG], raw_query=_BQ_AUTHORED_RAW,
        has_passthrough_expressions=True,
        input_dialect="bigquery",
    )


@pytest.mark.asyncio
async def test_bigquery_authored_passthrough_transpiles_cleanly():
    """Bug-6035: a BigQuery-authored passthrough must emit valid BigQuery SQL —
    physical table substituted, BQ-native function preserved, and the whole
    statement re-parses as bigquery (proving the sqlglot boundary ran, not the
    lossy regex fallback that would have surfaced the un-validated form)."""
    bq = _bq_authored_bound()
    db = G._fact_db()
    await attach_fixture_deployed_shape(bq, db)
    sql = await rewrite_for_source(bq, db, target_dialect="bigquery")

    # Physical table substituted with BigQuery backtick quoting.
    assert "`demo`.`sales`" in sql, sql
    # BQ-native TIMESTAMP_TRUNC(col, MONTH) preserved (arg order + bare keyword).
    assert "TIMESTAMP_TRUNC(" in sql, sql
    assert "MONTH" in sql, sql
    # No ANSI double-quoted identifiers leaked into the BigQuery output.
    assert '"' not in sql, sql
    # The crux: emission re-parses cleanly in its own dialect.
    assert sqlglot.parse_one(sql, read="bigquery") is not None


def test_requote_ladder_honours_input_dialect_known_sql():
    """Unit pin on the transpile boundary: a BQ-authored raw statement
    transpiles to KNOWN BigQuery through _translate_raw_sql. A PG-authored
    statement goes through _transpile_to_dialect."""
    from src.rewrite.dialects import _translate_raw_sql, _transpile_to_dialect

    # BQ-authored input: _translate_raw_sql honours the BQ read dialect.
    bq_authored = (
        "SELECT `region`, TIMESTAMP_TRUNC(`order_ts`, MONTH) AS m "
        "FROM `phys_ds`.`phys_fact` WHERE `region` = 'EMEA'"
    )
    out = _translate_raw_sql(bq_authored, "bigquery", "bigquery")
    assert "`region`" in out, out
    assert "TIMESTAMP_TRUNC" in out, out

    # PG-authored input: _transpile_to_dialect converts correctly.
    pg = 'SELECT DATE_TRUNC(\'month\', "d") AS p FROM "s"."t"'
    out2 = _transpile_to_dialect(pg, "bigquery")
    assert "TIMESTAMP_TRUNC(`d`, MONTH) AS p FROM `s`.`t`" in out2, out2


def test_bug_7012_unparseable_sql_raises_passthrough_error():
    """Bug-7012: when sqlglot cannot parse the SQL, the function must raise
    PassthroughTranspileError instead of attempting a regex requote.  A regex
    cannot disambiguate identifiers from string literals on BigQuery without
    silently corrupting results in both directions."""
    import pytest
    from src.rewrite.dialects import (
        _requote_identifiers_for_bigquery,
        PassthroughTranspileError,
    )
    # MATCHES keyword causes sqlglot ParseError on all dialects.
    unparseable = (
        'SELECT "col_a" FROM "schema"."tbl" '
        'WHERE status IN ("active","closed") '
        'AND col MATCHES "pattern"'
    )
    with pytest.raises(PassthroughTranspileError):
        _requote_identifiers_for_bigquery(unparseable)


def test_bug_7012_values_unparseable_raises():
    """Bug-7012: VALUES with unparseable syntax raises, not silently requotes."""
    import pytest
    from src.rewrite.dialects import (
        _requote_identifiers_for_bigquery,
        PassthroughTranspileError,
    )
    unparseable = 'INSERT INTO "schema"."tbl" VALUES ("active","closed") MATCHES x'
    with pytest.raises(PassthroughTranspileError):
        _requote_identifiers_for_bigquery(unparseable)


def test_bug_7012_function_args_unparseable_raises():
    """Bug-7012: function args with unparseable syntax raises."""
    import pytest
    from src.rewrite.dialects import (
        _requote_identifiers_for_bigquery,
        PassthroughTranspileError,
    )
    unparseable = 'SELECT col FROM "s"."t" WHERE fn("active","closed") MATCHES x'
    with pytest.raises(PassthroughTranspileError):
        _requote_identifiers_for_bigquery(unparseable)


def test_bug_7012_match_recognize_unparseable_raises():
    """Bug-7012 Codex gate repro: MATCH_RECOGNIZE with internal clauses is
    unparseable by sqlglot and must raise PassthroughTranspileError, not
    silently return a query where double-quoted tokens may be corrupted."""
    import pytest
    from src.rewrite.dialects import (
        _requote_identifiers_for_bigquery,
        PassthroughTranspileError,
    )
    # MATCH_RECOGNIZE with PARTITION BY + MEASURES is genuinely unparseable
    # by sqlglot under both postgres and bigquery read dialects.
    unparseable = (
        'SELECT "first_status" FROM "s"."t" '
        'MATCH_RECOGNIZE (PARTITION BY "x" ORDER BY ts '
        'MEASURES "first_status" AS "fs")'
    )
    with pytest.raises(PassthroughTranspileError):
        _requote_identifiers_for_bigquery(unparseable)


def test_bug_7012_parseable_sql_still_works():
    """Bug-7012: SQL that sqlglot CAN parse must still be correctly transpiled
    to BigQuery (the happy path is unchanged)."""
    from src.rewrite.dialects import _requote_identifiers_for_bigquery
    # Standard SQL that sqlglot parses successfully.
    parseable = 'SELECT "col_a" FROM "schema"."tbl" WHERE "status" = \'active\''
    result = _requote_identifiers_for_bigquery(parseable)
    # sqlglot AST path: identifiers become backticks, string stays single-quoted.
    assert '`col_a`' in result, f"identifier not backtick-quoted: {result}"
    assert '`schema`' in result, f"schema not backtick-quoted: {result}"
    assert '`tbl`' in result, f"table not backtick-quoted: {result}"
    assert "'active'" in result, f"string literal corrupted: {result}"


def test_bug_7012_value_position_literal_via_sqlglot_correct():
    """Bug-7012: parseable SQL with a double-quoted identifier after = is
    correctly handled by sqlglot (it knows "active" after = is a column
    reference in PG, and backtick-quotes it).  This is the happy path."""
    from src.rewrite.dialects import _requote_identifiers_for_bigquery
    # In PG, "active" after = IS an identifier (column reference).
    parseable = 'SELECT "col" FROM "tbl" WHERE "col" = "other_col"'
    result = _requote_identifiers_for_bigquery(parseable)
    assert '`col`' in result
    assert '`tbl`' in result
    assert '`other_col`' in result
