"""Semantic comparison between an expected decomposition and the actual
plan produced by run_turn().

The expected decomposition can be either:
  - A structured dict: {"model": "modelx", "measures": ["revenue"],
    "dimensions": ["region"], "filters": ["year = 2024"]}
  - A freeform string: "model: modelx, measures: [revenue, quantity],
    dimensions: [region], filters: [year = 2024]"

The actual plan is the dict produced by ``_plan_dict()`` in the pipeline,
which has the shape: {"query": {"model_id": "...", "measures": [...],
"dimensions": [...], "where": [...], "having": [...], ...}}.

Comparison is semantic (set-based for lists, case-insensitive for model
names, structural for filters).

Design decisions (review round 1):
- The actual plan's ``model_id`` is a UUID (the planner tool contract),
  while authored decompositions carry a human name — the two can never
  match by string equality, and UUIDs differ per environment/reseed. The
  authoritative baseline model is therefore the model CONTEXT the example
  question belongs to (``context_model_id``): the model field matches
  when the plan routed to the question's own model (or the author pasted
  the exact UUID).
- Expected ``filters`` are compared against the plan's where + having
  MERGED — an authored baseline lists the business conditions without
  distinguishing pre/post-aggregation placement.
- A present-but-unparseable expected decomposition is reported as
  ``skip_reason="unparseable_decomposition"`` (never a silent vacuous
  pass) so the runner can count and surface it.
- Compound (``and``/``or``/``not``) and expression predicates in the
  plan have no comparable flat rendering; when present, the filters
  field is skipped VISIBLY (``FieldComparison.skipped`` with a detail)
  rather than rendered as garbage that would false-fail every such plan.
- Filter normalisation strips quotes and unifies operators (``<>`` ==
  ``!=``, token ops -> symbols) — systematic false fails from authoring
  style cost more than the theoretical false passes this admits.
- Grained dimensions: the plan's ``dimensions`` list carries derived
  ALIASES (e.g. "order_date_month") whenever any dimension is grained or
  an expression; comparable base names are recovered from the raw
  ``dimension_exprs`` entries ({"name","grain"} -> name). Grain itself is
  intentionally not compared (freeform baselines carry no reliable grain
  syntax), so a month -> year grain change on the same base dimension is
  a known invisible regression. Expression dimensions have no comparable
  name and skip the dimensions field visibly.
- Parser hazards (degrade to a VISIBLE mismatch or unparseable skip,
  never a crash or silent pass): unquoted ``|`` acts as a hard
  separator; a lone apostrophe in a value (``name = O'Brien``) derails
  quote tracking; the ", key:" boundary split is not quote-aware.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# The four fields we compare between expected and actual.
_COMPARISON_FIELDS = ("model", "measures", "dimensions", "filters")


@dataclass
class FieldComparison:
    """Result of comparing one field between expected and actual.

    ``skipped`` marks a field that could not be compared (e.g. the plan
    uses compound/expression predicates with no comparable rendering) —
    it appears in the diff with a detail but does not affect the overall
    match verdict."""

    field_name: str
    matched: bool
    expected: Any = None
    actual: Any = None
    detail: Optional[str] = None
    skipped: bool = False


@dataclass
class DecompositionComparison:
    """Full comparison result for one eval question."""

    matched: bool
    fields: list[FieldComparison] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None


_KEY_RE = re.compile(
    r"(models?|measures?|dimensions?|filters?)\s*:\s*(.*)",
    re.IGNORECASE,
)

# ", key:" boundaries — handles comma-separated keys when values use
# brackets to disambiguate list commas. Not quote-aware (see module
# docstring parser hazards).
_BOUNDARY_RE = re.compile(
    r",\s*(?=(?:models?|measures?|dimensions?|filters?)\s*:)",
    re.IGNORECASE,
)


def _parse_string_decomposition(raw: str) -> dict[str, Any]:
    """Best-effort parse of a freeform decomposition string into a
    structured dict with keys: model, measures, dimensions, filters.

    Supported formats:
      "model: modelx, measures: [revenue, quantity], dimensions: [region]"
      "model: modelx; measures: revenue, quantity; dimensions: region"
      "model: modelx\\nmeasures: revenue\\nfilters: year = 2024"

    Hard separators (semicolon / pipe / newline — the natural textarea
    format) are split first, quote-aware so a quoted value containing a
    separator survives. Within each part, ", key:" boundaries are split
    so mixed comma/newline styles parse completely — a half-parsed
    baseline would silently skip the dropped fields (false pass).
    """
    result: dict[str, Any] = {}

    if ";" in raw or "|" in raw or "\n" in raw:
        parts = _split_top_level(raw, separators=";|\n")
    else:
        parts = [raw]

    for part in parts:
        for sub in _BOUNDARY_RE.split(part):
            sub = sub.strip().strip(",").strip()
            m = _KEY_RE.match(sub)
            if m:
                key = _normalise_key(m.group(1))
                val = m.group(2).strip()
                result[key] = _parse_value(key, val)

    return result


def _normalise_key(raw: str) -> str:
    """Canonicalise a field name to one of the four comparison keys."""
    low = raw.strip().lower().rstrip("s")
    mapping = {
        "model": "model",
        "measure": "measures",
        "dimension": "dimensions",
        "filter": "filters",
    }
    return mapping.get(low, raw.lower())


def _split_top_level(raw: str, separators: str = ",") -> list[str]:
    """Split on any of ``separators`` at depth 0 only — separators inside
    parentheses, brackets, or quotes stay within their item (e.g. the
    filter ``region IN (East, West)`` is ONE item, and a quoted value
    containing ``;`` does not derail key splitting)."""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: Optional[str] = None
    for ch in raw:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch in separators and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return [item for item in items if item]


def _parse_value(key: str, raw: str) -> Any:
    """Parse a value string into the appropriate type for comparison."""
    # Strip surrounding brackets if present.
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()

    if key == "model":
        return raw.strip()

    # For list fields, split on top-level commas only and strip each item.
    return _split_top_level(raw)


def normalise_decomposition(raw: Any) -> Optional[dict[str, Any]]:
    """Normalise a decomposition value (str or dict) into a structured
    dict with keys: model, measures, dimensions, filters.  Returns None
    if the input is empty/unparseable."""
    if raw is None:
        return None

    if isinstance(raw, dict):
        result: dict[str, Any] = {}
        for raw_key, val in raw.items():
            # Normalise dict keys the same way as string-parsed keys so
            # "Measures"/"measure" are not silently dropped.
            key = _normalise_key(str(raw_key))
            if key not in _COMPARISON_FIELDS or val is None:
                continue
            if key == "model" and isinstance(val, str):
                result[key] = val.strip()
            elif isinstance(val, list):
                result[key] = [
                    str(v).strip() for v in val if v is not None
                ]
            elif isinstance(val, str):
                result[key] = _parse_value(key, val)
            else:
                result[key] = val
        return result if result else None

    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        return _parse_string_decomposition(raw) or None

    return None


def _extract_actual_decomposition(plan: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Extract a comparable decomposition dict from the actual plan
    produced by run_turn().  The plan has shape:
        {"query": {"model_id": "...", "measures": [...], ...}}
    or  {"run_recipe": {...}}
    or  {"compound_query": {"steps": [...], ...}}
    """
    if not plan or not isinstance(plan, dict):
        return None

    query = plan.get("query")
    if not query or not isinstance(query, dict):
        # Compound query or recipe — not decomposable into a single
        # model/measures/dimensions/filters tuple.
        return None

    result: dict[str, Any] = {}

    model_id = query.get("model_id")
    if model_id:
        result["model"] = str(model_id).strip()

    measures = query.get("measures")
    if measures and isinstance(measures, list):
        result["measures"] = [str(m).strip() for m in measures if m]

    # Dimensions: when the plan carries ``dimension_exprs`` (present
    # exactly when any dimension is grained/expression — pipeline
    # _plan_dict, decision D2), the ``dimensions`` list holds derived
    # ALIASES (e.g. "order_date_month"), which can never match a bare
    # authored baseline name. Derive comparable names from the raw
    # entries instead: bare string -> itself, {"name","grain"} -> name
    # (grain intentionally not compared), expression dicts -> flag the
    # dimensions field uncomparable (skipped visibly, like filters).
    dimension_exprs = query.get("dimension_exprs")
    if dimension_exprs and isinstance(dimension_exprs, list):
        dim_names: list[str] = []
        dims_uncomparable = False
        for entry in dimension_exprs:
            if isinstance(entry, str) and entry.strip():
                dim_names.append(entry.strip())
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                dim_names.append(entry["name"].strip())
            else:
                dims_uncomparable = True
        if dim_names:
            result["dimensions"] = dim_names
        if dims_uncomparable:
            result["dimensions_uncomparable"] = True
    else:
        dimensions = query.get("dimensions")
        if dimensions and isinstance(dimensions, list):
            result["dimensions"] = [str(d).strip() for d in dimensions if d]

    # Filters: combine where + having into a single list for comparison.
    filters: list[str] = []
    filters_uncomparable = False
    for filter_key in ("where", "having"):
        filter_list = query.get(filter_key)
        if filter_list and isinstance(filter_list, list):
            for f in filter_list:
                if isinstance(f, str):
                    filters.append(f.strip())
                elif isinstance(f, dict):
                    # Structured predicate — serialise to a canonical
                    # "column op value" string for comparison. None means
                    # the predicate has no comparable rendering (compound
                    # or expression AST) — flag it rather than emitting
                    # garbage that would always mismatch.
                    rendered = _predicate_to_string(f)
                    if rendered is None:
                        filters_uncomparable = True
                    else:
                        filters.append(rendered)
    if filters:
        result["filters"] = filters
    if filters_uncomparable:
        result["filters_uncomparable"] = True

    return result if result else None


def _first_present(pred: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first key whose value is not None. Explicit None check —
    a legitimate falsy value (0, False, "") must not fall through."""
    for key in keys:
        val = pred.get(key)
        if val is not None:
            return val
    return None


def _predicate_to_string(pred: dict[str, Any]) -> Optional[str]:
    """Convert a structured predicate dict to a canonical string for
    comparison.  The planner's flat predicate shape (tools/spec.py) is
    ``{"name": ..., "op": ..., "value": ...}``; legacy/external shapes
    with column/dimension/lhs/values/rhs keys are also accepted.

    Returns ``None`` for predicates that cannot be rendered comparably:
    compound predicates (``and``/``or``/``not``) and expression
    predicates whose operands are AST node dicts. Emitting a dict repr
    (or an empty ``"="``) would guarantee a false regression on every
    plan that legitimately uses them — the caller must skip the filters
    comparison instead of comparing garbage.
    """
    # Compound predicates — no comparable flat rendering exists.
    if any(k in pred for k in ("and", "or", "not")):
        return None

    col = _first_present(pred, ("name", "column", "dimension", "lhs", "left"))
    val = _first_present(pred, ("value", "values", "rhs", "right"))

    # Expression predicates: operands are AST node dicts (or lists of
    # them) — str() would produce a Python repr, never matching an
    # authored baseline.
    if isinstance(col, (dict, list)):
        return None
    if isinstance(val, dict):
        return None
    if isinstance(val, list) and any(isinstance(v, (dict, list)) for v in val):
        return None

    if col is None:
        col = ""
    op = pred.get("op", "=")
    # Canonicalise token operators so the rendered predicate matches the
    # naturally authored form ("year = 2024", not "year eq 2024").
    _op_canon = {
        "eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    }
    op_low = str(op).lower()
    op = _op_canon.get(op_low, op)

    if op_low == "is_null":
        return f"{col} is null"
    if op_low == "is_not_null":
        return f"{col} is not null"
    if op_low == "between" and isinstance(val, list) and len(val) == 2:
        return f"{col} between {val[0]} and {val[1]}"

    if val is None:
        val = ""
    if isinstance(val, list):
        val_str = ", ".join(str(v) for v in val)
        return f"{col} {op} ({val_str})"
    return f"{col} {op} {val}"


def _normalise_for_compare(
    items: list[str], *, collapse_ops: bool = False
) -> set[str]:
    """Normalise a list of strings into a set for order-independent,
    case-insensitive comparison. Internal whitespace is collapsed; with
    ``collapse_ops`` (filters), spaces around comparison operators are
    removed (``year = 2024`` == ``year=2024``), ``<>`` unifies with
    ``!=``, and quote characters are stripped so ``region = 'East'``
    matches the unquoted rendering of a structured predicate.

    Deliberate trade (eval-quality comparison, not SQL semantics): quote
    stripping and op collapsing also apply INSIDE quoted literals, so
    values differing only in quoting or internal operator spacing compare
    equal — a theoretical false pass accepted to avoid the systematic
    false FAILS that quoted authored baselines would otherwise produce."""
    result: set[str] = set()
    for item in items:
        if not item:
            continue
        s = re.sub(r"\s+", " ", str(item).lower().strip())
        if collapse_ops:
            s = re.sub(r"\s*(<=|>=|!=|<>|=|<|>)\s*", r"\1", s)
            s = s.replace("<>", "!=")
            s = s.replace("'", "").replace('"', "")
            # Comma spacing inside IN-lists: "(East,West)" == "(East, West)".
            s = re.sub(r"\s*,\s*", ",", s)
        result.add(s)
    return result


def _expected_is_present(raw: Any) -> bool:
    """True when the author supplied SOME expected decomposition value,
    even if it later fails to parse. Distinguishes 'nothing authored'
    (vacuous skip) from 'authored but unparseable' (must be surfaced)."""
    if raw is None:
        return False
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, (dict, list)):
        return bool(raw)
    return True


def compare_decomposition(
    expected_raw: Any,
    plan: Optional[dict[str, Any]],
    *,
    context_model_id: Optional[str] = None,
    turn_status: str = "ok",
) -> DecompositionComparison:
    """Compare an expected decomposition against the actual plan.

    ``context_model_id`` is the UUID of the model context the example
    question belongs to — the authoritative baseline model (see module
    docstring). ``turn_status`` is the pipeline outcome status: a question
    with an expected decomposition that no longer produces a successful
    plan (refused / clarify / error) is a regression even when the
    rejected plan happens to match the baseline.

    Returns a DecompositionComparison with per-field match/mismatch
    details.
    """
    expected = normalise_decomposition(expected_raw)
    if expected is None:
        if _expected_is_present(expected_raw):
            # An expected decomposition WAS authored but could not be
            # parsed into comparable fields. Never a silent pass — the
            # runner surfaces these separately (fail-visible).
            return DecompositionComparison(
                matched=False,
                skipped=True,
                skip_reason="unparseable_decomposition",
            )
        return DecompositionComparison(
            matched=True,  # No expectation = vacuously true.
            skipped=True,
            skip_reason="no_expected_decomposition",
        )

    if turn_status != "ok":
        # The baseline expects a successful plan; a refused/clarify/error
        # turn is a behavioural regression regardless of what plan the
        # LLM emitted before the pipeline rejected it.
        return DecompositionComparison(
            matched=False,
            fields=[
                FieldComparison(
                    field_name="status",
                    matched=False,
                    expected="ok",
                    actual=turn_status,
                    detail=f"turn status regressed to '{turn_status}'"
                    " — expected a successful plan",
                ),
            ],
        )

    actual = _extract_actual_decomposition(plan)
    if actual is None:
        # Expected a decomposition but got no plan or an undecomposable
        # plan (recipe, compound query).
        return DecompositionComparison(
            matched=False,
            fields=[
                FieldComparison(
                    field_name="plan",
                    matched=False,
                    expected="structured query plan",
                    actual=None,
                    detail="no decomposable plan produced",
                ),
            ],
        )

    field_results: list[FieldComparison] = []
    all_matched = True

    for field_name in _COMPARISON_FIELDS:
        exp_val = expected.get(field_name)
        if exp_val is None:
            # Field not specified in expected — skip (don't penalise).
            continue

        act_val = actual.get(field_name)

        if field_name == "model":
            # The plan carries the model UUID; authored baselines carry a
            # human name. Match when the plan routed to the question's own
            # context model (the authoritative baseline) or the author
            # wrote the exact identifier the plan used.
            exp_str = str(exp_val).lower().strip()
            act_str = str(act_val).lower().strip() if act_val else ""
            ctx_str = (
                str(context_model_id).lower().strip()
                if context_model_id
                else ""
            )
            matched = bool(act_str) and (
                exp_str == act_str or (bool(ctx_str) and act_str == ctx_str)
            )
            field_results.append(
                FieldComparison(
                    field_name=field_name,
                    matched=matched,
                    expected=exp_val,
                    actual=act_val,
                    detail=None
                    if matched
                    else "model mismatch: plan model is neither the"
                    " question's context model nor the expected identifier",
                )
            )
        elif field_name in ("measures", "dimensions"):
            if field_name == "dimensions" and actual.get(
                "dimensions_uncomparable"
            ):
                # The plan contains expression dimensions with no
                # comparable name rendering — skip VISIBLY, like filters.
                field_results.append(
                    FieldComparison(
                        field_name=field_name,
                        matched=True,
                        skipped=True,
                        expected=sorted(exp_val)
                        if isinstance(exp_val, list)
                        else exp_val,
                        actual=None,
                        detail="plan contains expression dimensions that"
                        " cannot be rendered for comparison; dimensions"
                        " not compared",
                    )
                )
                continue
            # Set-based, case-insensitive comparison. A non-list expected
            # value is rendered as a single item (like the filters branch)
            # — collapsing it to an empty set would let a plan that
            # DROPPED the field entirely pass vacuously (authored but
            # unusable must never be a silent pass).
            exp_set = _normalise_for_compare(
                exp_val if isinstance(exp_val, list) else [str(exp_val)]
            )
            act_set = _normalise_for_compare(act_val if isinstance(act_val, list) else [])
            matched = exp_set == act_set
            detail = None
            if not matched:
                missing = exp_set - act_set
                extra = act_set - exp_set
                parts = []
                if missing:
                    parts.append(f"missing: {sorted(missing)}")
                if extra:
                    parts.append(f"extra: {sorted(extra)}")
                detail = "; ".join(parts)
            field_results.append(
                FieldComparison(
                    field_name=field_name,
                    matched=matched,
                    expected=sorted(exp_val) if isinstance(exp_val, list) else exp_val,
                    actual=sorted(act_val) if isinstance(act_val, list) else act_val,
                    detail=detail,
                )
            )
        elif field_name == "filters":
            if actual.get("filters_uncomparable"):
                # The plan contains compound (and/or/not) or expression
                # predicates with no comparable flat rendering. Comparing
                # the remaining fragments would false-fail every such
                # plan; skip the filters field VISIBLY instead.
                field_results.append(
                    FieldComparison(
                        field_name=field_name,
                        matched=True,
                        skipped=True,
                        expected=sorted(exp_val)
                        if isinstance(exp_val, list)
                        else exp_val,
                        actual=None,
                        detail="plan contains compound/expression"
                        " predicates that cannot be rendered for"
                        " comparison; filters not compared",
                    )
                )
                continue
            # Structural comparison: normalise both sides and compare as
            # sets.  Filter strings are inherently order-independent;
            # collapse_ops makes "year = 2024" equal "year=2024".
            exp_set = _normalise_for_compare(
                exp_val if isinstance(exp_val, list) else [str(exp_val)],
                collapse_ops=True,
            )
            act_set = _normalise_for_compare(
                act_val if isinstance(act_val, list) else [],
                collapse_ops=True,
            )
            matched = exp_set == act_set
            detail = None
            if not matched:
                missing = exp_set - act_set
                extra = act_set - exp_set
                parts = []
                if missing:
                    parts.append(f"missing: {sorted(missing)}")
                if extra:
                    parts.append(f"extra: {sorted(extra)}")
                detail = "; ".join(parts)
            field_results.append(
                FieldComparison(
                    field_name=field_name,
                    matched=matched,
                    expected=sorted(exp_val) if isinstance(exp_val, list) else exp_val,
                    actual=sorted(act_val) if isinstance(act_val, list) else act_val,
                    detail=detail,
                )
            )

        if not field_results[-1].matched:
            all_matched = False

    return DecompositionComparison(
        matched=all_matched,
        fields=field_results,
    )
