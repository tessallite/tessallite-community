"""Tests for Bug-5557 (connector_qualify quoting in subtotal engine) and
Bug-5558 (type-aware WHERE filter literals in XMLA gateway).

Bug-5557: subtotal_engine must use shared.connector_qualify.quote_identifier
          instead of hardcoded f'"{name}"' double-quote quoting.

Bug-5558: _build_where_sql_clauses must render numeric dimension values as
          bare numeric literals (no quotes) when the column data_type is
          numeric, mirroring the Bug-5538 fix in the query-router.
"""

import pytest
from src.dax.subtotal_engine import (
    SubtotalHierarchy,
    SubtotalLevel,
    build_subtotal_queries,
    build_multi_subtotal_queries,
)
from src.dax.xmla_server import (
    _build_where_sql_clauses,
    _dim_type_map_from_meta,
    _is_integer_type,
    _RANGE_PREFIX,
    _RANGE_SEP,
    _where_literal,
)
from shared.connector_qualify import quote_identifier


# ---------------------------------------------------------------------------
# Bug-5557: connector_qualify identifier quoting in subtotal engine
# ---------------------------------------------------------------------------

class TestBug5557SubtotalEngineQuoting:
    """Subtotal engine queries must use shared.connector_qualify.quote_identifier."""

    def _make_hierarchy(self):
        return SubtotalHierarchy(
            hierarchy_name="Calendar",
            mdx_dim_name="Business Date",
            mdx_hier_name="Calendar",
            levels=[
                SubtotalLevel(name="Year", ordinal=0, dim_name="year_col", time_unit="year"),
                SubtotalLevel(name="Month", ordinal=1, dim_name="month_col", time_unit="month"),
                SubtotalLevel(name="Day", ordinal=2, dim_name="day_col", time_unit="day"),
            ],
        )

    def _measures_meta(self):
        return [{"name": "revenue", "default_agg": "sum"}]

    def _canonical(self):
        return {"revenue": "revenue"}

    def test_postgresql_quoting_uses_double_quotes(self):
        """Default (postgresql) connector produces double-quoted identifiers."""
        queries = build_subtotal_queries(
            mdx_dims=["year_col", "month_col", "day_col"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=self._measures_meta(),
            hierarchy=self._make_hierarchy(),
            measure_canonical=self._canonical(),
            connector_type="postgresql",
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert '"year_col"' in year_q.sql
        assert '"revenue"' in year_q.sql
        assert '"test_model"' in year_q.sql

    def test_bigquery_quoting_uses_backticks(self):
        """BigQuery connector produces backtick-quoted identifiers."""
        queries = build_subtotal_queries(
            mdx_dims=["year_col", "month_col", "day_col"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=self._measures_meta(),
            hierarchy=self._make_hierarchy(),
            measure_canonical=self._canonical(),
            connector_type="bigquery",
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert "`year_col`" in year_q.sql
        assert "`revenue`" in year_q.sql
        assert "`test_model`" in year_q.sql
        assert '"' not in year_q.sql

    def test_bigquery_grand_total_backtick(self):
        """Grand total query on BigQuery uses backtick-quoted model slug."""
        queries = build_subtotal_queries(
            mdx_dims=["year_col", "month_col", "day_col"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="my_model",
            measures_meta=self._measures_meta(),
            hierarchy=self._make_hierarchy(),
            measure_canonical=self._canonical(),
            connector_type="bigquery",
        )
        gt_q = next(q for q in queries if q.grain_ordinal == -1)
        assert "`my_model`" in gt_q.sql

    def test_default_connector_is_postgresql(self):
        """When connector_type is omitted, default is postgresql (double-quotes)."""
        queries = build_subtotal_queries(
            mdx_dims=["year_col", "month_col", "day_col"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test",
            measures_meta=self._measures_meta(),
            hierarchy=self._make_hierarchy(),
            measure_canonical=self._canonical(),
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert '"year_col"' in year_q.sql

    def test_multi_subtotal_bigquery_quoting(self):
        """build_multi_subtotal_queries also threads connector_type for BigQuery."""
        cal = SubtotalHierarchy(
            hierarchy_name="Calendar",
            mdx_dim_name="Date",
            mdx_hier_name="Calendar",
            levels=[
                SubtotalLevel(name="Year", ordinal=0, dim_name="yr"),
                SubtotalLevel(name="Month", ordinal=1, dim_name="mo"),
            ],
        )
        queries = build_multi_subtotal_queries(
            mdx_dims=["yr", "mo"],
            mdx_measures=["amount"],
            where_sql_clauses=[],
            model_slug="bq_model",
            measures_meta=[{"name": "amount", "default_agg": "sum"}],
            hierarchies=[cal],
            measure_canonical={"amount": "amount"},
            connector_type="bigquery",
        )
        for q in queries:
            assert "`bq_model`" in q.sql
            assert '"' not in q.sql

    def test_sqlserver_bracket_quoting(self):
        """SQL Server connector produces bracket-quoted identifiers."""
        queries = build_subtotal_queries(
            mdx_dims=["year_col", "month_col", "day_col"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="test_model",
            measures_meta=self._measures_meta(),
            hierarchy=self._make_hierarchy(),
            measure_canonical=self._canonical(),
            connector_type="sqlserver",
        )
        year_q = next(q for q in queries if q.grain_ordinal == 0)
        assert "[year_col]" in year_q.sql
        assert "[revenue]" in year_q.sql

    def test_identifier_with_special_chars_escaped(self):
        """Identifiers with special characters are properly escaped through
        quote_identifier, not raw string formatting."""
        hierarchy = SubtotalHierarchy(
            hierarchy_name="Calendar",
            mdx_dim_name="Date",
            mdx_hier_name="Calendar",
            levels=[
                SubtotalLevel(name="Year", ordinal=0, dim_name='col"with"quotes'),
                SubtotalLevel(name="Month", ordinal=1, dim_name="normal_col"),
                SubtotalLevel(name="Day", ordinal=2, dim_name="day_col"),
            ],
        )
        queries = build_subtotal_queries(
            mdx_dims=['col"with"quotes', "normal_col", "day_col"],
            mdx_measures=["revenue"],
            where_sql_clauses=[],
            model_slug="m",
            measures_meta=self._measures_meta(),
            hierarchy=hierarchy,
            measure_canonical=self._canonical(),
            connector_type="postgresql",
        )
        # The intermediate query at ordinal 1 includes Year-level dim (col"with"quotes)
        month_q = next(q for q in queries if q.grain_ordinal == 1)
        # quote_identifier escapes internal double-quotes by doubling them
        expected = quote_identifier("postgresql", 'col"with"quotes')
        assert expected in month_q.sql


# ---------------------------------------------------------------------------
# Bug-5558: type-aware WHERE filter literal rendering
# ---------------------------------------------------------------------------

class TestBug5558WhereLiteralTyping:
    """_build_where_sql_clauses must render numeric values unquoted
    for numeric columns, and quoted for text/unknown columns."""

    def _quote(self, name: str) -> str:
        return f'"{name}"'

    def test_string_dim_always_quoted(self):
        """Text dimension values are always single-quoted."""
        clauses = _build_where_sql_clauses(
            {"country": ["US"]},
            self._quote,
            dim_type_map={"country": "text"},
        )
        assert clauses == ['"country" = \'US\'']

    def test_int64_dim_numeric_value_unquoted(self):
        """INT64 dimension with a numeric value renders bare literal."""
        clauses = _build_where_sql_clauses(
            {"year_key": ["2024"]},
            self._quote,
            dim_type_map={"year_key": "INT64"},
        )
        assert clauses == ['"year_key" = 2024']

    def test_integer_dim_numeric_value_unquoted(self):
        """INTEGER dimension with a numeric value renders bare literal."""
        clauses = _build_where_sql_clauses(
            {"month_num": ["12"]},
            self._quote,
            dim_type_map={"month_num": "INTEGER"},
        )
        assert clauses == ['"month_num" = 12']

    def test_float_dim_decimal_value_unquoted(self):
        """FLOAT64 dimension with a decimal value renders bare literal."""
        clauses = _build_where_sql_clauses(
            {"rate": ["3.14"]},
            self._quote,
            dim_type_map={"rate": "FLOAT64"},
        )
        assert clauses == ['"rate" = 3.14']

    def test_numeric_dim_with_precision(self):
        """NUMERIC(10,2) dimension with numeric value renders bare."""
        clauses = _build_where_sql_clauses(
            {"amount": ["100"]},
            self._quote,
            dim_type_map={"amount": "NUMERIC(10,2)"},
        )
        assert clauses == ['"amount" = 100']

    def test_int_dim_non_numeric_value_stays_quoted(self):
        """INT64 dimension with a non-numeric value (e.g. text code) stays quoted."""
        clauses = _build_where_sql_clauses(
            {"year_key": ["FY2024"]},
            self._quote,
            dim_type_map={"year_key": "INT64"},
        )
        assert clauses == ['"year_key" = \'FY2024\'']

    def test_no_type_map_all_quoted(self):
        """Without dim_type_map, all values are single-quoted (backward compat)."""
        clauses = _build_where_sql_clauses(
            {"year_key": ["2024"]},
            self._quote,
        )
        assert clauses == ['"year_key" = \'2024\'']

    def test_unknown_dim_defaults_to_quoted(self):
        """Dimension not in type map defaults to quoted."""
        clauses = _build_where_sql_clauses(
            {"unknown_col": ["42"]},
            self._quote,
            dim_type_map={"other_col": "INT64"},
        )
        assert clauses == ['"unknown_col" = \'42\'']

    def test_in_list_numeric_values(self):
        """Multiple numeric values in an IN list are all bare."""
        clauses = _build_where_sql_clauses(
            {"year_key": ["2023", "2024", "2025"]},
            self._quote,
            dim_type_map={"year_key": "INT64"},
        )
        assert len(clauses) == 1
        assert "IN (2023, 2024, 2025)" in clauses[0]

    def test_in_list_mixed_numeric_non_numeric(self):
        """When any value is non-numeric, all are quoted for an INT column."""
        clauses = _build_where_sql_clauses(
            {"year_key": ["2023", "unknown"]},
            self._quote,
            dim_type_map={"year_key": "INT64"},
        )
        # Each value rendered individually, "2023" is numeric and bare,
        # "unknown" is non-numeric and quoted
        assert len(clauses) == 1
        assert "2023" in clauses[0]
        assert "'unknown'" in clauses[0]

    def test_between_numeric_values_unquoted(self):
        """BETWEEN with numeric values on a numeric column renders bare."""
        # Bug-6945: sentinel uses collision-safe null-byte separator.
        sentinel = f"{_RANGE_PREFIX}2020{_RANGE_SEP}2024"
        clauses = _build_where_sql_clauses(
            {"year_key": [sentinel]},
            self._quote,
            dim_type_map={"year_key": "INT64"},
        )
        assert len(clauses) == 1
        assert "BETWEEN 2020 AND 2024" in clauses[0]

    def test_between_text_values_quoted(self):
        """BETWEEN with text column keeps quoted values."""
        # Bug-6945: sentinel uses collision-safe null-byte separator.
        sentinel = f"{_RANGE_PREFIX}Alpha{_RANGE_SEP}Zeta"
        clauses = _build_where_sql_clauses(
            {"name": [sentinel]},
            self._quote,
            dim_type_map={"name": "VARCHAR"},
        )
        assert len(clauses) == 1
        assert "BETWEEN 'Alpha' AND 'Zeta'" in clauses[0]

    def test_negative_numeric_literal_unquoted(self):
        """Negative numeric value is rendered bare for numeric column."""
        clauses = _build_where_sql_clauses(
            {"balance": ["-100"]},
            self._quote,
            dim_type_map={"balance": "NUMERIC"},
        )
        assert clauses == ['"balance" = -100']

    def test_scientific_notation_stays_quoted(self):
        """Scientific notation (1e9) is NOT a valid bare numeric literal."""
        clauses = _build_where_sql_clauses(
            {"val": ["1e9"]},
            self._quote,
            dim_type_map={"val": "INT64"},
        )
        assert clauses == ['"val" = \'1e9\'']

    def test_value_with_leading_plus_stays_quoted(self):
        """Leading + is not a valid bare numeric literal."""
        clauses = _build_where_sql_clauses(
            {"val": ["+42"]},
            self._quote,
            dim_type_map={"val": "INT64"},
        )
        assert clauses == ['"val" = \'+42\'']

    def test_sql_injection_attempt_stays_quoted(self):
        """An injection attempt like '1 OR 1=1' must remain quoted."""
        clauses = _build_where_sql_clauses(
            {"val": ["1 OR 1=1"]},
            self._quote,
            dim_type_map={"val": "INT64"},
        )
        assert clauses == ['"val" = \'1 OR 1=1\'']

    def test_fractional_integer_slicer_literal_fails_clearly(self):
        with pytest.raises(ValueError, match="Invalid integer slicer literal"):
            _build_where_sql_clauses(
                {"year_key": ["3.14"]},
                self._quote,
                dim_type_map={"year_key": "INT64"},
            )


class TestWhereLiteralHelper:
    """Direct tests for the _where_literal helper."""

    def test_numeric_type_numeric_value(self):
        assert _where_literal("42", "INT64") == "42"

    def test_numeric_type_non_numeric_value(self):
        assert _where_literal("abc", "INT64") == "'abc'"

    def test_text_type_numeric_value(self):
        assert _where_literal("42", "VARCHAR") == "'42'"

    def test_none_type_numeric_value(self):
        assert _where_literal("42", None) == "'42'"

    def test_quote_escaping(self):
        assert _where_literal("O'Brien", None) == "'O''Brien'"

    def test_empty_string(self):
        assert _where_literal("", "INT64") == "''"

    def test_decimal_value_on_float_column(self):
        assert _where_literal("3.14", "FLOAT64") == "3.14"

    def test_decimal_value_on_int_column(self):
        with pytest.raises(ValueError, match="Invalid integer slicer literal"):
            _where_literal("3.14", "INT64")

    def test_negative_integer_value_on_int_column(self):
        assert _where_literal("-42", "INT64") == "-42"


class TestIntegerTypeDetection:
    """Integer slicer validation must not reject decimal-capable numeric types."""

    @pytest.mark.parametrize("col_type", ["INT64", "integer", "bigint", "smallint", "int4"])
    def test_integer_types(self, col_type):
        assert _is_integer_type(col_type)

    @pytest.mark.parametrize("col_type", ["FLOAT64", "decimal", "numeric(10,2)", "number", "real"])
    def test_decimal_capable_numeric_types(self, col_type):
        assert not _is_integer_type(col_type)


class TestDimTypeMapFromMeta:
    """Tests for _dim_type_map_from_meta helper."""

    def test_builds_map_from_metadata(self):
        meta = [
            {"name": "year_key", "data_type": "INT64"},
            {"name": "country", "data_type": "STRING"},
            {"name": "no_type", "data_type": None},
            {"name": "", "data_type": "INT64"},
        ]
        result = _dim_type_map_from_meta(meta)
        assert result == {"year_key": "INT64", "country": "STRING"}

    def test_empty_metadata(self):
        assert _dim_type_map_from_meta([]) == {}

    def test_missing_fields(self):
        meta = [{"other_field": "value"}]
        assert _dim_type_map_from_meta(meta) == {}
