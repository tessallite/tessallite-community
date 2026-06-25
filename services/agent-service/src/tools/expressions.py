"""Structured expression framework for the conversational agent (Bug-5349).

The agent never emits raw SQL. It emits structured nodes that this module
normalizes, validates against a central function registry, and renders as
PostgreSQL-canonical SQL. Rendering only ever happens from allow-listed node
types, so the LLM cannot smuggle arbitrary SQL through the tool schema.

Phase 1 (Bug-5349) wires this into the DIMENSION clause only — the projection
of groupings, GROUP BY, and ORDER BY — so "monthly/quarterly/yearly/weekly
trend" works from any raw date column via ``DATE_TRUNC`` without a pre-built
hierarchy level. The registry, AST, renderer, and field walker are deliberately
general so Phase 2 (WHERE predicates) and Phase 3 (projection / HAVING) plug in
without a parallel schema.

Engine contract (verified, no engine change permitted): the query-router parser
flags ``has_function_grain`` for any non-Column/Literal in GROUP BY
(`sql_parser.py`), the binder forces the passthrough/source path on it
(`binder.py`), and the router skips SELECT-vs-GROUP-BY validation for it
(`router.py`). PostgreSQL-canonical SQL is transpiled to the source dialect at
the single query-router boundary, so this module always emits PG-canonical SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

# ── clause constants ──────────────────────────────────────────────────────
SELECT = "select"
WHERE = "where"
GROUP_BY = "group_by"
ORDER_BY = "order_by"
HAVING = "having"

_MAX_DEPTH = 8


class ExpressionError(ValueError):
    """A structured expression node is malformed, references an unregistered
    function, violates arity/literal/clause constraints, or is otherwise not
    renderable. Surfaced to the LLM as a tool-call parse error."""


# ── quoting (PostgreSQL-canonical; mirrors exec/query.py exactly so legacy
#    bare-dimension SQL stays byte-for-byte identical) ──────────────────────
def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "NULL"
    s = str(v).replace("'", "''")
    return f"'{s}'"


# ── expression AST ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldRef:
    name: str


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class FuncCall:
    fn: str  # registry key (lowercase)
    args: tuple["ExprNode", ...]


@dataclass(frozen=True)
class Arith:
    """Binary arithmetic on two scalar nodes (Phase 3 projection / HAVING).
    ``op`` is one of ``+ - * /``. Rendered fully parenthesised so precedence is
    explicit and never ambiguous to the source dialect transpiler."""

    op: str
    left: "ExprNode"
    right: "ExprNode"


@dataclass(frozen=True)
class CaseWhen:
    """One WHEN <predicate> THEN <result> arm of a CASE expression."""

    when: "PredNode"
    then: "ExprNode"


@dataclass(frozen=True)
class Case:
    """Structured searched-CASE conditional (Phase 3). Represented as its own
    node — never a raw function call — so the LLM cannot smuggle SQL through a
    function name. ``else_`` is optional (NULL when absent)."""

    arms: tuple[CaseWhen, ...]
    else_: Optional["ExprNode"]


ExprNode = Union[FieldRef, Literal, FuncCall, Arith, Case]

# Arithmetic operator allow-list (rendered verbatim between parens).
_ARITH_OPS: dict[str, str] = {"add": "+", "sub": "-", "mul": "*", "div": "/"}


# ── predicate AST (Phase 2 WHERE / Phase 3 HAVING) ─────────────────────────
@dataclass(frozen=True)
class Comparison:
    """A single comparison: ``<left> <op> <right>``. Both sides are scalar
    ``ExprNode``s so a comparison can be column-to-column
    (``settlement_date > transaction_date``), function-on-column
    (``EXTRACT(MONTH FROM d) = 6``), or aggregate-to-aggregate in HAVING
    (``SUM(a) / SUM(b) > 0.5``)."""

    op: str          # eq/neq/gt/gte/lt/lte/in/between/like/is_null/is_not_null
    left: ExprNode
    right: Any       # ExprNode | tuple[ExprNode, ...] | None (op-dependent)


@dataclass(frozen=True)
class BoolOp:
    """``AND`` / ``OR`` over two-or-more sub-predicates."""

    op: str          # and / or
    args: tuple["PredNode", ...]


@dataclass(frozen=True)
class NotPred:
    """``NOT (<predicate>)``."""

    arg: "PredNode"


PredNode = Union[Comparison, BoolOp, NotPred]

_COMPARE_SYMBOLS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
# Operators that compare a left expression against a single right expression.
_BINARY_OPS = frozenset(_COMPARE_SYMBOLS) | {"like"}
# Full predicate operator allow-list.
_PRED_OPS = _BINARY_OPS | {"in", "between", "is_null", "is_not_null"}


# ── function registry ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class FunctionSpec:
    """One allow-listed SQL function. Adding a function means adding it here
    (and tests) — there is no per-function ``if`` branching anywhere else."""

    key: str
    sql_name: str
    category: str
    min_arity: int
    max_arity: int
    clauses: frozenset[str]
    is_aggregate: bool = False
    # Optional per-spec argument validator (literal constraints, etc.).
    arg_validator: Optional[Callable[[tuple["ExprNode", ...]], None]] = None
    # Optional custom renderer for non-``NAME(a, b)`` syntax (EXTRACT, COUNT
    # DISTINCT). Receives the already-normalized args.
    renderer: Optional[Callable[[tuple["ExprNode", ...]], str]] = None

    def validate(self, args: tuple["ExprNode", ...]) -> None:
        if not (self.min_arity <= len(args) <= self.max_arity):
            raise ExpressionError(
                f"function {self.sql_name} takes {self.min_arity}..{self.max_arity} "
                f"argument(s), got {len(args)}"
            )
        if self.arg_validator is not None:
            self.arg_validator(args)

    def render(self, args: tuple["ExprNode", ...]) -> str:
        if self.renderer is not None:
            return self.renderer(args)
        return f"{self.sql_name}(" + ", ".join(render(a) for a in args) + ")"


# Literal value allow-lists for date functions.
_TRUNC_UNITS = frozenset({
    "microseconds", "milliseconds", "second", "minute", "hour", "day",
    "week", "month", "quarter", "year", "decade", "century", "millennium",
})
_DATE_PARTS = frozenset({
    "century", "day", "decade", "dow", "doy", "epoch", "hour", "isodow",
    "isoyear", "microseconds", "millennium", "milliseconds", "minute",
    "month", "quarter", "second", "week", "year",
})

# Grains the agent dimension shorthand ({"name":..,"grain":..}) accepts.
VALID_GRAINS = frozenset({"year", "quarter", "month", "week", "day", "hour"})

# Date/time functions — used by trend detection so a DATE_TRUNC grouping is
# recognised as a chronological trend (Bug-5351 floor) even when the base
# column name is not obviously date-shaped.
DATE_FNS = frozenset({"date_trunc", "extract", "date_part"})


def _require_unit_literal(args: tuple["ExprNode", ...], allowed: frozenset[str], label: str) -> None:
    unit = args[0]
    if not isinstance(unit, Literal) or not isinstance(unit.value, str):
        raise ExpressionError(f"{label} first argument must be a string literal unit")
    if unit.value.lower() not in allowed:
        raise ExpressionError(
            f"{label} unit {unit.value!r} is not allowed; "
            f"valid: {', '.join(sorted(allowed))}"
        )


def _validate_date_trunc(args: tuple["ExprNode", ...]) -> None:
    _require_unit_literal(args, _TRUNC_UNITS, "date_trunc")


def _validate_extract(args: tuple["ExprNode", ...]) -> None:
    _require_unit_literal(args, _DATE_PARTS, "extract/date_part")


def _render_date_trunc(args: tuple["ExprNode", ...]) -> str:
    unit = args[0].value.lower()  # validated literal
    return f"DATE_TRUNC('{unit}', {render(args[1])})"


def _render_extract(args: tuple["ExprNode", ...]) -> str:
    part = args[0].value.upper()  # validated literal -> bare keyword
    return f"EXTRACT({part} FROM {render(args[1])})"


def _render_date_part(args: tuple["ExprNode", ...]) -> str:
    part = args[0].value.lower()
    return f"DATE_PART('{part}', {render(args[1])})"


def _render_count_distinct(args: tuple["ExprNode", ...]) -> str:
    return f"COUNT(DISTINCT {render(args[0])})"


_SCALAR = frozenset({SELECT, WHERE, GROUP_BY, ORDER_BY})
_SCALAR_AND_HAVING = _SCALAR | {HAVING}
_AGG_CLAUSES = frozenset({SELECT, ORDER_BY, HAVING})


def _spec(key, sql_name, category, lo, hi, clauses, **kw) -> FunctionSpec:
    return FunctionSpec(
        key=key, sql_name=sql_name, category=category,
        min_arity=lo, max_arity=hi, clauses=clauses, **kw,
    )


FUNCTIONS: dict[str, FunctionSpec] = {
    # Date / time
    "date_trunc": _spec("date_trunc", "DATE_TRUNC", "datetime", 2, 2, _SCALAR,
                        arg_validator=_validate_date_trunc, renderer=_render_date_trunc),
    "extract": _spec("extract", "EXTRACT", "datetime", 2, 2, _SCALAR_AND_HAVING,
                     arg_validator=_validate_extract, renderer=_render_extract),
    "date_part": _spec("date_part", "DATE_PART", "datetime", 2, 2, _SCALAR_AND_HAVING,
                       arg_validator=_validate_extract, renderer=_render_date_part),
    # Text
    "lower": _spec("lower", "LOWER", "text", 1, 1, _SCALAR),
    "upper": _spec("upper", "UPPER", "text", 1, 1, _SCALAR),
    "trim": _spec("trim", "TRIM", "text", 1, 1, _SCALAR),
    "concat": _spec("concat", "CONCAT", "text", 2, 16, _SCALAR),
    "substring": _spec("substring", "SUBSTRING", "text", 2, 3, _SCALAR),
    # Numeric
    "round": _spec("round", "ROUND", "numeric", 1, 2, _SCALAR_AND_HAVING),
    "abs": _spec("abs", "ABS", "numeric", 1, 1, _SCALAR_AND_HAVING),
    "ceil": _spec("ceil", "CEIL", "numeric", 1, 1, _SCALAR_AND_HAVING),
    "floor": _spec("floor", "FLOOR", "numeric", 1, 1, _SCALAR_AND_HAVING),
    # Null handling
    "coalesce": _spec("coalesce", "COALESCE", "null", 2, 16, _SCALAR_AND_HAVING),
    "nullif": _spec("nullif", "NULLIF", "null", 2, 2, _SCALAR_AND_HAVING),
    # Aggregate wrappers (Phase 3 projection/HAVING; NOT valid as a grouping
    # dimension, so they are absent from GROUP_BY clauses).
    "sum": _spec("sum", "SUM", "aggregate", 1, 1, _AGG_CLAUSES, is_aggregate=True),
    "avg": _spec("avg", "AVG", "aggregate", 1, 1, _AGG_CLAUSES, is_aggregate=True),
    "min": _spec("min", "MIN", "aggregate", 1, 1, _AGG_CLAUSES, is_aggregate=True),
    "max": _spec("max", "MAX", "aggregate", 1, 1, _AGG_CLAUSES, is_aggregate=True),
    "count": _spec("count", "COUNT", "aggregate", 1, 1, _AGG_CLAUSES, is_aggregate=True),
    "count_distinct": _spec("count_distinct", "COUNT", "aggregate", 1, 1, _AGG_CLAUSES,
                            is_aggregate=True, renderer=_render_count_distinct),
}


# ── normalization ─────────────────────────────────────────────────────────
def _reject_extra_keys(raw: dict, allowed: set[str]) -> None:
    extra = set(raw) - allowed
    if extra:
        raise ExpressionError(
            f"unexpected key(s) {sorted(extra)}; allowed: {sorted(allowed)}"
        )


def normalize_node(raw: Any, *, clause: Optional[str] = None, _depth: int = 0) -> ExprNode:
    """Turn a raw JSON expression node into a typed, validated ``ExprNode``.

    ``clause`` (when given) restricts which functions are allowed (e.g. an
    aggregate is rejected in GROUP_BY). Raises :class:`ExpressionError`."""
    if _depth > _MAX_DEPTH:
        raise ExpressionError("expression nesting too deep")
    if not isinstance(raw, dict):
        raise ExpressionError("expression node must be an object")
    if "field" in raw:
        _reject_extra_keys(raw, {"field"})
        name = raw["field"]
        if not isinstance(name, str) or not name:
            raise ExpressionError("field reference must be a non-empty string")
        return FieldRef(name)
    if "literal" in raw:
        _reject_extra_keys(raw, {"literal"})
        val = raw["literal"]
        if not isinstance(val, (str, int, float, bool)) and val is not None:
            raise ExpressionError("literal must be a scalar (string/number/bool/null)")
        return Literal(val)
    if "fn" in raw:
        _reject_extra_keys(raw, {"fn", "args"})
        fn = raw["fn"]
        if not isinstance(fn, str) or not fn:
            raise ExpressionError("fn must be a non-empty string")
        spec = FUNCTIONS.get(fn.lower())
        if spec is None:
            raise ExpressionError(
                f"unknown or unregistered function {fn!r}; "
                f"allowed: {', '.join(sorted(FUNCTIONS))}"
            )
        if clause is not None and clause not in spec.clauses:
            raise ExpressionError(
                f"function {spec.sql_name} is not allowed in the {clause} clause"
            )
        args_raw = raw.get("args") or []
        if not isinstance(args_raw, list):
            raise ExpressionError(f"{spec.sql_name} args must be a list")
        args = tuple(
            normalize_node(a, clause=clause, _depth=_depth + 1) for a in args_raw
        )
        spec.validate(args)
        return FuncCall(spec.key, args)
    if "arith" in raw:
        _reject_extra_keys(raw, {"arith", "left", "right"})
        op = raw["arith"]
        if not isinstance(op, str) or op.lower() not in _ARITH_OPS:
            raise ExpressionError(
                f"invalid arithmetic op {op!r}; allowed: {', '.join(sorted(_ARITH_OPS))}"
            )
        if "left" not in raw or "right" not in raw:
            raise ExpressionError("arith node requires 'left' and 'right'")
        left = normalize_node(raw["left"], clause=clause, _depth=_depth + 1)
        right = normalize_node(raw["right"], clause=clause, _depth=_depth + 1)
        return Arith(op.lower(), left, right)
    if "case" in raw:
        _reject_extra_keys(raw, {"case", "else"})
        arms_raw = raw["case"]
        if not isinstance(arms_raw, list) or not arms_raw:
            raise ExpressionError("case node requires a non-empty 'case' arm list")
        arms: list[CaseWhen] = []
        for arm in arms_raw:
            if not isinstance(arm, dict):
                raise ExpressionError("each case arm must be an object")
            _reject_extra_keys(arm, {"when", "then"})
            if "when" not in arm or "then" not in arm:
                raise ExpressionError("each case arm requires 'when' and 'then'")
            when = normalize_predicate(arm["when"], clause=clause, _depth=_depth + 1)
            then = normalize_node(arm["then"], clause=clause, _depth=_depth + 1)
            arms.append(CaseWhen(when, then))
        else_raw = raw.get("else")
        else_ = (
            normalize_node(else_raw, clause=clause, _depth=_depth + 1)
            if else_raw is not None
            else None
        )
        return Case(tuple(arms), else_)
    raise ExpressionError(
        f"expression node must have one of 'field', 'literal', 'fn', 'arith', "
        f"'case'; got keys {sorted(raw)}"
    )


def render(node: ExprNode) -> str:
    """Render a typed node as PostgreSQL-canonical SQL."""
    if isinstance(node, FieldRef):
        return _quote_ident(node.name)
    if isinstance(node, Literal):
        return _quote_literal(node.value)
    if isinstance(node, FuncCall):
        return FUNCTIONS[node.fn].render(node.args)
    if isinstance(node, Arith):
        return f"({render(node.left)} {_ARITH_OPS[node.op]} {render(node.right)})"
    if isinstance(node, Case):
        parts = ["CASE"]
        for arm in node.arms:
            parts.append(f"WHEN {render_predicate(arm.when)} THEN {render(arm.then)}")
        if node.else_ is not None:
            parts.append(f"ELSE {render(node.else_)}")
        parts.append("END")
        return " ".join(parts)
    raise ExpressionError(f"unrenderable node: {node!r}")


def base_field_names(node: ExprNode) -> set[str]:
    """Every underlying semantic field name referenced anywhere in the tree —
    used by persona-scope validation so functions cannot hide a hidden field."""
    if isinstance(node, FieldRef):
        return {node.name}
    if isinstance(node, FuncCall):
        out: set[str] = set()
        for a in node.args:
            out |= base_field_names(a)
        return out
    if isinstance(node, Arith):
        return base_field_names(node.left) | base_field_names(node.right)
    if isinstance(node, Case):
        out = set()
        for arm in node.arms:
            out |= predicate_base_fields(arm.when) | base_field_names(arm.then)
        if node.else_ is not None:
            out |= base_field_names(node.else_)
        return out
    return set()


def has_aggregate(node: ExprNode) -> bool:
    """True when the scalar tree contains any aggregate function call —
    used to enforce that a HAVING predicate references aggregates."""
    if isinstance(node, FuncCall):
        if FUNCTIONS[node.fn].is_aggregate:
            return True
        return any(has_aggregate(a) for a in node.args)
    if isinstance(node, Arith):
        return has_aggregate(node.left) or has_aggregate(node.right)
    if isinstance(node, Case):
        for arm in node.arms:
            if predicate_has_aggregate(arm.when) or has_aggregate(arm.then):
                return True
        return node.else_ is not None and has_aggregate(node.else_)
    return False


# ── predicate normalization / render / field-walk ─────────────────────────
def normalize_predicate(raw: Any, *, clause: str, _depth: int = 0) -> PredNode:
    """Turn a raw JSON predicate node into a typed, validated ``PredNode``.

    Forms:
      {"and"|"or": [<pred>, ...]}                     boolean composition
      {"not": <pred>}                                 negation
      {"left": <node>, "op": "<cmp>", "right": <...>} a comparison

    ``clause`` (WHERE / HAVING) restricts which functions the operand
    expressions may use, exactly as the scalar path. Raises ExpressionError."""
    if _depth > _MAX_DEPTH:
        raise ExpressionError("predicate nesting too deep")
    if not isinstance(raw, dict):
        raise ExpressionError("predicate node must be an object")
    if "and" in raw or "or" in raw:
        bop = "and" if "and" in raw else "or"
        _reject_extra_keys(raw, {bop})
        sub = raw[bop]
        if not isinstance(sub, list) or len(sub) < 2:
            raise ExpressionError(f"{bop!r} requires a list of at least 2 predicates")
        args = tuple(
            normalize_predicate(s, clause=clause, _depth=_depth + 1) for s in sub
        )
        return BoolOp(bop, args)
    if "not" in raw:
        _reject_extra_keys(raw, {"not"})
        return NotPred(normalize_predicate(raw["not"], clause=clause, _depth=_depth + 1))
    # comparison
    _reject_extra_keys(raw, {"left", "op", "right"})
    if "left" not in raw or "op" not in raw:
        raise ExpressionError("comparison predicate requires 'left' and 'op'")
    op = raw["op"]
    if not isinstance(op, str) or op.lower() not in _PRED_OPS:
        raise ExpressionError(
            f"invalid predicate op {op!r}; allowed: {', '.join(sorted(_PRED_OPS))}"
        )
    op = op.lower()
    left = normalize_node(raw["left"], clause=clause, _depth=_depth + 1)
    right_raw = raw.get("right")
    if op in ("is_null", "is_not_null"):
        if right_raw is not None:
            raise ExpressionError(f"{op} takes no right operand")
        return Comparison(op, left, None)
    if op == "in":
        if not isinstance(right_raw, list) or not right_raw:
            raise ExpressionError("'in' requires a non-empty right list")
        items = tuple(
            normalize_node(r, clause=clause, _depth=_depth + 1) for r in right_raw
        )
        return Comparison(op, left, items)
    if op == "between":
        if not isinstance(right_raw, list) or len(right_raw) != 2:
            raise ExpressionError("'between' requires a right list of exactly 2 values")
        lo = normalize_node(right_raw[0], clause=clause, _depth=_depth + 1)
        hi = normalize_node(right_raw[1], clause=clause, _depth=_depth + 1)
        return Comparison(op, left, (lo, hi))
    # binary op (eq/neq/gt/.../like): right is a single scalar node
    if right_raw is None:
        raise ExpressionError(f"{op} requires a 'right' operand")
    right = normalize_node(right_raw, clause=clause, _depth=_depth + 1)
    return Comparison(op, left, right)


def render_predicate(node: PredNode) -> str:
    """Render a typed predicate as PostgreSQL-canonical SQL (parenthesised)."""
    if isinstance(node, BoolOp):
        joiner = " AND " if node.op == "and" else " OR "
        return "(" + joiner.join(render_predicate(a) for a in node.args) + ")"
    if isinstance(node, NotPred):
        return f"(NOT {render_predicate(node.arg)})"
    if isinstance(node, Comparison):
        left = render(node.left)
        if node.op in _COMPARE_SYMBOLS:
            return f"{left} {_COMPARE_SYMBOLS[node.op]} {render(node.right)}"
        if node.op == "like":
            return f"{left} LIKE {render(node.right)}"
        if node.op == "in":
            return f"{left} IN (" + ", ".join(render(r) for r in node.right) + ")"
        if node.op == "between":
            lo, hi = node.right
            return f"{left} BETWEEN {render(lo)} AND {render(hi)}"
        if node.op == "is_null":
            return f"{left} IS NULL"
        if node.op == "is_not_null":
            return f"{left} IS NOT NULL"
    raise ExpressionError(f"unrenderable predicate: {node!r}")


def predicate_base_fields(node: PredNode) -> set[str]:
    """Every semantic field name referenced anywhere in a predicate tree —
    used by persona-scope validation (the Phase 2/3 security trap)."""
    if isinstance(node, BoolOp):
        out: set[str] = set()
        for a in node.args:
            out |= predicate_base_fields(a)
        return out
    if isinstance(node, NotPred):
        return predicate_base_fields(node.arg)
    if isinstance(node, Comparison):
        out = base_field_names(node.left)
        if isinstance(node.right, tuple):
            for r in node.right:
                out |= base_field_names(r)
        elif node.right is not None:
            out |= base_field_names(node.right)
        return out
    return set()


def predicate_has_aggregate(node: PredNode) -> bool:
    """True when any operand in the predicate is an aggregate (HAVING gate)."""
    if isinstance(node, BoolOp):
        return any(predicate_has_aggregate(a) for a in node.args)
    if isinstance(node, NotPred):
        return predicate_has_aggregate(node.arg)
    if isinstance(node, Comparison):
        if has_aggregate(node.left):
            return True
        if isinstance(node.right, tuple):
            return any(has_aggregate(r) for r in node.right)
        if node.right is not None:
            return has_aggregate(node.right)
    return False


def default_alias(node: ExprNode) -> str:
    """Deterministic alias from a node when the caller supplies none."""
    if isinstance(node, FieldRef):
        return node.name
    if isinstance(node, FuncCall):
        fields = sorted(base_field_names(node))
        base = fields[0] if fields else node.fn
        return f"{base}_{node.fn}"
    if isinstance(node, Arith):
        fields = sorted(base_field_names(node))
        base = fields[0] if fields else "expr"
        return f"{base}_{node.op}"
    if isinstance(node, Case):
        fields = sorted(base_field_names(node))
        base = fields[0] if fields else "case"
        return f"{base}_case"
    return "expr"


def dedupe_alias(alias: str, taken: set[str]) -> str:
    """Collision-safe alias (R4): append _2, _3, ... if already taken. Mutates
    ``taken`` to include the returned alias."""
    candidate = alias
    n = 2
    while candidate in taken:
        candidate = f"{alias}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


# ── dimension reference (Phase 1 surface) ─────────────────────────────────
@dataclass(frozen=True)
class DimRef:
    """A normalized dimension: a bare semantic name, a grain shorthand, or a
    general scalar expression. Drives SQL composition, persona scope, and trend
    detection from one typed source (decision D1)."""

    alias: str
    node: ExprNode
    raw: Any  # original entry (str or dict) preserved for plan-dict reuse (D2)
    is_bare: bool

    @property
    def base_fields(self) -> tuple[str, ...]:
        return tuple(sorted(base_field_names(self.node)))

    @property
    def is_date_fn(self) -> bool:
        return isinstance(self.node, FuncCall) and self.node.fn in DATE_FNS

    def render_select(self) -> str:
        # Bare dimension: name == alias, no AS — byte-for-byte with legacy SQL.
        if self.is_bare:
            return _quote_ident(self.alias)
        return f"{render(self.node)} AS {_quote_ident(self.alias)}"

    def render_group_by(self) -> str:
        return _quote_ident(self.alias) if self.is_bare else render(self.node)

    def render_order_by(self, direction: str = "ASC") -> str:
        target = _quote_ident(self.alias) if self.is_bare else render(self.node)
        return f"{target} {direction}"


def normalize_dimension(entry: Any, taken: set[str]) -> DimRef:
    """Normalize one dimension entry. Accepts:
      - a bare string                       -> legacy field name
      - {"name": "d", "grain": "month"}     -> DATE_TRUNC shorthand
      - {"name": "d"}                        -> bare field name (no grain)
      - {"expr": <node>, "alias"?: "a"}     -> general scalar expression

    Aggregate functions are rejected (a grouping dimension must be scalar).
    ``taken`` accumulates aliases for collision-safe naming (R4)."""
    if isinstance(entry, str):
        if not entry:
            raise ExpressionError("dimension name must be a non-empty string")
        taken.add(entry)
        return DimRef(alias=entry, node=FieldRef(entry), raw=entry, is_bare=True)

    if not isinstance(entry, dict):
        raise ExpressionError("dimension must be a string or an object")

    if "expr" in entry:
        _reject_extra_keys(entry, {"expr", "alias"})
        node = normalize_node(entry["expr"], clause=GROUP_BY)
        alias = entry.get("alias")
        if alias is not None and (not isinstance(alias, str) or not alias):
            raise ExpressionError("dimension alias must be a non-empty string")
        alias = dedupe_alias(alias or default_alias(node), taken)
        return DimRef(alias=alias, node=node, raw=entry, is_bare=False)

    # grain shorthand / bare-by-object
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ExpressionError(
            "dimension object must be a grain shorthand "
            "({\"name\":..,\"grain\":..}) or an expression ({\"expr\":..})"
        )
    grain = entry.get("grain")
    if grain is None:
        _reject_extra_keys(entry, {"name"})
        taken.add(name)
        return DimRef(alias=name, node=FieldRef(name), raw=entry, is_bare=True)
    _reject_extra_keys(entry, {"name", "grain"})
    if not isinstance(grain, str) or grain.lower() not in VALID_GRAINS:
        raise ExpressionError(
            f"invalid grain {grain!r}; allowed: {', '.join(sorted(VALID_GRAINS))}"
        )
    g = grain.lower()
    node = FuncCall("date_trunc", (Literal(g), FieldRef(name)))
    alias = dedupe_alias(f"{name}_{g}", taken)
    return DimRef(alias=alias, node=node, raw=entry, is_bare=False)


def normalize_dimensions(entries: list[Any]) -> list[DimRef]:
    """Normalize a dimension list, sharing one alias-collision namespace."""
    taken: set[str] = set()
    return [normalize_dimension(e, taken) for e in entries]


# ── projection reference (Phase 3 SELECT-derived columns) ──────────────────
@dataclass(frozen=True)
class ProjRef:
    """A computed projection column: a registered scalar/arithmetic/CASE/
    aggregate expression with a deterministic output alias. Drives the SELECT
    list and persona scope (base fields walked exactly like a dimension)."""

    alias: str
    node: ExprNode
    raw: Any

    @property
    def base_fields(self) -> tuple[str, ...]:
        return tuple(sorted(base_field_names(self.node)))

    def render_select(self) -> str:
        return f"{render(self.node)} AS {_quote_ident(self.alias)}"


def normalize_projection(entry: Any, taken: set[str]) -> ProjRef:
    """Normalize one projection entry: ``{"expr": <node>, "alias"?: "a"}``.
    Aggregates ARE permitted here (a computed column may wrap SUM/AVG/…)."""
    if not isinstance(entry, dict) or "expr" not in entry:
        raise ExpressionError(
            "projection entry must be an object {\"expr\": <node>, \"alias\"?: ..}"
        )
    _reject_extra_keys(entry, {"expr", "alias"})
    node = normalize_node(entry["expr"], clause=SELECT)
    alias = entry.get("alias")
    if alias is not None and (not isinstance(alias, str) or not alias):
        raise ExpressionError("projection alias must be a non-empty string")
    alias = dedupe_alias(alias or default_alias(node), taken)
    return ProjRef(alias=alias, node=node, raw=entry)


def normalize_projections(entries: list[Any], taken: set[str] | None = None) -> list[ProjRef]:
    """Normalize a projection list, sharing the caller's alias namespace so a
    computed column cannot collide with a dimension or measure alias."""
    taken = taken if taken is not None else set()
    return [normalize_projection(e, taken) for e in entries]


# ── filter / having reference (Phase 2 / Phase 3 predicates) ───────────────
@dataclass(frozen=True)
class PredRef:
    """A normalized predicate for a WHERE or HAVING clause. Carries the typed
    predicate AST that drives SQL rendering and persona scope. The ``raw`` is
    preserved for plan-dict round-trips."""

    node: PredNode
    raw: Any

    @property
    def base_fields(self) -> tuple[str, ...]:
        return tuple(sorted(predicate_base_fields(self.node)))

    def render(self) -> str:
        return render_predicate(self.node)


def normalize_filter(entry: Any, *, clause: str) -> PredRef:
    """Normalize one structured WHERE/HAVING predicate entry. ``clause`` is
    WHERE or HAVING and constrains which functions the operands may use."""
    return PredRef(node=normalize_predicate(entry, clause=clause), raw=entry)


def is_structured_predicate(entry: Any) -> bool:
    """True when a where/having entry is a structured predicate (boolean
    composition or {left/op/right}) rather than the legacy flat
    {name/op/value} filter. Legacy entries are left untouched (back-compat)."""
    if not isinstance(entry, dict):
        return False
    return any(k in entry for k in ("and", "or", "not", "left"))
