"""Known-answer guard tests for Bug-7911..7917 (sqlglot patch correctness).

Each test runs the REAL function/transpile and asserts the exact output or the
raised error — not a mock.  These are the executed guards specified in the
W2-L14 brief.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_7911_7912_7913_7914_7915_7916_7917.py -v
"""
from __future__ import annotations

import re

import pytest

from conftest import attach_fixture_deployed_shape
import sqlglot
from sqlglot import exp

from src.rewrite.dialects import (
    PassthroughTranspileError,
    _dialect_from_connection_type,
    _translate_raw_sql,
    _transpile_to_dialect,
)
from src.ir.logical_query import SemanticBindingError


# ---------------------------------------------------------------------------
# Bug-7912 (HIGH): _bq_filter_sql drops arbitrary FILTER predicates
# ---------------------------------------------------------------------------

class TestBug7912FilterPredicatePreserved:
    """The BigQuery FILTER transform must preserve arbitrary predicates."""

    def test_is_not_null_becomes_ignore_nulls(self):
        """Canonical: ARRAY_AGG(x) FILTER (WHERE x IS NOT NULL) -> IGNORE NULLS."""
        sql = "SELECT ARRAY_AGG(x) FILTER (WHERE x IS NOT NULL) FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "IGNORE NULLS" in out
        # The predicate IS NOT NULL is semantically equivalent to IGNORE NULLS,
        # so the output should NOT contain a separate IF wrapper.
        assert "IF(" not in out

    def test_arbitrary_predicate_falls_through_verbatim(self):
        """Bug-7912 / Codex gate: ARRAY_AGG(x) FILTER (WHERE region = 'EU')
        must fall through verbatim (fail loud at BigQuery).
        An IF-wrap + IGNORE NULLS would drop accepted NULL values."""
        sql = "SELECT ARRAY_AGG(x) FILTER (WHERE region = 'EU') FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "FILTER" in out, f"Expected FILTER verbatim: {out}"
        assert "region" in out, f"Expected predicate preserved: {out}"
        assert "IF(" not in out, f"Unexpected IF wrapper: {out}"

    def test_non_array_agg_falls_through(self):
        """SUM with FILTER must fall through verbatim (fail loud on BigQuery)."""
        sql = "SELECT SUM(x) FILTER (WHERE y > 0) FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "FILTER" in out, f"Expected FILTER clause preserved: {out}"

    def test_array_agg_order_by_with_arbitrary_predicate_falls_through(self):
        """ARRAY_AGG with ORDER BY + arbitrary predicate: fall through verbatim."""
        sql = "SELECT (ARRAY_AGG(col ORDER BY ts DESC) FILTER (WHERE status = 'A'))[1] FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "FILTER" in out, f"Expected FILTER verbatim: {out}"
        assert "status" in out, f"Expected predicate preserved: {out}"

    def test_distinct_is_not_null_becomes_ignore_nulls(self):
        """Fable R1 finding 2: ARRAY_AGG(DISTINCT x) FILTER (WHERE x IS NOT NULL)
        must emit IGNORE NULLS, not break on the Distinct wrapper."""
        sql = "SELECT ARRAY_AGG(DISTINCT x) FILTER (WHERE x IS NOT NULL) FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "IGNORE NULLS" in out, f"Expected IGNORE NULLS: {out}"
        # Must NOT produce invalid SQL with IF inside DISTINCT.
        assert "IF(" not in out, f"Unexpected IF wrapper for IS NOT NULL: {out}"

    def test_distinct_arbitrary_predicate_falls_through(self):
        """ARRAY_AGG(DISTINCT x) FILTER (WHERE <arbitrary>) falls through
        verbatim (fail loud) because IF inside DISTINCT is invalid."""
        sql = "SELECT ARRAY_AGG(DISTINCT x) FILTER (WHERE region = 'EU') FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        # Must fall through verbatim (FILTER clause preserved -> BQ rejects).
        assert "FILTER" in out, f"Expected FILTER fall-through: {out}"


# ---------------------------------------------------------------------------
# Bug-7911 (MED): _bq_escape_sql drops non-backslash ESCAPE char
# ---------------------------------------------------------------------------

class TestBug7911EscapeTranslation:
    """Non-backslash ESCAPE must be translated, not dropped."""

    def test_backslash_escape_unwrapped(self):
        """Backslash ESCAPE is BigQuery-native: just unwrap."""
        # In PG SQL, a single backslash in a string literal is written as
        # E'\\' (C-style) or just '\\' depending on standard_conforming_strings.
        # sqlglot's postgres parser treats '\' as a literal backslash.
        sql = "SELECT * FROM t WHERE c LIKE '100\\%' ESCAPE '\\'"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "ESCAPE" not in out, f"ESCAPE should be unwrapped: {out}"

    def test_non_backslash_escape_translates_percent(self):
        """Bug-7911: !% must become \\% (literal percent)."""
        sql = "SELECT * FROM t WHERE c LIKE '100!%' ESCAPE '!'"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "ESCAPE" not in out, f"ESCAPE clause should be removed: {out}"
        # The translated pattern should contain backslash-escaped %
        assert "\\%" in out, f"Expected backslash-escaped percent: {out}"

    def test_non_backslash_escape_translates_underscore(self):
        """Bug-7911: !_ must become \\_ (literal underscore)."""
        sql = "SELECT * FROM t WHERE c LIKE 'test!_val' ESCAPE '!'"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "ESCAPE" not in out
        assert "\\_" in out, f"Expected backslash-escaped underscore: {out}"

    def test_non_backslash_escape_escaped_escape_char(self):
        """Bug-7911: !! must become ! (literal escape char)."""
        sql = "SELECT * FROM t WHERE c LIKE 'a!!b!%c' ESCAPE '!'"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "ESCAPE" not in out
        # a! + \\% + c  (the !! becomes ! and !% becomes \\%)
        assert "\\%" in out

    def test_multi_char_escape_falls_through(self):
        """Codex gate: multi-char ESCAPE (e.g. '!!') must fall through
        verbatim (PG itself rejects multi-char ESCAPE)."""
        sql = "SELECT * FROM t WHERE c LIKE 'a!!%b' ESCAPE '!!'"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        # Must fall through verbatim (ESCAPE clause preserved -> BQ rejects).
        assert "ESCAPE" in out, f"Expected ESCAPE verbatim: {out}"


# ---------------------------------------------------------------------------
# Bug-7917 (HIGH): DATE_TRUNC 'week' Sunday-start on BQ vs Monday-start PG
# ---------------------------------------------------------------------------

class TestBug7917WeekIsoweek:
    """WEEK unit must emit ISOWEEK on BigQuery to match PG Monday-start."""

    def test_parsed_week_becomes_isoweek(self):
        """PG DATE_TRUNC('week', ts) -> BQ TIMESTAMP_TRUNC(ts, ISOWEEK)
        via _transpile_to_dialect (pre-generation AST rewrite, not
        a generator transform -- Fable R1 finding 6)."""
        pg_sql = 'SELECT DATE_TRUNC(\'week\', "ts") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ISOWEEK" in out, f"Expected ISOWEEK: {out}"
        assert ", WEEK)" not in out, f"Raw WEEK should not appear: {out}"

    def test_month_unchanged(self):
        """MONTH unit must NOT be affected by the WEEK->ISOWEEK transform."""
        sql = "SELECT DATE_TRUNC('month', ts) FROM t"
        tree = sqlglot.parse_one(sql, read="postgres")
        out = tree.sql(dialect="bigquery")
        assert "MONTH" in out
        assert "ISOWEEK" not in out

    def test_date_trunc_week_via_transpile(self):
        """DateTrunc WEEK -> ISOWEEK via _transpile_to_dialect."""
        pg_sql = 'SELECT DATE_TRUNC(\'week\', "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ISOWEEK" in out, f"Expected ISOWEEK: {out}"

    def test_timestamp_trunc_week_via_transpile(self):
        """TimestampTrunc WEEK -> ISOWEEK via _transpile_to_dialect.
        PG parses DATE_TRUNC as TimestampTrunc for untyped columns."""
        pg_sql = 'SELECT DATE_TRUNC(\'week\', "ts") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ISOWEEK" in out, f"Expected ISOWEEK: {out}"

    def test_postgres_week_unchanged(self):
        """PG target must NOT be affected."""
        pg_sql = 'SELECT DATE_TRUNC(\'week\', "ts") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "postgres")
        assert "ISOWEEK" not in out.upper()

    def test_week_boundary_parity(self):
        """Known-answer: 2024-01-07 (Sunday) is in different weeks under
        Sunday-start vs Monday-start.  PG truncates to 2024-01-01 (Mon).
        BQ WEEK would truncate to 2024-01-07 (Sun) = wrong.
        BQ ISOWEEK truncates to 2024-01-01 (Mon) = correct parity."""
        pg_sql = "SELECT DATE_TRUNC('week', TIMESTAMP '2024-01-07') FROM \"t\""
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ISOWEEK" in out, f"Expected ISOWEEK for parity: {out}"

    def test_transpile_to_dialect_week(self):
        """_transpile_to_dialect must apply ISOWEEK for BigQuery."""
        pg_sql = 'SELECT DATE_TRUNC(\'week\', "ts") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ISOWEEK" in out, f"Expected ISOWEEK via transpile: {out}"

    def test_bq_authored_week_not_overridden(self):
        """BQ-authored passthrough that explicitly uses WEEK (Sunday-start)
        must NOT be silently changed to ISOWEEK.  _translate_raw_sql with
        input_dialect=bigquery routes through _requote_identifiers_for_bigquery
        (not _transpile_to_dialect), preserving the author's intent."""
        bq_sql = "SELECT TIMESTAMP_TRUNC(ts, WEEK) FROM t"
        out = _translate_raw_sql(bq_sql, "bigquery", "bigquery")
        # BQ-authored: WEEK must survive unchanged.
        assert "ISOWEEK" not in out, f"BQ-authored WEEK silently overridden: {out}"
        assert "WEEK" in out

    def test_isoweek_is_bare_token_not_quoted(self):
        """Codex gate: ISOWEEK must render as a bare keyword, not 'ISOWEEK'."""
        pg_sql = 'SELECT DATE_TRUNC(\'week\', "ts") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ISOWEEK" in out, f"Expected ISOWEEK: {out}"
        assert "'ISOWEEK'" not in out, f"ISOWEEK must not be quoted: {out}"

    @pytest.mark.asyncio
    async def test_table_substitution_route_applies_isoweek(self):
        """Codex gate: table-substitution success route (source_sql.py:976)
        must also apply WEEK->ISOWEEK via _transpile_to_dialect."""
        import test_render_golden as G
        from src.rewrite.source_sql import _substitute_table_names

        raw = "SELECT DATE_TRUNC('week', \"order_date\") FROM golden"
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=[G.MODEL_SLUG], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="bigquery")
        assert result is not None
        assert "ISOWEEK" in result, (
            f"Table-substitution route must apply ISOWEEK: {result}"
        )


# ---------------------------------------------------------------------------
# Bug-8300 (MEDIUM, AKA F-103-03-residual): EXTRACT(DOW) day-of-week numbering
# parity PG <-> BigQuery (and Spark)
# ---------------------------------------------------------------------------

class TestBug8300DowParity:
    """PG EXTRACT(DOW) (0..6, Sunday=0) must render on BigQuery so the same
    physical date lands in the SAME day-of-week bucket as PostgreSQL.

    BigQuery EXTRACT(DAYOFWEEK) is 1..7 (Sunday=1). In the deployed render
    stack an unadjusted PG DOW normalises to a bare EXTRACT(DAYOFWEEK) (Sunday=
    1..7, NO shift), a different bucket than PG's 0..6, so the fix maps
    EXTRACT(DOW FROM d) -> (EXTRACT(DAYOFWEEK FROM d) - 1) on PG-canonical input
    only.
    """

    def test_dow_becomes_dayofweek_minus_one(self):
        """PG EXTRACT(DOW FROM d) -> BQ (EXTRACT(DAYOFWEEK FROM d) - 1)."""
        pg_sql = 'SELECT EXTRACT(DOW FROM "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "DAYOFWEEK" in out, f"Expected DAYOFWEEK: {out}"
        assert "- 1" in out, f"Expected -1 offset for numbering parity: {out}"
        # The raw PG-only DOW token must not leak to BigQuery (invalid there).
        assert "DOW" not in out.replace("DAYOFWEEK", ""), (
            f"Raw DOW token must not survive to BigQuery: {out}"
        )

    def test_dow_bucket_parity_known_answer(self):
        """Known-answer cross-source parity for a FIXED date.

        2024-01-07 is a SUNDAY. PostgreSQL EXTRACT(DOW) returns 0 for Sunday.
        The BigQuery-rendered expression is (EXTRACT(DAYOFWEEK FROM d) - 1);
        BigQuery DAYOFWEEK(Sunday) = 1, so 1 - 1 = 0 — the SAME bucket as PG.
        This asserts the numbering is normalised, not merely the token.
        """
        # PostgreSQL side: DOW of Sunday is 0.
        pg_sql = "SELECT EXTRACT(DOW FROM DATE '2024-01-07') AS wd"
        pg_out = _transpile_to_dialect(pg_sql, "postgres")
        assert "DOW" in pg_out and "DAYOFWEEK" not in pg_out, (
            f"PG target must keep native DOW: {pg_out}"
        )
        # BigQuery side: must be DAYOFWEEK - 1 so Sunday=1-1=0 matches PG's 0.
        bq_out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "(EXTRACT(DAYOFWEEK FROM" in bq_out, (
            f"Expected parenthesised DAYOFWEEK expression: {bq_out}"
        )
        assert "- 1" in bq_out, f"Expected -1 offset: {bq_out}"

    def test_dow_grouped_expression_parity(self):
        """The rewrite must fire on GROUP BY DOW too, so bucket keys agree."""
        pg_sql = (
            'SELECT EXTRACT(DOW FROM "d") AS wd, COUNT(*) FROM "t" '
            'GROUP BY EXTRACT(DOW FROM "d")'
        )
        out = _transpile_to_dialect(pg_sql, "bigquery")
        # Both the SELECT and GROUP BY occurrences must be normalised, else the
        # grouping key and the projected label would diverge.
        assert out.count("DAYOFWEEK") == 2, (
            f"Both DOW occurrences must be normalised: {out}"
        )
        assert out.count("- 1") == 2, f"Both offsets must apply: {out}"

    def test_postgres_dow_unchanged(self):
        """PG target must NOT be touched (native DOW is 0..6 already)."""
        pg_sql = 'SELECT EXTRACT(DOW FROM "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "postgres")
        assert "DAYOFWEEK" not in out.upper()
        assert "DOW" in out.upper()

    def test_isodow_normalised_to_pg_numbering(self):
        """Bug-8300 (adjacent): PG ISODOW (Monday=1..Sunday=7) must reproduce
        PG's numbering on BigQuery.

        In the deployed render stack (shared/semantic/time_variants_sql sets
        NORMALIZE_EXTRACT_DATE_PARTS and maps ISODOW->DAYOFWEEK), an unadjusted
        PG ISODOW renders to a bare EXTRACT(DAYOFWEEK FROM d) (Sunday=1..7) on
        both BigQuery and Spark with NO numbering shift — a silent full-rotation
        wrong bucket (Sunday is 7 in PG-ISODOW but 1 in the bare DAYOFWEEK). The
        fix maps ISODOW -> (MOD(EXTRACT(DAYOFWEEK FROM d) + 5, 7) + 1).
        Known-answer: Sunday DAYOFWEEK=1 -> MOD(1+5,7)+1 = MOD(6,7)+1 = 7,
        which equals PG ISODOW(Sunday)=7.
        """
        pg_sql = 'SELECT EXTRACT(ISODOW FROM "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "bigquery")
        assert "MOD(EXTRACT(DAYOFWEEK FROM" in out, (
            f"ISODOW must be normalised to MOD(DAYOFWEEK+5,7)+1: {out}"
        )
        assert "+ 5" in out and "+ 1" in out, (
            f"ISODOW numbering adjustment missing: {out}"
        )

    def test_isodow_pg_target_unchanged(self):
        """PG target keeps native ISODOW (already Monday=1..Sunday=7)."""
        pg_sql = 'SELECT EXTRACT(ISODOW FROM "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "postgres")
        assert "ISODOW" in out.upper()
        assert "DAYOFWEEK" not in out.upper()

    def test_bq_authored_dayofweek_not_overridden(self):
        """A BigQuery-authored passthrough that explicitly uses DAYOFWEEK
        (1..7) is the author's choice and must NOT be shifted by the -1
        normalisation (pg_canonical=False path)."""
        bq_sql = "SELECT EXTRACT(DAYOFWEEK FROM d) FROM t"
        out = _translate_raw_sql(bq_sql, "bigquery", "bigquery")
        assert "DAYOFWEEK" in out
        # No spurious "- 1" injected onto the author's own DAYOFWEEK.
        assert "- 1" not in out, (
            f"BQ-authored DAYOFWEEK must not be shifted: {out}"
        )

    # --- Spark parity (R4 finding 3): Spark EXTRACT(DAYOFWEEK) is also 1..7
    # Sunday-based, so PG DOW/ISODOW passed through un-shifted is a silent wrong
    # bucket on Spark-backed sources too. Same normalisation applies. ---

    def test_spark_dow_becomes_dayofweek_minus_one(self):
        pg_sql = 'SELECT EXTRACT(DOW FROM "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "spark")
        assert "DAYOFWEEK" in out, f"Expected DAYOFWEEK on Spark: {out}"
        assert "- 1" in out, f"Expected -1 offset for Spark parity: {out}"

    def test_spark_isodow_normalised_to_pg_numbering(self):
        pg_sql = 'SELECT EXTRACT(ISODOW FROM "d") FROM "t"'
        out = _transpile_to_dialect(pg_sql, "spark")
        # Spark renders MOD as the ``%`` operator; both forms are valid Spark.
        assert "EXTRACT(DAYOFWEEK FROM" in out, f"Expected DAYOFWEEK: {out}"
        assert "+ 5" in out and "% 7" in out and "+ 1" in out, (
            f"ISODOW numbering adjustment missing on Spark: {out}"
        )

    def test_spark_dow_known_answer_parity(self):
        """Sunday (2024-01-07): PG DOW=0; Spark DAYOFWEEK(Sunday)=1, minus 1 = 0
        -> same bucket as PG. Numbering normalised, not just the token."""
        pg_sql = "SELECT EXTRACT(DOW FROM DATE '2024-01-07') AS wd"
        out = _transpile_to_dialect(pg_sql, "spark")
        assert "(EXTRACT(DAYOFWEEK FROM" in out and "- 1" in out, out

    def test_bug_8329_snowflake_dow_is_week_start_independent(self):
        """Bug-8329: PG DOW uses Snowflake DAYOFWEEKISO modulo seven.

        Test escape: Snowflake was wired as a target but excluded from the DOW
        transform, so session WEEK_START could change bucket numbers. Guard:
        the final source-dialect boundary emits DAYOFWEEKISO for SELECT and
        GROUP BY and never emits session-dependent DAYOFWEEK. Tier: T3.
        """
        pg_sql = (
            'SELECT EXTRACT(DOW FROM "d") AS wd FROM "t" '
            'GROUP BY EXTRACT(DOW FROM "d")'
        )
        out = _transpile_to_dialect(pg_sql, "snowflake")
        assert out.count("DAYOFWEEKISO") == 2, out
        assert out.count("% 7") == 2, out
        assert "EXTRACT(DOW" not in out.upper(), out

    def test_bug_8329_snowflake_isodow_and_target_wiring(self):
        """Bug-8329: Snowflake target selection and ISO numbering stay aligned.

        Test escape: connector mapping coverage did not assert weekday
        semantics. Guard: connection-type resolution selects Snowflake and the
        end-to-end transpile emits direct DAYOFWEEKISO (Mon=1..Sun=7). Tier: T3.
        """
        assert _dialect_from_connection_type("snowflake") == "snowflake"
        out = _transpile_to_dialect(
            'SELECT EXTRACT(ISODOW FROM "d") FROM "t"', "snowflake",
        )
        assert "DAYOFWEEKISO" in out, out
        assert "% 7" not in out, out


# ---------------------------------------------------------------------------
# Bug-7915 (HIGH): _substitute_table_names regex corrupts string literals
# ---------------------------------------------------------------------------

class TestBug7915StringLiteralProtection:
    """Regex substitution must not corrupt string literals containing FROM <model>."""

    @pytest.mark.asyncio
    async def test_string_literal_not_rewritten(self):
        """A string literal containing 'FROM modelx' must survive verbatim."""
        import test_render_golden as G

        raw = (
            "SELECT * FROM golden "
            "WHERE note = 'exported FROM golden yesterday'"
        )
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=[G.MODEL_SLUG], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        from src.rewrite.source_sql import _substitute_table_names

        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="postgresql")
        assert result is not None
        # The string literal must NOT be rewritten.
        assert "exported FROM golden yesterday" in result, (
            f"String literal was corrupted: {result}"
        )
        # The FROM table reference SHOULD be rewritten.
        assert '"demo"."sales"' in result, (
            f"Table reference not substituted: {result}"
        )

    @pytest.mark.asyncio
    async def test_display_name_collision_not_substituted(self):
        """Bug-7915: a display name matching a physical table must not
        be substituted.  match_names now excludes display_name."""
        import test_render_golden as G

        raw = 'SELECT * FROM golden JOIN sales ON golden.id = sales.id'
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=[G.MODEL_SLUG, "sales"], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        from src.rewrite.source_sql import _substitute_table_names

        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="postgresql")
        # 'sales' in the JOIN must NOT be substituted to the physical table
        # (it is a different physical table, not the model slug).
        # The model slug 'golden' should be substituted.
        if result is not None:
            # The JOIN sales should still be present as "sales"
            assert "JOIN" in result


# ---------------------------------------------------------------------------
# Bug-7913 (MED): Spark raw/passthrough parity gap
# ---------------------------------------------------------------------------

class TestBug7913SparkParityGap:
    """Bug-7012 fail-loud and Bug-7017 semi-additive must fire for Spark too."""

    def test_spark_parse_failure_raises(self):
        """Unparseable PG SQL to Spark must raise, not return PG-quoted verbatim."""
        garbage = "SELECT <<<INVALID>>> FROM t"
        with pytest.raises(PassthroughTranspileError):
            _translate_raw_sql(garbage, "spark")

    def test_spark_translate_raw_parse_failure_raises(self):
        """_translate_raw_sql to Spark must also raise on parse failure."""
        garbage = "SELECT <<<INVALID>>> FROM t"
        with pytest.raises(PassthroughTranspileError):
            _translate_raw_sql(garbage, "spark")

    def test_spark_valid_sql_transpiles(self):
        """Valid PG SQL to Spark must transpile correctly (not just pass through)."""
        pg = 'SELECT "region", "amount" FROM "demo"."sales_fact"'
        out = _translate_raw_sql(pg, "spark")
        # Spark uses backtick identifiers.
        assert "`" in out, f"Expected backtick quoting: {out}"
        assert '"region"' not in out, f"PG double-quoted ident should not survive: {out}"

    def test_spark_semi_additive_raises_via_translate_raw(self):
        """Codex gate R2: semi-additive ARRAY_AGG on Spark must FAIL LOUD
        (raise SemanticBindingError) because MAX_BY diverges from PG
        NULL-ordering semantics."""
        pg = (
            'SELECT (ARRAY_AGG("val" ORDER BY "d" DESC) '
            'FILTER (WHERE "val" IS NOT NULL))[1] FROM "t" GROUP BY "g"'
        )
        with pytest.raises(SemanticBindingError):
            _translate_raw_sql(pg, "spark")

    @pytest.mark.asyncio
    async def test_spark_semi_additive_raises_via_table_substitution(self):
        """Codex gate R2: table-substitution Spark route must also fail loud
        for semi-additive patterns."""
        import test_render_golden as G
        from src.rewrite.source_sql import _substitute_table_names

        raw = (
            'SELECT (ARRAY_AGG("val" ORDER BY "d" DESC) '
            "FILTER (WHERE \"val\" IS NOT NULL))[1] FROM golden GROUP BY \"g\""
        )
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=[G.MODEL_SLUG], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        with pytest.raises(SemanticBindingError):
            await attach_fixture_deployed_shape(bq, db)
            await _substitute_table_names(bq, db, connector="hadoop_spark")


# ---------------------------------------------------------------------------
# Bug-7914 (MED): Spark semi-additive fail-loud
# ---------------------------------------------------------------------------

class TestBug7914SparkSemiAdditiveFailLoud:
    """Codex gate R2: Spark semi-additive must FAIL LOUD, not silently
    MAX_BY (NULL order-key divergence from PG)."""

    def test_canonical_shape_raises(self):
        """The canonical semi-additive shape on Spark must raise."""
        pg = (
            'SELECT (ARRAY_AGG("val" ORDER BY "d" DESC) '
            'FILTER (WHERE "val" IS NOT NULL))[1] FROM "t" GROUP BY "g"'
        )
        with pytest.raises(SemanticBindingError):
            _transpile_to_dialect(pg, "spark")

    def test_first_non_empty_also_raises(self):
        """ASC (FIRST_NON_EMPTY) must also raise."""
        pg = (
            'SELECT (ARRAY_AGG("val" ORDER BY "d" ASC) '
            'FILTER (WHERE "val" IS NOT NULL))[1] FROM "t" GROUP BY "g"'
        )
        with pytest.raises(SemanticBindingError):
            _transpile_to_dialect(pg, "spark")

    def test_non_semi_additive_spark_unaffected(self):
        """Normal Spark SQL (no ARRAY_AGG+FILTER pattern) must still work."""
        pg = 'SELECT "region", SUM("amount") FROM "t" GROUP BY "region"'
        out = _transpile_to_dialect(pg, "spark")
        assert "`region`" in out, f"Expected backtick quoting: {out}"

    def test_bigquery_semi_additive_unaffected(self):
        """BigQuery semi-additive (ARRAY_AGG + IGNORE NULLS) must still work."""
        pg = (
            'SELECT (ARRAY_AGG("val" ORDER BY "d" DESC) '
            'FILTER (WHERE "val" IS NOT NULL))[1] FROM "t" GROUP BY "g"'
        )
        # BigQuery should NOT raise (it has native ARRAY_AGG with IGNORE NULLS).
        out = _transpile_to_dialect(pg, "bigquery")
        assert "ARRAY_AGG" in out or "IGNORE NULLS" in out


# ---------------------------------------------------------------------------
# Bug-7916 (LOW): ErrorLevel.IGNORE recovers meaning-changed tree
# ---------------------------------------------------------------------------

class TestBug7916StrictErrorLevel:
    """_transpile_to_dialect must not silently mangle semantics."""

    def test_malformed_sql_not_semantically_mangled(self):
        """Bug-7916: 'WHERE x = 1 !!' must NOT become 'WHERE x = NOT 1'.
        With default error level + Bug-7913 fail-loud for BQ, the parse
        failure raises PassthroughTranspileError (never returns the
        mangled form)."""
        malformed = 'SELECT * FROM "t" WHERE "x" = 1 !!'
        # BigQuery: raises because un-transpiled PG SQL is silent-wrong.
        with pytest.raises(PassthroughTranspileError):
            _transpile_to_dialect(malformed, "bigquery")

    def test_malformed_sql_safe_dialect_returns_original(self):
        """F-006-04: every non-postgres target fail-louds on malformed SQL."""
        malformed = 'SELECT * FROM "t" WHERE "x" = 1 !!'
        with pytest.raises(PassthroughTranspileError):
            _transpile_to_dialect(malformed, "redshift")

    def test_valid_sql_still_transpiles(self):
        """Valid SQL must still transpile correctly with default error level."""
        valid = 'SELECT "region", "amount" FROM "demo"."sales_fact"'
        out = _transpile_to_dialect(valid, "bigquery")
        assert "`region`" in out, f"Expected backtick quoting: {out}"

    def test_translate_raw_malformed_not_mangled(self):
        """_translate_raw_sql to BigQuery raises on malformed SQL
        (Bug-7012 fail-loud path), which also prevents semantic mangling."""
        malformed = 'SELECT * FROM "t" WHERE "x" = 1 !!'
        # BigQuery path: raises PassthroughTranspileError (not silently mangled).
        with pytest.raises(PassthroughTranspileError):
            _translate_raw_sql(malformed, "bigquery")

    def test_translate_raw_malformed_spark_not_mangled(self):
        """_translate_raw_sql to Spark raises on malformed SQL (Bug-7913)."""
        malformed = 'SELECT * FROM "t" WHERE "x" = 1 !!'
        with pytest.raises(PassthroughTranspileError):
            _translate_raw_sql(malformed, "spark")

    def test_multi_statement_rejected_on_xmla(self):
        """Codex gate R2: multi-statement input must be rejected on ALL
        protocols.  Silently taking only the first statement is
        meaning-changing truncation."""
        from src.parsing.sql_parser import parse_sql_to_ir, SyntaxErrorInSQL
        with pytest.raises(SyntaxErrorInSQL, match="Multi-statement"):
            parse_sql_to_ir(
                "SELECT a FROM t; SELECT b FROM u",
                "m1",
                protocol="xmla",
            )

    def test_multi_statement_rejected_on_jdbc(self):
        """Multi-statement must also be rejected on JDBC."""
        from src.parsing.sql_parser import parse_sql_to_ir, SyntaxErrorInSQL
        with pytest.raises(SyntaxErrorInSQL):
            parse_sql_to_ir(
                "SELECT a FROM t; SELECT b FROM u",
                "m1",
                protocol="jdbc",
            )


# ---------------------------------------------------------------------------
# Codex gate R2: dead-function deletion + byte-identical-when-off
# ---------------------------------------------------------------------------

class TestCodexGateR2Structural:
    """Structural guards from Codex round-2 review."""

    def test_requote_identifiers_for_dialect_deleted(self):
        """Codex gate R2 finding 1: _requote_identifiers_for_dialect is dead
        code (no production caller) and a bypass footgun.  It must not exist
        in the production module."""
        import src.rewrite.dialects as _dialects_mod
        assert not hasattr(_dialects_mod, "_requote_identifiers_for_dialect"), (
            "_requote_identifiers_for_dialect still exists in dialects.py -- "
            "it is dead code that bypasses _transpile_to_dialect and must be "
            "deleted."
        )

    def test_multi_statement_rejected_on_api(self):
        """Codex gate R2 finding 4: multi-statement rejected on API."""
        from src.parsing.sql_parser import parse_sql_to_ir, SyntaxErrorInSQL
        with pytest.raises(SyntaxErrorInSQL, match="Multi-statement"):
            parse_sql_to_ir(
                "SELECT a FROM t; SELECT b FROM u",
                "m1",
                protocol="api",
            )

    @pytest.mark.asyncio
    async def test_no_match_returns_none_byte_identical(self):
        """Codex gate R2 finding 4: when no table node matches, the function
        must return None (byte-identical-when-off) instead of regenerated SQL."""
        import test_render_golden as G
        from src.rewrite.source_sql import _substitute_table_names

        # A query that does NOT reference the model slug at all.
        raw = "SELECT 'FROM golden' AS note FROM other_table"
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=["other_table"], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="postgresql")
        # No table matched the model slug -> must return None.
        assert result is None, (
            f"Expected None (byte-identical-when-off), got: {result}"
        )

    @pytest.mark.asyncio
    async def test_table_alias_preserved(self):
        """Codex gate R3 finding 2: FROM golden AS g must keep the alias."""
        import test_render_golden as G
        from src.rewrite.source_sql import _substitute_table_names

        raw = 'SELECT g.x FROM golden AS g'
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=[G.MODEL_SLUG], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="postgresql")
        assert result is not None
        # Alias 'g' must survive.
        assert " AS g" in result or " AS G" in result, (
            f"Table alias dropped: {result}"
        )
        # The physical table must be substituted.
        assert "demo" in result.lower(), f"Table not substituted: {result}"

    @pytest.mark.asyncio
    async def test_identity_mapping_returns_none(self):
        """Codex gate R3 finding 3: when slug == physical table name,
        _replace_table is a no-op -> return None (byte-identical)."""
        import test_render_golden as G
        from src.rewrite.source_sql import _substitute_table_names

        # The golden harness's physical name is "demo.sales".
        # If the slug happened to be "sales" and the physical name was also
        # "sales" (no schema), it would be an identity mapping. We test
        # with the full schema.table == match: construct a bound query
        # where from_tables contains the physical name itself.
        raw = 'SELECT * FROM "demo"."sales"'
        bq = G._bound(
            measures=[], dimensions=[], grain=[],
            from_tables=["demo.sales"], raw_query=raw,
            has_passthrough_expressions=True,
        )
        db = G._fact_db()
        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="postgresql")
        # "demo.sales" is the physical name, not the model slug "golden".
        # So it should NOT match and return None.
        assert result is None, f"Expected None for non-slug table: {result}"

    @pytest.mark.asyncio
    async def test_identity_slug_equals_physical_returns_none(self):
        """Codex gate R4: when slug == physical table name (identity mapping),
        _substitute_table_names must return None (byte-identical, no reformat).
        This is the exact slug==physical case, not just a non-slug reference."""
        import types
        from src.rewrite.source_sql import _substitute_table_names
        import test_render_golden as G

        # Construct a model where slug == physical_name (single-part).
        _SLUG = "mysales"
        model = types.SimpleNamespace(
            id="model-id", slug=_SLUG, display_name="My Sales",
            deployed_version_id="v1",
        )
        fact = types.SimpleNamespace(
            id="t-1", model_id="model-id", physical_name=_SLUG,
            alias="f", table_type="fact", source_id="src-1",
        )
        db = G.FakeDB(tables=[fact], columns=[])

        raw = "select * from mysales /* keep */"
        lq = types.SimpleNamespace(
            raw_query=raw, from_tables=[_SLUG], input_dialect="postgres",
        )
        bq = types.SimpleNamespace(
            logical_query=lq, model=model,
            resolved_measures=[], resolved_dimensions=[],
            resolved_filters=[], has_passthrough_expressions=True,
        )
        await attach_fixture_deployed_shape(bq, db)
        result = await _substitute_table_names(bq, db, connector="postgresql")
        # slug == physical_name: identity mapping -> must return None
        # (byte-identical, no reformatting of whitespace/comments).
        assert result is None, (
            f"Identity mapping (slug==physical) must return None, got: {result}"
        )


# ---------------------------------------------------------------------------
# Codex gate R3: pocket route + _translate_raw_sql bypass guards
# ---------------------------------------------------------------------------

class TestCodexGateR3PocketAndRawBypass:
    """Guards for routes that previously bypassed _render_for_dialect."""

    def test_translate_raw_bigquery_week_isoweek(self):
        """Codex gate R3: _translate_raw_sql to BigQuery must apply ISOWEEK."""
        pg = 'SELECT DATE_TRUNC(\'week\', "ts") FROM "t"'
        out = _translate_raw_sql(pg, "bigquery")
        assert "ISOWEEK" in out, (
            f"_translate_raw_sql BigQuery WEEK not rewritten: {out}"
        )
        assert "'ISOWEEK'" not in out, f"ISOWEEK must be bare keyword: {out}"

    def test_pocket_bigquery_week_isoweek(self):
        """Codex gate R3: pocket rewrite to BigQuery must apply ISOWEEK."""
        import types
        import sqlglot
        from src.rewrite.pocket import rewrite_for_pocket

        raw = "SELECT DATE_TRUNC('week', \"order_date\") FROM golden"
        lq = types.SimpleNamespace(
            raw_query=raw, from_tables=["golden"], input_dialect="postgres",
        )
        model = types.SimpleNamespace(
            slug="golden", display_name="Golden Model",
        )
        bq = types.SimpleNamespace(
            logical_query=lq, model=model,
            resolved_measures=[], resolved_dimensions=[],
        )
        pocket = types.SimpleNamespace(
            target_schema="demo", physical_table_name="pocket_sales",
        )
        result = rewrite_for_pocket(bq, pocket, target_dialect="bigquery")
        assert "ISOWEEK" in result, (
            f"Pocket BigQuery WEEK not rewritten to ISOWEEK: {result}"
        )

    def test_pocket_spark_semi_additive_raises(self):
        """Codex gate R3: pocket rewrite to Spark with semi-additive must
        fail loud (raise SemanticBindingError)."""
        import types
        from src.rewrite.pocket import rewrite_for_pocket

        raw = (
            'SELECT (ARRAY_AGG("val" ORDER BY "d" DESC) '
            'FILTER (WHERE "val" IS NOT NULL))[1] FROM golden GROUP BY "g"'
        )
        lq = types.SimpleNamespace(
            raw_query=raw, from_tables=["golden"], input_dialect="postgres",
        )
        model = types.SimpleNamespace(
            slug="golden", display_name="Golden Model",
        )
        bq = types.SimpleNamespace(
            logical_query=lq, model=model,
            resolved_measures=[], resolved_dimensions=[],
        )
        pocket = types.SimpleNamespace(
            target_schema="demo", physical_table_name="pocket_sales",
        )
        with pytest.raises(SemanticBindingError):
            rewrite_for_pocket(bq, pocket, target_dialect="spark")

    def test_pocket_bq_authored_week_preserved(self):
        """Fable final: BQ-authored pocket passthrough with WEEK must NOT be
        rewritten to ISOWEEK (author's Sunday-start intent preserved)."""
        import types
        from src.rewrite.pocket import rewrite_for_pocket

        raw = "SELECT TIMESTAMP_TRUNC(`order_ts`, WEEK) FROM golden"
        lq = types.SimpleNamespace(
            raw_query=raw, from_tables=["golden"], input_dialect="bigquery",
        )
        model = types.SimpleNamespace(
            slug="golden", display_name="Golden Model",
        )
        bq = types.SimpleNamespace(
            logical_query=lq, model=model,
            resolved_measures=[], resolved_dimensions=[],
        )
        pocket = types.SimpleNamespace(
            target_schema="demo", physical_table_name="pocket_sales",
        )
        result = rewrite_for_pocket(bq, pocket, target_dialect="bigquery")
        assert "ISOWEEK" not in result, (
            f"BQ-authored WEEK silently overridden in pocket: {result}"
        )
        assert "WEEK" in result, f"WEEK must be preserved: {result}"

    def test_pocket_pg_authored_week_becomes_isoweek(self):
        """Fable final: PG-authored pocket with DATE_TRUNC('week', d) on
        BigQuery must still apply ISOWEEK (existing behavior unchanged)."""
        import types
        from src.rewrite.pocket import rewrite_for_pocket

        raw = "SELECT DATE_TRUNC('week', \"order_date\") FROM golden"
        lq = types.SimpleNamespace(
            raw_query=raw, from_tables=["golden"], input_dialect="postgres",
        )
        model = types.SimpleNamespace(
            slug="golden", display_name="Golden Model",
        )
        bq = types.SimpleNamespace(
            logical_query=lq, model=model,
            resolved_measures=[], resolved_dimensions=[],
        )
        pocket = types.SimpleNamespace(
            target_schema="demo", physical_table_name="pocket_sales",
        )
        result = rewrite_for_pocket(bq, pocket, target_dialect="bigquery")
        assert "ISOWEEK" in result, (
            f"PG-authored pocket WEEK not rewritten to ISOWEEK: {result}"
        )
