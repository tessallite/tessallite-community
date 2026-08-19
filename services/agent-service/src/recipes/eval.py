"""Combine-expression evaluator for cross-model recipes and compound queries.

Bug-5346 — the expression is a **typed semantic tree carried as data**, not a
string parsed as code. The agent (and the recipe editor) emit nodes; the
runtime walks them. Nothing is ever handed to a code parser, so identifiers
(step / measure names) are pure data and can never collide with a grammar's
reserved words — the class of crash where a step named ``global`` made Python's
``ast.parse`` raise ``SyntaxError`` is impossible by construction.

Node grammar
------------
    ExprNode =
        | { "const": <number | str | bool> }
        | { "ref":   { "step": <str>, "measure": <str> } }
        | { "op":    <OpName>, "args": [ExprNode, ...] }

OpName + arity:
  - 2-ary : add sub mul div floordiv mod pow · eq ne lt le gt ge
  - 1-ary : not neg abs len
  - round : 1-2 · min max sum : >=1 · and or : >=2 · if : exactly 3

The evaluator runs against a ``context`` dict where each step's first row is
exposed by step name (e.g. ``{"sales": {"revenue": 100}, ...}``); a ``ref`` node
resolves ``context[step][measure]``. Numeric coercion, None-propagation,
safe division/modulo-by-zero → None, and none-safe functions match the prior
behaviour exactly — only the front door changed from "parse a string" to
"walk a tree".
"""
from __future__ import annotations

from typing import Any

from shared.recipes.schema import (
    CombineSchemaError,
    collect_combine_references,
)


# ---------------------------------------------------------------------------
# Numeric helpers (semantics preserved from the prior evaluator)
# ---------------------------------------------------------------------------

def _coerce_numeric(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return int(v)
    except (ValueError, TypeError):
        pass
    try:
        return float(v)
    except (ValueError, TypeError):
        return v


def _safe_bin(op):
    def _wrapped(a, b):
        a, b = _coerce_numeric(a), _coerce_numeric(b)
        if a is None or b is None:
            return None
        try:
            return op(a, b)
        except TypeError:
            return None
    return _wrapped


_BIN_OPS = {
    "add": _safe_bin(lambda a, b: a + b),
    "sub": _safe_bin(lambda a, b: a - b),
    "mul": _safe_bin(lambda a, b: a * b),
    "div": _safe_bin(lambda a, b: a / b if b != 0 else None),
    "floordiv": _safe_bin(lambda a, b: a // b if b != 0 else None),
    "mod": _safe_bin(lambda a, b: a % b if b != 0 else None),
    "pow": _safe_bin(lambda a, b: a ** b),
}

def _safe_compare(op):
    """Bug-7365 -- wrap comparison operators with numeric coercion and null
    propagation, matching the semantics of ``_safe_bin`` for arithmetic.

    * Both operands are coerced numerically when possible (``"100" == 100``
      is ``True``).
    * Null operand returns ``None`` (SQL NULL semantics).
    * Incompatible types after coercion (e.g. string vs number where
      ``_coerce_numeric`` cannot convert) return ``None`` via the
      ``TypeError`` catch. Two non-numeric strings compare lexicographically
      (Python default).
    """
    def _wrapped(a, b):
        a, b = _coerce_numeric(a), _coerce_numeric(b)
        if a is None or b is None:
            return None
        try:
            return op(a, b)
        except TypeError:
            return None
    return _wrapped


_COMPARE_OPS = {
    "eq": _safe_compare(lambda a, b: a == b),
    "ne": _safe_compare(lambda a, b: a != b),
    "lt": _safe_compare(lambda a, b: a < b),
    "le": _safe_compare(lambda a, b: a <= b),
    "gt": _safe_compare(lambda a, b: a > b),
    "ge": _safe_compare(lambda a, b: a >= b),
}


def _none_safe_fn(fn):
    """Wrap an allow-listed function so a None argument propagates to None.

    The tool spec guarantees "round(null, n) returns null"; div-by-zero already
    yields None via ``_safe_bin``, so ``round(g / t * 100, 2)`` becomes
    ``round(None, 2)`` → None rather than crashing the turn.
    """
    def _wrapped(*args):
        if any(a is None for a in args):
            return None
        try:
            return fn(*args)
        except TypeError:
            return None
    return _wrapped


def _none_safe_reduce(fn):
    """Wrap a reducer (sum/min/max) so it operates over the *collection* of
    arguments.

    Bug-6330 — the tool spec advertises ``sum`` as "total across steps" with
    ``1+`` scalar arguments (``sum(a, b, c)``), but Python's builtin ``sum``
    takes a single *iterable*, so ``sum(100, 200)`` raised ``TypeError`` and the
    old ``_none_safe_fn`` swallowed it to ``None`` — every scalar ``sum`` (and a
    single-argument ``min``/``max``) silently evaluated to null. Passing the
    coerced argument tuple as one iterable makes these operate as the
    "reduce across the given values" operators the spec describes, for any arity
    ``>= 1``. Any ``None`` argument still propagates to ``None`` (matching
    ``_none_safe_fn`` / ``_safe_bin``), and numeric coercion mirrors the binary
    operators so string-encoded measure values reduce numerically."""
    def _wrapped(*args):
        if any(a is None for a in args):
            return None
        vals = [_coerce_numeric(a) for a in args]
        if any(v is None for v in vals):
            return None
        try:
            return fn(vals)
        except (TypeError, ValueError):
            return None
    return _wrapped


_FUNCTIONS = {
    "round": _none_safe_fn(round),
    "abs": _none_safe_fn(abs),
    "min": _none_safe_reduce(min),
    "max": _none_safe_reduce(max),
    "sum": _none_safe_reduce(sum),
    "len": _none_safe_fn(len),
}

# Allowed op name -> (min_args, max_args | None). Single source of truth for
# both shape validation and semantic validation.
class CombineEvalError(ValueError):
    """The combine expression tree is malformed or references missing data."""


# ---------------------------------------------------------------------------
# Shape validation (structural only — no step/measure resolution)
# ---------------------------------------------------------------------------

def check_node_shape(node: Any, *, path: str = "expression") -> None:
    """Raise ``ValueError`` if ``node`` is not a structurally valid ExprNode.

    Used at the tool-call / API boundary to reject malformed trees before they
    reach the evaluator. Does not check that step/measure refs exist — that is
    ``validate_expression``'s job (it needs the step definitions).
    """
    try:
        collect_combine_references(node, path=path)
    except CombineSchemaError as exc:
        raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Semantic validation (refs resolve against the step definitions)
# ---------------------------------------------------------------------------

def validate_expression(node: Any, steps: list) -> list[str]:
    """Validate a combine expression tree against step definitions.

    Each step object must have ``.name: str`` and ``.measures: list[str]``.
    Returns a list of error strings (empty = valid). ``None``/empty node = no
    expression = valid (no errors).
    """
    if node is None:
        return []
    step_map: dict[str, set[str]] = {s.name: set(s.measures) for s in steps}
    try:
        references = collect_combine_references(node, path="expression")
    except CombineSchemaError as exc:
        return [str(exc)]
    errors: list[str] = []
    for reference in references:
        step = reference.step
        measure = reference.measure
        if step not in step_map:
            errors.append(
                f"Step '{step}' not found. Available steps: "
                f"{', '.join(sorted(step_map))}."
            )
        elif measure not in step_map[step]:
            errors.append(
                f"Measure '{measure}' not found in step '{step}'. "
                f"Available: {', '.join(sorted(step_map[step]))}."
            )
    return errors


# ---------------------------------------------------------------------------
# Evaluation (recursive tree walk)
# ---------------------------------------------------------------------------

def evaluate_combine(node: Any, context: dict[str, Any]) -> Any:
    if node is None:
        raise CombineEvalError("Combine expression is empty.")
    try:
        collect_combine_references(node, path="expression")
    except CombineSchemaError as exc:
        raise CombineEvalError(str(exc)) from exc
    return _eval(node, context)


def _eval(node: Any, ctx: dict[str, Any]) -> Any:
    if not isinstance(node, dict):
        raise CombineEvalError(f"Expression node must be an object, got {type(node).__name__}.")
    if "const" in node:
        return node["const"]
    if "ref" in node:
        ref = node["ref"]
        step, measure = ref.get("step"), ref.get("measure")
        row = ctx.get(step)
        if not isinstance(row, dict):
            raise CombineEvalError(f"Step {step!r} not present in result context.")
        if measure not in row:
            raise CombineEvalError(
                f"Measure {measure!r} not present on step {step!r}."
            )
        return row[measure]
    if "op" not in node:
        raise CombineEvalError("Expression node must have one of: const, ref, op.")

    op = node["op"]
    args = node.get("args", [])

    # Short-circuit / lazy operators evaluate operands on demand.
    if op == "and":
        for a in args:
            if not _eval(a, ctx):
                return False
        return True
    if op == "or":
        for a in args:
            r = _eval(a, ctx)
            if r:
                return r
        return False
    if op == "if":
        cond, then, otherwise = args
        return _eval(then, ctx) if _eval(cond, ctx) else _eval(otherwise, ctx)
    if op == "not":
        return not _eval(args[0], ctx)
    if op == "neg":
        v = _coerce_numeric(_eval(args[0], ctx))
        return None if v is None else -v

    if op in _BIN_OPS:
        return _BIN_OPS[op](_eval(args[0], ctx), _eval(args[1], ctx))
    if op in _COMPARE_OPS:
        return _COMPARE_OPS[op](_eval(args[0], ctx), _eval(args[1], ctx))
    if op in _FUNCTIONS:
        return _FUNCTIONS[op](*[_eval(a, ctx) for a in args])

    raise CombineEvalError(f"Operator {op!r} not allowed.")


# ---------------------------------------------------------------------------
# Row-aligned evaluation (joins step rows on shared dimensions, evaluates
# the tree per output row). Structure preserved from the prior evaluator;
# only the per-row evaluation now walks the tree.
# ---------------------------------------------------------------------------

def evaluate_combine_aligned(
    node: Any,
    step_rows: dict[str, list[dict[str, Any]]],
    step_dimensions: dict[str, list[str]],
    result_label: str,
) -> tuple[list[dict[str, Any]], list[str], bool, str]:
    """Row-aligned compound expression evaluation.

    When steps share dimensions, joins rows on matching dimension values and
    applies the expression per-row. Returns:
      (result_rows, result_columns, is_multi_row, alignment_mode)

    ``alignment_mode`` is one of: ``"evaluated"``, ``"stacked_no_shared_dims"``,
    ``"stacked_no_overlap"``, ``"stacked_ambiguous_grain"``. Falls back to
    scalar evaluation when no steps have dimensions.
    """
    has_dims = any(len(d) > 0 for d in step_dimensions.values())
    if not has_dims:
        flat_ctx = {name: rows[0] if rows else {} for name, rows in step_rows.items()}
        value = evaluate_combine(node, flat_ctx)
        return [{result_label: value}], [result_label], False, "evaluated"

    # Scalar broadcast: a dimension-less step is a constant that joins to every
    # row of the dimensioned steps. Compute alignment over the dimensioned steps
    # only and inject each scalar step's single row into every evaluation.
    scalar_step_names = [n for n, d in step_dimensions.items() if not d]
    dim_step_names = [n for n, d in step_dimensions.items() if d]
    broadcastable = all(len(step_rows.get(n, [])) <= 1 for n in scalar_step_names)
    scalar_ctx: dict[str, dict[str, Any]] = {}
    if scalar_step_names and dim_step_names and broadcastable:
        for n in scalar_step_names:
            rows = step_rows.get(n, [])
            scalar_ctx[n] = rows[0] if rows else {}

    dim_dims: list[set[str]] = [
        set(step_dimensions[n]) for n in dim_step_names
    ] if scalar_ctx else [set(d) for d in step_dimensions.values()]
    shared_dims: list[str] = (
        sorted(set.intersection(*dim_dims)) if dim_dims else []
    )

    if not shared_dims:
        rows, cols, multi = _stack_step_rows(step_rows, step_dimensions, result_label)
        return rows, cols, multi, "stacked_no_shared_dims"

    align_step_names = dim_step_names if scalar_ctx else list(step_rows.keys())
    step_names = align_step_names

    # Dimensioned steps may carry unequal dimension sets; the join is on the
    # shared dims but the output grain is the finest-grained (driving) step.
    step_dim_count = {n: len(step_dimensions.get(n, [])) for n in step_names}
    driving_step = max(step_names, key=lambda n: step_dim_count[n])
    driving_dims = list(step_dimensions.get(driving_step, []))
    output_dims = sorted(set(driving_dims) | set(shared_dims))
    first_rows = step_rows[driving_step]

    def _non_unique_on_shared(name: str) -> bool:
        seen: set[str] = set()
        for row in step_rows.get(name, []):
            k = str(tuple(row.get(d) for d in shared_dims))
            if k in seen:
                return True
            seen.add(k)
        return False

    fine_steps = [n for n in step_names if _non_unique_on_shared(n)]
    if len(fine_steps) > 1:
        stk_rows, stk_cols, stk_multi = _stack_step_rows(
            step_rows, step_dimensions, result_label,
        )
        return stk_rows, stk_cols, stk_multi, "stacked_ambiguous_grain"

    index: dict[str, dict[str, dict[str, Any]]] = {}
    for name in step_names:
        if name == driving_step:
            continue
        index[name] = {}
        for row in step_rows.get(name, []):
            key = tuple(row.get(d) for d in shared_dims)
            index[name][str(key)] = row

    other_steps = [n for n in step_names if n != driving_step]
    result_rows: list[dict[str, Any]] = []

    for row in first_rows:
        key_str = str(tuple(row.get(d) for d in shared_dims))
        if not all(key_str in index[name] for name in other_steps):
            continue
        row_ctx = {name: index[name][key_str] for name in other_steps}
        row_ctx[driving_step] = row
        row_ctx.update(scalar_ctx)
        try:
            value = evaluate_combine(node, row_ctx)
        except CombineEvalError:
            value = None
        result_row: dict[str, Any] = {d: row.get(d) for d in output_dims}
        result_row[result_label] = value
        result_rows.append(result_row)

    if not result_rows and all(rows for rows in step_rows.values()):
        stk_rows, stk_cols, stk_multi = _stack_step_rows(
            step_rows, step_dimensions, result_label,
        )
        return stk_rows, stk_cols, stk_multi, "stacked_no_overlap"

    return result_rows, output_dims + [result_label], True, "evaluated"


def _stack_step_rows(
    step_rows: dict[str, list[dict[str, Any]]],
    step_dimensions: dict[str, list[str]],
    result_label: str,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Union all step rows with a 'Step' column when alignment fails.

    Used when compound steps share dimension column names but have
    non-overlapping dimension values (e.g. UK cities vs German cities).
    """
    all_dim_names: set[str] = set()
    all_measure_names: set[str] = set()
    for name, rows in step_rows.items():
        dims = set(step_dimensions.get(name, []))
        all_dim_names |= dims
        if rows:
            all_measure_names |= (set(rows[0].keys()) - dims)

    dim_cols = sorted(all_dim_names)
    measure_cols = sorted(all_measure_names)
    result_columns = ["Step"] + dim_cols + measure_cols

    result_rows: list[dict[str, Any]] = []
    for step_name, rows in step_rows.items():
        for row in rows:
            out: dict[str, Any] = {"Step": step_name}
            for c in dim_cols:
                out[c] = row.get(c)
            for c in measure_cols:
                out[c] = row.get(c)
            result_rows.append(out)

    return result_rows, result_columns, True
