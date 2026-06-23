"""User-defined-attribute (UDA) expression rendering for the query rewriter.

Pure helpers: normalise stored UDA expression quoting to PostgreSQL style and
render a UDA expression qualified to a table alias and transpiled to the target
dialect.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


def _normalize_uda_expression_quoting(expression: str) -> str:
    """Normalise identifier quoting in a stored UDA expression to PostgreSQL style.

    UDA expressions stored in the DB may use backtick-quoted identifiers
    (BigQuery / MySQL style: ``EXTRACT(YEAR FROM `full_date`)``).  sqlglot's
    PostgreSQL parser rejects backtick quoting, raising "Invalid user-defined
    expression: Expecting )."

    Backtick-quoted SQL identifiers are never string literals, so a regex
    substitution is safe:  `name` → "name".

    Architectural note (Bug-909): this regex pre-processor is intentional and
    correct.  SQLGlot cannot parse backtick-quoted identifiers in ``read="postgres"``
    mode, so the quoting must be normalised *before* parsing.  The regex is
    applied only to identifier tokens (backticks never appear inside
    single-quoted string literals in standard SQL), making it safe to apply
    unconditionally.  Full migration to SQLGlot AST construction (which would
    eliminate this step) is deferred to a future hardening pass.
    """
    return re.sub(r"`([^`]+)`", r'"\1"', expression)


def _render_uda_expression(
    *,
    expression: str,
    table_alias: str,
    target_dialect: str,
) -> str:
    """
    Parse canonical PostgreSQL expression, qualify columns with the table alias,
    then transpile to target dialect.

    Stored expressions may use backtick quoting (BigQuery/MySQL style).
    ``_normalize_uda_expression_quoting`` converts those to double-quotes
    before parsing so the parser always sees valid PostgreSQL syntax.
    """
    normalised = _normalize_uda_expression_quoting(expression)
    try:
        tree = sqlglot.parse_one(normalised, read="postgres")
    except Exception as exc:
        raise ValueError(f"Invalid user-defined expression: {exc}") from exc

    def _qualify(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            return exp.column(node.name, table=table_alias, quoted=True)
        return node

    try:
        qualified = tree.transform(_qualify)
        return qualified.sql(dialect=target_dialect)
    except Exception as exc:
        raise ValueError(
            f"Failed to translate user-defined expression to {target_dialect}: {exc}"
        ) from exc

