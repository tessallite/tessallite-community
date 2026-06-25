"""Bug-982: BigQuery percentile rendering on the source route.

A percentile measure that routes to a BigQuery source is built PostgreSQL-
canonical as ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)`` and transpiled
to BigQuery at the return boundary. BigQuery's ``PERCENTILE_CONT`` /
``PERCENTILE_DISC`` are analytic-only (require ``OVER()``) and are invalid in a
GROUP BY aggregate query, so the WITHIN GROUP form errors on real BigQuery.

The BigQuery dialect patch (``shared.sqlglot_compat._bq_within_group_sql``)
rewrites the construct to ``APPROX_QUANTILES(col, 100)[OFFSET(ROUND(p * 100))]``
via a sqlglot generator TRANSFORM — no per-connector if-branch in the SQL
builder, no change to the forbidden rewrite engines. These tests lock that
contract.
"""
from __future__ import annotations

import sqlglot

from shared.sqlglot_compat import register_bigquery_patches

register_bigquery_patches()


def _bq(sql: str) -> str:
    return sqlglot.transpile(
        sql, read="postgres", write="bigquery",
        error_level=sqlglot.ErrorLevel.IGNORE,
    )[0]


def test_percentile_cont_becomes_approx_quantiles_bigquery():
    out = _bq(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) AS p "
        "FROM t GROUP BY region"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(50)]" in out
    # The invalid analytic form must NOT survive.
    assert "WITHIN GROUP" not in out
    assert "PERCENTILE_CONT" not in out


def test_percentile_offset_ascending_p90():
    out = _bq(
        "SELECT region, PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount) AS p "
        "FROM t GROUP BY region"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(90)]" in out


def test_percentile_desc_inverts_offset():
    """APPROX_QUANTILES always sorts ASCENDING, so the 90th percentile counted
    in DESCENDING order is the ascending boundary at OFFSET(100 - 90) = 10.
    Stripping DESC and keeping OFFSET(90) would silently return the wrong
    quantile."""
    out = _bq(
        "SELECT region, PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount DESC) AS p "
        "FROM t GROUP BY region"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(10)]" in out


def test_percentile_literal_uses_half_up_rounding():
    """BigQuery ROUND is half-up; the folded literal offset must match it.
    0.125 * 100 = 12.5 -> 13 (half-up), not 12 (Python banker's rounding)."""
    out = _bq("SELECT PERCENTILE_CONT(0.125) WITHIN GROUP (ORDER BY amount) AS p FROM t")
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(13)]" in out


def test_percentile_nonliteral_fraction_renders_runtime_round():
    """A parameterised / expression fraction stays a runtime ROUND(... * 100)
    so the construct is valid for non-constant percentiles, with DESC handled
    via 100 - ROUND(...)."""
    asc = _bq(
        "SELECT PERCENTILE_CONT(pct_col) WITHIN GROUP (ORDER BY amount) FROM t"
    )
    assert "OFFSET(CAST(ROUND(pct_col * 100) AS INT64))" in asc
    desc = _bq(
        "SELECT PERCENTILE_CONT(pct_col) WITHIN GROUP (ORDER BY amount DESC) FROM t"
    )
    assert "OFFSET(100 - CAST(ROUND(pct_col * 100) AS INT64))" in desc


def test_percentile_disc_also_rewritten():
    out = _bq("SELECT PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY amount) AS p FROM t")
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(25)]" in out


def test_qualified_identifier_quoting_preserved():
    """The rewrite goes through the sqlglot generator, so identifier quoting
    (BigQuery backticks) is applied to the ordered column, not bypassed."""
    out = _bq(
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "fact"."amt") FROM t'
    )
    assert "APPROX_QUANTILES(`fact`.`amt`, 100)[OFFSET(50)]" in out


def test_postgres_output_unchanged():
    """Postgres remains exact-quantile: the WITHIN GROUP form must be emitted
    verbatim for the postgres target (no APPROX_QUANTILES leak)."""
    out = sqlglot.transpile(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t",
        read="postgres", write="postgres",
        error_level=sqlglot.ErrorLevel.IGNORE,
    )[0]
    assert "PERCENTILE_CONT(0.5)" in out
    assert "WITHIN GROUP" in out
    assert "APPROX_QUANTILES" not in out


def test_register_is_idempotent():
    """Calling the registrar repeatedly must not stack or break the transform."""
    register_bigquery_patches()
    register_bigquery_patches()
    out = _bq("SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t")
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(50)]" in out


def test_non_percentile_within_group_falls_through():
    """STRING_AGG ... WITHIN GROUP (a non-percentile within-group aggregate)
    must NOT be turned into APPROX_QUANTILES — the patch only targets
    percentile nodes and defers everything else to the default rendering."""
    sql = "SELECT STRING_AGG(name, ',') WITHIN GROUP (ORDER BY name) FROM t"
    out = _bq(sql)
    assert "APPROX_QUANTILES" not in out


def test_fallback_paths_render_through_default_generator():
    """Directly exercise the two non-percentile fallback paths against the
    BigQuery generator so the delegation calls the real generator method
    (``withingroup_sql``) and does not raise. A wrong method name would crash
    every non-percentile WITHIN GROUP that reaches the generator — the
    parse-level STRING_AGG test above does not reach it because sqlglot folds
    GROUP_CONCAT before the generator sees a WithinGroup node."""
    from sqlglot import exp
    from sqlglot.dialects.bigquery import BigQuery

    gen = BigQuery.Generator()
    # Non-percentile aggregate inside WITHIN GROUP (with an ORDER).
    node = exp.WithinGroup(
        this=exp.GroupConcat(this=exp.column("name")),
        expression=exp.Order(expressions=[exp.Ordered(this=exp.column("name"))]),
    )
    rendered = gen.sql(node)
    assert "APPROX_QUANTILES" not in rendered
    assert "WITHIN GROUP" in rendered

    # WITHIN GROUP with no resolvable ordered column also defers, not crashes.
    node_no_order = exp.WithinGroup(this=exp.GroupConcat(this=exp.column("name")))
    assert "APPROX_QUANTILES" not in gen.sql(node_no_order)
