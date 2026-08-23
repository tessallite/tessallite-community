"""Bug-982: BigQuery percentile rendering is CAPABILITY-SELECTED (exact vs approx).

A percentile measure that routes to a BigQuery source is built PostgreSQL-
canonical as ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)``. BigQuery has no
aggregate (GROUP BY) exact quantile — its ``PERCENTILE_CONT`` / ``PERCENTILE_DISC``
are analytic-only and the only aggregate quantile is the APPROXIMATE
``APPROX_QUANTILES``.

EXACT mode (the DEFAULT) must therefore rewrite the whole statement to the exact
analytic form ``PERCENTILE_CONT|DISC(col, frac IGNORE NULLS)
OVER (PARTITION BY <grain>)`` with ``SELECT DISTINCT`` and no ``GROUP BY``, or
raise the typed ``ExactQuantileUnavailable`` when the shape cannot be served
exactly — NEVER a silent ``APPROX_QUANTILES`` (a wrong number the caller
believes is exact).

APPROX mode (explicit opt-in) keeps the ``APPROX_QUANTILES`` generator transform.

These tests lock BOTH modes and the CONT/DISC and DESC semantics.

Known-value context (asserted where a live engine would compute it): exact CONT
p90 of ``[1, 100]`` = ``90.1`` (interpolated), NOT the ``APPROX_QUANTILES``
boundary ``100``; exact CONT p50 of ``[1,2,3,4]`` = ``2.5``; exact DISC p50 of
``[1,2,3,4]`` = ``2``. The renderer must emit CONT for a CONT request and DISC
for a DISC request so those values are produced by the engine.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
import pytest

from shared.sqlglot_compat import (
    ExactQuantileUnavailable,
    bigquery_approx_quantiles,
    register_bigquery_patches,
    rewrite_bigquery_exact_quantiles,
)

register_bigquery_patches()


def _exact_bq(pg_sql: str) -> str:
    """Apply the EXACT-mode statement rewrite and emit BigQuery SQL."""
    tree = sqlglot.parse_one(pg_sql, read="postgres")
    tree = rewrite_bigquery_exact_quantiles(tree)
    return tree.sql(dialect="bigquery")


def _approx_bq(pg_sql: str) -> str:
    """APPROX mode: transpile straight through the generator transform. Must be
    entered EXPLICITLY — in the default EXACT mode the generator fails closed."""
    with bigquery_approx_quantiles():
        return sqlglot.transpile(
            pg_sql, read="postgres", write="bigquery",
            error_level=sqlglot.ErrorLevel.IGNORE,
        )[0]


# ---------------------------------------------------------------------------
# EXACT mode (default) — analytic PERCENTILE_CONT/DISC OVER (PARTITION BY ...)
# ---------------------------------------------------------------------------

def test_exact_cont_becomes_analytic_over_partition():
    out = _exact_bq(
        'SELECT "region", PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "amount") '
        'AS p FROM t GROUP BY "region"'
    )
    # Exact analytic form: PERCENTILE_CONT(col, frac) OVER (PARTITION BY grain),
    # SELECT DISTINCT, IGNORE NULLS, and NO approximate / GROUP BY leak.
    assert "PERCENTILE_CONT(`amount`, 0.9 IGNORE NULLS)" in out
    assert "OVER (PARTITION BY `region`)" in out
    assert out.upper().startswith("SELECT DISTINCT")
    assert "IGNORE NULLS" in out
    assert "APPROX_QUANTILES" not in out
    assert "GROUP BY" not in out.upper()
    assert "WITHIN GROUP" not in out.upper()


def test_exact_cont_p90_uses_exact_fraction_not_approx_boundary():
    """Exact CONT p90 of [1, 100] = 90.1; the exact renderer must carry the
    fraction 0.9 into an analytic PERCENTILE_CONT, never the APPROX_QUANTILES
    boundary offset (which would return 100, a wrong number)."""
    out = _exact_bq(
        'SELECT PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "v") AS p FROM t'
    )
    assert "PERCENTILE_CONT(`v`, 0.9 IGNORE NULLS)" in out
    assert "APPROX_QUANTILES" not in out


def test_exact_disc_stays_discrete():
    """A DISC request must render PERCENTILE_DISC (exact DISC p50 of [1,2,3,4]
    = 2), never PERCENTILE_CONT (which would give 2.5) or APPROX."""
    out = _exact_bq(
        'SELECT "region", PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY "amount") '
        'AS p FROM t GROUP BY "region"'
    )
    assert "PERCENTILE_DISC(`amount`, 0.5 IGNORE NULLS)" in out
    assert "PERCENTILE_CONT" not in out
    assert "APPROX_QUANTILES" not in out


def test_exact_cont_desc_normalises_fraction():
    """Continuous interpolation is positionally symmetric: CONT(0.9) DESC ==
    CONT(0.1) ASC. The exact analytic call is ascending-only, so the fraction
    is normalised to 1 - p. Over [1, 100], CONT(0.9) DESC = 10.9 = CONT(0.1)."""
    out = _exact_bq(
        'SELECT PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "v" DESC) FROM t'
    )
    assert "PERCENTILE_CONT(`v`, 0.1 IGNORE NULLS)" in out


def test_exact_global_quantile_uses_empty_over():
    """A global (no grain) percentile renders OVER () — a valid analytic call
    over the whole relation."""
    out = _exact_bq(
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "v") AS p FROM t'
    )
    assert "OVER ()" in out
    assert "PERCENTILE_CONT(`v`, 0.5 IGNORE NULLS)" in out


def test_exact_multiple_percentiles_same_partition_ok():
    """Two percentiles at the same grain are both constant per partition, so
    SELECT DISTINCT dedupe stays sound."""
    out = _exact_bq(
        'SELECT "region", '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p50, '
        'PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "amount") AS p90 '
        'FROM t GROUP BY "region"'
    )
    assert out.upper().startswith("SELECT DISTINCT")
    assert "PERCENTILE_CONT(`amount`, 0.5 IGNORE NULLS)" in out
    assert "PERCENTILE_CONT(`amount`, 0.9 IGNORE NULLS)" in out
    assert "APPROX_QUANTILES" not in out


# ---------------------------------------------------------------------------
# EXACT mode — typed EXACT_QUANTILE_UNAVAILABLE where exact is unachievable
# ---------------------------------------------------------------------------

def test_exact_disc_desc_is_unavailable():
    """Discrete DESC has no data-independent ascending equivalent (spec §4.1),
    and BigQuery's analytic PERCENTILE_DISC is ascending-only -> typed error,
    never a silent approximate or a wrong ascending value."""
    with pytest.raises(ExactQuantileUnavailable) as exc:
        _exact_bq(
            'SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY "v" DESC) FROM t'
        )
    assert exc.value.reason == "EXACT_QUANTILE_UNAVAILABLE"


def test_exact_mixed_aggregate_is_unavailable():
    """SELECT DISTINCT dedupe is only sound when every projection is constant
    per partition; a percentile mixed with SUM at the same level is a deferred
    hybrid plan -> typed error, never a wrong dedup."""
    with pytest.raises(ExactQuantileUnavailable) as exc:
        _exact_bq(
            'SELECT "region", SUM("amount") AS s, '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )
    assert exc.value.reason == "EXACT_QUANTILE_UNAVAILABLE"


def test_exact_nonliteral_fraction_is_unavailable():
    """A parameterised / computed fraction cannot be an exact analytic constant
    -> typed error, never a runtime APPROX offset."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT PERCENTILE_CONT(pct_col) WITHIN GROUP (ORDER BY "v") FROM t'
        )


def test_exact_percentile_in_having_is_unavailable():
    """A percentile that is not a direct selected column (here in HAVING) is
    outside the Phase-0 minimum-core renderer -> typed error."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region" FROM t GROUP BY "region" '
            'HAVING PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") > 100'
        )


def test_exact_having_is_unavailable():
    """A HAVING filter cannot be reproduced over the analytic rewrite; dropping
    the GROUP BY would orphan it (invalid BigQuery) -> typed error (Fable R1 #2)."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region", '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region" HAVING COUNT(*) > 5'
        )


def test_exact_aggregate_in_order_by_is_unavailable():
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region", '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region" ORDER BY SUM("amount")'
        )


def test_exact_rollup_grain_is_unavailable():
    """ROLLUP produces multiple grains (subtotal rows) a single PARTITION BY
    cannot express -> typed error, never a silent global percentile (Fable R1 #3)."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "a", "b", '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY ROLLUP("a", "b")'
        )


def test_exact_positional_grain_is_unavailable():
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region", '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY 1'
        )


def test_exact_window_projection_is_unavailable():
    """A window function that numbers source rows is not constant per partition;
    under SELECT DISTINCT it would explode rows -> typed error (Fable R1 #4)."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT ROW_NUMBER() OVER (ORDER BY "region") AS rn, '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


def test_exact_bare_nongrain_column_is_unavailable():
    """A bare non-grain source column alongside the percentile is not constant
    per partition; under SELECT DISTINCT it would return one row per distinct
    (region, amount) — a row explosion, not one row per group -> typed error."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region", "amount", '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


def test_exact_constant_and_grain_projections_ok():
    """Grain columns (in GROUP BY) and pure constants ARE constant per partition
    and remain servable exactly."""
    out = _exact_bq(
        'SELECT "region", 1 AS one, '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
        'FROM t GROUP BY "region"'
    )
    assert out.upper().startswith("SELECT DISTINCT")
    assert "PERCENTILE_CONT(`amount`, 0.5 IGNORE NULLS)" in out
    assert "OVER (PARTITION BY `region`)" in out


def test_exact_grain_not_projected_is_unavailable():
    """The projected tuple must identify the partition; a GROUP BY key missing
    from the projection lets SELECT DISTINCT merge distinct groups with equal
    projected values -> silent row collapse. Fail closed (Fable R2 #1)."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


def test_exact_partial_grain_projection_is_unavailable():
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "g1", '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "g1", "g2"'
        )


def test_exact_grain_only_in_complex_expression_is_unavailable():
    """A grain key bound only inside a CASE bucket (Bug-879: not projected bare)
    is not a projected grain key, so groups sharing the same bucket + equal
    percentile would collapse -> fail closed."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            "SELECT CASE WHEN \"region\" = 'a' THEN 'x' ELSE \"region\" END AS r, "
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


def test_exact_full_grain_projected_ok():
    """When every grain key is projected, the group is returned exactly once."""
    out = _exact_bq(
        'SELECT "g1", "g2", '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
        'FROM t GROUP BY "g1", "g2"'
    )
    assert out.upper().startswith("SELECT DISTINCT")
    assert "OVER (PARTITION BY `g1`, `g2`)" in out


def test_exact_volatile_projection_is_unavailable():
    """A volatile function (RANDOM/UUID/CURRENT_*) is NOT constant per partition
    even with no column references; under SELECT DISTINCT every row stays
    distinct (row explosion) -> fail closed (Fable R3 #1)."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region", RANDOM() AS r, '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


def test_exact_volatile_over_grain_is_unavailable():
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            'SELECT "region", (RANDOM() + LENGTH("region")) AS x, '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


def test_exact_deterministic_expression_over_grain_ok():
    """A DETERMINISTIC expression over the grain key (UPPER(region), also the
    GROUP BY key) is constant per partition and remains servable."""
    out = _exact_bq(
        'SELECT UPPER("region") AS r, '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
        'FROM t GROUP BY UPPER("region")'
    )
    assert out.upper().startswith("SELECT DISTINCT")
    assert "PERCENTILE_CONT(`amount`, 0.5 IGNORE NULLS)" in out


@pytest.mark.parametrize(
    "volatile_sql",
    [
        # ANSI / dedicated-node clock and session functions ...
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "LOCALTIMESTAMP",
        "LOCALTIME",
        "CURRENT_USER",
        "SESSION_USER",
        "CURRENT_SCHEMA",
        "CURRENT_CATALOG",
        # ... UTC synonyms, random-string, session/db, and Anonymous ...
        "UTC_TIMESTAMP()",
        "UTC_DATE()",
        "UTC_TIME()",
        "RANDSTR(10, 1)",
        "CURRENT_SESSION()",
        "CURRENT_DATABASE()",
        "RANDOM()",
        "UUID()",
        "NOW()",
        "CLOCK_TIMESTAMP()",
        "NEXTVAL('seq')",
        # ... and a volatile buried inside an expression over a grain column.
        "EXTRACT(EPOCH FROM LOCALTIMESTAMP)",
        "(\"region\" || RANDOM()::text)",
    ],
)
def test_exact_column_free_function_projection_is_unavailable(volatile_sql):
    """Any volatile / session / non-deterministic value fails closed — proven
    STRUCTURALLY (a column-free function call), NOT via a volatile-name list.
    This closes the whole class permanently: LOCALTIMESTAMP/UTC_TIMESTAMP/
    RANDSTR/SESSION_USER/CURRENT_SESSION and every dialect synonym are
    column-free functions, so none can slip the guard the way individual names
    did across the R5 adversarial rounds."""
    with pytest.raises(ExactQuantileUnavailable):
        _exact_bq(
            f'SELECT "region", {volatile_sql} AS v, '
            'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
            'FROM t GROUP BY "region"'
        )


@pytest.mark.parametrize(
    "deterministic_sql",
    [
        'UPPER("region")',
        'LENGTH("region")',
        "('region-' || \"region\")",
        "(\"region\" || '-suffix')",
        "CONCAT(\"region\", '-x')",
        "CASE WHEN \"region\" = 'a' THEN 'x' ELSE \"region\" END",
        "1",
        "100 * 2",
        "'label'",
    ],
)
def test_exact_deterministic_over_grain_projection_is_served(deterministic_sql):
    """A DETERMINISTIC expression over the grain column (or a pure literal) is
    constant per partition and must STILL be served exactly — the structural
    volatile guard must not over-reject a deterministic function of a grain
    key (whose subtree references the grain column)."""
    out = _exact_bq(
        f'SELECT "region", {deterministic_sql} AS v, '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "amount") AS p '
        'FROM t GROUP BY "region"'
    )
    assert out.upper().startswith("SELECT DISTINCT")
    assert "PERCENTILE_CONT(`amount`, 0.5 IGNORE NULLS)" in out


def test_approx_context_is_task_scoped_no_async_leak():
    """The approx-mode context is task-scoped (ContextVar): a concurrent asyncio
    task must NOT observe another task's approx mode (Fable R2 #3)."""
    import asyncio
    from shared.sqlglot_compat import _approx_quantiles_enabled

    async def _leaker():
        with bigquery_approx_quantiles():
            await asyncio.sleep(0.01)

    async def _observer():
        await asyncio.sleep(0.005)
        return _approx_quantiles_enabled()

    async def _main():
        results = await asyncio.gather(_leaker(), _observer())
        return results[1]

    assert asyncio.run(_main()) is False


def test_generator_fails_closed_in_exact_mode():
    """Fail-closed backstop (Fable R1 #1): an un-rewritten percentile WITHIN
    GROUP reaching the BigQuery generator in the default EXACT mode must raise,
    never emit a silent APPROX_QUANTILES."""
    with pytest.raises(ExactQuantileUnavailable):
        sqlglot.transpile(
            "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t",
            read="postgres", write="bigquery",
            error_level=sqlglot.ErrorLevel.IGNORE,
        )


def test_generator_approx_only_under_explicit_opt_in():
    """The SAME statement renders APPROX_QUANTILES only inside the explicit
    approximate-mode context."""
    with bigquery_approx_quantiles():
        out = sqlglot.transpile(
            "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t",
            read="postgres", write="bigquery",
            error_level=sqlglot.ErrorLevel.IGNORE,
        )[0]
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(50)]" in out


def test_approx_mode_context_is_restored():
    """After leaving the approx context, exact mode (fail-closed) is restored."""
    with bigquery_approx_quantiles():
        pass
    with pytest.raises(ExactQuantileUnavailable):
        sqlglot.transpile(
            "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t",
            read="postgres", write="bigquery",
            error_level=sqlglot.ErrorLevel.IGNORE,
        )


def test_exact_no_percentile_statement_unchanged():
    """A statement with no percentile is returned unchanged (byte-identical
    guarantee for every non-quantile query)."""
    pg = 'SELECT "region", SUM("amount") FROM t GROUP BY "region"'
    out = rewrite_bigquery_exact_quantiles(sqlglot.parse_one(pg, read="postgres"))
    assert out.sql(dialect="postgres") == pg


# ---------------------------------------------------------------------------
# APPROX mode (explicit opt-in) — APPROX_QUANTILES retained
# ---------------------------------------------------------------------------

def test_approx_cont_becomes_approx_quantiles():
    out = _approx_bq(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) AS p "
        "FROM t GROUP BY region"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(50)]" in out
    assert "WITHIN GROUP" not in out


def test_approx_offset_ascending_p90():
    out = _approx_bq(
        "SELECT region, PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount) AS p "
        "FROM t GROUP BY region"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(90)]" in out


def test_approx_desc_inverts_offset():
    out = _approx_bq(
        "SELECT region, PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount DESC) "
        "AS p FROM t GROUP BY region"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(10)]" in out


def test_approx_literal_uses_half_up_rounding():
    out = _approx_bq(
        "SELECT PERCENTILE_CONT(0.125) WITHIN GROUP (ORDER BY amount) AS p FROM t"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(13)]" in out


def test_approx_nonliteral_fraction_renders_runtime_round():
    asc = _approx_bq(
        "SELECT PERCENTILE_CONT(pct_col) WITHIN GROUP (ORDER BY amount) FROM t"
    )
    assert "OFFSET(CAST(ROUND(pct_col * 100) AS INT64))" in asc
    desc = _approx_bq(
        "SELECT PERCENTILE_CONT(pct_col) WITHIN GROUP (ORDER BY amount DESC) FROM t"
    )
    assert "OFFSET(100 - CAST(ROUND(pct_col * 100) AS INT64))" in desc


def test_approx_disc_also_rewritten():
    out = _approx_bq(
        "SELECT PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY amount) AS p FROM t"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(25)]" in out


def test_approx_qualified_identifier_quoting_preserved():
    out = _approx_bq(
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "fact"."amt") FROM t'
    )
    assert "APPROX_QUANTILES(`fact`.`amt`, 100)[OFFSET(50)]" in out


# ---------------------------------------------------------------------------
# Non-BigQuery targets and non-percentile WITHIN GROUP are unaffected
# ---------------------------------------------------------------------------

def test_postgres_output_unchanged():
    """Postgres remains exact-quantile via the native WITHIN GROUP form."""
    out = sqlglot.transpile(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t",
        read="postgres", write="postgres",
        error_level=sqlglot.ErrorLevel.IGNORE,
    )[0]
    assert "PERCENTILE_CONT(0.5)" in out
    assert "WITHIN GROUP" in out
    assert "APPROX_QUANTILES" not in out


def test_register_is_idempotent():
    register_bigquery_patches()
    register_bigquery_patches()
    out = _approx_bq(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t"
    )
    assert "APPROX_QUANTILES(amount, 100)[OFFSET(50)]" in out


def test_non_percentile_within_group_falls_through_exact():
    """A non-percentile WITHIN GROUP (STRING_AGG) is not a percentile, so the
    exact rewrite leaves the statement unchanged (no analytic / no error)."""
    pg = "SELECT STRING_AGG(name, ',') WITHIN GROUP (ORDER BY name) FROM t"
    tree = rewrite_bigquery_exact_quantiles(sqlglot.parse_one(pg, read="postgres"))
    out = tree.sql(dialect="bigquery")
    assert "APPROX_QUANTILES" not in out
    assert "OVER (" not in out


def test_non_percentile_within_group_falls_through_approx():
    sql = "SELECT STRING_AGG(name, ',') WITHIN GROUP (ORDER BY name) FROM t"
    out = _approx_bq(sql)
    assert "APPROX_QUANTILES" not in out


def test_approx_fallback_paths_render_through_default_generator():
    """Directly exercise the non-percentile fallback paths against the BigQuery
    generator so the delegation calls the real ``withingroup_sql`` method."""
    from sqlglot.dialects.bigquery import BigQuery

    gen = BigQuery.Generator()
    node = exp.WithinGroup(
        this=exp.GroupConcat(this=exp.column("name")),
        expression=exp.Order(expressions=[exp.Ordered(this=exp.column("name"))]),
    )
    rendered = gen.sql(node)
    assert "APPROX_QUANTILES" not in rendered
    assert "WITHIN GROUP" in rendered

    node_no_order = exp.WithinGroup(this=exp.GroupConcat(this=exp.column("name")))
    assert "APPROX_QUANTILES" not in gen.sql(node_no_order)
