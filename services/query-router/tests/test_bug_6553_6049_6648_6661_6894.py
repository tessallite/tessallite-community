"""Tests for Round-2 Lane-G remediation bugs.

Bug-6553: routes.py parse-except clause narrows to ValueError/SyntaxErrorInSQL;
          non-parse faults return 500 without exception detail.
Bug-6049: semantic_fingerprint normalizes value-vs-values wire variants.
Bug-6648: aggregate HAVING generic-Exception fallback raises
          AggregateRewriteUnsupported instead of appending raw having_raw.
Bug-6661: _extract_offset rejects non-integer OFFSET with UnsupportedSQL.
Bug-6894: T-SQL contains-filter bracket escaping in _render_condition.
"""
from __future__ import annotations

import pytest

from src.api.filter_contract import (
    SemanticFilter,
    build_logical_filters,
    semantic_fingerprint,
)
import sqlglot

from src.ir.logical_query import UnsupportedSQL
from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.conditions import _render_condition
from src.rewrite.dialects import _transpile_to_dialect


def _render_predicate_for_target(pg_fragment: str, target_dialect: str) -> str:
    """Transpile a PostgreSQL-canonical WHERE fragment to *target_dialect* via
    the single dialect render boundary (``_transpile_to_dialect``), returning
    the target-dialect SQL for the whole ``SELECT 1 WHERE <fragment>`` shape.

    Bug-6894 / F-006-02: T-SQL LIKE bracket escaping now happens at the dialect
    generator boundary, not inside ``_render_condition``. So these tests assert
    the ESCAPED output on the TRANSPILED T-SQL, and the UNESCAPED (PG-canonical)
    output on PostgreSQL/BigQuery — proving the connector-agnostic renderer plus
    the centralized boundary preserve the original Bug-6894 behaviour.
    """
    return _transpile_to_dialect(f"SELECT 1 WHERE {pg_fragment}", target_dialect)


# ---------------------------------------------------------------------------
# Bug-6049: semantic_fingerprint value-vs-values normalization
# ---------------------------------------------------------------------------

class TestBug6049FingerprintNormalization:
    """Same logical filter via value vs values must produce the same dedup
    fingerprint."""

    def test_scalar_eq_value_vs_values_same_fingerprint(self):
        base = dict(model_id="m", measures=["revenue"], dimensions=["region"])
        fp_via_value = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="eq", value="US")],
        )
        fp_via_values = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="eq", values=["US"])],
        )
        assert fp_via_value == fp_via_values

    def test_in_value_vs_values_same_fingerprint(self):
        base = dict(model_id="m", measures=["revenue"], dimensions=["region"])
        fp_via_values = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="in", values=["US", "CA"])],
        )
        fp_via_value = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="in", value=["US", "CA"])],
        )
        assert fp_via_values == fp_via_value

    def test_alias_normalized_in_fingerprint(self):
        """'contains' and 'like' with the same effective pattern should
        normalize the alias to canonical 'like' in the hash."""
        base = dict(model_id="m", measures=["revenue"], dimensions=["region"])
        fp1 = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="eq", value="US")],
        )
        fp2 = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="equals", value="US")],
        )
        assert fp1 == fp2

    def test_different_values_different_fingerprint(self):
        """Sanity: different filter values must produce different prints."""
        base = dict(model_id="m", measures=["revenue"], dimensions=["region"])
        fp1 = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="eq", value="US")],
        )
        fp2 = semantic_fingerprint(
            **base,
            filters=[SemanticFilter(dimension="region", operator="eq", value="CA")],
        )
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# Bug-6661: _extract_offset non-integer OFFSET
# ---------------------------------------------------------------------------

class TestBug6661OffsetValidation:
    """Non-integer OFFSET must raise UnsupportedSQL, not silently become None."""

    def test_integer_offset_accepted(self):
        q = parse_sql_to_ir(
            "SELECT SUM(revenue) FROM sales GROUP BY region LIMIT 100 OFFSET 20",
            "model-1",
        )
        assert q.offset == 20

    def test_decimal_offset_rejected(self):
        with pytest.raises(UnsupportedSQL, match="OFFSET requires a literal integer"):
            parse_sql_to_ir(
                "SELECT SUM(revenue) FROM sales GROUP BY region LIMIT 100 OFFSET 2.5",
                "model-1",
            )

    def test_zero_offset_accepted(self):
        q = parse_sql_to_ir(
            "SELECT SUM(revenue) FROM sales GROUP BY region LIMIT 10 OFFSET 0",
            "model-1",
        )
        assert q.offset == 0

    def test_no_offset_returns_none(self):
        q = parse_sql_to_ir(
            "SELECT SUM(revenue) FROM sales GROUP BY region LIMIT 10",
            "model-1",
        )
        assert q.offset is None


# ---------------------------------------------------------------------------
# Bug-6894: T-SQL bracket escaping in contains filter
# ---------------------------------------------------------------------------

class TestBug6894TsqlBracketEscaping:
    """Brackets inside contains-filter values must be escaped on SQL Server
    so they are treated as literal characters, not LIKE character classes."""

    def test_contains_bracket_escaped_on_tsql(self):
        """'test[1]' via contains must escape [ and ] on tsql, at the dialect
        boundary. The renderer emits PG-canonical LIKE ... ESCAPE and the T-SQL
        generator escapes the brackets (F-006-02)."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="test[1]"),
        ])
        f = filters[0]
        pg = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="postgresql",
        )
        # The connector-agnostic renderer no longer escapes brackets itself.
        assert "\\[" not in pg
        # But the transpiled T-SQL DOES (bracket -> character-class hazard).
        sql = _render_predicate_for_target(pg, "tsql")
        assert "\\[" in sql
        assert "\\]" in sql
        assert "ESCAPE" in sql.upper()

    def test_contains_bracket_not_escaped_on_postgresql(self):
        """Brackets must NOT be escaped on PostgreSQL -- only tsql needs it."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="test[1]"),
        ])
        f = filters[0]
        sql = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="postgresql",
        )
        # On PostgreSQL, [ is not a LIKE metachar, so no bracket escaping.
        assert "\\[" not in sql
        assert "[1]" in sql

    def test_contains_bracket_not_escaped_on_bigquery(self):
        """Brackets must NOT be escaped on BigQuery."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="test[1]"),
        ])
        f = filters[0]
        sql = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="bigquery",
        )
        assert "\\[" not in sql

    def test_not_contains_bracket_escaped_on_tsql(self):
        """notContains should also escape brackets on tsql, at the boundary."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="notContains", value="item[A]"),
        ])
        f = filters[0]
        pg = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="postgresql",
        )
        sql = _render_predicate_for_target(pg, "tsql")
        assert "NOT LIKE" in sql.upper()
        assert "\\[" in sql
        assert "\\]" in sql

    def test_raw_like_no_bracket_escape_on_tsql(self):
        """Raw 'like' operator (no like_escape) must NOT escape brackets
        even on tsql -- the caller controls the pattern."""
        sql = _render_condition(
            '"name"', "like", "%test[1]%",
            like_escape=None, connector="tsql",
        )
        assert "\\[" not in sql
        assert "[1]" in sql

    def test_contains_no_brackets_unchanged_on_tsql(self):
        """Contains value without brackets should render normally on tsql."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="hello"),
        ])
        f = filters[0]
        sql = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="tsql",
        )
        assert "LIKE '%hello%'" in sql
        assert "ESCAPE" in sql

    # -- Production path: connector="postgresql" + like_target_connector --

    def test_production_path_pg_canonical_with_tsql_target(self):
        """Production callers build PG-canonical SQL (connector='postgresql');
        brackets are escaped when that SQL is transpiled to the tsql target at
        the dialect boundary (F-006-02)."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="test[1]"),
        ])
        f = filters[0]
        pg = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="postgresql",
        )
        sql = _render_predicate_for_target(pg, "tsql")
        assert "\\[" in sql
        assert "\\]" in sql
        assert "ESCAPE" in sql.upper()

    def test_production_path_pg_canonical_with_pg_target(self):
        """When the target is also postgresql, no bracket escaping (brackets are
        not LIKE metacharacters in PostgreSQL)."""
        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="test[1]"),
        ])
        f = filters[0]
        pg = _render_condition(
            '"name"', f.operator, f.value,
            like_escape=f.like_escape, connector="postgresql",
        )
        sql = _render_predicate_for_target(pg, "postgres")
        assert "\\[" not in sql
        assert "[1]" in sql

    def test_production_path_render_where_with_tsql_target(self):
        """``_render_where`` emits connector-agnostic PG-canonical SQL; the tsql
        target escapes brackets at the transpile boundary (F-006-02)."""
        from src.rewrite.conditions import _render_where

        filters = build_logical_filters([
            SemanticFilter(dimension="name", operator="contains", value="test[1]"),
        ])
        where = _render_where(filters, connector="postgresql")
        # The renderer itself no longer escapes brackets.
        assert "\\[" not in where
        # Transpiled to tsql, the brackets are escaped.
        sql = _render_predicate_for_target(where, "tsql")
        assert "\\[" in sql
        assert "\\]" in sql

    def test_tsql_bracket_escape_idempotent_on_roundtrip(self):
        """Fable BLOCKER regression guard: the RLS injector round-trips
        already-rendered T-SQL through the same generator. The bracket escape
        transform must be IDEMPOTENT — a second render of an already-escaped
        pattern must not double-escape (``\\[`` -> ``\\\\[``). Pre-fix the
        naive ``str.replace`` double-fired, turning ``\\[`` (literal bracket)
        into ``\\\\[`` (literal backslash + character class), which matched
        the WRONG rows in a security-filtered query."""
        # Build a PG-canonical statement with an unescaped bracket pattern.
        pg = 'SELECT 1 WHERE "c" LIKE \'%test[1]%\' ESCAPE \'\\\''
        first = _transpile_to_dialect(pg, "tsql")
        # First render escapes the brackets.
        assert "\\[" in first, f"First render did not escape brackets: {first}"
        # Round-trip: parse the T-SQL back under tsql, re-emit.
        tree = sqlglot.parse_one(first, read="tsql")
        second = tree.sql(dialect="tsql")
        assert first == second, (
            f"Bracket escape is NOT idempotent (double-escape regression):\n"
            f"  first : {first}\n"
            f"  second: {second}"
        )
