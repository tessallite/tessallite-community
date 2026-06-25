"""WHERE / condition / value rendering for the query rewriter.

Pure helpers that render ``LogicalFilter`` predicates and literal values into
PostgreSQL-canonical SQL fragments, plus identifier quoting helpers and the
column-type constants shared with the join builder.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import math
import re
from typing import Any

from shared.connector_qualify import quote_identifier
from src.ir.logical_query import LogicalFilter, SemanticBindingError

# Column-type sets used by value coercion (and by joins.py for date/timestamp
# join-key alignment). Kept here as the canonical home; joins.py imports them.
_TIMESTAMP_TYPES = {"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP_TZ", "DATETIME"}
_DATE_TYPES = {"DATE"}


def _render_where(
    filters: list[LogicalFilter],
    field_expr_by_name: dict[str, str] | None = None,
    connector: str = "postgresql",
    col_type_by_name: dict[str, str] | None = None,
) -> str:
    parts: list[str] = []
    for f in filters:
        fallback = quote_identifier(connector, f.dimension_name)
        col = field_expr_by_name.get(f.dimension_name, fallback) if field_expr_by_name else fallback
        col_type = col_type_by_name.get(f.dimension_name) if col_type_by_name else None
        parts.append(_render_condition(col, f.operator, f.value, col_type))
    return " AND ".join(parts)


_CAST_TYPE_RE = re.compile(
    r"\bCAST\s*\(.*?\bAS\s+(\w+)\s*\)", re.IGNORECASE
)


def _coerce_value(col_expr: str, rendered: str, col_type: str | None = None) -> str:
    """Wrap a rendered literal with CAST(<lit> AS <type>) when the column
    expression itself contains a CAST to a narrower type (e.g. DATE).
    Prevents BigQuery "no matching signature" errors when a TIMESTAMP
    literal is compared to a DATE expression.

    Skips coercion when the column's declared output type is numeric
    (e.g. INTEGER from ``EXTRACT(YEAR FROM CAST(ts AS DATE))``).
    """
    if is_numeric_col_type(col_type):
        return rendered
    m = _CAST_TYPE_RE.search(col_expr)
    if not m:
        return rendered
    target_type = m.group(1).upper()
    if target_type in ("DATE", "TIME", "DATETIME"):
        try:
            float(rendered)
            return rendered
        except ValueError:
            pass
        return f"CAST({rendered} AS {target_type})"
    return rendered


def _render_condition(col: str, operator: str, value: Any, col_type: str | None = None) -> str:
    def _rv(v: Any) -> str:
        return _coerce_value(col, _render_value(v, col_type), col_type)

    if operator == "eq":
        return f"{col} = {_rv(value)}"
    elif operator == "neq":
        return f"{col} != {_rv(value)}"
    elif operator == "gt":
        return f"{col} > {_rv(value)}"
    elif operator == "gte":
        return f"{col} >= {_rv(value)}"
    elif operator == "lt":
        return f"{col} < {_rv(value)}"
    elif operator == "lte":
        return f"{col} <= {_rv(value)}"
    elif operator == "in":
        items = list(value or [])
        if not items:
            return "1=0"  # empty IN list is always false
        vals = ", ".join(_rv(v) for v in items)
        return f"{col} IN ({vals})"
    elif operator == "not_in":
        items = list(value or [])
        if not items:
            return "1=1"  # empty NOT IN list is always true
        vals = ", ".join(_rv(v) for v in items)
        return f"{col} NOT IN ({vals})"
    elif operator == "between":
        # Bug-918: only a 2-element bound is valid. An empty value keeps the
        # pre-existing degenerate (NULL BETWEEN NULL); any other arity is a
        # producer error and must fail loudly rather than crash on unpacking
        # or silently render a wrong predicate.
        if not value:
            low, high = None, None
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            low, high = value
        else:
            raise ValueError(
                f"BETWEEN filter on {col} requires exactly two bounds, "
                f"got {value!r}"
            )
        return f"{col} BETWEEN {_rv(low)} AND {_rv(high)}"
    elif operator == "like":
        return f"{col} LIKE {_render_value(value, col_type)}"
    elif operator == "not_like":
        # Bug-3609: "Not Contains" support. Mirrors the `like` rendering with
        # the same value escaping (_render_value `''`-escapes single quotes).
        return f"{col} NOT LIKE {_render_value(value, col_type)}"
    elif operator == "is_null":
        return f"{col} IS NULL"
    elif operator == "is_not_null":
        return f"{col} IS NOT NULL"
    else:
        # F-006-13 (Bug-2741): an unrecognised operator must fail loud, not
        # silently degrade to equality. Bug-628 (not_in rendered as `=`) showed
        # this exact branch turning a producer gap into silently wrong data.
        # Producers already validate operators (e.g. the plugin endpoint 422s
        # unsupported ones); this is the defence-in-depth backstop.
        raise ValueError(
            f"Unsupported filter operator {operator!r} on {col}; "
            f"refusing to render — an unknown operator must not become equality."
        )


_NUMERIC_TYPES = {
    "INT64", "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT",
    "FLOAT", "FLOAT64", "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
    "NUMBER",
}


_TEXT_TYPES = frozenset({
    "TEXT", "VARCHAR", "CHAR", "CHARACTER VARYING", "STRING",
})


def _render_value(value: Any, col_type: str | None = None) -> str:
    if value is None:
        return "NULL"
    from src.parsing.sql_parser import RawSQL
    # Bug-5462 / Bug-5538 (Codex round-2 finding 1): the numeric-column guard runs
    # BEFORE the RawSQL short-circuit so it is UNBYPASSABLE. A ``LogicalFilter``
    # value wrapped as ``RawSQL`` must clear the SAME strict
    # ``value_is_numeric_literal`` validator as any other value before it may emit
    # a bare token against an INT/NUMERIC column — otherwise an arbitrary raw
    # token (``1e9``, ``+1``, ``1 OR 1=1``) would slip past the gate. For every
    # value type the value has to be a finite numeric literal to render unquoted;
    # anything else (``"abc"``/``""``/``"1_000"``/``inf``/``nan``/``True``/a
    # non-numeric ``RawSQL``) fails loud instead of silently emitting ``'abc'``
    # (string vs INT64) or a bare token (invalid SQL / injection surface).
    if is_numeric_col_type(col_type):
        candidate = str(value) if isinstance(value, RawSQL) else value
        if value_is_numeric_literal(candidate):
            # Bug-5539 (review finding 1): emit the literal's PRESERVED original
            # spelling when present. ``str(float)`` switches to scientific form
            # for very large/small magnitudes (``0.0000001`` -> ``1e-07``,
            # ``1e19`` -> ``1e+19``), which would launder a grammar-conformant
            # plain decimal into a bare scientific token against a numeric column
            # — the exact precision gap this fix closes. ``original_text`` is the
            # token the strict grammar just validated, so it is guaranteed
            # non-scientific and safe to emit bare. Falls back to ``str`` for
            # plain int/float/str values that carry no preserved spelling.
            original = getattr(candidate, "original_text", None)
            if isinstance(original, str):
                return original
            return str(candidate)
        raise SemanticBindingError(
            f"Non-numeric value {value!r} for numeric column "
            f"(type {col_type!r}); refusing to render a string literal or bare "
            f"token for a numeric comparison"
        )
    # RawSQL marker: emit as-is (SQL expression like CAST('2024-01-01' AS DATE)).
    # Reached only for non-numeric columns — the numeric gate above already
    # validated/short-circuited every RawSQL bound for a numeric column.
    if isinstance(value, RawSQL):
        return str(value)
    normalized = (col_type or "").upper().split("(")[0].strip()
    if not isinstance(value, str) and normalized in _TEXT_TYPES:
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, str):
        if normalized in _TIMESTAMP_TYPES:
            escaped = value.replace("'", "''")
            # Architectural note (Bug-906): ``TIMESTAMP 'xxx'`` is PostgreSQL
            # canonical syntax.  These predicate fragments are assembled into
            # a full SQL query that is later transpiled via SQLGlot in
            # ``_build_source_sql``, which converts the literal to the
            # appropriate target-dialect form (e.g. BigQuery TIMESTAMP(...),
            # SQL Server CAST(... AS DATETIME)).  Migrating to AST-level
            # literal construction is deferred to a future hardening pass.
            return f"TIMESTAMP '{escaped}'"
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)



def is_numeric_col_type(col_type: str | None) -> bool:
    """True when ``col_type`` names a numeric source type (INT64, INTEGER,
    DECIMAL, FLOAT, …). Normalises case and strips any ``(precision)`` suffix
    so e.g. ``NUMERIC(10,2)`` is recognised. Shared by every WHERE renderer so
    the numeric-type decision lives in exactly one place."""
    normalized = (col_type or "").upper().split("(")[0].strip()
    return normalized in _NUMERIC_TYPES


# A finite, *safe* integer/decimal literal: an optional leading MINUS sign, ASCII
# digits, and an optional single ``.`` fraction. Deliberately tight (Bug-5538,
# Codex round-2 finding 3):
#   - NO exponent (``1e9`` is rejected — a slicer member key is never written in
#     scientific form, and a bare ``1e9`` is an injection/precision surface).
#   - NO leading ``+`` (``+1`` is rejected — a member key never carries a unary
#     plus; accepting it widens the bare-token grammar for no real input).
#   - NO surrounding whitespace (``' 1 '`` is rejected — matched WITHOUT a strip,
#     so a padded token can never reach ``exp.Literal.number`` as a bare token).
#     The tail is anchored with ``\Z`` (not ``$``): ``$`` also matches just before
#     a single trailing newline, so ``$`` would let ``"12\n"`` slip through as a
#     bare ``= 12\n`` token. ``\Z`` matches only the true end of string.
# Still rejects ``inf``/``nan`` and Python's underscore grouping (``1_000``),
# both of which ``float()`` accepts but which are NOT safe bare SQL. ``re.ASCII``
# keeps ``\d`` to 0-9 so non-ASCII decimal digits (e.g. Arabic-Indic ``١٩٩٩``)
# fail loud rather than emitting an unparseable bare token to the source DB.
_NUMERIC_LITERAL_RE = re.compile(r"^-?(\d+(\.\d+)?|\.\d+)\Z", re.ASCII)


def value_is_numeric_literal(value: Any) -> bool:
    """True when ``value`` is a finite numeric literal safe to emit bare.

    Accepts a real int/float (finite only) or a string that matches a plain
    integer/decimal form (optional leading ``-``, digits, optional single ``.``
    fraction). Rejects scientific notation (``1e9``), a leading ``+`` (``+1``),
    surrounding whitespace (``' 1 '``), ``inf``/``nan``/``1_000`` and anything
    else ``float()`` would over-accept — those must never reach
    ``exp.Literal.number`` as a bare token. The match is performed WITHOUT
    trimming, so a padded token can never slip through."""
    if isinstance(value, bool):
        return False
    # Bug-5539 (Codex round-3 finding 3): a numeric literal extracted from raw
    # SQL carries its ORIGINAL spelling on ``.original_text`` (a NumericLiteral
    # from the parser). Validate that original token through the SAME strict
    # grammar rather than the lenient "any finite float" check below — otherwise
    # a scientific/leading-plus source token (``1e9`` -> ``float`` ``1e9``) would
    # launder into a bare ``1000000000.0`` token against a numeric column.
    original = getattr(value, "original_text", None)
    if isinstance(original, str):
        return bool(_NUMERIC_LITERAL_RE.match(original))
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        return bool(_NUMERIC_LITERAL_RE.match(value))
    return False


def _quote(name: str) -> str:
    """Double-quote an identifier."""
    return f'"{name}"'


def _quote_compound(name: str) -> str:
    return ".".join(_quote(part) for part in name.split("."))


def _qualified_column(table_alias: str, column_name: str) -> str:
    return f'{_quote(table_alias)}.{_quote(column_name)}'

