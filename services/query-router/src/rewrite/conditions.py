"""WHERE / condition / value rendering for the query rewriter.

Pure helpers that render ``LogicalFilter`` predicates and literal values into
PostgreSQL-canonical SQL fragments, plus identifier quoting helpers and the
column-type constants shared with the join builder.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import re
from typing import Any

from shared.connector_qualify import quote_identifier
from src.ir.logical_query import LogicalFilter

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
    normalized_col_type = (col_type or "").upper().split("(")[0].strip()
    if normalized_col_type in _NUMERIC_TYPES:
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
    # RawSQL marker: emit as-is (SQL expression like CAST('2024-01-01' AS DATE)).
    from src.parsing.sql_parser import RawSQL
    if isinstance(value, RawSQL):
        return str(value)
    normalized = (col_type or "").upper().split("(")[0].strip()
    if not isinstance(value, str) and normalized in _TEXT_TYPES:
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, str):
        if normalized in _NUMERIC_TYPES:
            try:
                float(value)
                return value
            except ValueError:
                pass
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



def _quote(name: str) -> str:
    """Double-quote an identifier."""
    return f'"{name}"'


def _quote_compound(name: str) -> str:
    return ".".join(_quote(part) for part in name.split("."))


def _qualified_column(table_alias: str, column_name: str) -> str:
    return f'{_quote(table_alias)}.{_quote(column_name)}'

