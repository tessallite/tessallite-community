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

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres

from shared.connector_qualify import (
    _DATE_TYPES,  # noqa: F401 -- re-exported for back-compat (see comment below)
    _TIMESTAMP_TYPES,
    _TZ_AWARE_TIMESTAMP_TYPES,
    canonical_timestamp_type,
    normalize_type_token,
    quote_identifier,
    safe_ident,
)
from src.ir.logical_query import LogicalFilter, SemanticBindingError


class _CanonicalFragmentPostgres(Postgres):
    """PostgreSQL canonical spelling for legacy-stable predicate fragments.

    SQLGlot's default PostgreSQL generator normalises ``!=`` to ``<>`` and
    prefix-renders ``NOT``. Both are valid, but this boundary has an established
    string contract consumed by golden tests and downstream diagnostics. The
    AST remains authoritative; this generator preserves only those spellings.
    """

    class Generator(Postgres.Generator):
        def neq_sql(self, expression: exp.NEQ) -> str:
            return self.binary(expression, "!=")

        def filter_sql(self, expression: exp.Filter) -> str:
            # Bug-5173 regression guard: the legacy semi-additive producer
            # contract spells the clause ``FILTER (WHERE ...)`` (space after
            # FILTER). SQLGlot's default postgres generator renders
            # ``FILTER(WHERE ...)``; restoring the canonical spelling keeps
            # the established source-route string contract (pinned by
            # test_dax_time_variants) byte-identical to the pre-AST producer.
            agg = self.sql(expression, "this")
            where = self.sql(expression, "expression").strip()
            return f"{agg} FILTER ({where})"

        def eq_sql(self, expression: exp.EQ) -> str:
            if expression.meta.get("compact"):
                return f"{self.sql(expression, 'this')}={self.sql(expression, 'expression')}"
            return super().eq_sql(expression)

        def not_sql(self, expression: exp.Not) -> str:
            inner = expression.this
            if isinstance(inner, exp.Paren):
                inner = inner.this
            rendered = self.sql(inner)
            candidate = inner.this if isinstance(inner, exp.Escape) else inner
            for node_type, token in ((exp.In, " IN "), (exp.Like, " LIKE ")):
                if isinstance(candidate, node_type) and token in rendered:
                    return rendered.replace(token, f" NOT{token}", 1)
            if isinstance(candidate, exp.Is) and " IS " in rendered:
                return rendered.replace(" IS ", " IS NOT ", 1)
            return super().not_sql(expression)

# Column-type families (DATE / TIMESTAMP) live in shared/connector_qualify.py,
# the single canonical home shared with source_sql.py, and are imported above
# so any existing ``from src.rewrite.conditions import _DATE_TYPES`` call site
# keeps resolving to the same frozensets (no local duplicate to drift).


def _render_where(
    filters: list[LogicalFilter],
    field_expr_by_name: dict[str, str] | None = None,
    connector: str = "postgresql",
    col_type_by_name: dict[str, str] | None = None,
    like_target_connector: str | None = None,
    source_connector: str | None = None,
) -> str:
    # ``source_connector`` is the connector the COLUMN TYPES in
    # ``col_type_by_name`` were introspected from — NOT the ``connector``
    # argument above, which is always ``"postgresql"`` because this renderer
    # emits PostgreSQL-canonical SQL for sqlglot to transpile. See
    # ``_render_condition`` for why the distinction matters (Bug-7918).
    #
    # ``like_target_connector`` is retained for call-site compatibility but is
    # NO LONGER used for T-SQL bracket escaping (F-006-02): that connector-
    # specific LIKE-pattern mutation moved to the T-SQL generator boundary
    # (``dialects._tsql_escape_sql``), so this renderer stays connector-agnostic
    # and emits PostgreSQL-canonical ``LIKE ... ESCAPE`` for every target.
    parts: list[str] = []
    for f in filters:
        fallback = quote_identifier(connector, f.dimension_name)
        col = field_expr_by_name.get(f.dimension_name, fallback) if field_expr_by_name else fallback
        col_type = col_type_by_name.get(f.dimension_name) if col_type_by_name else None
        parts.append(
            _render_condition(
                col, f.operator, f.value, col_type,
                like_escape=getattr(f, "like_escape", None),
                connector=connector,
                source_connector=source_connector,
            )
        )
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
        # Bug-7026: if _render_value already emitted a typed literal
        # whose type MATCHES the cast target, do not double-wrap with
        # CAST.  Only skip when the types agree (e.g. DATE literal with
        # DATE cast); a TIMESTAMP literal against a DATE cast still needs
        # the CAST to narrow the type correctly.
        if target_type == "DATE" and rendered.startswith("DATE "):
            return rendered
        if target_type in ("DATETIME", "TIME") and rendered.startswith("TIMESTAMP "):
            return rendered
        return f"CAST({rendered} AS {target_type})"
    return rendered


def _render_condition(
    col: str,
    operator: str,
    value: Any,
    col_type: str | None = None,
    like_escape: str | None = None,
    connector: str = "postgresql",
    like_target_connector: str | None = None,
    source_connector: str | None = None,
) -> str:
    # ``like_target_connector`` is accepted for call-site compatibility but is
    # unused: T-SQL LIKE bracket escaping now lives at the dialect generator
    # boundary (``dialects._tsql_escape_sql``), F-006-02.
    #
    # Bug-7918: ``col_type`` arrives as the SOURCE's own spelling (BigQuery
    # introspection persists ``field_type`` verbatim, so a tz-aware BigQuery
    # column reads ``"timestamp"``), while this renderer emits PostgreSQL-
    # canonical SQL. The token ``TIMESTAMP`` means "no time zone" in
    # PostgreSQL and "absolute instant" in BigQuery/Spark, so the type has to
    # be re-spelled in PostgreSQL-canonical terms BEFORE it decides a literal.
    # ``canonical_timestamp_type`` does that through sqlglot's own per-dialect
    # type parser — no per-connector branch here or in the helper (SQL rule 1)
    # — and returns non-timestamp types untouched. It is applied ONCE per
    # condition rather than inside ``_render_value``, so an ``IN`` list of a
    # thousand members pays for one resolution, not a thousand.
    #
    # NOTE the two connector arguments are NOT interchangeable: ``connector``
    # is the canonical RENDERING dialect (always ``"postgresql"`` on every
    # production path) and drives identifier quoting; ``source_connector`` is
    # where ``col_type`` came from. Passing the target dialect as ``connector``
    # would corrupt the PG-canonical quoting sqlglot then re-quotes.
    col_type = canonical_timestamp_type(col_type, source_connector)

    # The column expression was already assembled and identifier-quoted by the
    # shared connector boundary. Keep it as an opaque canonical expression leaf
    # while SQLGlot owns the predicate structure around it; this also preserves
    # legacy backtick-authored expressions until the final full-statement parse.
    column = exp.Var(this=col)

    def _rv(v: Any) -> exp.Expression:
        rendered = _coerce_value(col, _render_value(v, col_type), col_type)
        return exp.Var(this=rendered)

    def _escaped(predicate: exp.Expression) -> exp.Expression:
        # Bug-6383 [SECURITY/correctness]: LIKE patterns produced by the
        # ``contains`` / ``notContains`` aliases backslash-escape wildcard
        # metacharacters (``%`` / ``_`` / ``\``) inside the user's search text.
        # Backslash is PostgreSQL's DEFAULT LIKE escape, so the escaping worked
        # only on Postgres; on BigQuery / Spark / SQL Server the backslash is a
        # literal, so ``100\%`` matched any string starting with ``100`` (wrong
        # rows). Emitting an explicit ``ESCAPE`` clause makes the escape char
        # portable — sqlglot transpiles the whole statement per dialect (SQL
        # rule 1). Raw ``like`` / ``not_like`` (no escape intent) is unchanged.
        #
        # Bug-7008: the previous ``if connector == "bigquery": return ""``
        # early return was a per-connector branch in the WHERE renderer that
        # violated SQL Rule 1 (dialect differences via sqlglot transpilation,
        # NOT per-connector if-branches). BigQuery ESCAPE handling is now
        # uniformly handled at the sqlglot generator boundary via
        # ``_bq_escape_sql`` in dialects.py, which drops the ``exp.Escape``
        # node for BigQuery during transpilation. Always emit the ESCAPE
        # clause in PG-canonical form; dialects.py handles per-target removal.
        if not like_escape:
            return predicate
        return exp.Escape(
            this=predicate,
            expression=exp.Literal.string(str(like_escape)),
        )

    if operator == "eq":
        predicate = exp.EQ(this=column, expression=_rv(value))
    elif operator == "neq":
        predicate = exp.NEQ(this=column, expression=_rv(value))
    elif operator == "gt":
        predicate = exp.GT(this=column, expression=_rv(value))
    elif operator == "gte":
        predicate = exp.GTE(this=column, expression=_rv(value))
    elif operator == "lt":
        predicate = exp.LT(this=column, expression=_rv(value))
    elif operator == "lte":
        predicate = exp.LTE(this=column, expression=_rv(value))
    elif operator == "in":
        items = list(value or [])
        if any(v is None for v in items):
            raise SemanticBindingError(
                f"IN filter on {col} cannot contain NULL members"
            )
        if not items:
            predicate = exp.EQ(
                this=exp.Literal.number(1), expression=exp.Literal.number(0),
            )
            predicate.meta["compact"] = True
        else:
            predicate = exp.In(this=column, expressions=[_rv(v) for v in items])
    elif operator == "not_in":
        items = list(value or [])
        if any(v is None for v in items):
            raise SemanticBindingError(
                f"NOT IN filter on {col} cannot contain NULL members"
            )
        if not items:
            predicate = exp.EQ(
                this=exp.Literal.number(1), expression=exp.Literal.number(1),
            )
            predicate.meta["compact"] = True
        else:
            predicate = exp.Not(
                this=exp.Paren(
                    this=exp.In(
                        this=column, expressions=[_rv(v) for v in items],
                    )
                )
            )
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
        predicate = exp.Between(
            this=column, low=_rv(low), high=_rv(high),
        )
    elif operator == "like":
        # Bug-6894 / F-006-02: T-SQL bracket metacharacter escaping is applied
        # at the dialect generator boundary (``dialects._tsql_escape_sql``),
        # NOT here — this renderer emits PostgreSQL-canonical ``LIKE ... ESCAPE``
        # and sqlglot transpiles it per target (SQL Rule 1).
        predicate = _escaped(
            exp.Like(
                this=column,
                expression=exp.Var(this=_render_value(value, col_type)),
            )
        )
    elif operator == "not_like":
        # Bug-3609: "Not Contains" support. Mirrors the `like` rendering with
        # the same value escaping (_render_value `''`-escapes single quotes).
        # T-SQL bracket escaping is at the dialect boundary (F-006-02).
        predicate = exp.Not(
            this=exp.Paren(
                this=_escaped(
                    exp.Like(
                        this=column,
                        expression=exp.Var(this=_render_value(value, col_type)),
                    )
                )
            )
        )
    elif operator == "is_null":
        predicate = exp.Is(this=column, expression=exp.Null())
    elif operator == "is_not_null":
        predicate = exp.Not(
            this=exp.Paren(
                this=exp.Is(this=column, expression=exp.Null()),
            )
        )
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

    # Bug-5173: the whole predicate is now a SQLGlot expression. PostgreSQL is
    # the canonical identity side; target syntax remains the final boundary's
    # responsibility.
    return _CanonicalFragmentPostgres().generate(predicate)


_INTEGER_TYPES = {
    "INT64", "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT",
}

_NUMERIC_TYPES = _INTEGER_TYPES | {
    "FLOAT", "FLOAT64", "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
    "NUMBER",
}


# Compared against ``normalize_type_token(col_type)``, which keeps only the
# leading token, so the verbose PostgreSQL spelling ``character varying`` (and
# fixed ``character``) both normalise to ``CHARACTER`` — the multi-word
# ``CHARACTER VARYING`` entry the old ``.split("(")`` normalisation matched is
# unreachable after first-word normalisation and is replaced by ``CHARACTER``.
_TEXT_TYPES = frozenset({
    "TEXT", "VARCHAR", "CHAR", "CHARACTER", "STRING",
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
            token = original if isinstance(original, str) else str(candidate)
            # Bug-5546: an INTEGER column must reject a fractional literal
            # (e.g. year = 19.99). value_is_numeric_literal accepts decimals so
            # FLOAT/NUMERIC columns keep working, but rendering "year = 19.99"
            # against an INT64 column makes BigQuery silently coerce and return
            # an empty result — a type mismatch that must fail loud, not pass
            # silently. Fractional literals stay valid for non-integer numeric
            # columns (FLOAT/NUMERIC/DECIMAL).
            if is_integer_col_type(col_type) and "." in token:
                raise SemanticBindingError(
                    f"Fractional value {value!r} for integer column "
                    f"(type {col_type!r}); refusing to render a non-integer "
                    f"literal against an integer comparison"
                )
            return token
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
    # Bug intake 2026-07-07 (variant-date-anchor): normalise through the shared
    # ``normalize_type_token`` so verbose source spellings — PostgreSQL's
    # ``timestamp without time zone`` / ``timestamp with time zone`` and
    # precision-carrying forms like ``TIMESTAMP(3)`` — reduce to their leading
    # token and match the DATE / TIMESTAMP families. The prior local
    # ``.split("(")`` normalisation kept the trailing ``WITHOUT TIME ZONE``
    # words, so those columns silently missed ``TIMESTAMP 'lit'`` rendering
    # (latent on PostgreSQL via implicit coercion; wrong on stricter dialects).
    normalized = normalize_type_token(col_type)
    if not isinstance(value, str) and normalized in _TEXT_TYPES:
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, str):
        if normalized in _TIMESTAMP_TYPES:
            escaped = value.replace("'", "''")
            # Bug-6618: distinguish tz-aware vs tz-unaware timestamp column
            # types so the PG-canonical literal transpiles correctly to the
            # target dialect.  PostgreSQL ``TIMESTAMP 'x'`` is tz-unaware;
            # sqlglot transpiles it to BigQuery ``CAST('x' AS DATETIME)``.
            # If the physical column is tz-aware (TIMESTAMPTZ, TIMESTAMP_TZ,
            # TIMESTAMP_LTZ, or DATETIMEOFFSET), BigQuery requires
            # ``CAST('x' AS TIMESTAMP)``; the PG-canonical form for that is
            # ``TIMESTAMPTZ 'x'``.  Without this, a BigQuery
            # TIMESTAMP column filtered by a timestamp value fails with
            # "no matching signature for >=" (DATETIME vs TIMESTAMP mismatch).
            if normalized in _TZ_AWARE_TIMESTAMP_TYPES:
                return f"TIMESTAMPTZ '{escaped}'"
            return f"TIMESTAMP '{escaped}'"
        if normalized in _DATE_TYPES:
            # Bug-7026: bare DATE columns were previously rendered as plain
            # string literals (``'2024-01-02'``).  BigQuery is strict about
            # DATE-vs-STRING comparisons, so ``order_date = '2024-01-02'``
            # fails with a type error.  Emit ``DATE 'YYYY-MM-DD'`` in
            # PostgreSQL-canonical form; sqlglot transpiles it to the
            # target-dialect form (e.g. BigQuery ``DATE('2024-01-02')``).
            escaped = value.replace("'", "''")
            return f"DATE '{escaped}'"
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)



def is_numeric_col_type(col_type: str | None) -> bool:
    """True when ``col_type`` names a numeric source type (INT64, INTEGER,
    DECIMAL, FLOAT, …). Uses the shared ``normalize_type_token`` so precision
    suffixes (``NUMERIC(10,2)``) and multi-word spellings (``double
    precision`` → ``DOUBLE``) both resolve. Shared by every WHERE renderer so
    the numeric-type decision lives in exactly one place."""
    return normalize_type_token(col_type) in _NUMERIC_TYPES


def is_integer_col_type(col_type: str | None) -> bool:
    """True when ``col_type`` names an INTEGER source type (INT64, INTEGER,
    BIGINT, …) — the subset of numeric types that must reject fractional
    literals. Uses the shared ``normalize_type_token`` for the same reasons as
    ``is_numeric_col_type``."""
    return normalize_type_token(col_type) in _INTEGER_TYPES


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
    """Double-quote a SQL identifier with proper escaping (Bug-7023).

    Uses ``safe_ident`` from ``shared.connector_qualify`` to escape embedded
    double-quotes, preventing identifier-context SQL injection.
    """
    return safe_ident(name)


def _quote_compound(name: str) -> str:
    return ".".join(_quote(part) for part in name.split("."))


def _qualified_column(table_alias: str, column_name: str) -> str:
    return f'{_quote(table_alias)}.{_quote(column_name)}'
