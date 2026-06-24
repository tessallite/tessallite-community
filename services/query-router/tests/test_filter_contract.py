"""Canonical filter-operator contract tests (B10: F-027-03 / F-025-01).

One test per canonical operator and per accepted alias, asserting both
the contract translation (``build_logical_filters``) and — for the
operators where drift caused wrong data (F-027-04) — the SQL the
rewriter actually renders for the translated filter.

The contract lives in ``src/api/filter_contract.py`` and is shared by
/plugin/execute and /headless/query. The Excel add-in (batch B11)
adopts the same vocabulary client-side.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.filter_contract import (
    ACCEPTED_OPERATORS,
    CANONICAL_OPERATORS,
    OPERATOR_ALIASES,
    SemanticFilter,
    build_logical_filters,
    normalize_order_by,
    semantic_fingerprint,
)
from src.rewrite.conditions import _render_condition


def _one(**kwargs):
    filters = build_logical_filters([SemanticFilter(**kwargs)])
    assert len(filters) == 1
    return filters[0]


# ---------------------------------------------------------------------------
# Contract invariants
# ---------------------------------------------------------------------------

class TestContractInvariants:
    def test_canonical_set_matches_rewriter_vocabulary(self):
        """Every canonical operator must render to real SQL — the
        rewriter's silent default-to-equality fallback must never be
        reachable through this contract."""
        for op in sorted(CANONICAL_OPERATORS):
            value = {
                "in": ["a"], "not_in": ["a"], "between": ("1", "2"),
                "is_null": None, "is_not_null": None,
            }.get(op, "x")
            sql = _render_condition('"col"', op, value)
            if op == "neq":
                assert "!=" in sql, "neq must not fall through to equality"
            elif op not in ("eq",):
                assert sql != "\"col\" = 'x'", (
                    f"operator {op!r} fell through to the equality default"
                )

    def test_canonical_set_matches_persona_default_filter_vocabulary(self):
        from src.security.persona_gate import _SUPPORTED_OPERATORS
        assert CANONICAL_OPERATORS == _SUPPORTED_OPERATORS

    def test_aliases_resolve_to_canonical(self):
        for alias, target in OPERATOR_ALIASES.items():
            assert target in CANONICAL_OPERATORS

    def test_operator_field_schema_documents_accepted_set(self):
        """Machine-checked contract: the OpenAPI schema for the filter
        operator field carries the full accepted enum."""
        schema = SemanticFilter.model_json_schema()
        assert schema["properties"]["operator"]["enum"] == ACCEPTED_OPERATORS


# ---------------------------------------------------------------------------
# Canonical operators — one test per operator
# ---------------------------------------------------------------------------

class TestCanonicalOperators:
    def test_eq(self):
        f = _one(dimension="region", operator="eq", value="US")
        assert (f.operator, f.value) == ("eq", "US")
        assert _render_condition('"region"', f.operator, f.value) == "\"region\" = 'US'"

    def test_neq(self):
        f = _one(dimension="region", operator="neq", value="US")
        assert (f.operator, f.value) == ("neq", "US")
        assert _render_condition('"region"', f.operator, f.value) == "\"region\" != 'US'"

    def test_gt(self):
        f = _one(dimension="amount", operator="gt", value=100)
        assert (f.operator, f.value) == ("gt", 100)
        assert _render_condition('"amount"', f.operator, f.value) == '"amount" > 100'

    def test_gte(self):
        f = _one(dimension="amount", operator="gte", value=100)
        assert _render_condition('"amount"', f.operator, f.value) == '"amount" >= 100'

    def test_lt(self):
        f = _one(dimension="amount", operator="lt", value=100)
        assert _render_condition('"amount"', f.operator, f.value) == '"amount" < 100'

    def test_lte(self):
        f = _one(dimension="amount", operator="lte", value=100)
        assert _render_condition('"amount"', f.operator, f.value) == '"amount" <= 100'

    def test_in(self):
        f = _one(dimension="region", operator="in", values=["US", "EU"])
        assert (f.operator, f.value) == ("in", ["US", "EU"])
        assert _render_condition('"region"', f.operator, f.value) == "\"region\" IN ('US', 'EU')"

    def test_not_in(self):
        f = _one(dimension="region", operator="not_in", values=["US"])
        assert (f.operator, f.value) == ("not_in", ["US"])
        assert _render_condition('"region"', f.operator, f.value) == "\"region\" NOT IN ('US')"

    def test_between(self):
        f = _one(dimension="year", operator="between", values=[2020, 2025])
        assert (f.operator, f.value) == ("between", (2020, 2025))
        assert _render_condition('"year"', f.operator, f.value) == '"year" BETWEEN 2020 AND 2025'

    def test_like(self):
        f = _one(dimension="name", operator="like", value="Acme%")
        assert (f.operator, f.value) == ("like", "Acme%")
        assert _render_condition('"name"', f.operator, f.value) == "\"name\" LIKE 'Acme%'"

    def test_is_null(self):
        f = _one(dimension="region", operator="is_null")
        assert (f.operator, f.value) == ("is_null", None)
        assert _render_condition('"region"', f.operator, f.value) == '"region" IS NULL'

    def test_is_not_null(self):
        f = _one(dimension="region", operator="is_not_null")
        assert (f.operator, f.value) == ("is_not_null", None)
        assert _render_condition('"region"', f.operator, f.value) == '"region" IS NOT NULL'


# ---------------------------------------------------------------------------
# Aliases — the Excel add-in / Cube-style vocabulary (F-027-03)
# ---------------------------------------------------------------------------

class TestAliases:
    def test_ne_normalizes_to_neq(self):
        """F-027-04 regression: 'ne' must never reach the rewriter raw —
        the rewriter would silently invert it to equality."""
        f = _one(dimension="region", operator="ne", value="US")
        assert f.operator == "neq"
        assert _render_condition('"region"', f.operator, f.value) == "\"region\" != 'US'"

    def test_equals_normalizes_to_eq(self):
        f = _one(dimension="region", operator="equals", values=["US"])
        assert (f.operator, f.value) == ("eq", "US")

    def test_notEquals_normalizes_to_neq(self):
        f = _one(dimension="region", operator="notEquals", values=["US"])
        assert (f.operator, f.value) == ("neq", "US")

    def test_set_normalizes_to_in(self):
        f = _one(dimension="region", operator="set", values=["US", "EU"])
        assert (f.operator, f.value) == ("in", ["US", "EU"])

    def test_inDateRange_normalizes_to_between(self):
        f = _one(
            dimension="order_date", operator="inDateRange",
            values=["2025-01-01", "2025-12-31"],
        )
        assert (f.operator, f.value) == ("between", ("2025-01-01", "2025-12-31"))

    def test_contains_normalizes_to_like_with_wildcards(self):
        f = _one(dimension="name", operator="contains", values=["Acme"])
        assert (f.operator, f.value) == ("like", "%Acme%")
        assert _render_condition('"name"', f.operator, f.value) == "\"name\" LIKE '%Acme%'"

    def test_contains_escapes_percent_in_user_value(self):
        """B10 round-1 finding 7: 'contains' carries end-user text — a
        literal % inside the value must match literally, not as a
        wildcard ("100%" must not match "1000...")."""
        f = _one(dimension="name", operator="contains", values=["100%"])
        assert (f.operator, f.value) == ("like", "%100\\%%")

    def test_contains_escapes_underscore_in_user_value(self):
        f = _one(dimension="name", operator="contains", values=["a_b"])
        assert (f.operator, f.value) == ("like", "%a\\_b%")

    def test_contains_escapes_backslash_in_user_value(self):
        f = _one(dimension="name", operator="contains", values=["a\\b"])
        assert (f.operator, f.value) == ("like", "%a\\\\b%")

    def test_like_pattern_passes_through_unescaped(self):
        """'like' stays the raw pattern-passthrough operator — callers
        who want wildcards keep full control."""
        f = _one(dimension="name", operator="like", values=["C%"])
        assert (f.operator, f.value) == ("like", "C%")

    def test_notContains_normalizes_to_not_like_with_wildcards(self):
        """Bug-3609: 'notContains' wraps the user text in %...% and renders
        NOT LIKE — the mirror of 'contains'."""
        f = _one(dimension="name", operator="notContains", values=["Acme"])
        assert (f.operator, f.value) == ("not_like", "%Acme%")
        assert _render_condition('"name"', f.operator, f.value) == "\"name\" NOT LIKE '%Acme%'"

    def test_notContains_escapes_wildcards_in_user_value(self):
        """End-user TEXT — a literal % / _ inside the value must match
        literally, identical to the 'contains' escaping."""
        f = _one(dimension="name", operator="notContains", values=["100%"])
        assert (f.operator, f.value) == ("not_like", "%100\\%%")


# ---------------------------------------------------------------------------
# Scalar payload resolution — the add-in only ever sends `values`
# ---------------------------------------------------------------------------

class TestScalarPayload:
    def test_scalar_operator_accepts_values_first_element(self):
        """F-027-03: 'Amount gt 100' from the add-in arrives as
        values=[100] with no value — must not render `> NULL`."""
        f = _one(dimension="amount", operator="gt", values=[100])
        assert f.value == 100
        assert _render_condition('"amount"', f.operator, f.value) == '"amount" > 100'

    def test_value_wins_over_values(self):
        f = _one(dimension="amount", operator="gt", value=5, values=[100])
        assert f.value == 5

    def test_scalar_operator_without_payload_422(self):
        with pytest.raises(HTTPException) as exc:
            _one(dimension="amount", operator="gt")
        assert exc.value.status_code == 422
        assert "requires a value" in exc.value.detail

    def test_between_accepts_two_element_value_list(self):
        f = _one(dimension="year", operator="between", value=[2020, 2025])
        assert f.value == (2020, 2025)


# ---------------------------------------------------------------------------
# Fail-loud rejections (F-027-10)
# ---------------------------------------------------------------------------

class TestRejections:
    def test_unknown_operator_422_lists_accepted(self):
        with pytest.raises(HTTPException) as exc:
            _one(dimension="x", operator="regex", value=".*")
        assert exc.value.status_code == 422
        assert "regex" in exc.value.detail
        assert "eq" in exc.value.detail

    def test_in_empty_values_422(self):
        with pytest.raises(HTTPException) as exc:
            _one(dimension="x", operator="in", values=[])
        assert exc.value.status_code == 422
        assert "at least one" in exc.value.detail.lower()

    def test_not_in_empty_values_422(self):
        with pytest.raises(HTTPException) as exc:
            _one(dimension="x", operator="not_in", values=[])
        assert exc.value.status_code == 422

    def test_between_one_value_422(self):
        with pytest.raises(HTTPException) as exc:
            _one(dimension="x", operator="between", values=[1])
        assert exc.value.status_code == 422
        assert "exactly two" in exc.value.detail.lower()

    def test_between_three_values_422(self):
        with pytest.raises(HTTPException) as exc:
            _one(dimension="x", operator="between", values=[1, 2, 3])
        assert exc.value.status_code == 422
        assert "exactly two" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# canonical_raw_query — QueryLog preview payload (B10 round-1 finding 3)
# ---------------------------------------------------------------------------

class TestCanonicalRawQuery:
    def test_renders_compact_json(self):
        import json

        from src.api.filter_contract import canonical_raw_query

        text = canonical_raw_query(
            measures=["revenue"],
            dimensions=["region"],
            filters=[SemanticFilter(
                dimension="region", operator="in", values=["US", "DE"],
            )],
            order_by=[("region", "asc")],
            limit=100,
            offset=10,
        )
        payload = json.loads(text)
        assert payload["measures"] == ["revenue"]
        assert payload["dimensions"] == ["region"]
        assert payload["filters"] == [
            {"dimension": "region", "operator": "in", "values": ["US", "DE"]}
        ]
        assert payload["order_by"] == [["region", "asc"]]
        assert payload["limit"] == 100
        assert payload["offset"] == 10

    def test_truncates_pathological_payloads(self):
        from src.api.filter_contract import _RAW_QUERY_MAX_CHARS, canonical_raw_query

        text = canonical_raw_query(
            measures=["m"],
            dimensions=[],
            filters=[SemanticFilter(
                dimension="d", operator="in",
                values=[f"member-{i}" for i in range(5000)],
            )],
        )
        assert len(text) <= _RAW_QUERY_MAX_CHARS
        assert text.endswith("...")


# ---------------------------------------------------------------------------
# Cross-surface semantic query helpers (F-027-17)
# ---------------------------------------------------------------------------

class TestSemanticQueryHelpers:
    """The headless and plugin surfaces share one direction-validator and
    one fingerprint helper so they cannot drift (F-027-04 / F-027-10)."""

    class _Order:
        def __init__(self, field, direction="asc"):
            self.field = field
            self.direction = direction

    def test_normalize_order_by_shared_direction_contract(self):
        assert normalize_order_by([
            self._Order("region", "ASC"),
            self._Order("revenue", "desc"),
        ]) == [("region", "asc"), ("revenue", "desc")]

    def test_normalize_order_by_rejects_invalid_direction(self):
        with pytest.raises(HTTPException) as exc:
            normalize_order_by([self._Order("region", "sideways")])
        assert exc.value.status_code == 422
        assert "direction" in exc.value.detail

    def test_normalize_order_by_rejects_sql_fragment(self):
        with pytest.raises(HTTPException) as exc:
            normalize_order_by([self._Order("region", "asc UNION SELECT 1")])
        assert exc.value.status_code == 422

    def test_semantic_fingerprint_default_is_page_independent(self):
        """The QueryLog / miss-log dedup fingerprint must NOT change with
        paging or sort — pages of one query group together (F-027-15)."""
        base = dict(
            model_id="m",
            measures=["revenue"],
            dimensions=["region"],
            filters=[SemanticFilter(dimension="region", operator="eq", value="US")],
        )
        fp = semantic_fingerprint(**base)
        assert len(fp) == 64
        # Same semantic query, different paging/sort -> same default fingerprint.
        assert fp == semantic_fingerprint(**base)

    def test_semantic_fingerprint_changes_with_query_shape(self):
        f1 = semantic_fingerprint(
            model_id="m", measures=["revenue"], dimensions=["region"], filters=[],
        )
        f2 = semantic_fingerprint(
            model_id="m", measures=["cost"], dimensions=["region"], filters=[],
        )
        assert f1 != f2

    def test_semantic_fingerprint_includes_sort_and_paging_when_supplied(self):
        base = dict(
            model_id="m",
            measures=["revenue"],
            dimensions=["region"],
            filters=[SemanticFilter(dimension="region", operator="eq", value="US")],
        )
        fp_page_1 = semantic_fingerprint(
            **base, order_by=[("region", "asc")], limit=10, offset=0,
        )
        fp_page_2 = semantic_fingerprint(
            **base, order_by=[("region", "asc")], limit=10, offset=10,
        )
        fp_desc = semantic_fingerprint(
            **base, order_by=[("region", "desc")], limit=10, offset=0,
        )
        assert len(fp_page_1) == 64
        assert len({fp_page_1, fp_page_2, fp_desc}) == 3
