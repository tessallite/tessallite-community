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


ExprNode = Union[FieldRef, Literal, FuncCall]


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
    raise ExpressionError(
        f"expression node must have one of 'field', 'literal', 'fn'; "
        f"got keys {sorted(raw)}"
    )


def render(node: ExprNode) -> str:
    """Render a typed node as PostgreSQL-canonical SQL."""
    if isinstance(node, FieldRef):
        return _quote_ident(node.name)
    if isinstance(node, Literal):
        return _quote_literal(node.value)
    if isinstance(node, FuncCall):
        return FUNCTIONS[node.fn].render(node.args)
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
    return set()


def default_alias(node: ExprNode) -> str:
    """Deterministic alias from a node when the caller supplies none."""
    if isinstance(node, FieldRef):
        return node.name
    if isinstance(node, FuncCall):
        fields = sorted(base_field_names(node))
        base = fields[0] if fields else node.fn
        return f"{base}_{node.fn}"
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
