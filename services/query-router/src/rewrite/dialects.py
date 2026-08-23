"""Dialect/connector support for the query rewriter.

Pure, self-contained helpers: dialect/connector name mapping, the BigQuery
generator patch, and the raw-SQL dialect translation / transpile boundary.
All SQL is built PostgreSQL-canonical and translated here at the return edge.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect

from src.ir.logical_query import SemanticBindingError

# Register missing sqlglot dialect generators (sqlglot 30.x gaps) on import.
from shared.sqlglot_compat import register_bigquery_patches

register_bigquery_patches()


# Map sqlglot dialect strings → connector_qualify connector names.
_DIALECT_TO_CONNECTOR: dict[str, str] = {
    "bigquery": "bigquery",
    "spark": "hadoop_spark",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "tsql": "sqlserver",
}


def _dialect_to_connector(dialect: str) -> str:
    """Convert a sqlglot dialect string to a connector_qualify connector name."""
    return _DIALECT_TO_CONNECTOR.get(dialect, "postgresql")


# Inverse of ``_DIALECT_TO_CONNECTOR`` (connector_qualify connector name →
# sqlglot dialect string). Used by the regex-substitution path to choose the
# right per-connector identifier quoting (F-006-03).
_CONNECTOR_TO_DIALECT: dict[str, str] = {
    connector: dialect for dialect, connector in _DIALECT_TO_CONNECTOR.items()
}
# Aliases that may appear as connector names but are not in the canonical map.
_CONNECTOR_TO_DIALECT.setdefault("spark", "spark")
_CONNECTOR_TO_DIALECT.setdefault("spark_sql", "spark")


def _connector_to_dialect(connector: str) -> str:
    """Convert a connector_qualify connector name back to a sqlglot dialect."""
    return _CONNECTOR_TO_DIALECT.get((connector or "").lower(), "postgres")


def _dialect_from_connection_type(connector_type: str | None) -> str:
    """Map a normalized connection_type string to a sqlglot dialect name.

    Always call normalize_connection_type() before this function so that
    legacy aliases (e.g. ``jdbc`` → ``hadoop_spark``) are already collapsed.
    """
    ct = (connector_type or "").lower()
    if ct == "bigquery":
        return "bigquery"
    if ct in ("hadoop_spark", "spark", "spark_sql"):
        return "spark"
    if ct == "redshift":
        return "redshift"
    if ct == "snowflake":
        return "snowflake"
    if ct == "sqlserver":
        return "tsql"
    return "postgres"


def _is_is_not_null_on_agg_col(where_node: exp.Where, agg: exp.Expression) -> bool:
    """True when *where_node* is exactly ``WHERE <agg_col> IS NOT NULL``.

    The canonical semi-additive producer emits FILTER (WHERE col IS NOT NULL)
    which is semantically equivalent to IGNORE NULLS.  sqlglot parses
    ``IS NOT NULL`` as ``Not(Is(col, Null))``.

    Bug-7912: the prior code applied IGNORE NULLS unconditionally, dropping
    arbitrary predicates.  This helper structurally verifies the exact
    IS-NOT-NULL-on-the-aggregated-column shape so only the safe substitution
    fires.
    """
    cond = where_node.this
    if not isinstance(cond, exp.Not):
        return False
    is_node = cond.this
    if not isinstance(is_node, exp.Is):
        return False
    if not isinstance(is_node.expression, exp.Null):
        return False
    # The left-hand side of IS must reference the same column the aggregate
    # operates on.  For ARRAY_AGG(col ORDER BY ...) the value column is
    # nested: ArrayAgg.this may be an Order node whose .this is the column.
    # For ARRAY_AGG(DISTINCT col) the value is inside a Distinct node.
    agg_col = agg.this
    if isinstance(agg_col, exp.Order):
        agg_col = agg_col.this
    if isinstance(agg_col, exp.Distinct):
        _distinct_exprs = agg_col.expressions
        agg_col = _distinct_exprs[0] if _distinct_exprs else agg_col
    return is_node.this.sql() == agg_col.sql()


def _bq_filter_sql(self: _BigQueryDialect.Generator, expression: exp.Filter) -> str:
    """Bug-7912: shape-guarded FILTER -> BigQuery rewrite.

    ARRAY_AGG(x) FILTER (WHERE x IS NOT NULL) -> ARRAY_AGG(x IGNORE NULLS)
    <any other shape>                          -> fall through verbatim
                                                  (FILTER is invalid GoogleSQL
                                                  -> fail loud at BQ)

    Codex GPT-5.6-sol gate: the prior IF-wrap path
    (ARRAY_AGG(IF(pred, x, NULL) IGNORE NULLS)) drops ACCEPTED NULL values
    from the aggregate (IGNORE NULLS excludes them). Only the exact
    IS-NOT-NULL-on-the-aggregated-column shape is semantically equivalent to
    IGNORE NULLS. Every other FILTER shape must fail loud.
    """
    agg = expression.this
    if not isinstance(agg, exp.ArrayAgg):
        return self.filter_sql(expression)

    where_node = expression.expression  # the Where node inside FILTER
    if _is_is_not_null_on_agg_col(where_node, agg):
        # Exact IS NOT NULL predicate on the aggregated column: IGNORE NULLS
        # is semantically equivalent.
        return self.sql(exp.IgnoreNulls(this=agg.copy()))

    # Any other predicate: fall through verbatim so BQ rejects FILTER
    # (fail loud). An IF-wrap + IGNORE NULLS would silently drop accepted
    # NULL values from the result.
    return self.filter_sql(expression)


if exp.Filter not in _BigQueryDialect.Generator.TRANSFORMS:
    _BigQueryDialect.Generator.TRANSFORMS[exp.Filter] = _bq_filter_sql


def _bq_escape_sql(self: _BigQueryDialect.Generator, expression: exp.Escape) -> str:
    """Bug-7911: shape-guarded LIKE ESCAPE → BigQuery rewrite.

    ``LIKE ... ESCAPE '<char>'`` is valid ANSI/PG but NOT valid GoogleSQL.
    BigQuery uses backslash as the native (and only) LIKE escape character.

    When the escape char IS backslash: unwrap the Escape node — sqlglot
    already renders the backslash-escaped pattern correctly (Bug-6383).

    When the escape char is NOT backslash (Bug-7911): translate the pattern
    so that the custom escape char's semantics are preserved under BigQuery's
    native backslash escaping.  Translation:
      - ``<esc>%`` → ``\\%``  (literal percent)
      - ``<esc>_`` → ``\\_``  (literal underscore)
      - ``<esc><esc>`` → ``<esc>``  (literal escape char)
      - existing backslashes in the pattern are doubled (``\\`` → ``\\\\``)
        so they remain literal under BigQuery's backslash interpretation.
    If the pattern is not a string literal (expression-valued), leave the
    node verbatim so BigQuery fails loud rather than silently wrong.
    """
    escape_literal = expression.expression  # the ESCAPE character node
    if not isinstance(escape_literal, exp.Literal) or not escape_literal.is_string:
        # Non-literal escape expression: cannot translate statically.
        # Emit verbatim → BigQuery rejects the ESCAPE clause (fail loud).
        return self.escape_sql(expression)

    esc_char = escape_literal.this
    # Codex gate: multi-char ESCAPE (e.g. ESCAPE '!!') is invalid per the
    # SQL standard (PG itself rejects it). Leave verbatim so BQ fails loud.
    if len(esc_char) != 1:
        return self.escape_sql(expression)
    if esc_char == "\\":
        # Backslash escape: BigQuery native -- just unwrap (Bug-6383).
        return self.sql(expression.this)

    # Non-backslash escape char (Bug-7911): translate the pattern.
    like_node = expression.this
    pattern_node = like_node.expression
    if not isinstance(pattern_node, exp.Literal) or not pattern_node.is_string:
        # Non-literal pattern: cannot translate statically → fail loud.
        return self.escape_sql(expression)

    pattern = pattern_node.this
    # Translate: scan character by character.
    translated: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == esc_char:
            # Next character is escaped by the custom escape char.
            if i + 1 < len(pattern):
                next_ch = pattern[i + 1]
                if next_ch in ("%", "_"):
                    # Escaped wildcard: emit backslash-escaped version.
                    translated.append("\\")
                    translated.append(next_ch)
                elif next_ch == esc_char:
                    # Escaped escape char: emit the literal char.
                    translated.append(esc_char)
                else:
                    # Unknown escape sequence: cannot translate safely.
                    # Emit verbatim → BigQuery rejects ESCAPE clause.
                    return self.escape_sql(expression)
                i += 2
            else:
                # Trailing escape char with no following character.
                return self.escape_sql(expression)
        elif ch == "\\":
            # Existing backslash: must be doubled for BigQuery's native
            # backslash escaping so it stays a literal backslash.
            translated.append("\\\\")
            i += 1
        else:
            translated.append(ch)
            i += 1

    # Build new LIKE node with translated pattern, no ESCAPE clause.
    new_pattern = exp.Literal.string("".join(translated))
    new_like = like_node.copy()
    new_like.set("expression", new_pattern)
    return self.sql(new_like)


if exp.Escape not in _BigQueryDialect.Generator.TRANSFORMS:
    _BigQueryDialect.Generator.TRANSFORMS[exp.Escape] = _bq_escape_sql


from sqlglot.dialects.tsql import TSQL as _TSQLDialect


def _tsql_escape_sql(self: _TSQLDialect.Generator, expression: exp.Escape) -> str:
    """Bug-6894 / F-006-02: T-SQL LIKE bracket escaping, centralized at the
    dialect generator boundary.

    In T-SQL a ``[abc]`` inside a LIKE pattern is a character class matching any
    one of a/b/c. Tessallite's ``contains`` / ``notContains`` filter operators
    render as ``LIKE '%<search>%' ESCAPE '<char>'`` where ``<search>`` is the
    user's literal text; if that text contains ``[`` or ``]`` the T-SQL engine
    treats it as a character class and matches the WRONG rows. Those must be
    escaped with the declared ESCAPE character so they match literally.

    This was previously done as a per-connector string mutation inside the
    generic WHERE renderer (``conditions._tsql_bracket_escape``), a direct
    violation of the single-dialect-boundary invariant (F-006-02). It now lives
    here, in the T-SQL generator, so the WHERE renderer stays connector-agnostic
    and every route (source / aggregate / raw) gets identical T-SQL behaviour.

    Safety of the structural marker: only the ``contains`` / ``notContains``
    producers set ``like_escape`` and therefore emit an ``ESCAPE`` clause via
    ``_render_condition``. A user-authored raw ``LIKE`` (operator ``like`` with
    no ``like_escape``) never emits an ESCAPE clause and never reaches this
    transform, so a user's intentional character class is not rewritten. When
    the escape char is not a single character or the pattern is not a string
    literal, fall through to the default rendering unchanged.
    """
    escape_literal = expression.expression  # the ESCAPE character node
    like_node = expression.this
    if (
        not isinstance(escape_literal, exp.Literal)
        or not escape_literal.is_string
        or len(escape_literal.this) != 1
        or not isinstance(like_node, exp.Like)
    ):
        return self.escape_sql(expression)
    pattern_node = like_node.expression
    if not isinstance(pattern_node, exp.Literal) or not pattern_node.is_string:
        return self.escape_sql(expression)
    esc = escape_literal.this
    pattern = pattern_node.this
    if "[" not in pattern and "]" not in pattern:
        return self.escape_sql(expression)
    # Fable review BLOCKER: the RLS injector re-renders already-emitted T-SQL
    # through the same generator, causing this transform to fire TWICE. A naive
    # ``str.replace("[", esc+"[")`` double-escapes on the second pass
    # (``\[`` -> ``\\[``), silently opening a character class. To be idempotent,
    # scan character by character and only escape brackets that are NOT already
    # preceded by the escape character.
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == esc:
            # The escape char escapes the NEXT character. Emit both and skip
            # ahead so the next char (which may be ``[`` or ``]``) is not
            # re-escaped.
            if i + 1 < len(pattern):
                out.append(ch)
                out.append(pattern[i + 1])
                i += 2
            else:
                out.append(ch)
                i += 1
        elif ch in ("[", "]"):
            # Unescaped bracket: escape it.
            out.append(esc)
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    escaped = "".join(out)
    if escaped == pattern:
        # Nothing changed (all brackets were already escaped). Avoid a
        # pointless copy.
        return self.escape_sql(expression)
    new_like = like_node.copy()
    new_like.set("expression", exp.Literal.string(escaped))
    new_escape = expression.copy()
    new_escape.set("this", new_like)
    return self.escape_sql(new_escape)


if exp.Escape not in _TSQLDialect.Generator.TRANSFORMS:
    _TSQLDialect.Generator.TRANSFORMS[exp.Escape] = _tsql_escape_sql


def _rewrite_week_to_isoweek_for_bigquery(tree: exp.Expression) -> exp.Expression:
    """Bug-7917 + F-103-03: pre-generation AST rewrite mapping WEEK -> ISOWEEK
    for BigQuery, for BOTH date truncation and week extraction.

    PG ``DATE_TRUNC('week', col)`` truncates to ISO Monday-start.  BigQuery's
    ``WEEK`` truncates to SUNDAY-start, so ``WEEK``-unit bucketing silently
    returns different week boundaries than PG -- cross-source parity
    divergence and wrong weekly totals.  ``ISOWEEK`` on BigQuery matches the
    PG Monday-start semantics.

    F-103-03: the SAME divergence exists for week EXTRACTION.  PG
    ``EXTRACT(WEEK FROM d)`` returns the ISO-8601 week number (Monday-start,
    weeks 1..53); BigQuery ``EXTRACT(WEEK FROM d)`` returns a Sunday-based
    week number (0..53).  The same physical date therefore lands in a
    different weekly bucket per source -- silently wrong weekly totals and
    year-boundary YTD/YoY comparisons.  BigQuery ``EXTRACT(ISOWEEK FROM d)``
    matches PG's ISO week numbering, so map the WEEK date-part on Extract
    nodes too.

    Applied as a pre-generation AST walk (not a generator transform) so it
    fires only on PG-canonical SQL passing through the render boundary
    -- NOT on BQ-authored passthroughs (which explicitly chose WEEK for
    Sunday-start and must not be silently overridden; callers pass
    ``pg_canonical=False`` there).  The walk mutates TimestampTrunc,
    DateTrunc, and DatetimeTrunc unit nodes and Extract date-part nodes
    whose value is WEEK.
    """
    _DtTrunc = getattr(exp, "DatetimeTrunc", None)
    _trunc_types = (exp.TimestampTrunc, exp.DateTrunc)
    if _DtTrunc is not None:
        _trunc_types = (exp.TimestampTrunc, exp.DateTrunc, _DtTrunc)

    def _unit_is_week(unit_node) -> bool:
        if isinstance(unit_node, exp.Var):
            return (unit_node.this or "").upper() == "WEEK"
        if isinstance(unit_node, exp.Literal) and unit_node.is_string:
            return (unit_node.this or "").upper() == "WEEK"
        return False

    def _transform(node):
        # Truncation: DATE_TRUNC / TIMESTAMP_TRUNC / DATETIME_TRUNC unit=WEEK.
        if isinstance(node, _trunc_types):
            unit_node = node.args.get("unit")
            if unit_node is not None and _unit_is_week(unit_node):
                node = node.copy()
                # Use Var (bare keyword), not Literal.string (quoted string).
                # BigQuery requires the bare ISOWEEK date-part token.
                node.set("unit", exp.Var(this="ISOWEEK"))
            return node
        # F-103-03: extraction: EXTRACT(WEEK FROM d) -> EXTRACT(ISOWEEK FROM d).
        # sqlglot parses the date-part into Extract.this (a Var).
        if isinstance(node, exp.Extract):
            part_node = node.this
            if _unit_is_week(part_node):
                node = node.copy()
                node.set("this", exp.Var(this="ISOWEEK"))
            return node
        return node

    return tree.transform(_transform)


def _rewrite_dow_to_dayofweek(
    tree: exp.Expression, target_dialect: str,
) -> exp.Expression:
    """Bug-8300 (AKA F-103-03-residual): pre-generation AST rewrite normalising
    day-of-week numbering
    from PostgreSQL to the target's session-independent weekday primitive for
    cross-source parity.

    Two PG date-parts number the days of the week, and both diverge from the
    BigQuery/Spark ``DAYOFWEEK`` primitive (Sunday=1 .. Saturday=7), while
    Snowflake's ordinary DOW is session-dependent:

    * PG ``EXTRACT(DOW FROM d)``    -> 0..6, Sunday=0 (Sunday-based).
    * PG ``EXTRACT(ISODOW FROM d)`` -> 1..7, Monday=1 .. Sunday=7 (ISO).

    Without this rewrite, an UNADJUSTED ``EXTRACT(DOW ...)`` renders to a bare
    ``EXTRACT(DAYOFWEEK ...)`` (Sunday=1..7) on BOTH BigQuery and Spark, with NO
    numbering shift. So the SAME physical date lands in a different day-of-week
    bucket than PostgreSQL: Sunday is DOW 0 on PG but DAYOFWEEK 1 on
    BigQuery/Spark -- a silently wrong weekday bucket label/number. This rewrite
    runs BEFORE generation and supplies the explicit numbering shift so the
    buckets match.

    ``ISODOW`` needs the same treatment for BigQuery, which has no ISO
    day-of-week part at all: ``shared/semantic/time_variants_sql`` (imported by
    the render stack) sets ``NORMALIZE_EXTRACT_DATE_PARTS=True`` and registers
    ``ISODOW -> DAYOFWEEK`` on BigQuery, so an unadjusted BigQuery ISODOW is a
    bare Sunday=1..7 ``DAYOFWEEK`` -- wrong numbering. Spark DOES have an ISO
    day-of-week part and time_variants_sql registers ``ISODOW -> DOW_ISO`` for
    it (Bug-8328: before that fix the two dialects shared ONE mapping dict and
    Spark silently inherited BigQuery's ``DAYOFWEEK``). This rewrite still
    applies to Spark ISODOW: it replaces the node with explicit
    ``DAYOFWEEK``-based arithmetic before generation, so the dialect mapping is
    simply not consulted for that node, and the emitted numbering (Monday=1..7)
    is identical either way. The rewrite is therefore the single authority for
    day-of-week numbering parity on both targets, independent of the mapping.

    Canonical convention is PostgreSQL's numbering (mirroring the WEEK->ISOWEEK
    direction, which also picks PG's numbering as canonical and adjusts the
    target to match). Reproduce PG's numbering from the target's Sunday=1..7
    ``DAYOFWEEK``:

    * ``DOW``   -> ``(EXTRACT(DAYOFWEEK FROM d) - 1)``           (0..6, Sun=0).
    * ``ISODOW`` -> ``(MOD(EXTRACT(DAYOFWEEK FROM d) + 5, 7) + 1)`` (1..7,
      Mon=1..Sun=7): Sun(1)->7, Mon(2)->1, Tue(3)->2, ... Sat(7)->6.

    BigQuery and Spark use the target-agnostic ``MOD``/``-``/``+`` AST below.
    Snowflake uses ``DAYOFWEEKISO`` directly: ISODOW is that value and DOW is
    its value modulo seven. This is independent of session ``WEEK_START``.

    Applied ONLY on PG-canonical input (``pg_canonical=True`` at the render
    boundary), exactly like the WEEK rewrite: a target-authored
    ``EXTRACT(DAYOFWEEK ...)`` (Sunday=1..7) is the author's explicit choice
    and is NOT rewritten.
    """
    def _sunday_dayofweek(node: exp.Extract) -> exp.Extract:
        # BigQuery requires the DAYOFWEEK date-part token; the bare PG DOW /
        # ISODOW tokens are not valid (or not numbering-equivalent) BigQuery
        # SQL. Returns EXTRACT(DAYOFWEEK FROM <same operand>), Sunday=1..7.
        new_extract = node.copy()
        new_extract.set("this", exp.Var(this="DAYOFWEEK"))
        return new_extract

    def _transform(node):
        if isinstance(node, exp.Extract):
            part_node = node.this
            part = (
                (part_node.this or "").upper()
                if isinstance(part_node, exp.Var)
                else ""
            )
            if part == "DOW":
                # DAYOFWEEK is 1..7 (Sunday=1); subtract 1 to match PG's
                # 0..6 (Sunday=0) so both sources bucket identically.
                return exp.Paren(
                    this=exp.Sub(
                        this=_sunday_dayofweek(node),
                        expression=exp.Literal.number(1),
                    )
                )
            if part == "ISODOW":
                # PG ISODOW is Monday=1..Sunday=7. From BQ DAYOFWEEK (Sun=1..7):
                # MOD(dayofweek + 5, 7) + 1 reproduces ISODOW exactly.
                return exp.Paren(
                    this=exp.Add(
                        this=exp.Mod(
                            this=exp.Paren(
                                this=exp.Add(
                                    this=_sunday_dayofweek(node),
                                    expression=exp.Literal.number(5),
                                )
                            ),
                            expression=exp.Literal.number(7),
                        ),
                        expression=exp.Literal.number(1),
                    )
                )
        return node

    def _snowflake_transform(node):
        if not isinstance(node, exp.Extract) or not isinstance(node.this, exp.Var):
            return node
        part = (node.this.this or "").upper()
        if part not in {"DOW", "ISODOW"}:
            return node
        # Bug-8329: DAYOFWEEK/DOW depends on the Snowflake WEEK_START session
        # parameter. DAYOFWEEKISO is always Monday=1..Sunday=7, so ISODOW maps
        # directly and PG DOW is ISO modulo seven (Sunday 7 -> 0).
        iso = exp.Anonymous(
            this="DAYOFWEEKISO", expressions=[node.expression.copy()],
        )
        if part == "ISODOW":
            return iso
        return exp.Paren(
            this=exp.Mod(
                this=iso,
                expression=exp.Literal.number(7),
            )
        )

    transforms = {
        "bigquery": _transform,
        "spark": _transform,
        "snowflake": _snowflake_transform,
    }
    transform = transforms.get(target_dialect)
    return tree.transform(transform) if transform is not None else tree


def _read_dialect_ladder(input_dialect: str | None) -> tuple[str, ...]:
    """Ordered sqlglot ``read`` dialects to try for a passthrough statement.

    The table-substitution path (``_substitute_table_names``) rewrites the
    physical table in PostgreSQL-canonical double-quoted form, so ``postgres``
    must always be attempted. But a passthrough authored in a non-PG dialect
    (``input_dialect`` carried on the logical query — e.g. a BigQuery client
    sending ``TIMESTAMP_TRUNC(col, MONTH)`` with backtick identifiers against a
    BigQuery-backed model) does NOT parse under ``read="postgres"``; without
    honouring the author's dialect the parse fails and the caller silently drops
    to a regex that cannot transpile dialect-specific FUNCTION syntax (Bug-6035).
    Try the author's dialect first when it differs, then ``postgres`` as the
    canonical fallback. The common PG-authored case parses under ``postgres`` on
    the first (and only) attempt, so its output is byte-identical to before.
    """
    _in = (input_dialect or "postgres").lower()
    if _in in ("postgresql",):
        _in = "postgres"
    if _in and _in != "postgres":
        return (_in, "postgres")
    return ("postgres",)


class PassthroughTranspileError(Exception):
    """Raised when a passthrough SQL statement cannot be safely transpiled to
    the target dialect because sqlglot could not parse it.

    Bug-7012: BigQuery treats double-quoted tokens as STRING LITERALS, not
    identifiers.  A regex-based identifier requote cannot disambiguate
    double-quoted identifiers from double-quoted string literals without
    silently corrupting results in BOTH directions:
      - Over-converting a literal to an identifier = silent wrong rows.
      - Under-converting an identifier (left double-quoted) = BigQuery reads
        it as a string constant = also silent wrong rows.
    Fail-loud is the only safe option when sqlglot cannot parse.

    Bug-7913: extended to Spark (and any backtick-quoting dialect where
    double-quoted tokens are string literals, the same silent-wrong class).
    """


# Bug-7913: dialects where PG double-quoted identifiers are silently
# interpreted as string literals.  Un-transpiled PG-quoted SQL reaching
# these targets produces constant-string columns (silent wrong values).
_STRING_LITERAL_QUOTE_DIALECTS: frozenset[str] = frozenset({
    "bigquery", "spark",
})


def _render_for_dialect(
    tree: exp.Expression, target_dialect: str, *, pg_canonical: bool = True,
) -> str:
    """Shared final-render function: apply all pre-generation transforms and emit.

    F-006-02 render boundary: EVERY non-postgres emission of a full statement
    or an expression fragment tree MUST go through this function (or its public
    aliases ``render_tree_for_dialect`` / ``render_expression_for_dialect``) so
    the WEEK->ISOWEEK truncation+extraction rewrite (Bug-7917 / F-103-03), the
    Spark semi-additive reject, and the T-SQL semi-additive reject fire
    uniformly on ALL routes (source, aggregate, pocket, passthrough, raw, UDA,
    calc). No caller may bypass it with a direct ``.sql(dialect=<target>)`` —
    ``test_render_boundary_static.py`` fails the build if one does.

    ``pg_canonical``: True when the tree was parsed from PG-canonical SQL
    (the default for all internal paths). False when the tree was parsed from
    a non-PG-authored source (e.g. BQ-authored passthrough) -- in that case,
    the WEEK->ISOWEEK rewrite does NOT fire because the author explicitly
    chose WEEK for Sunday-start semantics.

    For ``dialect="postgres"`` this is a no-op pass-through to ``tree.sql()``.
    """
    if target_dialect in ("postgres", "postgresql"):
        return tree.sql(dialect=target_dialect)
    # Bug-7917 / F-103-03: for BigQuery, rewrite WEEK -> ISOWEEK (truncation
    # AND extraction) only on PG-canonical input (the author's explicit WEEK
    # on a BQ-authored query is preserved).
    if target_dialect == "bigquery" and pg_canonical:
        tree = _rewrite_week_to_isoweek_for_bigquery(tree)
    # Bug-8300/Bug-8329: day-of-week numbering parity. BigQuery/Spark adjust
    # Sunday-based DAYOFWEEK; Snowflake uses WEEK_START-independent
    # DAYOFWEEKISO. PG-canonical input only.
    if target_dialect in ("bigquery", "spark", "snowflake") and pg_canonical:
        tree = _rewrite_dow_to_dayofweek(tree, target_dialect)
    # Bug-7914 / Codex gate R2: for Spark, reject semi-additive patterns.
    if target_dialect == "spark":
        _reject_semi_additive_for_spark(tree)
    # Bug-7192-F5: for T-SQL, reject semi-additive patterns.
    if target_dialect == "tsql":
        _reject_semi_additive_for_tsql(tree)
    return tree.sql(dialect=target_dialect)


def render_tree_for_dialect(
    tree: exp.Expression, target_dialect: str, *, pg_canonical: bool = True,
) -> str:
    """Public statement/fragment render boundary (F-006-02).

    The single sanctioned entry point for turning a PostgreSQL-canonical
    sqlglot AST (full statement OR expression fragment) into target-dialect
    SQL. Delegates to ``_render_for_dialect`` so every dialect-semantics guard
    (week numbering, semi-additive fail-loud) is applied uniformly. Callers in
    ``uda.py`` / ``aggregate.py`` / ``source_sql.py`` MUST use this (or
    ``render_expression_for_dialect``) instead of a raw
    ``tree.sql(dialect=target_dialect)``.
    """
    return _render_for_dialect(tree, target_dialect, pg_canonical=pg_canonical)


def render_expression_for_dialect(
    expression: exp.Expression, target_dialect: str, *, pg_canonical: bool = True,
) -> str:
    """Public fragment render boundary (F-006-02) — alias of
    :func:`render_tree_for_dialect` named for expression-fragment call sites
    (a WHERE fragment, a SELECT item, a GROUP BY item, a UDA expression).

    A bare expression is a valid sqlglot tree, so this is a thin alias; the
    separate name documents intent at the fragment emission sites and keeps
    the static ``.sql(dialect=...)`` ban readable.
    """
    return _render_for_dialect(expression, target_dialect, pg_canonical=pg_canonical)


class RewriteReparseError(Exception):
    """Raised when an internal rewrite reparse recovers a semantics-changed
    tree from malformed SQL (F-006-03).

    The top-level parser (``parsing/sql_parser.py``) already rejects any
    sqlglot recovery so a meaning-changed tree (e.g. ``WHERE x = 1 !!`` ->
    ``WHERE x = NOT 1``) never routes. Downstream rewrite helpers that must
    reparse (pocket table substitution, raw ORDER BY / WHERE preservation)
    independently reparsed with ``ErrorLevel.WARN``, silently accepting the
    same unsafe recovery. ``parse_one_strict`` raises this instead so those
    helpers fail loud in parity with the top-level parser.
    """


def parse_one_strict(sql: str, *, read: str = "postgres") -> exp.Expression:
    """Reparse *sql* in *read* dialect, rejecting any recovered parse error
    (F-006-03 fail-loud parity with the top-level parser).

    ``sqlglot.parse_one`` discards the Parser instance (and its ``errors``),
    so drive the dialect parser directly to keep access to ``parser.errors``.
    Any recovered error means real source SQL of that dialect would have been
    rejected; a recovered tree can carry altered semantics, so raise
    :class:`RewriteReparseError` rather than transform a meaning-changed tree.
    """
    _read = (read or "postgres").lower()
    if _read == "postgresql":
        _read = "postgres"
    sg_dialect = sqlglot.Dialect.get_or_raise(_read)
    tokens = sg_dialect.tokenizer_class().tokenize(sql)
    parser = sg_dialect.parser(error_level=sqlglot.ErrorLevel.WARN)
    trees = parser.parse(tokens, sql=sql)
    if parser.errors:
        raise RewriteReparseError(
            f"Refusing to reparse malformed SQL under a recovered tree "
            f"(read={_read}): {parser.errors[0]}"
        )
    _valid = [t for t in (trees or []) if t is not None]
    if len(_valid) > 1:
        raise RewriteReparseError(
            "Multi-statement SQL is not supported in a rewrite reparse."
        )
    tree = _valid[0] if _valid else None
    if tree is None:
        raise RewriteReparseError(
            f"Could not parse SQL under read={_read}."
        )
    return tree


def _requote_identifiers_for_bigquery(sql: str, input_dialect: str = "postgres") -> str:
    """Convert ANSI double-quoted identifiers to BigQuery backtick quoting.

    BigQuery treats ``"value"`` as a *string literal*, not an identifier.
    Identifiers must use backtick quoting (`` `value` ``).

    Uses sqlglot to parse (honouring ``input_dialect``) and emit as BigQuery so
    string literals and dialect-specific function syntax are handled correctly.

    Bug-6035: previously the read side was hardcoded to ``postgres``, so a
    BigQuery-authored passthrough (backtick identifiers / BQ-native functions)
    failed the parse and fell through to a regex fallback. Threading
    ``input_dialect`` through the read ladder fixes this while keeping the
    PG-authored path byte-identical (postgres still parses first).

    Bug-7012: when sqlglot cannot parse the SQL under ANY candidate read
    dialect, raises :class:`PassthroughTranspileError` instead of attempting
    a regex requote.  A regex cannot disambiguate double-quoted identifiers
    from string literals on BigQuery, and both over-conversion and
    under-conversion produce silent wrong results.  Fail-loud is the only
    safe option.
    """
    for _read in _read_dialect_ladder(input_dialect):
        try:
            tree = sqlglot.parse_one(sql, read=_read)
            # Codex gate R3: route through _render_for_dialect so WEEK->ISOWEEK
            # and all other pre-generation transforms fire.  pg_canonical=True
            # only when the read dialect is postgres (PG-canonical input);
            # a BQ-authored WEEK stays WEEK (author's explicit intent).
            _pg = _read in ("postgres", "postgresql")
            return _render_for_dialect(tree, "bigquery", pg_canonical=_pg)
        except Exception:
            continue
    # Bug-7012: fail loud — no regex fallback.
    raise PassthroughTranspileError(
        "Cannot safely transpile this SQL to BigQuery: sqlglot could not "
        "parse it under any candidate dialect, and a regex identifier-requote "
        "cannot disambiguate double-quoted identifiers from string literals "
        "without silently corrupting results."
    )


def _translate_raw_sql(sql: str, target_dialect: str, input_dialect: str = "postgres") -> str:
    """Best-effort dialect translation for raw-SQL fallback return paths.

    When the rewriter cannot fully bind a query this ensures the SQL is at
    minimum translated to the target dialect rather than returned in the
    author's original syntax.

    F-006-11: raw queries carry an ``input_dialect`` (the dialect the query was
    authored in -- honoured by the pocket and WHERE/passthrough re-parsers). The
    fallback previously always read ``postgres``, so a non-PG-authored raw
    query (e.g. BigQuery DATE syntax) was silently returned untranslated. Thread
    ``input_dialect`` so the read side matches how the query was written. The
    read is treated as a hint: if a parse under ``input_dialect`` fails we retry
    under ``postgres`` (the rewriter's canonical form) before giving up.

    For BigQuery the dedicated requote helper is preferred when the input is
    already PG-canonical; when an explicit non-PG input dialect is supplied the
    generic transpile path is used so the read side is honoured.

    Bug-7913: all translation now routes through ``_transpile_to_dialect`` so
    the semi-additive pre-generation rewrites (Bug-7017) and the fail-loud
    policies fire on raw/passthrough paths too -- not just the
    ``_build_source_sql`` boundary.  Parse errors on string-literal-quoting
    targets (BigQuery, Spark) raise :class:`PassthroughTranspileError`
    (fail-loud over silent wrong).

    Bug-7916: parse at DEFAULT error level (not IGNORE) so malformed input
    raises instead of recovering a semantically-altered tree.

    F-006-01: a PostgreSQL *target* is NOT synonymous with PostgreSQL-authored
    *input*. The early no-op previously returned the SQL unchanged for every
    postgres target regardless of ``input_dialect``, so a valid BigQuery- (or
    Spark-) authored raw query against a PG source was handed to PostgreSQL
    verbatim -- e.g. ``TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY)`` -> a 502
    ``syntax error`` at the source. Normalise BOTH dialects first and only
    short-circuit when the normalised INPUT is PostgreSQL; otherwise parse
    under the declared input dialect and re-emit as PostgreSQL (for a PG
    target) or route through the strict, shape-aware translation boundary
    (for a non-PG target).
    """
    _target = (target_dialect or "postgres").lower()
    if _target == "postgresql":
        _target = "postgres"
    _read = (input_dialect or "postgres").lower()
    if _read in ("postgresql",):
        _read = "postgres"
    # F-006-01: only a PostgreSQL-authored query on a PostgreSQL target is a
    # true no-op. A non-PG-authored query on a PG target still needs
    # translation to PostgreSQL syntax.
    if _target == "postgres":
        if _read == "postgres":
            return sql
        # Non-PG input, PostgreSQL target: parse under the author's dialect and
        # re-emit PostgreSQL. Retry under postgres as a fallback so a query the
        # author mislabelled (but that is already PG-canonical) still round-trips
        # instead of failing loud. A genuinely malformed query raises.
        for _candidate_read in (_read, "postgres"):
            try:
                tree = sqlglot.parse_one(sql, read=_candidate_read)
                return tree.sql(dialect="postgres")
            except (PassthroughTranspileError, SemanticBindingError):
                raise
            except Exception:
                continue
        # Could not parse under any candidate dialect: return unchanged so the
        # source executor surfaces the original (author-syntax) error rather
        # than a rewriter-invented one.
        return sql
    target_dialect = _target
    if target_dialect == "bigquery" and _read == "postgres":
        return _requote_identifiers_for_bigquery(sql)
    # When the input dialect IS the target dialect, parse under the input
    # dialect and re-emit directly.  This preserves the author's intent
    # (e.g. BQ-authored WEEK stays WEEK, not overridden to ISOWEEK).
    # No PG round-trip, no _transpile_to_dialect pre-generation rewrites.
    if _read == target_dialect:
        return _requote_identifiers_for_bigquery(sql, _read) if target_dialect == "bigquery" else sql
    # Bug-7913: parse at default error level (not IGNORE -- Bug-7916) and
    # route through _transpile_to_dialect which applies the fail-loud
    # policies and the WEEK->ISOWEEK BigQuery fix.
    for _candidate_read in (_read, "postgres") if _read != "postgres" else ("postgres",):
        try:
            tree = sqlglot.parse_one(sql, read=_candidate_read)
            # Re-emit as PG-canonical, then transpile through the choke
            # point so dialect-specific pre-generation transforms fire.
            pg_sql = tree.sql(dialect="postgres")
            return _transpile_to_dialect(pg_sql, target_dialect)
        except (PassthroughTranspileError, SemanticBindingError):
            raise
        except Exception:
            continue
    # Bug-7913: fail loud for string-literal-quoting targets.
    if target_dialect in _STRING_LITERAL_QUOTE_DIALECTS:
        raise PassthroughTranspileError(
            f"Cannot safely transpile this SQL to {target_dialect}: sqlglot "
            f"could not parse it under any candidate dialect, and "
            f"un-transpiled PG-quoted SQL on {target_dialect} reads "
            f"double-quoted identifiers as string literals (silent wrong "
            f"values)."
        )
    return sql


def _reject_semi_additive_for_spark(tree: exp.Expression) -> exp.Expression:
    """Bug-7017 + Bug-7914 / Codex gate R2: fail-loud for Spark semi-additive.

    Spark's COLLECT_LIST drops the ORDER BY from ARRAY_AGG, making
    semi-additive LAST/FIRST_NON_EMPTY non-deterministic (Bug-7017).  The
    prior MAX_BY/MIN_BY rewrite (Bug-7017 fix) was sound for the canonical
    producer shape EXCEPT when the ORDER BY key has NULL values: PG
    ``ORDER BY d DESC`` is NULLS FIRST (picks NULL-keyed rows first), while
    Spark MAX_BY ignores NULL keys (skips them) -- a silent wrong-value
    divergence (Bug-7914, Codex gate R2 finding 3).

    Since column-type metadata (needed for a COALESCE sentinel that
    replicates PG NULL ordering) is not available at the AST-transform
    boundary, the safe interim is FAIL LOUD: raise SemanticBindingError so
    the router falls to the source route or surfaces a clear error.

    The metadata-based parity-safe rewrite remains the tracked architectural
    question in ``docs/questions/questions_spark-semi-additive-null-key-parity.md``.

    Pattern detected (sqlglot AST):
      Bracket(Paren(Filter(ArrayAgg(this=Order(...)), where=...)), [0])
    """
    for node in tree.walk():
        if not isinstance(node, exp.Bracket):
            continue
        inner = node.this
        if isinstance(inner, exp.Paren):
            inner = inner.this
        if not isinstance(inner, exp.Filter):
            continue
        agg = inner.this
        if not isinstance(agg, exp.ArrayAgg):
            continue
        order_node = agg.this
        if isinstance(order_node, exp.Order):
            ordered_list = list(order_node.find_all(exp.Ordered))
            is_desc = ordered_list[0].args.get("desc") if ordered_list else None
            behavior = "LAST_NON_EMPTY" if is_desc else "FIRST_NON_EMPTY"
        else:
            behavior = "LAST_NON_EMPTY/FIRST_NON_EMPTY"
        raise SemanticBindingError(
            f"Semi-additive {behavior} aggregation is not supported on "
            f"Spark targets without column-type metadata for NULL-ordering "
            f"parity. Spark COLLECT_LIST drops ORDER BY and MAX_BY ignores "
            f"NULL keys, diverging from PostgreSQL NULL-ordering semantics. "
            f"Use a non-semi-additive aggregation (SUM, MIN, MAX, AVG) or "
            f"query from a PostgreSQL or BigQuery source."
        )
    return tree


def _reject_semi_additive_for_tsql(tree: exp.Expression) -> exp.Expression:
    """Bug-7192-F5: reject ARRAY_AGG+ORDER+FILTER[0] for T-SQL (fail-loud).

    T-SQL has no ARRAY_AGG, no FILTER clause, and no array subscript
    operator. sqlglot emits these constructs verbatim, producing SQL that
    hard-fails on SQL Server. Unlike Spark (which has MAX_BY) and BigQuery
    (which has ARRAY_AGG with IGNORE NULLS), T-SQL has no single aggregate
    function that means "last/first non-null value ordered by time" --
    implementing this requires query-level restructuring (CROSS APPLY or
    correlated subquery) that cannot be expressed as a node-level AST
    transform.

    This pre-generation check detects the PG-canonical semi-additive
    pattern and raises SemanticBindingError with a clear diagnostic,
    preventing invalid SQL from reaching SQL Server. The check fires at
    the ``_transpile_to_dialect`` boundary (SQL Rule 1 compliant -- one
    site, not per-connector branches in callers).

    Pattern detected (sqlglot AST):
      Bracket(Paren(Filter(ArrayAgg(this=Order(...)), where=...)), [0])
    """
    for node in tree.walk():
        if not isinstance(node, exp.Bracket):
            continue
        inner = node.this
        if isinstance(inner, exp.Paren):
            inner = inner.this
        if not isinstance(inner, exp.Filter):
            continue
        agg = inner.this
        if not isinstance(agg, exp.ArrayAgg):
            continue
        # This IS the semi-additive ARRAY_AGG+FILTER+subscript pattern.
        # Determine direction for the error message.
        order_node = agg.this
        if isinstance(order_node, exp.Order):
            ordered_list = list(order_node.find_all(exp.Ordered))
            is_desc = ordered_list[0].args.get("desc") if ordered_list else None
            behavior = "LAST_NON_EMPTY" if is_desc else "FIRST_NON_EMPTY"
        else:
            behavior = "LAST_NON_EMPTY/FIRST_NON_EMPTY"
        raise SemanticBindingError(
            f"Semi-additive {behavior} aggregation is not supported on "
            f"SQL Server targets. T-SQL has no ARRAY_AGG, FILTER clause, "
            f"or array subscript; the PostgreSQL-canonical pattern cannot "
            f"be transpiled to valid T-SQL. Use a non-semi-additive "
            f"aggregation (SUM, MIN, MAX, AVG) or query from a "
            f"PostgreSQL, BigQuery, or Spark source."
        )
    return tree


def _transpile_to_dialect(sql: str, target_dialect: str) -> str:
    """Transpile a PostgreSQL-canonical SQL string to *target_dialect* via SQLGlot.

    This is the single final translation step applied to every return path in
    ``_build_source_sql``.  All SQL construction inside that function uses
    PostgreSQL double-quoted identifiers and standard PostgreSQL syntax; this
    function is the only place where connector-native syntax (BigQuery backticks,
    SQL Server brackets, TSQL OFFSET/FETCH, etc.) is introduced.

    Bug-7916: parse at DEFAULT error level (not IGNORE).  IGNORE-level
    recovery can emit a valid tree with altered semantics (e.g.
    ``WHERE x = 1 !!`` -> ``WHERE x = NOT 1``).  The JDBC path is already
    strict-gated (``SyntaxErrorInSQL``), but XMLA/API callers can reach
    this boundary with non-strict SQL.  Using the default error level
    causes malformed input to raise rather than silently recovering a
    meaning-changed tree.  The catch block returns the original SQL on
    parse failure (fail-loud at the target for string-literal-quoting
    dialects, or safe passthrough for others).

    Returns the original SQL unchanged when:
    - target_dialect is "postgres" or "postgresql" (no translation needed).
    - SQLGlot cannot parse or transpile the query (safe fallback to avoid
      swallowing a valid query that was already formatted correctly for a target).
    """
    if target_dialect in ("postgres", "postgresql"):
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        # Codex gate R3: route through _render_for_dialect so all
        # pre-generation transforms fire uniformly.
        return _render_for_dialect(tree, target_dialect)
    except SemanticBindingError:
        raise
    except Exception:
        # F-006-04: un-transpiled PG SQL is silent-wrong on every non-postgres
        # target (identifier quoting, function names, pagination). Fail loud
        # rather than returning the original string.
        raise PassthroughTranspileError(
            f"Cannot safely transpile this SQL to {target_dialect}: "
            f"a parse or generation error occurred and un-transpiled "
            f"PostgreSQL-canonical SQL is not valid {target_dialect}."
        )


def dialect_to_connector(dialect: str) -> str:
    """Public alias for dialect → connector mapping — used by router.py."""
    return _dialect_to_connector(dialect)


def dialect_from_connection_type(connector_type: str | None) -> str:
    """Public alias for connection_type → sqlglot dialect mapping — used by router.py."""
    return _dialect_from_connection_type(connector_type)
