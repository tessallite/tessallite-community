"""Parsing and validation for calculated-measure expressions (Phase 4A).

A calculated measure carries a SQL-flavoured expression like::

    safe_div(measure("gross_margin"), measure("sales"))

References to other measures use the ``measure("name")`` function form
(Q9=D in ``docs/archive/archive_phase-4-questions.md``).
At parse time we replace every ``measure("...")`` call with a placeholder
token, run the remainder through ``sqlglot``, and then validate that the
resulting AST is inside the function whitelist.

Only the *string form* is parsed here. Expansion into concrete SQL (using
the referenced measure's ``source_column_id`` and ``default_agg``) is the
responsibility of the query-router rewriter.

Also provides :func:`detect_cycles`, which takes a map of
``measure_id -> referenced_measure_ids`` and returns a list of cycles for
save-time rejection. Even though v1 is single-pass (calculated measures
can only reference ``standard`` / ``variant`` measures), we keep cycle
detection ready so the multi-pass v2 can reuse it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

import sqlglot
from sqlglot import exp


# Placeholder token template used to stand in for ``measure("name")``
# calls while sqlglot parses the rest of the expression. The regex allows
# any identifier characters plus dots / spaces in measure names.
_MEASURE_REF_RE = re.compile(
    r"""measure\(\s*        # 'measure(' with optional whitespace
        ["']([^"']+)["']    # quoted measure name (captured)
        \s*\)""",
    re.VERBOSE,
)

# Placeholder inserted in the SQL text given to sqlglot. We embed the
# measure name as a bare identifier inside a backticked alias so sqlglot
# treats it as a column reference that we then post-process back into a
# MeasureReference.
_PLACEHOLDER_PREFIX = "__tessallite_measure_ref__"


# Functions we allow inside a calculated-measure expression.
#
#   - Arithmetic is covered via AST node kinds, not names.
#   - Whitelisted scalar functions below map to operations that work
#     uniformly across Postgres / BigQuery / Spark once sqlglot renders
#     the target dialect.
#   - ``safe_div`` / ``safe_ratio`` are Tessallite helpers: they expand
#     into ``CASE WHEN den = 0 THEN NULL ELSE num / den END`` at rewrite
#     time. We accept them as function calls here and defer the
#     expansion.
_ALLOWED_FUNCTIONS: frozenset[str] = frozenset({
    "coalesce", "nullif", "abs", "greatest", "least", "round",
    "safe_div", "safe_ratio",
})

# sqlglot models CASE WHEN and IF as Func subclasses, but they are
# structural control flow rather than scalar functions. Accept them
# unconditionally.
_STRUCTURAL_FUNC_TYPES: tuple[type, ...] = (
    exp.Case,
    exp.If,
)


# Node kinds that are never safe inside a calculated measure. Everything
# else is treated as structural (identifiers, parens, comparisons, CASE
# arms, ...) since the real threat model is "no subqueries, no window
# functions, no inline aggregates, no undeclared function calls" rather
# than a closed whitelist of AST shapes.
_FORBIDDEN_NODE_TYPES: tuple[type, ...] = (
    exp.Subquery,
    exp.Window,
    exp.AggFunc,
    exp.Select,
    exp.From,
    exp.Join,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Union,
    exp.CTE,
)


class ExpressionValidationError(ValueError):
    """Raised when a calculated-measure expression fails parse/validation."""


@dataclass(frozen=True)
class MeasureReference:
    """A single ``measure("name")`` invocation found in an expression."""
    name: str
    placeholder: str


@dataclass(frozen=True)
class ParsedExpression:
    """Result of parsing and validating a calculated-measure expression.

    The AST is a sqlglot ``Expression`` with each ``measure("name")`` call
    replaced by a ``Column(_PLACEHOLDER_PREFIX + N)`` node. The rewriter
    uses :meth:`render_with_substitutions` to re-emit SQL after swapping
    placeholders for the underlying aggregated expressions.
    """
    original: str
    ast: exp.Expression
    references: tuple[MeasureReference, ...]

    @property
    def referenced_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.references)


def parse_expression(expression: str) -> ParsedExpression:
    """Parse ``expression`` into a :class:`ParsedExpression`.

    Raises :class:`ExpressionValidationError` on any of:
      * empty / whitespace-only expression,
      * sqlglot syntax error,
      * disallowed function call,
      * disallowed AST node kind (e.g. subquery, aggregate, window).
    """
    if not expression or not expression.strip():
        raise ExpressionValidationError("expression is empty")

    rewritten, references = _replace_measure_refs(expression)

    if re.search(r"\bmeasure\s*\(", rewritten, re.IGNORECASE):
        raise ExpressionValidationError(
            'measure() references must use the form measure("name") with a '
            "non-empty quoted name"
        )

    try:
        ast = sqlglot.parse_one(rewritten, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise ExpressionValidationError(f"syntax error: {exc}") from exc

    _validate_ast(ast, valid_placeholders={r.placeholder for r in references})

    return ParsedExpression(original=expression, ast=ast, references=tuple(references))


def _replace_measure_refs(expression: str) -> tuple[str, list[MeasureReference]]:
    refs: list[MeasureReference] = []
    counter = {"n": 0}

    def _sub(match: re.Match) -> str:
        name = match.group(1).strip()
        if not name:
            raise ExpressionValidationError(
                "measure() reference has an empty name"
            )
        placeholder = f"{_PLACEHOLDER_PREFIX}{counter['n']}"
        counter["n"] += 1
        refs.append(MeasureReference(name=name, placeholder=placeholder))
        return placeholder

    rewritten = _MEASURE_REF_RE.sub(_sub, expression)
    return rewritten, refs


def _validate_ast(ast: exp.Expression, *, valid_placeholders: set[str]) -> None:
    for node in ast.walk():
        # sqlglot's walk() yields Expression nodes. Older versions yielded
        # (expression, parent, key) tuples, so normalise defensively.
        if isinstance(node, tuple):
            node = node[0]

        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            raise ExpressionValidationError(
                f"{type(node).__name__} is not allowed in calculated-measure "
                "expressions (no subqueries, window functions, inline "
                "aggregates, or query clauses)"
            )

        if isinstance(node, exp.Column):
            if node.name in valid_placeholders:
                continue
            raise ExpressionValidationError(
                f"bare identifier {node.name!r} is not allowed; reference "
                'other measures via measure("name")'
            )

        if isinstance(node, _STRUCTURAL_FUNC_TYPES):
            continue

        if isinstance(node, exp.Anonymous):
            fn_name = str(node.this or "").lower()
            if fn_name not in _ALLOWED_FUNCTIONS:
                raise ExpressionValidationError(
                    f"function {fn_name!r} is not in the allowed list"
                )
            continue

        if isinstance(node, exp.Func):
            fn_name = _func_name(node)
            if fn_name not in _ALLOWED_FUNCTIONS:
                raise ExpressionValidationError(
                    f"function {fn_name!r} is not in the allowed list"
                )
            continue


def _func_name(node: exp.Func) -> str:
    """Return a lowercase canonical name for a sqlglot Func node."""
    if hasattr(node, "sql_name"):
        try:
            return node.sql_name().lower()
        except Exception:  # pragma: no cover — defensive
            pass
    return type(node).__name__.lower()


def expand_safe_helpers(ast: exp.Expression) -> exp.Expression:
    """Rewrite safe_div(num, den) / safe_ratio(num, den) into
    ``CASE WHEN den = 0 THEN NULL ELSE num / den END``.

    Kept in ``shared`` so the query-router rewriter and the optimiser's
    CTAS emitters produce the same NULL-on-zero expansion for calculated
    measures.
    """
    def _rewrite(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Anonymous):
            name = str(node.this or "").lower()
            if name in ("safe_div", "safe_ratio"):
                args = list(node.expressions or [])
                if len(args) == 2:
                    num, den = args
                    return exp.Case(
                        ifs=[
                            exp.If(
                                this=exp.EQ(this=den.copy(), expression=exp.Literal.number(0)),
                                true=exp.Null(),
                            )
                        ],
                        default=exp.Div(this=num.copy(), expression=den.copy()),
                    )
        return node
    return ast.transform(_rewrite)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def detect_cycles(
    dependency_map: dict[UUID, Iterable[UUID]],
) -> list[list[UUID]]:
    """Return all simple cycles in a ``measure_id -> references`` map.

    Uses an iterative coloured DFS (white / grey / black). A node is grey
    while it is on the current recursion stack and black once fully
    explored. A back-edge to a grey node is a cycle; an edge to a black
    node is a shared sub-DAG (e.g. the second arm of a diamond A->B, A->C,
    B->D, C->D) and is correctly ignored. Returns a list of cycles; each
    cycle is a list of measure ids ending where it began. An empty list
    means the graph is a DAG.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    cycles: list[list[UUID]] = []
    colour: dict[UUID, int] = {}

    def _visit(start: UUID) -> None:
        # Each frame is (node, iterator-over-its-neighbours). The explicit
        # ``stack`` of node ids mirrors the recursion path so a back-edge can
        # be sliced into a readable cycle.
        path: list[UUID] = [start]
        path_pos: dict[UUID, int] = {start: 0}
        colour[start] = GREY
        frames: list[tuple[UUID, "object"]] = [
            (start, iter(dependency_map.get(start, ())))
        ]
        while frames:
            node, it = frames[-1]
            advanced = False
            for nbr in it:
                state = colour.get(nbr, WHITE)
                if state == GREY:
                    # Back-edge to a node on the current path -> cycle.
                    idx = path_pos[nbr]
                    cycles.append(path[idx:] + [nbr])
                elif state == WHITE:
                    colour[nbr] = GREY
                    path_pos[nbr] = len(path)
                    path.append(nbr)
                    frames.append((nbr, iter(dependency_map.get(nbr, ()))))
                    advanced = True
                    break
                # state == BLACK: already fully explored, shared DAG node.
            if not advanced:
                colour[node] = BLACK
                path.pop()
                path_pos.pop(node, None)
                frames.pop()

    for root in dependency_map.keys():
        if colour.get(root, WHITE) == WHITE:
            _visit(root)
    return cycles
