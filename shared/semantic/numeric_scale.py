"""Numeric scale helpers for aggregate CTAS column rendering (Bug-5454).

Single source of truth shared by BOTH the optimizer CREATE path
(``optimizer/src/ddl/postgres_ddl.build_pg_ctas``) and the scheduler REFRESH
path (``scheduler/src/ddl/postgres_ddl`` via
``shared/semantic/aggregate_select_builder.build_select_parts``). The optimizer
re-exports this module from ``optimizer/src/ddl/_numeric_scale.py`` so there is
exactly one implementation — a divergence between create and refresh is what
let Bug-5454 regress in the first place (the typed create was undone by the
first plain-SUM refresh), so the two paths must round identically.

Root cause: ``SUM(numeric(18,2))`` yields an *unconstrained* PostgreSQL
``numeric`` whose total carries no fixed scale, so a zero sum renders "0"
instead of "0.00". A same-engine CTAS (``build_pg_ctas``) inherits that
unconstrained value, so the materialised aggregate cache loses the source
measure's rendered scale.

Fix: when the source measure column is a constrained ``numeric(p,s)`` /
``decimal(p,s)`` and the aggregation is scale-preserving (sum / min / max),
wrap the aggregate in ``ROUND(<agg>, s)`` so the cached value carries the
source scale and renders "0.00".

Why ROUND, not ``CAST(... AS numeric(p,s))``: PostgreSQL returns SUM as
*unconstrained* numeric precisely because the sum of N rows can exceed any
single row's precision. Casting the sum back to ``numeric(p,s)`` would raise
``numeric field overflow`` (ERROR 22003) once the total exceeds ``p`` digits —
a real value-affecting regression. ``ROUND(SUM(x), s)`` fixes the *scale* (the
actual symptom: "0" vs "0.00") with no precision ceiling and therefore no
overflow. ``s`` never exceeds the source value's own scale, so ROUND never
truncates a real fractional digit — only representation changes, never value.

These helpers never invent a scale: an unconstrained ``numeric`` (no
parenthesised scale) or a non-numeric type returns ``None`` and the caller
leaves the aggregate untouched.

PostgreSQL-only by design. BigQuery/Spark aggregate tables are built by their
own CTAS builders and have different numeric semantics — they never call into
this module. sqlglot transpiles the resulting PG ``ROUND(..., s)`` to
redshift/snowflake/sqlserver, where the two-argument ROUND is valid.
"""
from __future__ import annotations

import re

# Aggregations whose result type preserves the operand's numeric scale in
# PostgreSQL. AVG -> double precision and COUNT -> bigint are NOT scale-
# preserving and must never be cast to numeric(p,s).
SCALE_PRESERVING_AGGS: frozenset[str] = frozenset({"sum", "min", "max"})

# Matches "numeric(18,2)" / "decimal(10, 4)" (case/space-insensitive). A bare
# "numeric" / "decimal" with no parentheses does NOT match — by design, so an
# unconstrained source type yields no cast.
_NUMERIC_PS_RE = re.compile(
    r"^\s*(?:numeric|decimal)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*$",
    re.IGNORECASE,
)


def parse_numeric_type(source_type: str | None) -> tuple[int, int] | None:
    """Parse a ``numeric(p,s)`` / ``decimal(p,s)`` type string.

    Returns ``(precision, scale)`` when the type is a *constrained* numeric,
    else ``None`` (bare numeric, non-numeric, or unparseable). A scale that
    exceeds the precision is rejected as malformed.
    """
    if not source_type:
        return None
    m = _NUMERIC_PS_RE.match(source_type)
    if not m:
        return None
    precision = int(m.group(1))
    scale = int(m.group(2))
    if precision <= 0 or scale < 0 or scale > precision:
        return None
    return precision, scale


def format_scale_round(expr: str, scale: int) -> str:
    """Wrap ``expr`` in a PG ``ROUND(<expr>, scale)``.

    ROUND fixes the *rendered scale* of the aggregate value (so a zero sum is
    "0.00", not "0") without imposing a precision ceiling — it cannot overflow
    the way ``CAST(... AS numeric(p,s))`` would on a large SUM. ``scale`` equals
    the source column's own scale, so no real fractional digit is ever dropped:
    only representation changes, never the value.
    """
    return f"ROUND({expr}, {scale})"


def cast_for_agg(expr: str, agg: str, source_type: str | None) -> str:
    """Return ``expr`` scale-normalised when appropriate, else ``expr`` unchanged.

    Wraps in ``ROUND(expr, s)`` only when ``agg`` is scale-preserving
    (sum/min/max) AND ``source_type`` is a constrained ``numeric(p,s)``. In every
    other case the expression is returned unchanged — no scale is invented and no
    precision ceiling is imposed (so a large SUM cannot overflow).
    """
    if agg.lower() not in SCALE_PRESERVING_AGGS:
        return expr
    parsed = parse_numeric_type(source_type)
    if parsed is None:
        return expr
    _precision, scale = parsed
    return format_scale_round(expr, scale)
