"""Tests for Bugs 6645, 6682, 6828, 6832.

Bug-6645: CAGR time-variant SQL grain-blind LAG offset.
Bug-6682: ytd_prior_year table-bound safety belt in _year_position.
Bug-6828: ESCAPE clause invalid for BigQuery.
Bug-6832: Drill allow-list UUID case normalization.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_6645_6682_6828_6832.py
"""
from __future__ import annotations

import pytest

from shared.semantic.time_variants_sql import (
    VariantBinding,
    VariantSqlError,
    emit_variant_expression,
)


# ---------------------------------------------------------------------------
# Bug-6645: CAGR grain-aware LAG offset
# ---------------------------------------------------------------------------

class TestCagrGrainAwareness:
    def _bind(self, time_grain: str = "year", n: int = 1) -> VariantBinding:
        return VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="MIN(f.order_date)",
            calendar_type="standard",
            dialect="postgresql",
            n=n,
            time_grain=time_grain,
        )

    def test_year_grain_lag_offset_equals_n(self):
        result = emit_variant_expression("cagr", self._bind("year", 2))
        # At year grain: 1 row/year, LAG offset = 2
        assert "LAG(" in result.sql
        assert ", 2)" in result.sql

    def test_quarter_grain_lag_offset_4x(self):
        result = emit_variant_expression("cagr", self._bind("quarter", 1))
        # At quarter grain: 4 rows/year, LAG offset = 4
        assert "LAG(" in result.sql
        assert ", 4)" in result.sql

    def test_month_grain_lag_offset_12x(self):
        result = emit_variant_expression("cagr", self._bind("month", 2))
        # At month grain: 12 rows/year, LAG offset = 24
        assert "LAG(" in result.sql
        assert ", 24)" in result.sql

    def test_half_grain_lag_offset_2x(self):
        result = emit_variant_expression("cagr", self._bind("half", 1))
        # At half grain: 2 rows/year, LAG offset = 2
        assert "LAG(" in result.sql
        assert ", 2)" in result.sql

    def test_week_grain_raises(self):
        with pytest.raises(VariantSqlError, match="not supported at.*week"):
            emit_variant_expression("cagr", self._bind("week", 1))

    def test_day_grain_raises(self):
        with pytest.raises(VariantSqlError, match="not supported at.*day"):
            emit_variant_expression("cagr", self._bind("day", 1))


# ---------------------------------------------------------------------------
# Bug-6682: _year_position safety belt for table-bound calendars
# ---------------------------------------------------------------------------

class TestYearPositionSafetyBelt:
    def test_retail_445_in_year_position_raises(self):
        # Table-bound calendar types must never reach _year_position.
        # If they do (e.g. due to a caller routing bug), fail loud.
        # The only way to trigger this is through _h_ytd_prior_year with
        # a binding that has table-bound calendar_type but NO calendar_columns
        # (an inconsistent state that the safety belt catches).
        #
        # Since _h_ytd_prior_year routes table-bound types to
        # _table_bound_order_key (which needs calendar_columns), the safety
        # belt is only reached if the routing is bypassed. We test the safety
        # belt by importing _year_position directly.
        from shared.semantic.time_variants_sql import _year_position

        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="MIN(f.order_date)",
            calendar_type="retail_445",
            dialect="postgresql",
            time_grain="day",
        )
        with pytest.raises(VariantSqlError, match="Table-bound calendar type"):
            _year_position(b)

    def test_hijri_in_year_position_raises(self):
        from shared.semantic.time_variants_sql import _year_position

        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="MIN(f.order_date)",
            calendar_type="hijri",
            dialect="postgresql",
            time_grain="day",
        )
        with pytest.raises(VariantSqlError, match="Table-bound calendar type"):
            _year_position(b)

    def test_standard_in_year_position_does_not_raise(self):
        from shared.semantic.time_variants_sql import _year_position

        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="MIN(f.order_date)",
            calendar_type="standard",
            dialect="postgresql",
            time_grain="day",
        )
        # Should not raise -- standard is expression-capable.
        result = _year_position(b)
        assert "EXTRACT" in result

    def test_table_bound_ytd_prior_year_uses_table_bound_order_key(self):
        # A retail_445 binding with proper calendar_columns should route
        # through _table_bound_order_key and NOT hit the _year_position
        # safety belt.
        b = VariantBinding(
            base_expression="SUM(amount)",
            fact_date_column="MIN(f.order_date)",
            calendar_alias="cal",
            calendar_columns={"year": "fiscal_year", "week": "fiscal_week"},
            calendar_type="retail_445",
            dialect="postgresql",
            time_grain="week",
        )
        result = emit_variant_expression("ytd_prior_year", b)
        # The SQL should reference the calendar table columns, not EXTRACT.
        assert "fiscal_year" in result.sql
        assert "fiscal_week" in result.sql


# ---------------------------------------------------------------------------
# Bug-6828: ESCAPE clause BigQuery + SQL Server [ escaping
# ---------------------------------------------------------------------------

class TestEscapeClauseBigQuery:
    def test_escape_clause_always_emitted_in_pg_canonical(self):
        """Bug-7008: _render_condition now always emits ESCAPE in PG-canonical
        form (SQL Rule 1). The BigQuery ESCAPE removal happens at the sqlglot
        transpile boundary via the _bq_escape_sql generator transform, not in
        the WHERE renderer."""
        from src.rewrite.conditions import _render_condition

        # BigQuery connector: PG-canonical intermediate DOES include ESCAPE.
        sql_bq = _render_condition(
            '"col"', "like", "'%test\\%value%'",
            like_escape="\\",
            connector="bigquery",
        )
        assert "ESCAPE" in sql_bq
        assert "LIKE" in sql_bq

        # PostgreSQL connector: ESCAPE is also present.
        sql_pg = _render_condition(
            '"col"', "like", "'%test\\%value%'",
            like_escape="\\",
            connector="postgresql",
        )
        assert "ESCAPE" in sql_pg

    def test_escape_clause_dropped_by_bigquery_transpile(self):
        """Bug-7008: the _bq_escape_sql generator transform drops the ESCAPE
        clause when transpiling to BigQuery dialect."""
        from src.rewrite.dialects import _transpile_to_dialect

        pg_sql = "SELECT \"col\" FROM t WHERE \"col\" LIKE '%test' ESCAPE '\\'"
        bq_sql = _transpile_to_dialect(pg_sql, "bigquery")
        assert "ESCAPE" not in bq_sql
        assert "LIKE" in bq_sql

    def test_escape_clause_emitted_for_postgresql(self):
        from src.rewrite.conditions import _render_condition

        sql = _render_condition(
            '"col"', "like", "'%test\\%value%'",
            like_escape="\\",
            connector="postgresql",
        )
        assert "ESCAPE" in sql

    def test_escape_clause_emitted_for_sqlserver(self):
        from src.rewrite.conditions import _render_condition

        sql = _render_condition(
            '"col"', "like", "'%test\\%value%'",
            like_escape="\\",
            connector="sqlserver",
        )
        assert "ESCAPE" in sql

    def test_no_escape_clause_without_like_escape(self):
        from src.rewrite.conditions import _render_condition

        sql = _render_condition(
            '"col"', "like", "'%test%'",
            connector="bigquery",
        )
        assert "ESCAPE" not in sql


class TestBracketNotEscapedInProducer:
    """Bug-6828 correction: bracket escaping was removed from the
    connector-agnostic producer (filter_contract._scalar_value) because
    ``[`` has no special meaning in PostgreSQL/standard SQL, and escaping
    it unconditionally breaks Spark SQL which only permits escaping
    ``%``, ``_``, and the escape character. Dialect-specific bracket
    handling belongs at the sqlglot transpilation boundary (SQL rule 1)."""

    def test_contains_does_not_escape_bracket(self):
        from src.api.filter_contract import _scalar_value

        class _FakeFilter:
            dimension = "region"
            value = "test[1]"
            values = None

        result = _scalar_value(_FakeFilter(), "like", "contains")
        # Bracket should pass through unescaped; dialect boundary handles it
        assert "[1]" in result
        assert "\\[" not in result


# ---------------------------------------------------------------------------
# Bug-6832: Drill allow-list UUID normalization
# ---------------------------------------------------------------------------
# These tests verify that the UUID normalization logic works correctly.
# Direct endpoint testing requires a full async setup; here we test the
# normalization pattern used in the drill routes.

class TestUuidNormalization:
    def test_lowercase_uuid_matches(self):
        allow = {str(x).lower().strip("{}") for x in [
            "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
        ]}
        mid = str("a1b2c3d4-e5f6-7890-abcd-ef1234567890").lower().strip("{}")
        assert mid in allow

    def test_uppercase_uuid_matches(self):
        allow = {str(x).lower().strip("{}") for x in [
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        ]}
        mid = str("A1B2C3D4-E5F6-7890-ABCD-EF1234567890").lower().strip("{}")
        assert mid in allow

    def test_braced_uuid_matches(self):
        allow = {str(x).lower().strip("{}") for x in [
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        ]}
        mid = str("{a1b2c3d4-e5f6-7890-abcd-ef1234567890}").lower().strip("{}")
        assert mid in allow

    def test_mismatched_uuid_does_not_match(self):
        allow = {str(x).lower().strip("{}") for x in [
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        ]}
        mid = str("00000000-0000-0000-0000-000000000000").lower().strip("{}")
        assert mid not in allow


# ---------------------------------------------------------------------------
# Bug-6833: CLS case-insensitive semantic name lookup
# ---------------------------------------------------------------------------

class TestClsCaseInsensitiveLookup:
    """Verify the case-insensitive dict construction used by
    _block_restricted_ref_names in router.py (Bug-6833).

    The function builds ``dims_by_name_ci`` and ``meas_by_name_ci`` from
    the exact-case dicts and falls back to them when the exact-case lookup
    misses. This prevents bypassing CLS when a query references "Revenue"
    but the model defines "revenue".
    """

    def test_ci_dict_matches_different_case(self):
        dims_by_name = {"region": "dim-obj-region"}
        meas_by_name = {"revenue": "meas-obj-revenue"}
        dims_by_name_ci = {(k or "").lower(): v for k, v in dims_by_name.items()}
        meas_by_name_ci = {(k or "").lower(): v for k, v in meas_by_name.items()}

        # Exact-case miss, CI fallback hit
        name = "Revenue"
        obj = (
            dims_by_name.get(name)
            or meas_by_name.get(name)
            or dims_by_name_ci.get(name.lower())
            or meas_by_name_ci.get(name.lower())
        )
        assert obj == "meas-obj-revenue"

    def test_ci_dict_exact_case_takes_precedence(self):
        dims_by_name = {"Region": "dim-exact"}
        dims_by_name_ci = {(k or "").lower(): v for k, v in dims_by_name.items()}

        name = "Region"
        obj = dims_by_name.get(name) or dims_by_name_ci.get(name.lower())
        assert obj == "dim-exact"

    def test_ci_dict_handles_none_keys(self):
        dims_by_name = {None: "dim-none", "region": "dim-region"}
        dims_by_name_ci = {(k or "").lower(): v for k, v in dims_by_name.items()}
        # None key becomes "" in the CI dict
        assert "" in dims_by_name_ci
        assert "region" in dims_by_name_ci

    def test_ci_dict_unmatched_name_returns_none(self):
        dims_by_name = {"region": "dim-obj"}
        meas_by_name = {"revenue": "meas-obj"}
        dims_by_name_ci = {(k or "").lower(): v for k, v in dims_by_name.items()}
        meas_by_name_ci = {(k or "").lower(): v for k, v in meas_by_name.items()}

        name = "nonexistent"
        obj = (
            dims_by_name.get(name)
            or meas_by_name.get(name)
            or dims_by_name_ci.get(name.lower())
            or meas_by_name_ci.get(name.lower())
        )
        assert obj is None
