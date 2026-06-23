"""End-to-end seam test: parse_sql_to_ir → rewrite_for_source.

Bug-102 (column-vs-column WHERE silently dropped during rewrite) lived
in the seam between the parser and the rewriter. Each side was tested
in isolation — the parser asserted ``f.operator == "gt"`` for shapes
it understood; the rewriter took a hand-built ``LogicalFilter`` and
emitted SQL — but no test wired the two together for an unhandled
WHERE shape. So a predicate that the parser silently dropped to
``filters=[]`` reached a rewriter that dutifully emitted no WHERE.

This module covers that seam for the dangerous shapes the user
specifically asked us to verify (column arithmetic on each side,
mixed subquery operands, all-literal/all-column predicates):

    a+b > a*b
    a+b > (subquery)
    a   > b+(subquery)
    (subquery) > (subquery)
    (subquery) > literal
    (subquery) > a
    1+a+b = 2 + c

For each: parse the SQL, build a minimal BoundQuery, and call
``rewrite_for_source`` with ``db=None``. The rewriter's ``db is None``
path returns the raw query verbatim — that's the API-boundary
guarantee we're verifying: the seam between parser and rewriter does
not lose the predicate. The rebuild path (``_build_source_sql`` with
real metadata) is covered by the demo-data integration suite.
"""
from __future__ import annotations

import types

import pytest

from src.ir.logical_query import BoundQuery
from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import rewrite_for_source


def _bind(raw_sql: str) -> BoundQuery:
    lq = parse_sql_to_ir(raw_sql, "m-1")
    model = types.SimpleNamespace(id="m-1", slug="t", deployed_version_id="v1")
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
    )


# (raw_sql, substring that MUST appear in the rewritten output, label)
_DANGEROUS_WHERE_SHAPES = [
    (
        "SELECT a FROM t WHERE a + b > a * b",
        "a + b > a * b",
        "arith-vs-arith",
    ),
    (
        "SELECT a FROM t WHERE a + b > (SELECT MAX(x) FROM u)",
        "a + b > (SELECT MAX(x) FROM u)",
        "arith-vs-subquery",
    ),
    (
        "SELECT a FROM t WHERE a > b + (SELECT MAX(x) FROM u)",
        "a > b + (SELECT MAX(x) FROM u)",
        "col-vs-arith-plus-subquery",
    ),
    (
        "SELECT a FROM t WHERE (SELECT MAX(x) FROM u) > (SELECT MIN(y) FROM v)",
        "(SELECT MAX(x) FROM u) > (SELECT MIN(y) FROM v)",
        "subquery-vs-subquery",
    ),
    (
        "SELECT a FROM t WHERE (SELECT MAX(x) FROM u) > 100",
        "(SELECT MAX(x) FROM u) > 100",
        "subquery-vs-literal",
    ),
    (
        "SELECT a FROM t WHERE (SELECT MAX(x) FROM u) > a",
        "(SELECT MAX(x) FROM u) > a",
        "subquery-vs-col",
    ),
    (
        "SELECT a FROM t WHERE 1 + a + b = 2 + c",
        "1 + a + b = 2 + c",
        "literal-plus-cols-eq-literal-plus-col",
    ),
    (
        "SELECT a FROM t WHERE (a, b) IN (SELECT x, y FROM u)",
        "(a, b) IN (SELECT x, y FROM u)",
        "tuple-in-subquery",
    ),
    (
        "SELECT a FROM t WHERE 1 + a IN (SELECT x FROM u)",
        "1 + a IN (SELECT x FROM u)",
        "arith-in-subquery",
    ),
    (
        "SELECT a FROM t WHERE CONCAT('1', a) = '1foo'",
        "CONCAT('1', a) = '1foo'",
        "function-call-eq-literal",
    ),
]


@pytest.mark.parametrize(
    "raw_sql, must_contain, label",
    _DANGEROUS_WHERE_SHAPES,
    ids=[s[2] for s in _DANGEROUS_WHERE_SHAPES],
)
def test_parser_flags_dangerous_where_shape(raw_sql, must_contain, label):
    """The parser must flag every dangerous WHERE shape so the rewriter
    takes the raw-WHERE preservation path. Without this flag, the
    extractor's incomplete ``filters`` would drive a silent-drop
    rebuild (the Bug-102 regression class)."""
    q = parse_sql_to_ir(raw_sql, "m-1")
    assert q.has_unresolvable_where, (
        f"WHERE shape {label!r} not flagged unresolvable; the rewriter "
        f"would rebuild the SELECT from extracted filters and drop or "
        f"misrepresent the predicate. raw_sql={raw_sql!r}, "
        f"filters={q.filters!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_sql, must_contain, label",
    _DANGEROUS_WHERE_SHAPES,
    ids=[s[2] for s in _DANGEROUS_WHERE_SHAPES],
)
async def test_rewrite_for_source_preserves_dangerous_where(raw_sql, must_contain, label):
    """API-boundary seam: parsing then rewriting (with no metadata
    available) must NOT silently drop the WHERE predicate. The
    ``db=None`` path falls back to the raw query — the test asserts
    the predicate text survives that fallback. The rebuild path with
    real metadata is exercised by the integration suite."""
    bq = _bind(raw_sql)
    rewritten = await rewrite_for_source(bq, db=None)
    assert rewritten, f"rewriter returned empty string for {label}"
    # Normalise whitespace so the assertion isn't sensitive to a stray
    # double-space the formatter might introduce later.
    norm_out = " ".join(rewritten.split())
    norm_expected = " ".join(must_contain.split())
    assert norm_expected in norm_out, (
        f"{label}: rewriter dropped or mangled the WHERE predicate. "
        f"expected substring {norm_expected!r} not in rewritten "
        f"{norm_out!r}"
    )
