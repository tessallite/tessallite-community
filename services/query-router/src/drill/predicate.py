"""Drill-through predicate compiler.

Compiles drill filter / grouping-level dictionaries into canonical SQL
predicate strings using **sqlglot expression nodes** rather than hand-rolled
f-string concatenation. sqlglot renders each literal type-correctly and
escapes embedded quotes, so a fact column named ``"; DROP TABLE orders; --``
or a value containing a single quote is bound as a typed literal, never
spliced into the SQL text.

The output is canonical PostgreSQL-flavoured SQL; the downstream rewriter
transpiles it to the connector dialect via sqlglot (the single SQL authority
for this stack). No connector branching happens here.

Supported operators (reconciled with the documented API surface):
    eq, neq, gt, gte, lt, lte, like, ilike, in, between, is_null, is_not_null
"""
from __future__ import annotations

from typing import Any, Sequence

from sqlglot import expressions as exp


class DrillPredicateError(Exception):
    """Raised when a filter / grouping level is structurally invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = code


_SUPPORTED_OPS = {
    "eq", "neq", "gt", "gte", "lt", "lte",
    "like", "ilike", "in", "between", "is_null", "is_not_null",
}

_BINARY_OP = {
    "eq": exp.EQ,
    "neq": exp.NEQ,
    "gt": exp.GT,
    "gte": exp.GTE,
    "lt": exp.LT,
    "lte": exp.LTE,
}


def _literal(value: Any) -> exp.Expression:
    """Build a typed sqlglot literal node for a Python scalar.

    sqlglot owns the rendering/escaping, so this is the type-safe binding
    point — no caller ever concatenates a raw value into SQL.
    """
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _column(name: str) -> exp.Column:
    # quoted=True forces sqlglot to emit a double-quoted identifier and to
    # escape any embedded quote in the name.
    return exp.column(name, quoted=True)


def compile_predicate(spec: dict[str, Any]) -> exp.Expression | None:
    """Compile one filter/grouping-level dict into a sqlglot boolean node.

    ``spec`` shape: ``{"column": str, "op": str, "value": Any}``.
    ``op`` defaults to ``"eq"``. ``None`` value with a default ``eq`` op is
    treated as ``IS NULL`` (back-compat with the cell-coordinate contract).
    Returns ``None`` when the spec has no column (skipped).
    """
    col_name = spec.get("column")
    if not col_name:
        return None
    op = (spec.get("op") or "eq").lower()
    if op not in _SUPPORTED_OPS:
        raise DrillPredicateError(
            "DrillThroughUnsupportedOperator",
            f"Unsupported drill operator '{op}'. Supported: "
            f"{', '.join(sorted(_SUPPORTED_OPS))}.",
        )
    value = spec.get("value")
    col = _column(col_name)

    if op == "is_null":
        return exp.Is(this=col, expression=exp.Null())
    if op == "is_not_null":
        return exp.Not(this=exp.Is(this=col, expression=exp.Null()))

    # eq with a null value is the cell-coordinate "this dimension is null"
    # case — render IS NULL so it matches rather than ``= NULL`` (always
    # false).
    if op == "eq" and value is None:
        return exp.Is(this=col, expression=exp.Null())
    if op == "neq" and value is None:
        return exp.Not(this=exp.Is(this=col, expression=exp.Null()))

    if op in _BINARY_OP:
        return _BINARY_OP[op](this=col, expression=_literal(value))

    if op in ("like", "ilike"):
        if not isinstance(value, str):
            raise DrillPredicateError(
                "DrillThroughBadOperand",
                f"Operator '{op}' requires a string value.",
            )
        node = exp.Like if op == "like" else exp.ILike
        return node(this=col, expression=_literal(value))

    if op == "in":
        if not isinstance(value, (list, tuple)) or len(value) == 0:
            raise DrillPredicateError(
                "DrillThroughBadOperand",
                "Operator 'in' requires a non-empty list value.",
            )
        return exp.In(this=col, expressions=[_literal(v) for v in value])

    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise DrillPredicateError(
                "DrillThroughBadOperand",
                "Operator 'between' requires a [low, high] value.",
            )
        low, high = value
        return exp.Between(
            this=col, low=_literal(low), high=_literal(high)
        )

    # Unreachable: every supported op handled above.
    raise DrillPredicateError(
        "DrillThroughUnsupportedOperator", f"Unhandled operator '{op}'."
    )


def compile_where_expression(specs: Sequence[dict[str, Any]]) -> exp.Expression | None:
    """Bug-7285: compile filter specs into a sqlglot boolean expression tree.

    Returns the combined AND-chained sqlglot expression node (not a string)
    so callers can embed it directly into a sqlglot AST and render the whole
    query once at the correct target dialect, instead of rendering predicates
    to a postgres string that gets re-parsed.  Returns ``None`` for an empty
    list.
    """
    nodes: list[exp.Expression] = []
    for spec in specs:
        node = compile_predicate(spec)
        if node is not None:
            nodes.append(node)
    if not nodes:
        return None
    combined: exp.Expression = nodes[0]
    for node in nodes[1:]:
        combined = exp.And(this=combined, expression=node)
    return combined


def compile_where(specs: Sequence[dict[str, Any]]) -> str:
    """Compile a list of filter/grouping-level specs into a canonical SQL
    ``WHERE`` clause (without the leading ``WHERE``). Empty list → "".
    """
    combined = compile_where_expression(specs)
    if combined is None:
        return ""
    # Render canonical (postgres) SQL; downstream rewriter transpiles.
    return combined.sql(dialect="postgres")


def quote_ident(name: str) -> str:
    """Canonical, escaped identifier quoting via sqlglot (mirrors the
    frontend ``quoteIdent``). Doubles embedded quotes — closes F-019-12.
    """
    return _column(name).sql(dialect="postgres")
