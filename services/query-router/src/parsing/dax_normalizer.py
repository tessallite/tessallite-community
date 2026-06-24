"""
DAX normalizer — converts the restricted DAX subset into a LogicalQuery IR.

Supported DAX forms:
  EVALUATE SUMMARIZECOLUMNS(
      dim1[column], dim2[column], ...,
      [FilterTable],
      "MeasureName", [Measure], ...
  )

  EVALUATE SUMMARIZE(table, dim1[col], ..., "MeasureName", expr, ...)

Not yet supported (falls back to UnsupportedSQL):
  EVALUATE FILTER(table, condition)
  EVALUATE TOPN(N, table, [measure], order)

The parser is intentionally minimal for V1: it handles the most common
Power BI generated SUMMARIZECOLUMNS patterns. Other DAX forms are rejected
with a typed UnsupportedSQL error.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from src.ir.logical_query import LogicalFilter, LogicalQuery, UnsupportedSQL

logger = logging.getLogger(__name__)


class _UnresolvableDax(Exception):
    """Internal signal: a DAX argument could not be faithfully classified.

    Raised inside the structural parsers and converted to ``UnsupportedSQL``
    by ``parse_dax_to_ir`` so the API returns a clean typed 422
    ``feature_not_supported`` rather than silently dropping the grain/filter
    (F-003-03) or routing raw text to the source DB (F-003-04).
    """


# A DAX column reference: ``Table[Column]`` or ``'Table Name'[Column Name]``.
# The table part may be a bare identifier or a single-quoted name (which may
# contain spaces); the column part is inside square brackets and may contain
# spaces. Captures the column name.
_DIM_REF = re.compile(r"""^\s*(?:'[^']+'|[\w\s]+?)\s*\[\s*([^\]]+?)\s*\]\s*$""")
# Measure reference token: ``[Measure Name]`` (no table qualifier).
_MEASURE_REF = re.compile(r"""^\s*\[\s*([^\]]+?)\s*\]\s*$""")
# String alias token: ``"Alias"``.
_ALIAS_REF = re.compile(r'^\s*"([^"]+)"\s*$')
# A column reference embedded anywhere (used to pull the column out of a
# measure expression such as ``SUM('Sales Table'[Order Amount])``).
_EMBEDDED_COL = re.compile(r"""(?:'[^']+'|[\w\s]+?)\s*\[\s*([^\]]+?)\s*\]""")

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_dax_to_ir(
    raw_dax: str,
    model_id: str,
    *,
    parsed_dax: dict | None = None,
) -> LogicalQuery:
    """
    Parse a DAX EVALUATE statement into a LogicalQuery IR.

    If ``parsed_dax`` is provided (pre-parsed IR from the gateway), its
    ``time_variant_hints`` are applied to the returned LogicalQuery instead
    of re-parsing the raw DAX string for them.

    If the DAX cannot be parsed structurally, falls back to a passthrough
    LogicalQuery with no measures/dimensions extracted (routed to source).
    """
    normalized = raw_dax.strip()
    try:
        if re.search(r"\bSUMMARIZECOLUMNS\b", normalized, re.IGNORECASE):
            lq = _parse_summarize_columns(normalized, model_id, raw_dax)
        elif re.search(r"\bSUMMARIZE\b", normalized, re.IGNORECASE):
            lq = _parse_summarize(normalized, model_id, raw_dax)
        else:
            # F-003-04: the only production caller that reaches this branch is
            # the gateway's translator fallback, which ships RAW MDX as the
            # query. Routing that text to the source DB produces an opaque 502.
            # Reject loudly with a typed error instead.
            raise _UnresolvableDax(
                "Unsupported DAX/MDX statement: expected EVALUATE "
                "SUMMARIZECOLUMNS(...) or SUMMARIZE(...)."
            )
    except _UnresolvableDax as e:
        # F-003-03 / F-003-09: never silently degrade to source passthrough —
        # surface the exact reason and raise a typed error the API maps to a
        # clean 422 feature_not_supported.
        logger.warning("DAX normalisation rejected: %s", e)
        raise UnsupportedSQL(str(e)) from e
    except UnsupportedSQL:
        raise
    except Exception as e:
        # Structural/parsing failure (unbalanced parens, etc.). Log and reject
        # rather than route unknown text to source (F-003-09).
        logger.warning("DAX parse failed: %s", e)
        raise UnsupportedSQL(f"Could not parse DAX statement: {e}") from e

    if parsed_dax:
        hints = parsed_dax.get("time_variant_hints")
        if hints and isinstance(hints, dict):
            lq.time_variant_hints = hints

    return lq


# ---------------------------------------------------------------------------
# SUMMARIZECOLUMNS parser
# ---------------------------------------------------------------------------

def _parse_summarize_columns(dax: str, model_id: str, raw: str) -> LogicalQuery:
    """
    EVALUATE SUMMARIZECOLUMNS(
        Table[Dim1], Table[Dim2],          -- GROUP BY columns
        FILTER(...),                       -- optional filter table (skipped structurally)
        "MeasureAlias", [MeasureName],     -- named measure pairs
        ...
    )
    """
    # Extract the argument list inside SUMMARIZECOLUMNS(...)
    inner = _extract_outer_parens(dax, "SUMMARIZECOLUMNS")
    if inner is None:
        raise _UnresolvableDax("SUMMARIZECOLUMNS has no parseable argument list.")

    tokens = _split_top_level(inner)

    dimensions: list[str] = []
    measures: list[str] = []
    filters: list[LogicalFilter] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if not tok:
            i += 1
            continue
        # Table[Column] / 'Table Name'[Column Name] → dimension grain
        col_match = _DIM_REF.match(tok)
        if col_match:
            dimensions.append(col_match.group(1).strip())
            i += 1
            continue
        # "Alias", [MeasureName] pattern → measure (alias paired with a ref)
        alias_match = _ALIAS_REF.match(tok)
        if alias_match:
            if i + 1 < len(tokens):
                next_tok = tokens[i + 1].strip()
                meas_match = _MEASURE_REF.match(next_tok)
                if meas_match:
                    measures.append(meas_match.group(1).strip())
                    i += 2
                    continue
                # Alias followed by an inline expression (SUM(Table[col]),
                # CALCULATE(...), etc.): bind to the column inside it.
                col = _column_in_expression(next_tok)
                if col:
                    measures.append(col)
                    i += 2
                    continue
            # An alias with no resolvable measure ref is an inexpressible
            # projection — fail loud rather than dropping the column.
            raise _UnresolvableDax(
                f"SUMMARIZECOLUMNS measure alias {tok!r} is not followed by a "
                "resolvable measure reference."
            )
        # FILTER(...) → extract a comparison filter; reject if unparseable.
        if re.match(r'^FILTER\s*\(', tok, re.IGNORECASE):
            filt = _try_extract_filter(tok)
            if filt is None:
                raise _UnresolvableDax(
                    f"FILTER condition {tok!r} is not a representable comparison."
                )
            filters.append(filt)
            i += 1
            continue
        # Any other top-level argument we do not understand must NOT be
        # silently skipped (F-003-03) — it could be a grain or filter.
        raise _UnresolvableDax(
            f"Unrecognised SUMMARIZECOLUMNS argument {tok!r}."
        )

    grain = list(dimensions)
    fingerprint = _compute_fingerprint(measures, grain, filters)
    return LogicalQuery(
        model_id=model_id,
        protocol="dax",
        raw_query=raw,
        requested_measures=measures,
        requested_dimensions=dimensions,
        filters=filters,
        grain=grain,
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# SUMMARIZE parser
# ---------------------------------------------------------------------------

def _parse_summarize(dax: str, model_id: str, raw: str) -> LogicalQuery:
    """
    EVALUATE SUMMARIZE(
        Table,
        Table[Dim1], Table[Dim2],
        "MeasureAlias", SUM(Table[Column]),
        ...
    )
    """
    inner = _extract_outer_parens(dax, "SUMMARIZE")
    if inner is None:
        raise _UnresolvableDax("SUMMARIZE has no parseable argument list.")

    tokens = _split_top_level(inner)
    dimensions: list[str] = []
    measures: list[str] = []

    # First token is the table name — skip it. The remaining tokens are
    # dimension refs and (alias, expression) measure pairs.
    rest = [t.strip() for t in tokens[1:]]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok:
            i += 1
            continue
        col_match = _DIM_REF.match(tok)
        if col_match:
            dimensions.append(col_match.group(1).strip())
            i += 1
            continue
        alias_match = _ALIAS_REF.match(tok)
        if alias_match:
            # SUMMARIZE pairs an alias with an EXPRESSION (e.g.
            # "Total Sales", SUM(Sales[Amount])). F-003-03: bind to the COLUMN
            # inside the expression, not the alias string.
            if i + 1 < len(rest):
                col = _column_in_expression(rest[i + 1])
                if col:
                    measures.append(col)
                    i += 2
                    continue
            raise _UnresolvableDax(
                f"SUMMARIZE measure alias {tok!r} has no resolvable column "
                "expression."
            )
        raise _UnresolvableDax(f"Unrecognised SUMMARIZE argument {tok!r}.")

    grain = list(dimensions)
    fingerprint = _compute_fingerprint(measures, grain, [])
    return LogicalQuery(
        model_id=model_id,
        protocol="dax",
        raw_query=raw,
        requested_measures=measures,
        requested_dimensions=dimensions,
        filters=[],
        grain=grain,
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _column_in_expression(expr: str) -> str | None:
    """Return the column name inside a measure expression token.

    SUMMARIZE / SUMMARIZECOLUMNS may pass an inline aggregate expression after
    the alias, e.g. ``SUM('Sales Table'[Order Amount])`` or
    ``CALCULATE(SUM(Sales[Amount]))``. F-003-03: bind to the COLUMN referenced
    inside it rather than the alias string. Returns the first column ref found,
    or None if the expression contains no ``Table[Column]`` reference.
    """
    m = _EMBEDDED_COL.search(expr)
    if m:
        return m.group(1).strip()
    return None


def _extract_outer_parens(text: str, func_name: str) -> str | None:
    """Return the content inside the outermost parentheses of func_name(...)."""
    pattern = re.compile(rf'\b{func_name}\s*\(', re.IGNORECASE)
    m = pattern.search(text)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _split_top_level(text: str) -> list[str]:
    """Split by commas that are not inside parentheses or brackets."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in ("(", "["):
            depth += 1
        elif ch in (")", "]"):
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


# Map DAX comparison operators to LogicalFilter operator names.
_DAX_OPERATORS: dict[str, str] = {
    "=": "eq",
    "==": "eq",
    "<>": "neq",
    "!=": "neq",
    ">=": "gte",
    "<=": "lte",
    ">": "gt",
    "<": "lt",
}

# A FILTER condition that is EXACTLY ``Table[Col] <op> <value>`` — anchored
# end-to-end so a compound expression (``Table[A] + Table[B] > 100``) does NOT
# match a sub-term. Table may be bare or single-quoted; column may contain
# spaces. F-003-03: previously only ``=`` was handled (every other comparison
# silently dropped); Bug-102 class: an UNANCHORED search would also fabricate a
# filter from a sub-term of an arithmetic expression.
_FILTER_CMP = re.compile(
    r"""^\s*(?:'[^']+'|[\w\s]+?)\s*\[\s*([^\]]+?)\s*\]   # Table[Column]
        \s*(<>|!=|>=|<=|==|=|>|<)\s*                     # operator
        (?:"([^"]*)"|(-?\d+\.?\d*)|(TRUE|FALSE))          # quoted | number | bool
        \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _try_extract_filter(filter_expr: str) -> LogicalFilter | None:
    """Extract a comparison filter from ``FILTER(Table, <condition>)``.

    The condition (FILTER's second argument) must be EXACTLY a single
    ``Table[Col] <op> <value>`` comparison — operators ``=, ==, <>, !=, >, >=,
    <, <=`` with a quoted string, numeric, or boolean RHS, and quoted/spaced
    table and column names. Returns None when the condition is anything else
    (compound arithmetic, multiple predicates, function call) so the caller
    rejects the query (fail loud) rather than dropping or mis-reading it.
    """
    inner = _extract_outer_parens(filter_expr, "FILTER")
    if inner is None:
        return None
    # FILTER(Table, <condition>) — the condition is everything after the first
    # top-level comma.
    parts = _split_top_level(inner)
    if len(parts) < 2:
        return None
    condition = ",".join(parts[1:]).strip()
    m = _FILTER_CMP.match(condition)
    if not m:
        return None
    col_name = m.group(1).strip()
    op_token = m.group(2)
    operator = _DAX_OPERATORS.get(op_token)
    if operator is None:
        return None
    str_val, num_val, bool_val = m.group(3), m.group(4), m.group(5)
    value: Any
    if str_val is not None:
        value = str_val
    elif num_val is not None:
        value = int(num_val) if "." not in num_val else float(num_val)
    else:  # boolean
        value = bool_val.upper() == "TRUE"
    return LogicalFilter(dimension_name=col_name, operator=operator, value=value)


def _compute_fingerprint(
    measures: list[str], grain: list[str], filters: list[LogicalFilter]
) -> str:
    data = {
        "measures": sorted(measures),
        "grain": sorted(grain),
        "filter_cols": sorted(f.dimension_name for f in filters),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:64]
