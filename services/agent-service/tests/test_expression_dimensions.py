"""Bug-5349 Phase 1 — expression-capable dimensions.

The conversational agent can now emit structured function expressions in the
dimension clause (SELECT / GROUP BY / ORDER BY): a grain shorthand
({"name":..,"grain":..}) that becomes DATE_TRUNC, and a general scalar
expression object, both constrained to a central PostgreSQL built-in registry.

These tests pin: parser acceptance of legacy + grained + expression forms,
rejection of unregistered / aggregate / malformed expressions, PG-canonical SQL
rendering, the trend-floor and persona-scope traps (R1/R2), alias collision
safety (R4), byte-for-byte legacy SQL (R5), and the grain-preserving plan dict.
"""
from __future__ import annotations

import json

import pytest

from src.tools.spec import parse_tool_call, ToolCallParseError, QueryToolCall
from src.exec.query import (
    build_sql,
    effective_limit,
    _has_date_dimension,
    _persona_scope_violations,
    PersonaFieldScope,
)

MODEL = "11111111-1111-1111-1111-111111111111"


def _q(dimensions, measures=("amount",), **extra):
    body = {
        "model_id": MODEL,
        "measures": list(measures),
        "dimensions": dimensions,
        "where": [],
        "having": [],
        "sort": [],
    }
    body.update(extra)
    return parse_tool_call(json.dumps({"query": body}))


# ── parser acceptance ──────────────────────────────────────────────────────
def test_parser_accepts_legacy_string_dimensions():
    call = _q(["business_date"])
    assert call.dimensions == ["business_date"]
    assert len(call.dimension_refs) == 1
    assert call.dimension_refs[0].is_bare is True
    assert call.dimension_refs[0].base_fields == ("business_date",)


def test_parser_accepts_grain_shorthand():
    call = _q([{"name": "business_date", "grain": "month"}])
    # `dimensions` carries the generated alias; the ref carries the AST.
    assert call.dimensions == ["business_date_month"]
    ref = call.dimension_refs[0]
    assert ref.is_bare is False
    assert ref.is_date_fn is True
    assert ref.base_fields == ("business_date",)


def test_parser_accepts_scalar_expression_object():
    call = _q([{"expr": {"fn": "lower", "args": [{"field": "city"}]}}])
    assert call.dimensions == ["city_lower"]  # deterministic default alias
    assert call.dimension_refs[0].base_fields == ("city",)


def test_parser_accepts_explicit_alias():
    call = _q([{"expr": {"fn": "upper", "args": [{"field": "city"}]}, "alias": "CITY"}])
    assert call.dimensions == ["CITY"]


# ── parser rejection ───────────────────────────────────────────────────────
def test_parser_rejects_unregistered_function():
    with pytest.raises(ToolCallParseError):
        _q([{"expr": {"fn": "pg_sleep", "args": [{"literal": 5}]}}])


def test_parser_rejects_aggregate_as_dimension():
    # An aggregate is not a valid grouping dimension (clause restriction).
    with pytest.raises(ToolCallParseError):
        _q([{"expr": {"fn": "sum", "args": [{"field": "amount"}]}, "alias": "s"}])


def test_parser_rejects_invalid_grain():
    with pytest.raises(ToolCallParseError):
        _q([{"name": "business_date", "grain": "fortnight"}])


def test_parser_rejects_bad_date_trunc_unit():
    with pytest.raises(ToolCallParseError):
        _q([{"expr": {"fn": "date_trunc",
                      "args": [{"literal": "fortnight"}, {"field": "business_date"}]}}])


def test_parser_rejects_unknown_node_keys():
    with pytest.raises(ToolCallParseError):
        _q([{"expr": {"column": "city"}}])


def test_parser_rejects_wrong_arity():
    with pytest.raises(ToolCallParseError):
        _q([{"expr": {"fn": "lower", "args": [{"field": "a"}, {"field": "b"}]}}])


def test_compound_steps_accept_grained_expression_dimensions():
    body = {
        "compound_query": {
            "steps": [
                {"name": "a", "model_id": MODEL, "measures": ["amount"],
                 "dimensions": [{"name": "business_date", "grain": "month"}],
                 "where": [], "having": [], "sort": []},
                {"name": "b", "model_id": MODEL, "measures": ["amount"],
                 "dimensions": ["country_code"], "where": [], "having": [], "sort": []},
            ],
            "expression": {"ref": {"step": "a", "measure": "amount"}},
            "result_label": "x",
        }
    }
    call = parse_tool_call(json.dumps(body))
    assert call.steps[0].dimensions == ["business_date_month"]
    assert call.steps[0].dimension_refs[0].is_bare is False
    assert call.steps[0].dimension_refs[0].base_fields == ("business_date",)


# ── SQL rendering ──────────────────────────────────────────────────────────
def test_build_sql_emits_date_trunc_in_all_clauses():
    call = _q([{"name": "business_date", "grain": "month"}])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'DATE_TRUNC(\'month\', "business_date") AS "business_date_month"' in sql
    assert 'GROUP BY DATE_TRUNC(\'month\', "business_date")' in sql
    assert 'ORDER BY DATE_TRUNC(\'month\', "business_date") ASC' in sql


def test_build_sql_emits_scalar_text_function():
    call = _q([{"expr": {"fn": "lower", "args": [{"field": "city"}]}}])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'LOWER("city") AS "city_lower"' in sql
    assert 'GROUP BY LOWER("city")' in sql


def test_build_sql_emits_extract_and_round():
    call = _q([
        {"expr": {"fn": "extract",
                  "args": [{"literal": "month"}, {"field": "business_date"}]}, "alias": "m"},
        {"expr": {"fn": "round",
                  "args": [{"field": "amount"}, {"literal": 2}]}, "alias": "amt"},
    ])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'EXTRACT(MONTH FROM "business_date") AS "m"' in sql
    assert 'ROUND("amount", 2) AS "amt"' in sql


def test_legacy_bare_dimension_sql_byte_for_byte():
    # R5 — a bare-dimension query must produce exactly the pre-Bug-5349 SQL.
    # country_code is non-date so the trend floor does not apply (limit 100).
    call = _q(["country_code"])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert sql == (
        'SELECT "country_code", SUM("amount") AS "amount" '
        'FROM "modelx" '
        'GROUP BY "country_code" '
        'ORDER BY "country_code" ASC '
        'LIMIT 101'
    )


def test_alias_collision_is_deduped():
    # Two lower() expressions over different fields whose default aliases would
    # both be "<field>_lower" only collide when the field name matches; force a
    # collision against a bare dimension named the same as a generated alias.
    call = _q([
        "city_lower",
        {"expr": {"fn": "lower", "args": [{"field": "city"}]}},
    ])
    assert call.dimensions == ["city_lower", "city_lower_2"]


# ── trap fixes ─────────────────────────────────────────────────────────────
def test_trend_floor_applies_to_grained_date_dimension():
    # R2 — a DATE_TRUNC grained dimension must be recognised as a date trend so
    # the defaulted limit is raised to the trend floor (Bug-5351).
    call = _q([{"name": "business_date", "grain": "month"}])
    assert _has_date_dimension(call) is True
    assert effective_limit(call) == 1000


def test_persona_scope_walks_base_fields_of_expression():
    # R1 — a function must not hide a field that is outside the persona scope;
    # the violation reports the base field, not the alias.
    call = _q([{"expr": {"fn": "lower", "args": [{"field": "secret_col"}]}, "alias": "x"}])
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}),
        dimensions=frozenset({"city"}),  # secret_col NOT visible
    )
    violations = _persona_scope_violations(call, scope)
    assert "secret_col" in violations
    assert "x" not in violations  # alias is never reported


def test_persona_scope_allows_visible_base_field():
    call = _q([{"expr": {"fn": "lower", "args": [{"field": "city"}]}, "alias": "x"}])
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}),
        dimensions=frozenset({"city"}),
    )
    assert _persona_scope_violations(call, scope) == []


# ── plan dict (D2) ─────────────────────────────────────────────────────────
def test_plan_dict_carries_dimension_exprs_only_when_grained():
    from src.pipeline import _plan_dict
    grained = _q([{"name": "business_date", "grain": "month"}])
    plan = _plan_dict(grained)["query"]
    assert plan["dimensions"] == ["business_date_month"]
    assert plan["dimension_exprs"] == [{"name": "business_date", "grain": "month"}]

    bare = _q(["country_code"])
    assert "dimension_exprs" not in _plan_dict(bare)["query"]


# ── DR-B5349-P1-01 — explicit sort on an expression alias passes scope ──────
def test_explicit_sort_on_expression_alias_passes_persona_scope():
    call = _q(
        [{"name": "business_date", "grain": "month"}],
        sort=[{"name": "business_date_month", "direction": "asc"}],
    )
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}),
        dimensions=frozenset({"business_date"}),  # only the base field is visible
    )
    # The alias is a selected dimension whose base field passed scope, so the
    # explicit sort on it must not be a violation.
    assert _persona_scope_violations(call, scope) == []


def test_sort_on_unrelated_hidden_field_still_rejected():
    call = _q(
        [{"name": "business_date", "grain": "month"}],
        sort=[{"name": "secret_metric", "direction": "asc"}],
    )
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}),
        dimensions=frozenset({"business_date"}),
    )
    assert "secret_metric" in _persona_scope_violations(call, scope)


# ── DR-B5349-P1-02 — persisted grain survives follow-up plan replay ─────────
def test_dimension_exprs_round_trips_through_plan_replay():
    from src.pipeline import _plan_dict
    original = _q([{"name": "business_date", "grain": "month"}])
    plan = _plan_dict(original)  # what a follow-up sees as PREVIOUS QUERY PLAN

    # The planner replays the plan verbatim (including dimension_exprs).
    replayed = parse_tool_call(json.dumps(plan))
    assert replayed.dimension_refs[0].is_bare is False
    assert replayed.dimension_refs[0].base_fields == ("business_date",)
    sql = build_sql("modelx", replayed, {"amount": "SUM"})
    assert 'DATE_TRUNC(\'month\', "business_date")' in sql


def test_dimension_exprs_takes_precedence_over_dimensions_aliases():
    # If both keys are present, dimension_exprs (original objects) wins over the
    # display aliases so the grain is never silently dropped.
    body = {
        "model_id": MODEL,
        "measures": ["amount"],
        "dimensions": ["business_date_month"],  # flattened alias (display)
        "dimension_exprs": [{"name": "business_date", "grain": "month"}],
        "where": [], "having": [], "sort": [],
    }
    call = parse_tool_call(json.dumps({"query": body}))
    assert call.dimension_refs[0].is_bare is False
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'DATE_TRUNC(\'month\', "business_date")' in sql
