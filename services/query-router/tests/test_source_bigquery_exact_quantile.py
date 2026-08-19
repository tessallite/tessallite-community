"""Bug-982: source-route BigQuery quantile capability selection at the
``_build_source_sql`` transpile boundary (``_bigquery_exact_quantile_sql``).

A MEDIAN / percentile measure that falls to a BigQuery source is built
PostgreSQL-canonical as ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)`` and
converted at the single ``_final_transpile`` boundary. This test locks that the
boundary helper:

- rewrites the aggregate percentile into the EXACT analytic form for BigQuery
  (never a silent ``APPROX_QUANTILES``);
- returns ``None`` for a PostgreSQL target and for a non-percentile statement so
  those paths stay byte-identical to before;
- raises the typed ``ExactQuantileUnavailable`` when the shape cannot be served
  exactly, so the caller can route on it.

Known-value intent (computed by a live engine, documented here): exact CONT p90
of ``[1, 100]`` = ``90.1`` (NOT the APPROX boundary ``100``); exact CONT p50 of
``[1,2,3,4]`` = ``2.5``; exact DISC p50 = ``2``.
"""
from __future__ import annotations

import pytest

from src.rewrite.source_sql import _bigquery_exact_quantile_sql
from shared.sqlglot_compat import ExactQuantileUnavailable


# The PG-canonical statement _build_source_sql produces for MEDIAN(amount)
# GROUP BY region (MEDIAN -> internal p50 -> PERCENTILE_CONT(0.5) WITHIN GROUP).
_MEDIAN_BY_REGION = (
    'SELECT "base"."region" AS "region", '
    'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "base"."amount") AS "med" '
    'FROM "demo"."sales" AS "base" GROUP BY "base"."region"'
)

_P90_GLOBAL = (
    'SELECT PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "base"."v") AS "p90" '
    'FROM "demo"."t" AS "base"'
)


def test_bigquery_target_renders_exact_analytic():
    out = _bigquery_exact_quantile_sql(_MEDIAN_BY_REGION, "bigquery")
    assert out is not None
    assert out.upper().startswith("SELECT DISTINCT")
    assert "PERCENTILE_CONT(`base`.`amount`, 0.5 IGNORE NULLS)" in out
    assert "OVER (PARTITION BY `base`.`region`)" in out
    assert "APPROX_QUANTILES" not in out
    assert "GROUP BY" not in out.upper()


def test_bigquery_p90_carries_exact_fraction_not_approx_boundary():
    """Exact CONT p90 of [1, 100] = 90.1; the emitted SQL must carry fraction
    0.9 into an analytic PERCENTILE_CONT, never an APPROX_QUANTILES boundary
    (which would return 100)."""
    out = _bigquery_exact_quantile_sql(_P90_GLOBAL, "bigquery")
    assert out is not None
    assert "PERCENTILE_CONT(`base`.`v`, 0.9 IGNORE NULLS)" in out
    assert "APPROX_QUANTILES" not in out


def test_postgres_target_returns_none():
    """PostgreSQL keeps its native exact WITHIN GROUP path — the helper opts out
    (returns None) so the normal transpile boundary runs unchanged."""
    assert _bigquery_exact_quantile_sql(_MEDIAN_BY_REGION, "postgres") is None
    assert _bigquery_exact_quantile_sql(_MEDIAN_BY_REGION, "postgresql") is None


def test_non_percentile_bigquery_returns_none():
    """A BigQuery statement without a percentile is not touched by the helper
    (returns None) so it flows through the normal transpile boundary."""
    pg = 'SELECT "base"."region", SUM("base"."amount") FROM t AS "base" GROUP BY "base"."region"'
    assert _bigquery_exact_quantile_sql(pg, "bigquery") is None


def test_volatile_projection_raises_typed_error():
    """A volatile session/time function (incl. the ANSI LOCALTIMESTAMP synonym)
    projected alongside the percentile is not constant per partition -> typed
    error at the source-route boundary too (Opus adversarial R5 break)."""
    pg = (
        'SELECT "base"."region" AS "region", LOCALTIMESTAMP AS "ts", '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "base"."amount") AS "med" '
        'FROM t AS "base" GROUP BY "base"."region"'
    )
    with pytest.raises(ExactQuantileUnavailable) as exc:
        _bigquery_exact_quantile_sql(pg, "bigquery")
    assert exc.value.reason == "EXACT_QUANTILE_UNAVAILABLE"


def test_mixed_aggregate_raises_typed_error():
    """A percentile mixed with SUM at the same grain cannot be deduped safely by
    SELECT DISTINCT -> typed EXACT_QUANTILE_UNAVAILABLE, never a wrong number."""
    pg = (
        'SELECT "base"."region" AS "region", SUM("base"."amount") AS "s", '
        'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "base"."amount") AS "med" '
        'FROM t AS "base" GROUP BY "base"."region"'
    )
    with pytest.raises(ExactQuantileUnavailable) as exc:
        _bigquery_exact_quantile_sql(pg, "bigquery")
    assert exc.value.reason == "EXACT_QUANTILE_UNAVAILABLE"


def test_bug_7841_grainless_percentile_wraps_in_outer_aggregate():
    """Bug-7841: a GLOBAL (grainless, no GROUP BY) percentile on BigQuery must
    return exactly ONE row with NULL on empty input, matching the PostgreSQL
    ordered-set aggregate's cardinality.

    The exact analytic rewrite (SELECT DISTINCT ... OVER ()) returns 0 rows on
    empty input.  The fix wraps the grainless rewrite in an outer one-row
    aggregate: SELECT MIN(alias) AS alias FROM (inner) _sub.
    An aggregate-with-no-GROUP-BY returns 1 row (NULL) on empty input.
    """
    out = _bigquery_exact_quantile_sql(_P90_GLOBAL, "bigquery")
    assert out is not None
    upper = out.upper()
    # The outer query must be an aggregate (MIN wrapping), not SELECT DISTINCT.
    assert upper.startswith("SELECT")
    assert "MIN(" in upper
    # The inner subquery must still use SELECT DISTINCT ... OVER ()
    assert "SELECT DISTINCT" in upper
    assert "OVER ()" in out
    # No GROUP BY in the outer or inner queries.
    assert "GROUP BY" not in upper
    # No APPROX_QUANTILES anywhere.
    assert "APPROX_QUANTILES" not in upper
    # The result alias must be preserved through the outer wrap.
    assert "`p90`" in out


def test_bug_7841_grainless_constant_projection_not_nulled():
    """Codex gate Finding A: a grainless exact percentile with a constant
    projection (e.g. 'active' AS c) must preserve the constant on empty input.

    PG semantics: SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) AS p,
    'active' AS c FROM empty_table  =>  (NULL, 'active') -- 1 row, constant
    survives the empty aggregate group.

    The outer wrap must NOT MIN-wrap the constant column -- MIN('active') over
    0 rows returns NULL, silently nulling the constant.  Instead, the constant
    must be emitted directly in the outer SELECT.
    """
    pg = (
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP "
        '(ORDER BY "base"."v") AS "p", '
        "'active' AS \"c\" "
        'FROM "demo"."t" AS "base"'
    )
    out = _bigquery_exact_quantile_sql(pg, "bigquery")
    assert out is not None
    upper = out.upper()
    # The percentile column must be MIN-wrapped.
    assert "MIN(" in upper
    # The constant 'active' must appear directly in the outer SELECT,
    # NOT inside a MIN() call.
    # Find the constant in the output and verify it's not inside MIN.
    assert "'active'" in out, f"constant literal missing: {out}"
    # Verify MIN('active') is NOT present (the constant must be bare).
    assert "MIN('active')" not in out, (
        f"constant projection was MIN-wrapped (would NULL on empty input): {out}"
    )


def test_bug_7841_unaliased_percentile_gets_synthesized_alias():
    """Wrap guard: an unaliased grainless percentile must get a synthesized
    alias (_q0) so the outer MIN references a named column."""
    pg = (
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "base"."v") '
        'FROM "demo"."t" AS "base"'
    )
    out = _bigquery_exact_quantile_sql(pg, "bigquery")
    assert out is not None
    # The synthesized alias must appear in the output.
    assert "_q0" in out, f"synthesized alias _q0 missing: {out}"
    assert "MIN(" in out.upper()


def test_bug_7841_two_percentiles_both_min_wrapped():
    """Wrap guard: two grainless percentiles must both be MIN-wrapped and
    the inner must produce one DISTINCT row for non-empty input."""
    pg = (
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "base"."v") AS "p50", '
        'PERCENTILE_CONT(0.9) WITHIN GROUP '
        '(ORDER BY "base"."v") AS "p90" '
        'FROM "demo"."t" AS "base"'
    )
    out = _bigquery_exact_quantile_sql(pg, "bigquery")
    assert out is not None
    upper = out.upper()
    # Both aliases must be MIN-wrapped (sqlglot may or may not backtick
    # the outer column reference depending on the identifier).
    assert "MIN(p50)" in out or "MIN(`p50`)" in out, (
        f"p50 not MIN-wrapped: {out}"
    )
    assert "MIN(p90)" in out or "MIN(`p90`)" in out, (
        f"p90 not MIN-wrapped: {out}"
    )
    # Both aliases must appear somewhere in the output.
    assert "p50" in out
    assert "p90" in out
    # Inner must use SELECT DISTINCT.
    assert "SELECT DISTINCT" in upper


def test_bug_7841_grouped_percentile_not_wrapped():
    """Wrap guard: a GROUPED (non-grainless) percentile must NOT be wrapped --
    the standard SELECT DISTINCT ... OVER (PARTITION BY) is correct because
    0 groups means 0 rows on both paths."""
    out = _bigquery_exact_quantile_sql(_MEDIAN_BY_REGION, "bigquery")
    assert out is not None
    upper = out.upper()
    assert upper.startswith("SELECT DISTINCT")
    assert "MIN(" not in upper


def test_bug_7841_limit_zero_hoisted_to_outer():
    """Regression 1 guard: LIMIT 0 must be hoisted to the outer select so the
    result is 0 rows (PG semantics: aggregate first, then limit)."""
    pg = (
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "base"."v") AS "p" '
        'FROM "demo"."t" AS "base" LIMIT 0'
    )
    out = _bigquery_exact_quantile_sql(pg, "bigquery")
    assert out is not None
    upper = out.upper()
    # LIMIT must be on the OUTER query, not inside the subquery.
    # The outer query structure: SELECT MIN(...) FROM (...) _sub LIMIT 0
    # The inner subquery must NOT contain LIMIT.
    # Split at FROM to isolate inner/outer.
    assert "LIMIT" in upper, f"LIMIT missing from output: {out}"
    # Verify LIMIT is after _sub (on the outer, not inner).
    sub_pos = out.find("_sub")
    limit_pos = upper.rfind("LIMIT")
    assert sub_pos < limit_pos, (
        f"LIMIT should be after _sub (outer), not inside: {out}"
    )


def test_bug_7841_offset_hoisted_to_outer():
    """Regression 1 guard: OFFSET must be hoisted to the outer select."""
    pg = (
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "base"."v") AS "p" '
        'FROM "demo"."t" AS "base" OFFSET 1'
    )
    out = _bigquery_exact_quantile_sql(pg, "bigquery")
    assert out is not None
    upper = out.upper()
    assert "OFFSET" in upper, f"OFFSET missing from output: {out}"
    sub_pos = out.find("_sub")
    offset_pos = upper.rfind("OFFSET")
    assert sub_pos < offset_pos, (
        f"OFFSET should be after _sub (outer), not inside: {out}"
    )


def test_bug_7841_union_all_grainless_fails_closed():
    """Regression 2 guard: a grainless percentile inside a UNION ALL must
    fail closed (ExactQuantileUnavailable), not silently drop the other
    branch."""
    pg = (
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "base"."v") AS "p" '
        'FROM "demo"."t" AS "base" '
        'UNION ALL SELECT 1 AS "p"'
    )
    with pytest.raises(ExactQuantileUnavailable):
        _bigquery_exact_quantile_sql(pg, "bigquery")


def test_bug_7841_select_star_grainless_fails_closed():
    """Regression 3 guard: SELECT *, PERCENTILE... in the grainless case
    must fail closed, not emit invalid SQL."""
    pg = (
        'SELECT *, PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "base"."v") AS "p" '
        'FROM "demo"."t" AS "base"'
    )
    # This should either raise ExactQuantileUnavailable (for the star) or
    # for the mixed non-constant projection.
    with pytest.raises(ExactQuantileUnavailable):
        _bigquery_exact_quantile_sql(pg, "bigquery")
