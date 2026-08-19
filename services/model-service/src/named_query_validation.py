"""Create/update-time validation helpers for Named Query definitions.

Pure, unit-testable logic shared by the model-service authoring API
(``api/named_queries.py``): the definition is MODEL-BOUND semantic SQL over the
model's logical surface (model slug + measures + dimensions), never raw dialect
SQL. Dialect is compiled at refresh/execute time by the query-router via
sqlglot (SQL rule 1).

Responsibilities (spec §5.1):
1. Read-only / no-DML defence-in-depth keyword scan (the Named List
   ``sql_query`` DML scan — the router's read-only path is the primary guard).
2. Reject ``@`` references inside definitions (a Named Query reference inside
   a definition would recurse at query time; the definition must be plain
   model SQL).
3. Derive ``output_columns`` + ``shape`` from the parsed definition AST plus
   the model's dimension/measure name→type maps:
   ``shape == 'aggregated'`` when the query has GROUP BY or an aggregate
   function in the projection/HAVING, else ``projection``.
   Output types are best-effort (the refresh-time row manifest is the
   authoritative record of physical column types).
4. Caps: column width checked against the effective column cap (reject,
   never truncate).
"""
from __future__ import annotations

import re
from typing import Any, Optional

import sqlglot
from sqlglot import exp

NAMED_QUERY_SHAPES = ("projection", "aggregated")
NAMED_QUERY_OUTPUT_TYPES = ("string", "number", "boolean", "date", "timestamp")

# DML / DDL keywords that must never appear in a Named Query definition
# (defence-in-depth; the query-router read-only execution path is the primary
# guard). Same list as the Named List free-hand SQL scan.
_DML_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "MERGE", "UPSERT", "COPY", "CALL",
)

_DML_RE = re.compile(
    r"\b(" + "|".join(_DML_KEYWORDS) + r")\b", re.IGNORECASE
)

# SELECT-clause positions in which an aggregate makes the shape 'aggregated'.
_AGG_FUNC_NAMES = {
    "SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT", "COUNTDISTINCT",
    "APPROX_COUNT_DISTINCT", "MEDIAN", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
    "VARIANCE", "VAR_POP", "VAR_SAMP", "ANY_VALUE", "ARRAY_AGG",
    "STRING_AGG", "PERCENTILE_CONT", "PERCENTILE_DISC",
}

# Source data_type -> named-query output type domain (best-effort mapping).
_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "text": "string",
    "varchar": "string",
    "char": "string",
    "uuid": "string",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "datetime": "timestamp",
    "time": "timestamp",
    "int": "number",
    "integer": "number",
    "bigint": "number",
    "smallint": "number",
    "float": "number",
    "double": "number",
    "decimal": "number",
    "numeric": "number",
    "number": "number",
    "real": "number",
    "money": "number",
    "currency": "number",
    "percent": "number",
}

# Measure data_type values that map to 'number' output columns (measures are
# numeric on this platform; anything else maps best-effort).
_NUMERIC_MEASURE_TYPES = {
    "numeric", "number", "integer", "int", "bigint", "float", "double",
    "decimal", "currency", "percent", "money", "real",
}


class NamedQueryValidationError(ValueError):
    """A definition-level violation, surfaced as a 400 to the caller."""


def _strip_quotes(name: str) -> str:
    """Strip SQL identifier quoting from a resolved select name."""
    n = (name or "").strip()
    if len(n) >= 2 and n[0] in ('"', "`", "[") and n[-1] in ('"', "`", "]"):
        return n[1:-1]
    return n


def scan_dml_keywords(sql: str) -> Optional[str]:
    """Return the offending keyword when the definition contains DML/DDL.

    Strips string literals and comments first so a keyword inside a literal or
    comment does not trigger a false positive.
    """
    _noliterals = re.sub(r"'(?:''|[^'])*'", "''", sql or "")
    _nocomments = re.sub(r"--[^\n]*", "", _noliterals)
    _nocomments = re.sub(r"/\*.*?\*/", "", _nocomments, flags=re.DOTALL)
    m = _DML_RE.search(_nocomments)
    return m.group(1).upper() if m else None


def contains_parameter_reference(sql: str) -> bool:
    """True when the definition references an ``@name`` placeholder.

    Named Query definitions are plain model SQL; an ``@`` reference inside a
    definition would either fail to bind or recurse at query time.
    """
    # A bare `@` outside a string literal/comment. Token-level check via the
    # sqlglot lexer keeps `@` inside literals/identifiers untouched.
    try:
        tokens = sqlglot.Dialect.get_or_raise("postgres").tokenize(sql or "")
    except Exception:  # pragma: no cover - lexer robustness
        return "@" in (sql or "")
    for tok in tokens:
        if tok.token_type.name == "PARAMETER":
            return True
    return False


def parse_definition(sql: str) -> exp.Expression:
    """Parse the definition; raises NamedQueryValidationError on any failure."""
    stripped = (sql or "").strip()
    if not stripped:
        raise NamedQueryValidationError("definition_sql must not be empty")
    # Reject multiple statements (a trailing semicolon is tolerated).
    _noliterals = re.sub(r"'(?:''|[^'])*'", "''", stripped)
    _nocomments = re.sub(r"--[^\n]*", "", _noliterals)
    _nocomments = re.sub(r"/\*.*?\*/", "", _nocomments, flags=re.DOTALL)
    inner = _nocomments.strip().rstrip(";").strip()
    if ";" in inner:
        raise NamedQueryValidationError(
            "definition_sql must be a single statement"
        )
    try:
        ast = sqlglot.parse_one(stripped)
    except Exception as exc:  # sqlglot.ParseError
        raise NamedQueryValidationError(
            f"definition_sql could not be parsed: {exc}"
        ) from exc
    if ast is None or not isinstance(ast, exp.Select):
        raise NamedQueryValidationError(
            "definition_sql must be a single SELECT statement"
        )
    return ast


def derive_shape(ast: exp.Select) -> str:
    """'aggregated' when GROUP BY or an aggregate in projection/HAVING."""
    if ast.args.get("group"):
        return "aggregated"
    # Only the TOP-LEVEL projection and HAVING decide the shape — an
    # aggregate inside a subquery does not make the outer result aggregated.
    roots = list(ast.expressions)
    having = ast.args.get("having")
    if having is not None:
        roots.append(having)
    for root in roots:
        for node in root.walk():
            if isinstance(node, exp.AggFunc):
                return "aggregated"
            # User-defined aggregate-ish functions render as Anonymous funcs.
            if isinstance(node, (exp.Anonymous, exp.Func)):
                name = (node.name or "").upper()
                if name in _AGG_FUNC_NAMES:
                    return "aggregated"
    return "projection"


def _output_name(expr_node: exp.Expression) -> str:
    alias = getattr(expr_node, "alias", None)
    if alias:
        return _strip_quotes(alias)
    # Column reference: its name; anything else renders as its SQL text.
    if isinstance(expr_node, exp.Column):
        return _strip_quotes(expr_node.name)
    sql_text = expr_node.sql(dialect="postgres")
    return _strip_quotes(sql_text)


def _is_aggregate_expr(expr_node: exp.Expression) -> bool:
    # Unwrap an alias (``SUM(x) AS y`` parses as Alias(Sum(...))).
    if isinstance(expr_node, exp.Alias):
        return _is_aggregate_expr(expr_node.this)
    if isinstance(expr_node, exp.AggFunc):
        return True
    if isinstance(expr_node, (exp.Anonymous, exp.Func)):
        return (expr_node.name or "").upper() in _AGG_FUNC_NAMES
    return False


def _output_type_for(
    name: str,
    *,
    dimension_types: dict[str, str],
    measure_names: set[str],
    is_aggregate: bool,
    is_star: bool,
) -> str:
    """Best-effort output type for one select item."""
    key = name.lower()
    if is_star:
        # A SELECT * output column list is unrolled by the refresh manifest;
        # a placeholder string type is recorded at authoring time.
        return "string"
    if is_aggregate:
        return "number"
    if key in measure_names:
        return "number"
    if key in dimension_types:
        return dimension_types[key]
    # Unresolvable expression — best-effort "string"; the refresh-time
    # manifest is the authoritative record of physical types.
    return "string"


def derive_output_columns(
    ast: exp.Select,
    *,
    dimension_types: dict[str, str],
    measure_names: set[str],
) -> list[dict[str, str]]:
    """Derive ``[{name, type}]`` from the parsed select list.

    ``dimension_types`` is ``{lowercase_dimension_name: nq_type}``;
    ``measure_names`` is ``{lowercase_measure_name}``. Star projections get a
    single ``*`` placeholder column (the refresh manifest unrolls the real
    list from the target catalogue).
    """
    out: list[dict[str, str]] = []
    for sel in ast.expressions:
        if isinstance(sel, exp.Star):
            out.append({"name": "*", "type": "string"})
            continue
        name = _output_name(sel)
        out.append({
            "name": name,
            "type": _output_type_for(
                name,
                dimension_types=dimension_types,
                measure_names=measure_names,
                is_aggregate=_is_aggregate_expr(sel),
                is_star=False,
            ),
        })
    return out


def derive_definition_metadata(
    definition_sql: str,
    *,
    dimension_types: dict[str, str],
    measure_names: set[str],
) -> dict[str, Any]:
    """Full create-time derivation: parse + scan + shape + output columns.

    Raises ``NamedQueryValidationError`` on any violation. Returns
    ``{"shape": ..., "output_columns": [...]}``.
    """
    dml = scan_dml_keywords(definition_sql)
    if dml:
        raise NamedQueryValidationError(
            f"definition_sql contains the forbidden keyword '{dml}'. "
            f"Named Query definitions are read-only semantic queries."
        )
    if contains_parameter_reference(definition_sql):
        raise NamedQueryValidationError(
            "definition_sql must not reference @ placeholders — a Named Query "
            "definition is plain model SQL and cannot reference other named "
            "objects."
        )
    ast = parse_definition(definition_sql)
    shape = derive_shape(ast)
    output_columns = derive_output_columns(
        ast,
        dimension_types=dimension_types,
        measure_names=measure_names,
    )
    return {"shape": shape, "output_columns": output_columns}


def check_column_cap(
    output_columns: list[dict[str, str]],
    *,
    effective_column_cap: int,
) -> None:
    """Reject (never truncate) when the projection exceeds the column cap.

    A star projection has unknown width at authoring time; it is checked
    against the manifest at refresh time instead.
    """
    names = [c.get("name", "") for c in output_columns]
    if "*" in names:
        return
    if len(names) > effective_column_cap:
        raise NamedQueryValidationError(
            f"Named Query projects {len(names)} columns, exceeding the "
            f"named_query.max_columns cap of {effective_column_cap}. "
            f"Narrow the projection or raise the cap."
        )
