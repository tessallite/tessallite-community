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

from src.rewrite.dialects import render_expression_for_dialect


def _normalize_uda_expression_quoting(expression: str) -> str:
    """Normalise identifier quoting in a stored UDA expression to PostgreSQL style.

    UDA expressions stored in the DB may use backtick-quoted identifiers
    (BigQuery / MySQL style: ``EXTRACT(YEAR FROM `full_date`)``).  sqlglot's
    PostgreSQL parser rejects backtick quoting, raising "Invalid user-defined
    expression: Expecting )."

    Bug-5599: the substitution must only target backtick-quoted IDENTIFIERS,
    not backtick characters that happen to appear inside single-quoted string
    literals.  The expression is split on SQL string literals (single-quoted,
    handling '' escapes) so backtick normalisation is applied only to the
    non-literal segments, preserving string content verbatim.

    Architectural note (Bug-909): this regex pre-processor is intentional and
    correct.  SQLGlot cannot parse backtick-quoted identifiers in ``read="postgres"``
    mode, so the quoting must be normalised *before* parsing.  Full migration
    to SQLGlot AST construction (which would eliminate this step) is deferred
    to a future hardening pass.
    """
    # Split on single-quoted SQL string literals (handling '' escape sequences).
    # Odd-indexed segments are string literals (including their quotes);
    # even-indexed segments are SQL code where backtick normalisation applies.
    # Note: PostgreSQL dollar-quoted ($$...$$) and E-escape (E'...') strings
    # are not handled -- UDA expressions are simple SQL column expressions
    # (EXTRACT, CASE, COALESCE) sourced from the model builder or imports,
    # never PL/pgSQL function bodies or PostgreSQL-specific escape forms.
    parts = re.split(r"('(?:''|[^'])*')", expression)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"`([^`]+)`", r'"\1"', parts[i])
    return "".join(parts)


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
        # F-006-02: emit through the single dialect render boundary so week
        # semantics and fail-loud dialect guards apply to UDA expressions too
        # (a UDA can carry EXTRACT(WEEK ...) / DATE_TRUNC('week', ...)). The
        # expression is parsed from a PG-canonical normalised form above, so
        # pg_canonical stays True. A direct ``.sql(dialect=target_dialect)``
        # here bypassed those guards (the F-006-02 defect).
        return render_expression_for_dialect(qualified, target_dialect)
    except Exception as exc:
        raise ValueError(
            f"Failed to translate user-defined expression to {target_dialect}: {exc}"
        ) from exc

