"""Query-router security lane (QR-2) regression guards.

Covers the pure/unit-testable fixes in the CLS/RLS + rewrite + drill batch:

  * Bug-6133 — RLS role-predicate values escape per TARGET dialect (sqlglot),
    not ANSI-only, so ``O'Brien`` is not corrupted on BigQuery/Spark.
  * Bug-6383 — contains/notContains emit a portable ``ESCAPE`` clause that
    survives sqlglot transpilation to every dialect; raw ``like`` unchanged.
  * Bug-6123 — the WHERE renderer types numeric literals when a column type
    is supplied (bare token vs quoted string).
  * Bug-6382 — project-id match is UUID/case-insensitive (no false 403).
  * Bug-6273 — drill override_agg is validated against a supported allow-list.
  * Bug-6229 — count_distinct + cumulation family is caught as ineligible.
"""
import pytest
import sqlglot

from shared.security.predicate_compiler import _compile_dsl_expression
from shared.schemas.measure_formats import (
    SEMI_ADDITIVE_INELIGIBLE_FAMILIES,
    TIME_VARIANT_FAMILY,
)

from src.api.filter_contract import (
    SemanticFilter,
    build_logical_filters,
    project_ids_match,
)
from src.rewrite.conditions import _render_condition
from src.drill.semantic_builder import DrillSemanticError, _resolve_drill_agg


# ---------------------------------------------------------------------------
# Bug-6133 — RLS predicate value escaping is per-dialect
# ---------------------------------------------------------------------------

class TestRlsPredicateDialectEscaping:
    def test_ansi_dialects_double_the_quote(self):
        for connector in ("postgresql", "sqlserver"):
            out = _compile_dsl_expression(
                "dimension_equals('region.code', 'O''Brien')", connector,
            )
            assert "O''Brien" in out, (connector, out)

    def test_bigquery_backslash_escapes_the_quote(self):
        out = _compile_dsl_expression(
            "dimension_equals('region.code', 'O''Brien')", "bigquery",
        )
        # BigQuery/Spark escape an embedded quote with a backslash, NOT by
        # doubling it — the doubled form would be re-parsed as 'O' + 'Brien'.
        assert "O\\'Brien" in out
        assert "O''Brien" not in out

    def test_spark_backslash_escapes_the_quote(self):
        out = _compile_dsl_expression(
            "in('region.code', 'a''b', 'c')", "hadoop_spark",
        )
        assert "a\\'b" in out
        assert "a''b" not in out

    def test_predicate_reparses_to_a_single_literal_on_bigquery(self):
        # The corruption manifests as the RLS value splitting into two tokens
        # when the target engine reads the emitted SQL. Round-trip through the
        # target dialect and assert the literal survives intact.
        out = _compile_dsl_expression(
            "dimension_equals('region.code', 'O''Brien')", "bigquery",
        )
        parsed = sqlglot.parse_one(f"SELECT 1 WHERE {out}", read="bigquery")
        literals = [
            l.this for l in parsed.find_all(sqlglot.exp.Literal) if l.is_string
        ]
        assert "O'Brien" in literals


# ---------------------------------------------------------------------------
# Bug-6383 — contains/notContains emit a portable ESCAPE clause
# ---------------------------------------------------------------------------

class TestLikeEscapeClause:
    def _one(self, raw_op: str) -> object:
        [lf] = build_logical_filters(
            [SemanticFilter(dimension="name", operator=raw_op, value="10%_x")]
        )
        return lf

    def test_contains_sets_like_escape_and_escapes_wildcards(self):
        lf = self._one("contains")
        assert lf.operator == "like"
        assert lf.like_escape == "\\"
        assert lf.value == "%10\\%\\_x%"

    def test_not_contains_sets_like_escape(self):
        lf = self._one("notContains")
        assert lf.operator == "not_like"
        assert lf.like_escape == "\\"

    def test_render_contains_emits_escape_clause(self):
        lf = self._one("contains")
        sql = _render_condition('"name"', lf.operator, lf.value, like_escape=lf.like_escape)
        assert sql == "\"name\" LIKE '%10\\%\\_x%' ESCAPE '\\'"

    def test_raw_like_has_no_escape_clause(self):
        [lf] = build_logical_filters(
            [SemanticFilter(dimension="name", operator="like", value="E%")]
        )
        assert lf.like_escape is None
        sql = _render_condition('"name"', lf.operator, lf.value, like_escape=lf.like_escape)
        assert sql == "\"name\" LIKE 'E%'"
        assert "ESCAPE" not in sql

    # Bug-6383 (Fable deep-review): ESCAPE is dialect-CORRECT, not universal.
    # ``LIKE ... ESCAPE`` is valid on Postgres/SQL Server/Spark/Snowflake but is
    # INVALID GoogleSQL — BigQuery's LIKE has no ESCAPE clause and treats
    # backslash as the native pattern escape, so BigQuery must NOT emit ESCAPE.
    # Importing dialects registers the BigQuery generator patch that drops it.
    @pytest.mark.parametrize("dialect", ["postgres", "spark", "tsql", "snowflake"])
    def test_escape_clause_survives_transpile_on_supporting_dialects(self, dialect):
        from src.rewrite.dialects import _transpile_to_dialect
        lf = self._one("contains")
        sql = _render_condition('"name"', lf.operator, lf.value, like_escape=lf.like_escape)
        out = _transpile_to_dialect(f"SELECT 1 WHERE {sql}", dialect)
        assert "ESCAPE" in out.upper()
        node = sqlglot.parse_one(out, read=dialect)
        esc = node.find(sqlglot.exp.Escape)
        assert esc is not None
        assert esc.expression.this  # non-empty escape character

    def test_bigquery_omits_escape_clause(self):
        from src.rewrite.dialects import _transpile_to_dialect
        lf = self._one("contains")
        sql = _render_condition('"name"', lf.operator, lf.value, like_escape=lf.like_escape)
        out = _transpile_to_dialect(f"SELECT 1 WHERE {sql}", "bigquery")
        assert "ESCAPE" not in out.upper(), f"BigQuery must not emit ESCAPE: {out}"
        node = sqlglot.parse_one(out, read="bigquery")
        assert node.find(sqlglot.exp.Like) is not None
        assert node.find(sqlglot.exp.Escape) is None


# ---------------------------------------------------------------------------
# Bug-6123 — numeric-literal typing when a column type is supplied
# ---------------------------------------------------------------------------

class TestNumericLiteralTyping:
    def test_numeric_column_renders_bare_token(self):
        assert _render_condition('"qty"', "eq", "100", "INTEGER") == '"qty" = 100'

    def test_text_column_quotes_value(self):
        assert _render_condition('"region"', "eq", "US", "TEXT") == "\"region\" = 'US'"

    def test_untyped_still_quotes_a_string_value(self):
        # Without a column type the renderer cannot know it is numeric — this is
        # exactly the bypass the wiring fix closes upstream by SUPPLYING the type.
        assert _render_condition('"qty"', "eq", "100", None) == "\"qty\" = '100'"


# ---------------------------------------------------------------------------
# Bug-6382 — project-id match is UUID/case-insensitive
# ---------------------------------------------------------------------------

class TestProjectIdsMatch:
    def test_case_variant_uuid_matches(self):
        u = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"
        assert project_ids_match(u.lower(), u.upper()) is True

    def test_braced_uuid_matches(self):
        u = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        assert project_ids_match(u, "{" + u + "}") is True

    def test_different_uuids_do_not_match(self):
        assert project_ids_match(
            "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            "00000000-0000-0000-0000-000000000000",
        ) is False

    def test_none_or_empty_never_matches(self):
        assert project_ids_match(None, "x") is False
        assert project_ids_match("", "") is False
        assert project_ids_match("abc", "  ") is False

    def test_non_uuid_falls_back_to_casefold(self):
        assert project_ids_match("Proj-A", "proj-a") is True


# ---------------------------------------------------------------------------
# Bug-6273 — drill override_agg allow-list
# ---------------------------------------------------------------------------

class TestDrillOverrideAgg:
    def test_none_uses_default(self):
        assert _resolve_drill_agg(None, "sum") == "SUM"
        assert _resolve_drill_agg("", "avg") == "AVG"
        assert _resolve_drill_agg(None, None) == "SUM"

    def test_supported_override_wins(self):
        assert _resolve_drill_agg("avg", "sum") == "AVG"
        assert _resolve_drill_agg("count_distinct", "sum") == "COUNT_DISTINCT"

    def test_unsupported_override_rejected(self):
        with pytest.raises(DrillSemanticError):
            _resolve_drill_agg("sum(x); drop table", "sum")
        with pytest.raises(DrillSemanticError):
            _resolve_drill_agg("median", "sum")


# ---------------------------------------------------------------------------
# Bug-6229 — count_distinct + cumulation family is ineligible
# ---------------------------------------------------------------------------

class TestCountDistinctCumulationFamilies:
    def test_period_to_date_and_moving_window_are_ineligible(self):
        # These are exactly the families whose window rendering SUMs per-period
        # base values — non-additive for count_distinct.
        assert "period_to_date" in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
        assert "moving_window" in SEMI_ADDITIVE_INELIGIBLE_FAMILIES

    def test_ytd_maps_to_ineligible_family(self):
        assert TIME_VARIANT_FAMILY["ytd"] in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
        assert TIME_VARIANT_FAMILY["trailing_n"] in SEMI_ADDITIVE_INELIGIBLE_FAMILIES

    def test_lag_and_prior_period_stay_eligible(self):
        # Parallel-period / lag reference ONE prior period's distinct count,
        # which is correct — they must NOT be blocked.
        assert TIME_VARIANT_FAMILY["lag"] not in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
        assert TIME_VARIANT_FAMILY["prior_year"] not in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
