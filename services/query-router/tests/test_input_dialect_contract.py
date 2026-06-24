"""
Contract tests for the input-dialect boundary.

The query-router must parse incoming SQL in the dialect the producer wrote
it in, not in a single hardcoded dialect. Identifier quoting differs by
dialect (Postgres ``"x"`` vs BigQuery ``` `x` ```), and silently misparsing
quoted identifiers as string literals produces "non-integer constant in
GROUP BY" errors at the source DB.

These tests pin the contract: for every supported input dialect, a
representative ``SELECT col, SUM(m) FROM model [WHERE …] GROUP BY col``
must parse with the column extracted as a column — not a literal.
"""
import pytest

from src.parsing.sql_parser import _normalize_dialect, parse_sql_to_ir


# ---------------------------------------------------------------------------
# Dialect alias normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "alias, canonical",
    [
        (None, "postgres"),
        ("", "postgres"),
        ("postgres", "postgres"),
        ("postgresql", "postgres"),
        ("PostgreSQL", "postgres"),
        ("PG", "postgres"),
        ("jdbc", "postgres"),     # JDBC = Postgres wire protocol
        ("bigquery", "bigquery"),
        ("BigQuery", "bigquery"),
        ("spark", "spark"),
        ("spark_sql", "spark"),
        ("hadoop_spark", "spark"),
    ],
)
def test_dialect_alias_normalises(alias, canonical):
    assert _normalize_dialect(alias) == canonical


# ---------------------------------------------------------------------------
# Quoting contract: column references must come out as columns, not literals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "input_dialect, sql",
    [
        # Postgres / ANSI / Snowflake style: double-quoted identifiers.
        ("postgres",
         'SELECT "channel_code", SUM(amount) FROM model GROUP BY "channel_code"'),
        ("postgresql",
         'SELECT "channel_code", SUM(amount) FROM model GROUP BY "channel_code"'),
        # BigQuery / MySQL style: backtick identifiers.
        ("bigquery",
         "SELECT `channel_code`, SUM(amount) FROM model GROUP BY `channel_code`"),
    ],
)
def test_quoted_identifier_parses_as_column(input_dialect, sql):
    """Regression for the original pivot-table bug: a quoted identifier in
    GROUP BY must end up in ``grain`` as the column name, not be silently
    swallowed as a string literal."""
    q = parse_sql_to_ir(sql, "model-1", input_dialect=input_dialect)
    assert q.grain == ["channel_code"], (
        f"GROUP BY identifier dropped under input_dialect={input_dialect!r}; "
        f"grain={q.grain!r}"
    )
    assert "channel_code" in q.requested_dimensions
    assert "amount" in q.requested_measures


def test_default_dialect_is_postgres():
    """No ``input_dialect`` argument should behave the same as Postgres —
    that is the canonical internal dialect, and JDBC clients (which speak
    the Postgres wire protocol) rely on this default."""
    sql = 'SELECT "active_flag", COUNT(*) FROM t GROUP BY "active_flag"'
    q = parse_sql_to_ir(sql, "model-1")
    assert q.grain == ["active_flag"]


def test_postgres_pivot_shape_round_trip():
    """End-to-end shape the MeasureQueryPanel emits."""
    sql = (
        'SELECT "channel_code", "active_flag", "base_amount" '
        'FROM "modely" '
        'GROUP BY "channel_code", "active_flag"'
    )
    q = parse_sql_to_ir(sql, "model-1", input_dialect="postgresql")
    assert set(q.grain) == {"channel_code", "active_flag"}
    # base_amount appears in the SELECT list with no aggregate function — the
    # binder will resolve it as a measure using its default_agg, but the
    # parser sees a bare reference, which is the producer's contract here.
    assert "base_amount" in (q.requested_measures + q.requested_dimensions)


def test_input_dialect_recorded_on_logical_query():
    """The rewriter's raw-AST re-parses must use the same dialect the
    parser used; the IR carries the value to keep them consistent."""
    q = parse_sql_to_ir(
        'SELECT "x" FROM t', "m1", input_dialect="postgresql",
    )
    assert q.input_dialect == "postgres"

    q2 = parse_sql_to_ir(
        "SELECT `x` FROM t", "m1", input_dialect="bigquery",
    )
    assert q2.input_dialect == "bigquery"
