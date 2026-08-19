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

import logging
import re
from typing import Any

from shared.pocket.fingerprint import fingerprint_shape
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
# Bug-6959/F3: captures the FULL ``Table[Column]`` pair as group(1) (normalised
# form) and the bare column name as group(2).  Previous version captured only the
# column name, so ``Sales[Amount]`` and ``Budget[Amount]`` collapsed into one
# distinct entry — a wrong-numbers hole.
# Codex R2: single-quoted table names may contain escaped apostrophes (doubled:
# ``'North''s Sales'``); ``(?:[^']|'')+`` handles these correctly.
_EMBEDDED_COL = re.compile(
    r"""((?:'(?:[^']|'')+?'|[\w\s]+?)\s*\[\s*([^\]]+?)\s*\])"""
)
# Aggregate-node pattern: ``AGG_FUNC(Table[Column])`` — captures the function
# name as group(1) and the full Table[Column] as group(2).  Used to detect
# differing aggregations over the same column (e.g. SUM vs COUNT).
_EMBEDDED_AGG_NODE = re.compile(
    r"""(SUM|COUNT|COUNTROWS|MIN|MAX|AVERAGE|AVERAGEX|SUMX|COUNTX|MAXX|MINX|DISTINCTCOUNT)"""
    r"""\s*\(\s*((?:'(?:[^']|'')+?'|[\w\s]+?)\s*\[\s*[^\]]+?\s*\])""",
    re.IGNORECASE,
)

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

    If the DAX cannot be parsed structurally, raises a typed ``UnsupportedSQL``
    (F-003-01/F-003-03/F-003-09) rather than routing unknown text to source.
    """
    normalized = raw_dax.strip()
    try:
        # F-003-01: validate the COMPLETE DAX envelope with full-input
        # consumption BEFORE dispatching on a keyword. The recogniser used to be
        # a substring/``\b`` search, so any text OUTSIDE the recognised call was
        # silently discarded: ``EVALUATE TOPN(1, SUMMARIZECOLUMNS(...), [Rev],
        # DESC)`` narrowed to the inner call (returning ALL rows unsorted), and a
        # trailing ``... ORDER BY Sales[Region] DESC`` was dropped. TOPN /
        # ORDER BY / START AT are not represented in the IR (``order_by=[]``,
        # ``limit=None`` are hard-coded), so until they are, the ONLY
        # representable envelope is exactly ``EVALUATE <ws>? SUMMARIZECOLUMNS(...)``
        # or ``EVALUATE <ws>? SUMMARIZE(...)`` with nothing but whitespace before
        # ``EVALUATE``, between it and the call, and after the call's closing
        # paren. Any non-whitespace residue -> loud typed rejection, restoring the
        # loud-unsupported intent of closed Bug-5194/Bug-3710.
        envelope_func = _validate_dax_envelope(normalized)
        if envelope_func == "SUMMARIZECOLUMNS":
            lq = _parse_summarize_columns(normalized, model_id, raw_dax)
        elif envelope_func == "SUMMARIZE":
            lq = _parse_summarize(normalized, model_id, raw_dax)
        else:
            # F-003-04: raw MDX / an unrecognised top-level function reaches this
            # branch (the gateway translator fallback ships raw MDX). F-003-01: an
            # outer TOPN(...) wrapping SUMMARIZECOLUMNS, a trailing ORDER BY /
            # START AT, or any residue outside the single top-level call also
            # reaches here (``_validate_dax_envelope`` returned None because the
            # input was not fully consumed). Routing any of these to the source DB
            # produces an opaque 502 OR — worse — silently returns a widened /
            # unsorted result. Reject loudly with a typed error instead.
            raise _UnresolvableDax(
                "Unsupported DAX statement. The only supported form is a single "
                "top-level EVALUATE SUMMARIZECOLUMNS(...) or EVALUATE "
                "SUMMARIZE(...) with no surrounding wrapper (e.g. TOPN) and no "
                "trailing ORDER BY / START AT clause."
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
    # Bug-7796: collect requested aggregate overrides from inline DAX agg
    # expressions so the source rewriter applies the REQUESTED function.
    agg_overrides: dict[str, str] = {}

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
                # Alias followed by an inline expression. CALCULATE carries
                # filter context; parse it structurally rather than letting a
                # filter column masquerade as the measure target.
                col, agg_func, expression_filters = _measure_and_filters_in_expression(next_tok)
                if col:
                    measures.append(col)
                    if agg_func:
                        # Bug-7796: detect conflicting agg overrides for the
                        # same column (case-insensitive) and refuse -- silently
                        # picking one would serve the wrong function.
                        col_lower = col.lower()
                        existing = agg_overrides.get(col_lower)
                        if existing is not None and existing != agg_func:
                            raise _UnresolvableDax(
                                f"Column {col!r} is referenced with "
                                f"conflicting aggregates ({existing.upper()} "
                                f"and {agg_func.upper()}); define each as a "
                                "separate measure."
                            )
                        agg_overrides[col_lower] = agg_func
                    filters.extend(expression_filters)
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
    fingerprint = _compute_fingerprint(measures, dimensions, grain, filters)
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
        measure_agg_overrides=agg_overrides,
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
    agg_overrides: dict[str, str] = {}

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
                col, agg_func, expression_filters = _measure_and_filters_in_expression(rest[i + 1])
                if col:
                    measures.append(col)
                    if agg_func:
                        col_lower = col.lower()
                        existing = agg_overrides.get(col_lower)
                        if existing is not None and existing != agg_func:
                            raise _UnresolvableDax(
                                f"Column {col!r} is referenced with "
                                f"conflicting aggregates ({existing.upper()} "
                                f"and {agg_func.upper()}); define each as a "
                                "separate measure."
                            )
                        agg_overrides[col_lower] = agg_func
                    if expression_filters:
                        raise _UnresolvableDax(
                            "SUMMARIZE CALCULATE filter context is not supported; "
                            "use SUMMARIZECOLUMNS for filtered DAX projections."
                        )
                    i += 2
                    continue
            raise _UnresolvableDax(
                f"SUMMARIZE measure alias {tok!r} has no resolvable column "
                "expression."
            )
        raise _UnresolvableDax(f"Unrecognised SUMMARIZE argument {tok!r}.")

    grain = list(dimensions)
    fingerprint = _compute_fingerprint(measures, dimensions, grain, [])
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
        measure_agg_overrides=agg_overrides,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Regex to split a ``Table[Column]`` reference into its table and column parts.
# Group 1: quoted table (may contain escaped apostrophes as doubled '').
# Group 2: unquoted table (bare identifier, may contain spaces).
# Group 3: the column name (inside brackets, may contain spaces).
_TABLE_COL_PARTS = re.compile(
    r"""^\s*(?:'((?:[^']|'')+?)'|([\w\s]+?))\s*\[\s*([^\]]+?)\s*\]\s*$"""
)


def _normalise_table_col_ref(full_ref: str) -> str:
    """Canonical form of a ``Table[Column]`` reference for deduplication.

    Bug-6959/F3 codex finding: the naive ``re.sub(r"\\s+", "", ref).lower()``
    approach collapses identifiers that differ only by internal whitespace
    (e.g. ``'Sales Data'[Amount]`` vs ``'SalesData'[Amount]``) and treats
    ``Sales[Amount]`` vs ``'Sales'[Amount]`` as different.

    This function:
    - Strips optional surrounding single quotes from the table name.
    - Trims leading/trailing whitespace from both table and column parts,
      but preserves INTERNAL whitespace (``Order Amount`` != ``OrderAmount``).
    - Lowercases both parts (DAX identifiers are case-insensitive).
    - Reassembles as ``table[column]`` (no quotes, no extra spaces).
    """
    m = _TABLE_COL_PARTS.match(full_ref)
    if m:
        # Group 1 = quoted table, group 2 = unquoted table, group 3 = column.
        # Unescape doubled apostrophes in quoted table names (DAX escaping).
        raw_table = m.group(1) or m.group(2) or ""
        table = raw_table.replace("''", "'").strip().lower()
        col = m.group(3).strip().lower()
        return f"{table}[{col}]"
    # Fallback: if the regex doesn't match (shouldn't happen for valid
    # _EMBEDDED_COL output), strip outer whitespace and lowercase.
    return full_ref.strip().lower()


def _column_in_expression(expr: str) -> tuple[str | None, str | None]:
    """Return (column_name, requested_agg_function) from a measure expression.

    SUMMARIZE / SUMMARIZECOLUMNS may pass an inline aggregate expression after
    the alias, e.g. ``SUM('Sales Table'[Order Amount])`` or
    ``CALCULATE(SUM(Sales[Amount]))``. F-003-03: bind to the COLUMN referenced
    inside it rather than the alias string.

    Bug-6959: when the expression contains MORE THAN ONE distinct
    ``Table[Column]`` reference (e.g. ``DIVIDE(SUM(Sales[Revenue]),
    SUM(Sales[Cost]))``), binding to the first column alone produces a
    silently wrong number.  Raise ``_UnresolvableDax`` so the API returns a
    clean 422 ``feature_not_supported`` instead of the wrong result.

    Bug-6959/F3: the original fix compared COLUMN NAMES only, ignoring the
    table qualifier.  ``Sales[Amount]`` vs ``Budget[Amount]`` collapsed to
    one entry ("Amount") and passed the gate — wrong numbers.  We now
    compare the full normalised ``Table[Column]`` pair.  Additionally, we
    detect differing aggregate functions over the same column (e.g.
    ``SUM(Sales[Amount])`` vs ``COUNT(Sales[Amount])``) which would also
    silently bind to a single aggregate — wrong numbers.

    Bug-7796: returns a tuple ``(column_name, agg_function)`` where
    ``agg_function`` is the lowercased requested aggregate (e.g. ``"sum"``)
    when the expression is a lone ``AGG(Table[Column])``; ``None`` otherwise
    (bare column reference or refused expression). The caller stores the
    override so the source rewriter applies the REQUESTED function, not the
    measure's default_agg.

    Returns ``(None, None)`` if the expression contains no ``Table[Column]``
    reference.
    """
    matches = _EMBEDDED_COL.findall(expr)
    if not matches:
        return None, None
    # matches is a list of (full_table_col, bare_col) tuples.

    distinct_refs = {_normalise_table_col_ref(m[0]) for m in matches}
    if len(distinct_refs) > 1:
        readable = sorted({m[0].strip() for m in matches})
        raise _UnresolvableDax(
            f"Multi-column inline DAX expression references {len(distinct_refs)} "
            f"distinct columns ({', '.join(readable)}); "
            "only single-column aggregate expressions are supported."
        )

    # Bug-6959/F3 second gate: differing aggregate functions over the SAME
    # column (e.g. SUM vs COUNT) produce a ratio the semantic layer cannot
    # represent as a single aggregate binding.
    agg_nodes = _EMBEDDED_AGG_NODE.findall(expr)
    if agg_nodes:
        distinct_agg_nodes = {
            (func.upper(), _normalise_table_col_ref(ref))
            for func, ref in agg_nodes
        }
        if len(distinct_agg_nodes) > 1:
            readable = sorted(
                f"{func}({ref.strip()})" for func, ref in agg_nodes
            )
            raise _UnresolvableDax(
                f"Inline DAX expression uses {len(distinct_agg_nodes)} distinct "
                f"aggregate nodes ({', '.join(readable)}); "
                "only single-aggregate expressions are supported."
            )

    # Bug-7595: when the SAME column is referenced more than once (e.g.
    # ``SUM(Sales[Amount]) + SUM(Sales[Amount])``), the column count is 1
    # so the multi-column gate above passes, but the arithmetic structure
    # is lost — the downstream IR stores only ``requested_measures=['Amount']``
    # and the result is SUM(Amount) instead of 2*SUM(Amount).  Detect
    # repeated column references (more raw matches than distinct refs) and
    # refuse the expression so it routes to source instead of silently
    # binding to a single aggregate.  This check runs AFTER the different-
    # aggregation gate so SUM-vs-COUNT gets the specific diagnostic.
    if len(matches) > len(distinct_refs):
        col_ref = matches[0][0].strip()
        raise _UnresolvableDax(
            f"Inline DAX expression references {col_ref!r} {len(matches)} times; "
            "repeated column references with arithmetic operators are not "
            "representable as a single aggregate binding."
        )

    # Bug-7765: a SINGLE distinct column with SURROUNDING scalar structure still
    # binds to the bare column with the measure's default_agg, silently dropping
    # BOTH the scalar wrapper AND the requested aggregate function.  Examples the
    # earlier gates let through (one distinct column, one distinct agg node):
    #   SUM(Sales[Amount]) * 1.1        -> served as SUM(Amount), the *1.1 lost
    #   DIVIDE(SUM(Sales[Amount]), 100) -> served as SUM(Amount), the /100 lost
    #   MIN(Sales[Amount])              -> served with the measure default_agg,
    #                                      NOT MIN (function dropped)
    # The LogicalQuery IR has no slot for a scalar wrapper or a per-reference
    # aggregate override, so binding here is silently wrong numbers.  A lone
    # ``Table[Column]`` (no wrapper) or a lone ``AGG(Table[Column])`` whose
    # function IS the aggregate the semantic layer will apply is the only
    # representable shape; anything else must fail loud and route to source.
    #
    # We recognise the two safe shapes by reconstructing the canonical form of
    # the single reference / single agg node and comparing it (whitespace- and
    # case-insensitively) to the whole expression.  Any residual characters mean
    # extra structure (arithmetic, extra function wrapper, literals, CALCULATE).
    def _squash(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()

    squashed_expr = _squash(expr)
    lone_ref = _squash(matches[0][0])
    if squashed_expr == lone_ref:
        # Bare ``Table[Column]`` with no aggregate wrapper — bind by name
        # (existing, safe behaviour; the measure supplies its default_agg).
        return matches[0][1].strip(), None

    if agg_nodes:
        # Exactly one distinct agg node reached here.  It is representable ONLY
        # when the ENTIRE expression is that single ``AGG(Table[Column])`` node
        # with nothing around it — otherwise a scalar wrapper is being dropped.
        func, ref = agg_nodes[0]
        lone_agg = _squash(f"{func}({ref})")
        if squashed_expr == lone_agg:
            # Bug-7796: a lone ``SUM(Table[Column])`` or ``SUMX(Table[Column])``
            # carries the ``"sum"`` override so the source rewriter applies SUM
            # regardless of the measure's default_agg. This fixes the core
            # wrong-numbers case (SUM(col) where default_agg=avg silently served
            # AVG). ``SUMX`` is the degenerate single-arg form treated as SUM.
            #
            # Non-SUM functions (MIN, MAX, AVERAGE, DISTINCTCOUNT, COUNT) are
            # REFUSED (fail-closed) because the aggregate route does not read
            # measure_agg_overrides -- it would serve the measure's default_agg
            # instead of the requested function, a route-dependent wrong number.
            # Accepting them requires L3 to wire a matcher check first. Until
            # then, the user gets a clean 422 with guidance to define a measure
            # (the prior Bug-7765 behavior for these functions).
            func_upper = func.upper()
            if func_upper in {"SUM", "SUMX"}:
                return matches[0][1].strip(), "sum"
            raise _UnresolvableDax(
                f"Inline DAX expression {expr.strip()!r} requests {func_upper} "
                "over a column, but a single-aggregate binding applies the "
                "measure's own default aggregation; this would silently change "
                "the result. Define a measure with this aggregation instead."
            )

    # Single column wrapped in scalar structure (arithmetic, DIVIDE, an extra
    # function, or literals) that the IR cannot carry — fail loud.
    raise _UnresolvableDax(
        f"Inline DAX expression {expr.strip()!r} wraps a single aggregate in "
        "scalar structure that cannot be represented as one aggregate binding "
        "(the surrounding expression would be silently dropped). Route this to "
        "source or define it as a calculated measure."
    )


def _measure_and_filters_in_expression(
    expr: str,
) -> tuple[str | None, str | None, list[LogicalFilter]]:
    """Return (measure_name, agg_function, filters) from a DAX expression.

    ``CALCULATE([Revenue], FILTER(Sales, Sales[Channel] = "WEB"))`` must bind
    ``Revenue`` as the measure and preserve the Channel filter. A generic
    embedded-column scan would otherwise find ``Channel`` first and drop the
    filter context.

    Bug-7796: the aggregate function from ``_column_in_expression`` is now
    threaded through so the caller can record the requested aggregation.
    """
    inner = _extract_outer_parens(expr, "CALCULATE")
    if inner is None:
        col, agg = _column_in_expression(expr)
        return col, agg, []

    parts = [p.strip() for p in _split_top_level(inner) if p.strip()]
    if not parts:
        return None, None, []

    first = parts[0]
    measure_match = _MEASURE_REF.match(first)
    agg_func: str | None = None
    if measure_match:
        measure = measure_match.group(1).strip()
    else:
        measure, agg_func = _column_in_expression(first)
    if not measure:
        return None, None, []

    filters: list[LogicalFilter] = []
    for filter_part in parts[1:]:
        if re.match(r'^FILTER\s*\(', filter_part, re.IGNORECASE):
            filt = _try_extract_filter(filter_part)
            if filt is None:
                raise _UnresolvableDax(
                    f"CALCULATE filter context {filter_part!r} is not a "
                    "representable comparison."
                )
            filters.append(filt)
            continue
        raise _UnresolvableDax(
            f"CALCULATE argument {filter_part!r} is not a supported filter context."
        )
    return measure, agg_func, filters


# The leading envelope: optional whitespace, ``EVALUATE``, whitespace, then the
# top-level table function name and its opening paren. Anchored at the start so a
# nested ``SUMMARIZECOLUMNS`` inside an outer ``TOPN(...)`` is NOT mistaken for the
# top-level call (the outer call name would be captured instead, and the leftover
# outer ``TOPN(`` / trailing args would fail the residue check below).
_DAX_ENVELOPE_HEAD = re.compile(
    r"""^\s*EVALUATE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(""",
    re.IGNORECASE,
)


def _validate_dax_envelope(text: str) -> str | None:
    """Return the top-level table-function name iff the WHOLE input is exactly
    ``EVALUATE <ws> <FUNC>(...)`` (full-input consumption), else ``None``.

    F-003-01: the top-level call must be the FIRST token after ``EVALUATE`` and
    its matching close paren must be the LAST non-whitespace character. Anything
    else — an outer ``TOPN(...)`` wrapping ``SUMMARIZECOLUMNS``, a trailing
    ``ORDER BY`` / ``START AT``, a second statement — leaves non-whitespace
    residue and yields ``None`` (the caller then raises a typed ``UnsupportedSQL``
    rather than silently narrowing to the inner call).

    Returns the UPPER-CASED function name (``"SUMMARIZECOLUMNS"`` /
    ``"SUMMARIZE"`` for the supported forms, or any other name for the
    unsupported branch) only when the envelope fully consumes the input.
    """
    m = _DAX_ENVELOPE_HEAD.match(text)
    if m is None:
        return None
    func_name = m.group(1)
    # Walk from the opening paren (the char before m.end()) to its balanced close.
    # DAX string literals are double-quoted with ``""`` as the escaped quote; skip
    # string spans so a parenthesis INSIDE a measure alias (``"Revenue (USD)"``)
    # is treated as literal text and never moves the paren depth. This matches the
    # string handling the downstream ``_split_top_level`` argument splitter relies
    # on for BALANCED in-string parens (the common case). A rare UNBALANCED paren
    # inside a string alias would still be rejected further down the parse; that
    # fails SAFE (loud rejection, never a silent narrow), so the envelope keeps its
    # single string-aware pass here for the balanced case rather than duplicating a
    # full DAX tokenizer.
    open_idx = m.end() - 1
    depth = 0
    i = open_idx
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == '"':
                # ``""`` is an escaped quote inside the string, not a terminator.
                if i + 1 < n and text[i + 1] == '"':
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # Everything AFTER this close paren must be whitespace only.
                if text[i + 1:].strip():
                    return None
                return func_name.upper()
        i += 1
    # Unbalanced parentheses (outside strings) — the structural parser would fail
    # anyway; treat as a non-consuming envelope so the caller rejects loudly.
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
    measures: list[str],
    dimensions: list[str],
    grain: list[str],
    filters: list[LogicalFilter],
) -> str:
    """Query-shape fingerprint for a DAX query.

    Bug-6086: delegates to the shared ``fingerprint_shape`` authority
    (``shared/pocket/fingerprint.py``) — the SAME scheme the SQL parser
    uses (``sql_parser._compute_fingerprint``) — so a DAX query and an
    equivalent SQL query over the same measures/dimensions/grain/filters
    hash identically. This keeps cross-protocol QueryLog dedup and
    miss-log grouping correct. DAX has no HAVING clause, so ``having_cols``
    is empty.
    """
    return fingerprint_shape(
        measures=measures,
        dimensions=dimensions,
        grain=grain,
        filter_cols=[f.dimension_name for f in filters],
        having_cols=[],
    )
