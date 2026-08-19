"""Unified object-name qualification and quoting for all connector types.

Every site that builds or quotes a table reference should use these
functions instead of hand-rolling connector-specific logic.

Connector types (canonical): ``postgresql``, ``bigquery``, ``hadoop_spark``,
``redshift``, ``snowflake``, ``sqlserver``.

Redshift uses the same double-quote identifier quoting as PostgreSQL.
SQL Server uses bracket quoting: ``[identifier]`` with ``]`` escaped as ``]]``.
"""
from __future__ import annotations

from functools import lru_cache

import sqlglot
from sqlglot import exp

CONNECTOR_TO_SQLGLOT: dict[str, str] = {
    "postgresql": "postgres",
    "redshift": "postgres",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}


def transpile_preview_sql(connector: str, canonical_sql: str) -> str:
    """Transpile canonical PostgreSQL SQL to the connector's dialect via sqlglot."""
    target = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    if target == "postgres":
        return canonical_sql
    results = sqlglot.transpile(canonical_sql, read="postgres", write=target)
    return results[0] if results else canonical_sql


def qualify_table_name(
    connector: str,
    table_name: str,
    *,
    schema: str | None = None,
    project_id: str | None = None,
) -> str:
    """Build a fully-qualified dotted table name.

    Parameters
    ----------
    connector:
        Canonical connector type (``postgresql``, ``bigquery``, ``hadoop_spark``,
        ``redshift``, ``snowflake``).
    table_name:
        Bare table name (e.g. ``orders``).
    schema:
        Schema (PG), dataset (BQ), or database (Spark).
        For BQ, may already contain ``project_id.dataset`` — in that case
        *project_id* is not prepended again.
    project_id:
        GCP project ID (BQ only). Ignored for other connectors.

    Returns
    -------
    str
        The qualified name with dot separators, **unquoted**.
    """
    if not table_name:
        return table_name

    if connector == "bigquery":
        if schema and "." in schema:
            return f"{schema}.{table_name}"
        if schema and project_id:
            return f"{project_id}.{schema}.{table_name}"
        if schema:
            return f"{schema}.{table_name}"
        return table_name

    if schema:
        return f"{schema}.{table_name}"
    return table_name


def extract_dataset(connector: str, schema: str) -> str:
    """Return the bare dataset/schema name, stripping any project prefix.

    For BigQuery, ``tessallite-io.demo_data`` → ``demo_data``.
    For all other connectors the value is returned unchanged.
    """
    if not schema:
        return schema
    if connector == "bigquery" and "." in schema:
        return schema.rsplit(".", 1)[-1]
    return schema


def _strip_existing_quotes(identifier: str) -> str:
    """Remove pre-existing dialect-specific quoting to avoid double-quoting."""
    if len(identifier) >= 2:
        if identifier.startswith('"') and identifier.endswith('"'):
            return identifier[1:-1].replace('""', '"')
        if identifier.startswith("`") and identifier.endswith("`"):
            return identifier[1:-1]
        if identifier.startswith("[") and identifier.endswith("]"):
            return identifier[1:-1].replace("]]", "]")
    return identifier


def _escape_bigquery_identifier(identifier: str) -> str:
    """Escape literal characters using GoogleSQL quoted-identifier rules."""
    escapes = {
        "\\": "\\\\",
        "`": "\\`",
        "\a": "\\a",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    return "".join(escapes.get(char, char) for char in identifier)


def safe_ident(name: str) -> str:
    """Double-quote a SQL identifier using PostgreSQL quoting convention.

    Canonical quoting for SQL that will be processed by the query-router,
    which handles dialect-specific transpilation downstream.
    """
    return '"' + name.replace('"', '""') + '"'


def quote_identifier(connector: str, identifier: str) -> str:
    """Quote a single identifier for *connector* using sqlglot dialect handling."""
    if not identifier:
        return identifier
    bare = _strip_existing_quotes(identifier)
    if connector == "bigquery":
        return f"`{_escape_bigquery_identifier(bare)}`"
    dialect = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    return exp.Identifier(this=bare, quoted=True).sql(dialect=dialect)


# Literal escaping diverges from identifier quoting for Redshift. Redshift uses
# the same double-quote IDENTIFIERS as PostgreSQL (so ``CONNECTOR_TO_SQLGLOT``
# maps it to ``postgres``), but its STRING LITERALS are backslash-aware — a
# backslash escapes the following character, unlike PostgreSQL under
# ``standard_conforming_strings``. Rendering a Redshift literal through the
# ``postgres`` dialect would NOT double a backslash, so a trailing-backslash
# value could escape its closing quote (SQL injection). Resolve literals through
# each connector's OWN dialect, which applies the correct per-dialect escaping.
_CONNECTOR_TO_LITERAL_DIALECT: dict[str, str] = {
    "postgresql": "postgres",
    "redshift": "redshift",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}


def quote_literal(connector: str, value: str) -> str:
    """Render *value* as a dialect-correct SQL **string literal** for *connector*.

    Use this for every client-derived value that must appear inline in generated
    SQL (e.g. an ``IN`` list of member names, an equality predicate). Never
    hand-roll ``"'" + v.replace("'", "''") + "'"`` — naive single-quote doubling
    is unsafe on backslash-aware dialects (BigQuery, Spark, Snowflake, Redshift,
    MySQL), where a trailing backslash escapes the closing quote and lets the
    value break out of the literal (SQL injection). ``sqlglot`` applies the
    correct per-dialect escaping (backslash doubling on those dialects, plain
    quote doubling on PostgreSQL), keeping the value contained on every
    connector.

    Fails closed: an unknown connector raises ``ValueError`` rather than
    silently falling back to PostgreSQL escaping, which would under-escape a
    backslash for any unlisted backslash-aware connector.

    Parameters
    ----------
    connector:
        Canonical connector type (``postgresql``, ``bigquery``, ``hadoop_spark``,
        ``redshift``, ``snowflake``, ``sqlserver``).
    value:
        The raw value to embed. Coerced to ``str`` (``None`` -> empty string).
    """
    dialect = _CONNECTOR_TO_LITERAL_DIALECT.get(connector)
    if dialect is None:
        raise ValueError(
            f"quote_literal: unknown connector {connector!r}; cannot safely "
            "render a SQL string literal (refusing to under-escape)."
        )
    text = "" if value is None else str(value)
    return exp.Literal.string(text).sql(dialect=dialect)


_TIMESTAMP_TYPES = frozenset({
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "TIMESTAMP_TZ",
    "TIMESTAMP_LTZ",
    "TIMESTAMP_NTZ",
    "DATETIME",
    "DATETIME2",
    "DATETIMEOFFSET",
})
_DATE_TYPES = frozenset({"DATE"})

# Source-reported timestamp spellings that carry a time zone. The renderer
# emits PostgreSQL-canonical SQL, so these all use the ``TIMESTAMPTZ`` literal
# form; the source dialect is still responsible for translating its own type
# token in ``canonical_timestamp_type`` below.
_TZ_AWARE_TIMESTAMP_TYPES = frozenset({
    "TIMESTAMPTZ",
    "TIMESTAMP_TZ",
    "TIMESTAMP_LTZ",
    "DATETIMEOFFSET",
})


def normalize_type_token(data_type: str | None) -> str:
    """Reduce a raw connector data-type spelling to its canonical leading token.

    Sources report the same logical type under many spellings: PostgreSQL's
    ``information_schema`` yields verbose forms such as
    ``timestamp without time zone`` / ``timestamp with time zone``, driver
    metadata may carry precision (``TIMESTAMP(3)``), while snapshots hold the
    short tokens (``DATE``, ``TIMESTAMPTZ``). Membership checks against the
    ``_DATE_TYPES`` / ``_TIMESTAMP_TYPES`` families must normalise first:
    upper-case, strip the precision parenthesis, and keep only the first word
    (dropping ``WITH/WITHOUT TIME ZONE`` qualifiers). Without this, a
    ``timestamp without time zone`` column is not recognised as a date anchor
    or a coercible join side (Bug intake 2026-07-07 variant-date-anchor).
    """
    head = (data_type or "").upper().split("(", 1)[0].strip()
    if not head:
        return ""
    return head.split()[0]


# sqlglot's own verdict on "this type carries a time zone". TIMESTAMPTZ is the
# absolute-instant type; TIMESTAMPLTZ is the session-local-zone variant. Both
# render PostgreSQL-canonical as ``TIMESTAMPTZ 'lit'``.
_TZ_AWARE_SQLGLOT_TYPES = frozenset({
    exp.DataType.Type.TIMESTAMPTZ,
    exp.DataType.Type.TIMESTAMPLTZ,
})

# The PostgreSQL-canonical spellings this module hands back.
_PG_TZ_AWARE_TOKEN = "TIMESTAMPTZ"
_PG_TZ_NAIVE_TOKEN = "TIMESTAMP"

# Preserved for the fallback: the tokens that DECLARE tz-awareness in their own
# spelling, whatever dialect reported them.
_SELF_DECLARED_TZ_TOKENS = _TZ_AWARE_TIMESTAMP_TYPES


@lru_cache(maxsize=2048)
def canonical_timestamp_type(data_type: str | None, connector: str | None) -> str | None:
    """Re-spell a SOURCE-reported timestamp type in PostgreSQL-canonical form.

    Bug-7918. Generated SQL is built PostgreSQL-canonical and transpiled by
    sqlglot (SQL rule 1), so a timestamp literal must be emitted as
    ``TIMESTAMP 'lit'`` for a tz-NAIVE column and ``TIMESTAMPTZ 'lit'`` for a
    tz-AWARE one. Deciding which requires the SOURCE dialect, because the token
    ``TIMESTAMP`` names a different type in different dialects:

    ==============  ==================  ================================
    Source          reports for its     PostgreSQL-canonical equivalent
                    absolute-instant
                    column
    ==============  ==================  ================================
    PostgreSQL      ``timestamptz``     ``TIMESTAMPTZ``
    BigQuery        ``timestamp``       ``TIMESTAMPTZ``  (``datetime`` is the naive one)
    Spark           ``timestamp``       ``TIMESTAMPTZ``
    Snowflake       ``timestamp_tz``    ``TIMESTAMPTZ``  (bare ``timestamp`` is naive)
    SQL Server      ``datetimeoffset``  ``TIMESTAMPTZ``  (``timestamp`` is a ROWVERSION)
    ==============  ==================  ================================

    A token-only check therefore mis-reads BigQuery: its introspection persists
    ``field_type`` verbatim (``"timestamp"``, see
    ``source_introspection.py:1303``), which normalises to ``TIMESTAMP`` and
    was rendered tz-naive — transpiling to ``CAST('lit' AS DATETIME)`` and
    failing on BigQuery with "No matching signature for operator >=".

    The dialect knowledge lives entirely in sqlglot's type parser; this
    function contains NO per-connector branch (SQL rule 1). Non-timestamp
    types are returned unchanged, so callers may pass any ``data_type``.

    Fails SAFE on anything it cannot prove: an unknown connector, an
    unparseable spelling, or ``None`` falls back to the self-declaring-token
    check, which is exactly the pre-Bug-7918 behaviour — the function never
    invents tz-awareness it cannot demonstrate and never discards tz-awareness
    the token itself declares.
    """
    normalized = normalize_type_token(data_type)
    if normalized not in _TIMESTAMP_TYPES:
        # Not a timestamp-family column: DATE, numerics, text, and the
        # dialect-specific spellings outside ``_TIMESTAMP_TYPES`` are the
        # caller's business, not this function's. Hand back the original.
        return data_type
    dialect = CONNECTOR_TO_SQLGLOT.get(connector or "")
    if dialect:
        # Try the RAW spelling first — ``timestamp with time zone`` only reads
        # as tz-aware while its qualifier words survive, and
        # ``normalize_type_token`` drops them. Fall back to the normalised
        # token so a precision/qualifier form sqlglot rejects still resolves.
        for candidate in (data_type, normalized):
            if not candidate:
                continue
            try:
                parsed = sqlglot.parse_one(candidate, into=exp.DataType, read=dialect)
            except Exception:
                continue
            return (
                _PG_TZ_AWARE_TOKEN
                if parsed.this in _TZ_AWARE_SQLGLOT_TYPES
                else _PG_TZ_NAIVE_TOKEN
            )
    return (
        _PG_TZ_AWARE_TOKEN
        if normalized in _SELF_DECLARED_TZ_TOKENS
        else _PG_TZ_NAIVE_TOKEN
    )


def is_date_anchor_type(data_type: str | None) -> bool:
    """True when ``data_type`` normalises to a DATE or TIMESTAMP family token.

    Shared single source of truth for "can this column anchor time math"
    (window ORDER BY, calendar JOIN). Used by model-service window-variant
    admission (F-015-02) and query-router date-anchor resolution.
    """
    return normalize_type_token(data_type) in (_DATE_TYPES | _TIMESTAMP_TYPES)


def coerce_join_types(
    lhs_expr: str, lhs_type: str | None,
    rhs_expr: str, rhs_type: str | None,
) -> tuple[str, str]:
    """Wrap the TIMESTAMP side in CAST(... AS DATE) when joining DATE to TIMESTAMP.

    Returns the (possibly modified) pair of expressions unchanged when both
    sides are the same type family.  Uses ANSI ``CAST(... AS DATE)`` which
    transpiles correctly across all dialects via sqlglot.
    """
    lt = normalize_type_token(lhs_type)
    rt = normalize_type_token(rhs_type)
    def _as_date(expression: str) -> str:
        # Bug-5173: construct the coercion as a SQLGlot expression instead of
        # formatting a dialect SQL fragment. ``expression`` is already quoted
        # by this module's callers and remains an opaque canonical AST leaf.
        return exp.Cast(
            this=exp.Var(this=expression),
            to=exp.DataType.build("DATE", dialect="postgres"),
        ).sql(dialect="postgres")

    if lt in _TIMESTAMP_TYPES and rt in _DATE_TYPES:
        return _as_date(lhs_expr), rhs_expr
    if lt in _DATE_TYPES and rt in _TIMESTAMP_TYPES:
        return lhs_expr, _as_date(rhs_expr)
    return lhs_expr, rhs_expr


def quote_table_ref(connector: str, dotted_name: str) -> str:
    """Quote each segment of a dotted table reference.

    ``my-project.dataset.table`` becomes:

    - **PostgreSQL / Redshift / Snowflake**: ``"my-project"."dataset"."table"``
    - **BigQuery / Spark**: `` `my-project`.`dataset`.`table` ``
    """
    if not dotted_name:
        return dotted_name

    parts = dotted_name.split(".")
    return ".".join(quote_identifier(connector, p) for p in parts)
