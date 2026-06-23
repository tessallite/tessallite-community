"""
DAX-to-LogicalQuery translator.

Parses a subset of DAX (as produced by Excel/Power BI in DirectQuery mode)
and converts it to the query-router's JSON body format.

Supported DAX subset:
  EVALUATE
  SUMMARIZECOLUMNS(col_ref [, col_ref]*, [filter_expr,]* [measure_def]*)
  SUMMARIZE(table, col_ref [, col_ref]*, [measure_def]*)
  FILTER(table, condition)
  ALL(table | col_ref)  /  ALLSELECTED(table | col_ref)  /  REMOVEFILTERS()
  ROW("label", expr)
  TOPN(n, table, order_col, direction)
  CALCULATE([measure], filter1, filter2, ...) — multi-filter, ALL/REMOVEFILTERS
  CALCULATETABLE(table, filter1, filter2, ...)
  VAR name = expr  RETURN expr
  DISTINCT(column) / VALUES(column)
  SELECTCOLUMNS(table, "Name", expr, ...)  (Power BI)
  ADDCOLUMNS(table, "Name", expr, ...)     (Power BI)
  TREATAS(values, target_column)           (Power BI virtual relationships)
  Aggregates: SUM, AVERAGE, COUNT, COUNTROWS, MIN, MAX, DISTINCTCOUNT
  Time-intelligence stubs: TOTALYTD, TOTALQTD, TOTALMTD, SAMEPERIODLASTYEAR,
    PREVIOUSMONTH/QUARTER/YEAR, DATEADD (recognised, mapped to variant hints)
  Arithmetic: +, -, *, /
  Comparisons: =, <>, <, >, <=, >=
  DIVIDE(num, denom [, alt])  /  IF(cond, true_val, false_val)

The parser uses regex-based extraction (not a full AST parser).
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KPI member functions (Bug-3657)
# ---------------------------------------------------------------------------

# The four MDX/DAX KPI member functions Excel emits via CUBEKPIMEMBER. Each
# takes a KPI caption and a property; the statement translator resolves them to
# the KPI's published value / goal / status / trend expression.
KPI_MEMBER_FUNCTIONS = ("KPIValue", "KPIGoal", "KPIStatus", "KPITrend")

# KPIStatus("aa") / KPIGoal('aa') / KPIValue("a]a") — caption is a quoted
# string (single or double quotes). The caption may itself reference a bracketed
# member, but Excel emits the bare caption, so a quoted literal is the contract.
_KPI_FUNC_RE = re.compile(
    r'\b(KPIValue|KPIGoal|KPIStatus|KPITrend)\s*\(\s*'
    r'''(?P<q>["'])(?P<caption>(?:(?!(?P=q)).)*)(?P=q)\s*\)''',
    re.IGNORECASE,
)


def find_kpi_member_functions(statement: str) -> list[tuple[str, str, str]]:
    """Find KPI member function calls in an MDX/DAX statement.

    Returns a list of ``(matched_text, function_name, kpi_caption)`` tuples,
    one per call, preserving source order. Function names are normalised to the
    canonical casing in :data:`KPI_MEMBER_FUNCTIONS`. Empty when none are
    present, so the caller can skip KPI resolution entirely on the hot path.
    """
    _canon = {f.lower(): f for f in KPI_MEMBER_FUNCTIONS}
    out: list[tuple[str, str, str]] = []
    for m in _KPI_FUNC_RE.finditer(statement or ""):
        fn = _canon.get(m.group(1).lower(), m.group(1))
        out.append((m.group(0), fn, m.group("caption")))
    return out


# ---------------------------------------------------------------------------
# Output structure — maps to query-router /execute body
# ---------------------------------------------------------------------------

@dataclass
class ParsedDAX:
    """Intermediate representation produced by the DAX parser."""
    model_id: str
    dimensions: list[str] = field(default_factory=list)
    dimension_hierarchy_hints: list[str | None] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    order_by: list[dict[str, Any]] = field(default_factory=list)
    limit: Optional[int] = None
    raw_dax: str = ""
    warnings: list[str] = field(default_factory=list)
    time_variant_hints: dict[str, str] = field(default_factory=dict)

    def to_query_body(self) -> dict[str, Any]:
        """Serialize to query-router /execute request body."""
        body: dict[str, Any] = {
            "model_id": self.model_id,
            "query_type": "dax",
            "dimensions": self.dimensions,
            "measures": self.measures,
        }
        if self.filters:
            body["filters"] = self.filters
        if self.order_by:
            body["order_by"] = self.order_by
        if self.limit is not None:
            body["limit"] = self.limit
        if self.time_variant_hints:
            body["time_variant_hints"] = self.time_variant_hints
        return body


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def translate_dax(dax_statement: str, model_id: str) -> ParsedDAX:
    """
    Parse a DAX EVALUATE statement and return a ParsedDAX.

    Raises ValueError if the statement cannot be parsed (caller should
    fall back to forwarding the raw SQL to query-router).

    When ``USE_REGEX_PARSER`` is False (default), delegates to the
    Tree-sitter parser.  Set ``GATEWAY_USE_REGEX_PARSER=true`` to
    revert to the regex path.
    """
    from .constants import USE_REGEX_PARSER
    if not USE_REGEX_PARSER:
        from .ts_dax_parser import translate_dax_ts
        return translate_dax_ts(dax_statement, model_id)

    result = ParsedDAX(model_id=model_id, raw_dax=dax_statement)
    normalized = dax_statement.strip()

    # Strip outer EVALUATE keyword
    inner = _strip_evaluate(normalized)
    if inner is None:
        raise ValueError("DAX statement must start with EVALUATE")

    # Resolve VAR/RETURN blocks before dispatching
    inner = _resolve_vars(inner, result)

    # Dispatch to sub-parsers
    if _starts_with(inner, "SUMMARIZECOLUMNS"):
        _parse_summarize_columns(inner, result)
    elif _starts_with(inner, "SUMMARIZE"):
        _parse_summarize(inner, result)
    elif _starts_with(inner, "ROW"):
        _parse_row(inner, result)
    elif _starts_with(inner, "TOPN"):
        _parse_topn(inner, result)
    elif _starts_with(inner, "CALCULATETABLE") or _starts_with(inner, "FILTER"):
        _parse_filter_table(inner, result)
    elif _starts_with(inner, "CALCULATE"):
        _parse_calculate(inner, result)
    elif _starts_with(inner, "SELECTCOLUMNS"):
        _parse_select_columns(inner, result)
    elif _starts_with(inner, "ADDCOLUMNS"):
        _parse_add_columns(inner, result)
    elif _starts_with(inner, "DISTINCT") or _starts_with(inner, "VALUES"):
        _parse_distinct_values(inner, result)
    else:
        raise ValueError(f"Unsupported DAX expression: {inner[:80]}")

    if result.warnings:
        logger.info("DAX parse warnings: %s", result.warnings)

    logger.debug(
        "Parsed DAX → dims=%s measures=%s filters=%d limit=%s variants=%s",
        result.dimensions,
        result.measures,
        len(result.filters),
        result.limit,
        result.time_variant_hints or "none",
    )
    return result


# ---------------------------------------------------------------------------
# Sub-parsers
# ---------------------------------------------------------------------------

def _parse_summarize_columns(expr: str, result: ParsedDAX) -> None:
    """
    SUMMARIZECOLUMNS(
        [Table[Column], ...],       -- group-by columns
        [FILTER(table, cond), ...], -- filter expressions
        ["MeasureName", expr, ...]  -- named measure expressions
    )
    """
    args = _split_top_level_args(_unwrap_function(expr, "SUMMARIZECOLUMNS"))

    for arg in args:
        arg = arg.strip()
        if not arg:
            continue

        if _starts_with(arg, "FILTER") or _starts_with(arg, "ALL") or _starts_with(arg, "ALLSELECTED"):
            _extract_filter(arg, result)
        elif arg.startswith('"') or arg.startswith("'"):
            # Named measure definition: "Name", expression
            # The name is a label; expression is the measure reference
            _extract_named_measure(arg, args, result)
        else:
            parts = _extract_column_parts(arg)
            if parts:
                # [Column] / Table[Column] / 'Table Name'[Column] group-by ref → dimension
                col = parts[1]
                hint = parts[0]
                if col and col not in result.dimensions:
                    result.dimensions.append(col)
                    result.dimension_hierarchy_hints.append(hint)
                continue

            # Could be a plain measure name or expression
            _try_extract_measure(arg, result)


def _parse_summarize(expr: str, result: ParsedDAX) -> None:
    """
    SUMMARIZE(Table, Table[Column], ..., "Name", expr, ...)
    """
    args = _split_top_level_args(_unwrap_function(expr, "SUMMARIZE"))
    if not args:
        return

    # First arg is the table — skip it
    for arg in args[1:]:
        arg = arg.strip()
        parts = _extract_column_parts(arg)
        if parts:
            col = parts[1]
            hint = parts[0]
            if col and col not in result.dimensions:
                result.dimensions.append(col)
                result.dimension_hierarchy_hints.append(hint)
        elif arg.startswith('"') or arg.startswith("'"):
            _extract_named_measure(arg, args, result)


def _parse_row(expr: str, result: ParsedDAX) -> None:
    """
    ROW("label", expression) — single-value measure query.
    """
    args = _split_top_level_args(_unwrap_function(expr, "ROW"))
    # Ignore label (args[0]); expression is args[1]
    if len(args) >= 2:
        _try_extract_measure(args[1].strip(), result)


def _parse_topn(expr: str, result: ParsedDAX) -> None:
    """
    TOPN(n, table_expr, order_col, ASC|DESC)
    """
    args = _split_top_level_args(_unwrap_function(expr, "TOPN"))
    if not args:
        return

    try:
        result.limit = int(args[0].strip())
    except (ValueError, IndexError):
        pass

    if len(args) >= 2:
        # Parse inner table expression (usually SUMMARIZECOLUMNS)
        _parse_summarize_columns(args[1].strip(), result)

    if len(args) >= 3:
        parts = _extract_column_parts(args[2].strip())
        col = parts[1] if parts else _extract_column_name(args[2].strip())
        hint = parts[0] if parts else None
        direction = "DESC"
        if len(args) >= 4:
            direction = "ASC" if "ASC" in args[3].upper() else "DESC"
        if col:
            result.order_by.append({"column": col, "direction": direction, "table": hint or ""})


def _parse_filter_table(expr: str, result: ParsedDAX) -> None:
    """Handle CALCULATETABLE or FILTER at the top level."""
    _extract_filter(expr, result)


def _parse_calculate(expr: str, result: ParsedDAX) -> None:
    """
    CALCULATE([Measure], filter1, filter2, ...)
    Filters can be: boolean conditions, FILTER(), ALL(), ALLSELECTED(),
    REMOVEFILTERS(), or TREATAS().
    """
    inner = _unwrap_function(expr, "CALCULATE")
    args = _split_top_level_args(inner)
    if not args:
        return

    _try_extract_measure(args[0].strip(), result)

    for arg in args[1:]:
        arg = arg.strip()
        if _starts_with(arg, "ALL") or _starts_with(arg, "ALLSELECTED") or _starts_with(arg, "REMOVEFILTERS"):
            continue
        if _starts_with(arg, "FILTER"):
            _extract_filter(arg, result)
        elif _starts_with(arg, "TREATAS"):
            result.warnings.append(f"TREATAS ignored — unsupported: {arg[:60]}")
        else:
            parsed = _parse_condition(arg)
            if parsed:
                result.filters.append(parsed)


def _parse_select_columns(expr: str, result: ParsedDAX) -> None:
    """
    SELECTCOLUMNS(table, "Name", expr, "Name2", expr2, ...)
    Power BI uses this for custom column projections.
    """
    args = _split_top_level_args(_unwrap_function(expr, "SELECTCOLUMNS"))
    if not args:
        return

    # First arg is source table — may be a SUMMARIZECOLUMNS or table ref
    source = args[0].strip()
    if _starts_with(source, "SUMMARIZECOLUMNS"):
        _parse_summarize_columns(source, result)
    elif _starts_with(source, "SUMMARIZE"):
        _parse_summarize(source, result)

    # Remaining args are "Name", expression pairs
    i = 1
    while i < len(args):
        arg = args[i].strip()
        if arg.startswith('"') or arg.startswith("'"):
            name = arg.strip('"').strip("'").strip()
            if i + 1 < len(args):
                _try_extract_measure(args[i + 1].strip(), result)
                if name and name not in result.measures:
                    result.measures.append(name)
                i += 2
                continue
        i += 1


def _parse_add_columns(expr: str, result: ParsedDAX) -> None:
    """
    ADDCOLUMNS(table, "Name", expr, ...) — Power BI column injection.
    Same structure as SELECTCOLUMNS.
    """
    args = _split_top_level_args(_unwrap_function(expr, "ADDCOLUMNS"))
    if not args:
        return

    source = args[0].strip()
    if _starts_with(source, "SUMMARIZECOLUMNS"):
        _parse_summarize_columns(source, result)
    elif _starts_with(source, "SUMMARIZE"):
        _parse_summarize(source, result)
    elif _starts_with(source, "TOPN"):
        _parse_topn(source, result)

    i = 1
    while i < len(args):
        arg = args[i].strip()
        if arg.startswith('"') or arg.startswith("'"):
            name = arg.strip('"').strip("'").strip()
            if i + 1 < len(args):
                _try_extract_measure(args[i + 1].strip(), result)
                if name and name not in result.measures:
                    result.measures.append(name)
                i += 2
                continue
        i += 1


def _parse_distinct_values(expr: str, result: ParsedDAX) -> None:
    """
    DISTINCT(Table[Column]) / VALUES(Table[Column])
    Returns unique values — maps to SELECT DISTINCT.
    """
    func_name = "DISTINCT" if _starts_with(expr, "DISTINCT") else "VALUES"
    inner = _unwrap_function(expr, func_name).strip()
    parts = _extract_column_parts(inner)
    if parts:
        col = parts[1]
        hint = parts[0]
        if col and col not in result.dimensions:
            result.dimensions.append(col)
            result.dimension_hierarchy_hints.append(hint)


# ---------------------------------------------------------------------------
# Filter extraction
# ---------------------------------------------------------------------------

def _extract_filter(expr: str, result: ParsedDAX) -> None:
    """
    Extract a filter condition from FILTER(table, condition) or
    ALL/ALLSELECTED/REMOVEFILTERS (which remove filters — ignored).
    """
    if (_starts_with(expr, "ALL") or _starts_with(expr, "ALLSELECTED")
            or _starts_with(expr, "REMOVEFILTERS")):
        return

    # FILTER(table, condition)
    if _starts_with(expr, "FILTER"):
        inner = _unwrap_function(expr, "FILTER")
        parts = _split_top_level_args(inner)
        if len(parts) >= 2:
            condition = parts[1].strip()
            parsed = _parse_condition(condition)
            if parsed:
                result.filters.append(parsed)

    # CALCULATETABLE(table, filter1, filter2, ...)
    elif _starts_with(expr, "CALCULATETABLE"):
        inner = _unwrap_function(expr, "CALCULATETABLE")
        args = _split_top_level_args(inner)
        for arg in args[1:]:
            parsed = _parse_condition(arg.strip())
            if parsed:
                result.filters.append(parsed)


def _parse_condition(condition: str) -> Optional[dict[str, Any]]:
    """
    Parse a simple DAX condition: Table[Column] = value, <, >, etc.

    Returns {"column": name, "operator": op, "value": val} or None.
    """
    ops = ["<>", "<=", ">=", "=", "<", ">"]
    for op in ops:
        if op in condition:
            parts = condition.split(op, 1)
            col_parts = _extract_column_parts(parts[0].strip())
            col = col_parts[1] if col_parts else _extract_column_name(parts[0].strip())
            hint = col_parts[0] if col_parts else None
            raw_value = parts[1].strip()
            value_str = raw_value.strip('"').strip("'")
            # F-002-12: a quoted RHS is a string literal; keep that typing so the
            # SQL emitter does not unquote a numeric-looking string.
            value_is_string = raw_value.startswith(('"', "'"))
            if col:
                return {
                    "column": col,
                    "table": hint,
                    "operator": _normalize_op(op),
                    "value": value_str,
                    "value_is_string": value_is_string,
                }
    return None


def _normalize_op(op: str) -> str:
    mapping = {"=": "eq", "<>": "neq", "<": "lt", ">": "gt", "<=": "lte", ">=": "gte"}
    return mapping.get(op, op)


# ---------------------------------------------------------------------------
# Measure extraction helpers
# ---------------------------------------------------------------------------

def _extract_named_measure(
    first_arg: str,
    all_args: list[str],
    result: ParsedDAX,
) -> None:
    """
    Parse a "MeasureName", expression pair.
    The expression is typically SUM([Column]), [MeasureName], etc.
    We extract the measure name from the string label (first_arg).
    """
    measure_name = first_arg.strip('"').strip("'").strip()
    if measure_name and measure_name not in result.measures:
        result.measures.append(measure_name)


_TIME_INTEL_FUNCS = {
    "TOTALYTD": "ytd",
    "TOTALQTD": "qtd",
    "TOTALMTD": "mtd",
    "SAMEPERIODLASTYEAR": "prior_year",
    "PREVIOUSMONTH": "prior_month",
    "PREVIOUSQUARTER": "prior_quarter",
    "PREVIOUSYEAR": "prior_year",
    "DATEADD": "period_offset",
}


def _try_extract_measure(expr: str, result: ParsedDAX) -> None:
    """
    Try to extract a measure name from an expression like:
      SUM(Table[Column])
      [MeasureName]
      CALCULATE([Measure], ...)
      DIVIDE(num, denom [, alt])
      IF(cond, true_val, false_val)
      TOTALYTD([Measure], dates)
    """
    stripped = expr.strip()

    # [MeasureName] bracket reference
    bracket_match = re.match(r"^\[([^\]]+)\]$", stripped)
    if bracket_match:
        name = bracket_match.group(1)
        if name not in result.measures:
            result.measures.append(name)
        return

    # SUM(Table[Column]) → aggregate over column named as measure
    agg_match = re.match(
        r"^(SUM|AVERAGE|COUNT|COUNTROWS|MIN|MAX|DISTINCTCOUNT)\s*\(",
        stripped,
        re.IGNORECASE,
    )
    if agg_match:
        inner = _unwrap_function(stripped, agg_match.group(1))
        col = _extract_column_name(inner.strip())
        if col and col not in result.measures:
            result.measures.append(col)
        return

    # CALCULATE([Measure], filter1, filter2, ...)
    if _starts_with(stripped, "CALCULATE") and not _starts_with(stripped, "CALCULATETABLE"):
        inner = _unwrap_function(stripped, "CALCULATE")
        parts = _split_top_level_args(inner)
        if parts:
            _try_extract_measure(parts[0].strip(), result)
            measure_name = _last_added_measure(result)
            for farg in parts[1:]:
                farg = farg.strip()
                if (_starts_with(farg, "ALL") or _starts_with(farg, "ALLSELECTED")
                        or _starts_with(farg, "REMOVEFILTERS")):
                    continue
                if _starts_with(farg, "FILTER"):
                    _extract_filter(farg, result)
                    continue
                # Time-intelligence functions used as filter modifiers
                ti_matched = False
                for func_name, variant_key in _TIME_INTEL_FUNCS.items():
                    if _starts_with(farg, func_name):
                        if measure_name:
                            result.time_variant_hints[measure_name] = variant_key
                        ti_matched = True
                        break
                if ti_matched:
                    continue
                if _starts_with(farg, "TREATAS"):
                    result.warnings.append(f"TREATAS ignored — unsupported: {farg[:60]}")
                    continue
                parsed = _parse_condition(farg)
                if parsed:
                    result.filters.append(parsed)
        return

    # Time-intelligence functions: TOTALYTD([Measure], dates_column)
    for func_name, variant_key in _TIME_INTEL_FUNCS.items():
        if _starts_with(stripped, func_name):
            inner = _unwrap_function(stripped, func_name)
            parts = _split_top_level_args(inner)
            if parts:
                measure_expr = parts[0].strip()
                _try_extract_measure(measure_expr, result)
                measure_name = _last_added_measure(result)
                if measure_name:
                    result.time_variant_hints[measure_name] = variant_key
            return

    # DIVIDE(numerator, denominator [, alternate])
    if _starts_with(stripped, "DIVIDE"):
        inner = _unwrap_function(stripped, "DIVIDE")
        parts = _split_top_level_args(inner)
        for part in parts[:2]:
            _try_extract_measure(part.strip(), result)
        return

    # IF(condition, true_val, false_val)
    if _starts_with(stripped, "IF"):
        inner = _unwrap_function(stripped, "IF")
        parts = _split_top_level_args(inner)
        for part in parts[1:]:
            _try_extract_measure(part.strip(), result)
        return

    # Arithmetic expression: expr + expr, expr - expr, etc.
    arith = _split_arithmetic(stripped)
    if arith:
        for sub_expr in arith:
            _try_extract_measure(sub_expr, result)
        return


# ---------------------------------------------------------------------------
# String / parsing utilities
# ---------------------------------------------------------------------------

def _last_added_measure(result: ParsedDAX) -> Optional[str]:
    """Return the most recently added measure name, or None."""
    return result.measures[-1] if result.measures else None


def _resolve_vars(expr: str, result: ParsedDAX) -> str:
    """
    Resolve VAR/RETURN blocks by substituting variable references.
    VAR x = <expr>  VAR y = <expr>  RETURN <body>
    Returns the RETURN body with variables inlined.
    """
    upper = expr.upper()
    if "VAR " not in upper:
        return expr

    variables: dict[str, str] = {}
    remaining = expr
    var_pattern = re.compile(
        r"\bVAR\s+(\w+)\s*=\s*", re.IGNORECASE
    )

    while True:
        m = var_pattern.search(remaining)
        if not m:
            break
        var_name = m.group(1)
        after_eq = remaining[m.end():]

        # Find end of value: next VAR or RETURN keyword at same depth
        val_end = _find_var_boundary(after_eq)
        var_value = after_eq[:val_end].strip()
        variables[var_name] = var_value
        remaining = after_eq[val_end:]

    # Find RETURN keyword
    ret_match = re.match(r"\s*RETURN\s+", remaining, re.IGNORECASE)
    if ret_match:
        body = remaining[ret_match.end():]
    else:
        body = remaining

    # Substitute variable references in the body
    for var_name, var_value in variables.items():
        body = re.sub(r'\b' + re.escape(var_name) + r'\b', var_value, body)

    return body.strip()


def _find_var_boundary(text: str) -> int:
    """Find where a VAR value ends (next top-level VAR or RETURN)."""
    depth = 0
    in_str = False
    str_char = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == str_char:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_char = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            rest = text[i:]
            if re.match(r'\bVAR\s', rest, re.IGNORECASE) or re.match(r'\bRETURN\s', rest, re.IGNORECASE):
                return i
        i += 1
    return len(text)


def _split_arithmetic(expr: str) -> list[str] | None:
    """
    Split a top-level arithmetic expression (a + b, a - b, a * b, a / b)
    into operands. Only splits at depth 0. Returns None if not arithmetic.
    """
    depth = 0
    in_str = False
    str_char = ""
    operands = []
    buf: list[str] = []
    found_op = False

    for ch in expr:
        if in_str:
            buf.append(ch)
            if ch == str_char:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_char = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif depth == 0 and ch in "+-" and buf:
            token = "".join(buf).strip()
            if token:
                operands.append(token)
            buf = []
            found_op = True
        else:
            buf.append(ch)

    if buf:
        token = "".join(buf).strip()
        if token:
            operands.append(token)

    if found_op and len(operands) >= 2:
        return operands
    return None


def _starts_with(s: str, prefix: str) -> bool:
    return s.upper().startswith(prefix.upper())


def _strip_evaluate(dax: str) -> Optional[str]:
    """Remove leading EVALUATE keyword."""
    match = re.match(r"^EVALUATE\s+", dax, re.IGNORECASE)
    if match:
        return dax[match.end():]
    return None


def _unwrap_function(expr: str, func_name: str) -> str:
    """
    Return the inner content of funcName(...).
    E.g. SUMMARIZECOLUMNS(a, b, c) → 'a, b, c'
    """
    pattern = re.compile(
        r"^" + re.escape(func_name) + r"\s*\(", re.IGNORECASE
    )
    match = pattern.match(expr)
    if not match:
        return expr
    start = match.end()
    # Find the matching close paren
    depth = 1
    i = start
    while i < len(expr) and depth > 0:
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
        i += 1
    return expr[start : i - 1]


def _split_top_level_args(expr: str) -> list[str]:
    """
    Split a comma-separated argument list, respecting nested parentheses
    and quoted strings.
    """
    args: list[str] = []
    depth = 0
    in_str = False
    str_char = ""
    buf = []
    for ch in expr:
        if in_str:
            buf.append(ch)
            if ch == str_char:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_char = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf))
    return args


def _extract_column_name(ref: str) -> Optional[str]:
    """
    Extract the column name from a Table[Column] or [Column] reference.
    Returns the bare column name string or None.
    """
    parts = _extract_column_parts(ref)
    if parts:
        return parts[1]
    # Table[Column]
    match = re.search(r"\[([^\]]+)\]", ref)
    if match:
        return match.group(1)
    # Plain identifier
    plain = re.match(r"^[\w]+$", ref.strip())
    if plain:
        return ref.strip()
    return None


def _extract_column_parts(ref: str) -> tuple[str | None, str] | None:
    """
    Extract (table/hierarchy hint, column) from:
      Table[Column]
      'Table Name'[Column]
      [Column]
    """
    text = ref.strip()
    match = re.match(r"^(?:'([^']+)'|([A-Za-z_][\w]*))?\s*\[([^\]]+)\]$", text)
    if not match:
        return None
    table = (match.group(1) or match.group(2) or "").strip() or None
    column = (match.group(3) or "").strip()
    if not column:
        return None
    return table, column
