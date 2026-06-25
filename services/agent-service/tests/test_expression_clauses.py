"""Bug-5349 Phase 2/3 — expression-capable WHERE / HAVING / projection / CASE.

Phase 1 wired structured expressions into the DIMENSION clause. Phase 2/3
extend the same constrained AST into:
  - WHERE / filter: function-on-column, column-to-column, OR / NOT / grouped.
  - Projection / SELECT: derived columns (EXTRACT, DATE_TRUNC, CASE, arithmetic,
    ROUND, CONCAT).
  - HAVING: ratio-of-aggregates, computed aggregate expressions, OR.
  - CASE buckets.
  - compound-step expression filters / projections (D5 shape parity).

These tests pin: parser acceptance of the structured forms, rejection of
unregistered / malformed / wrong-clause expressions, PG-canonical SQL rendering,
legacy flat-filter back-compat (byte-for-byte), the persona-scope security trap
(every base field of every clause's expressions is walked), and plan-dict
round-trip of structured predicates / projections.
"""
from __future__ import annotations

import json

import pytest

from src.tools.spec import parse_tool_call, ToolCallParseError
from src.exec.query import (
    build_sql,
    _persona_scope_violations,
    PersonaFieldScope,
)

MODEL = "11111111-1111-1111-1111-111111111111"


def _q(measures=("amount",), dimensions=(), **extra):
    body = {
        "model_id": MODEL,
        "measures": list(measures),
        "dimensions": list(dimensions),
        "where": [],
        "having": [],
        "sort": [],
    }
    body.update(extra)
    return parse_tool_call(json.dumps({"query": body}))


# ── WHERE: function-on-column ──────────────────────────────────────────────
def test_where_extract_month_equals_literal():
    call = _q(where=[{
        "left": {"fn": "extract", "args": [{"literal": "month"}, {"field": "business_date"}]},
        "op": "eq", "right": {"literal": 6},
    }])
    assert len(call.where_refs) == 1
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'WHERE EXTRACT(MONTH FROM "business_date") = 6' in sql


def test_where_lower_city_equals_literal():
    call = _q(where=[{
        "left": {"fn": "lower", "args": [{"field": "city"}]},
        "op": "eq", "right": {"literal": "cairo"},
    }])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'WHERE LOWER("city") = \'cairo\'' in sql


def test_where_date_trunc_comparison():
    call = _q(where=[{
        "left": {"fn": "date_trunc", "args": [{"literal": "month"}, {"field": "d"}]},
        "op": "gte", "right": {"literal": "2026-01-01"},
    }])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'DATE_TRUNC(\'month\', "d") >= \'2026-01-01\'' in sql


# ── WHERE: column-to-column ────────────────────────────────────────────────
def test_where_column_to_column():
    call = _q(where=[{
        "left": {"field": "settlement_date"}, "op": "gt",
        "right": {"field": "transaction_date"},
    }])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'WHERE "settlement_date" > "transaction_date"' in sql


# ── WHERE: OR / NOT / grouped ──────────────────────────────────────────────
def test_where_or_predicate():
    call = _q(where=[{
        "or": [
            {"left": {"field": "city"}, "op": "eq", "right": {"literal": "cairo"}},
            {"left": {"field": "city"}, "op": "eq", "right": {"literal": "giza"}},
        ],
    }])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'WHERE ("city" = \'cairo\' OR "city" = \'giza\')' in sql


def test_where_not_predicate():
    call = _q(where=[{"not": {"left": {"field": "city"}, "op": "is_null"}}])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'WHERE (NOT "city" IS NULL)' in sql


def test_where_in_and_between():
    call = _q(where=[
        {"left": {"field": "c"}, "op": "in",
         "right": [{"literal": "a"}, {"literal": "b"}]},
        {"left": {"field": "amount"}, "op": "between",
         "right": [{"literal": 10}, {"literal": 20}]},
    ])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert '"c" IN (\'a\', \'b\')' in sql
    assert '"amount" BETWEEN 10 AND 20' in sql


# ── legacy flat filter back-compat (byte-for-byte) ─────────────────────────
def test_legacy_flat_where_unchanged():
    call = _q(where=[{"name": "country_code", "op": "eq", "value": "EG"}])
    assert call.where == [{"name": "country_code", "op": "eq", "value": "EG"}]
    assert call.where_refs == []
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'WHERE "country_code" = \'EG\'' in sql


def test_mixed_legacy_and_structured_where():
    call = _q(where=[
        {"name": "country_code", "op": "eq", "value": "EG"},
        {"left": {"fn": "lower", "args": [{"field": "city"}]},
         "op": "eq", "right": {"literal": "cairo"}},
    ])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    # legacy AND-joined first, structured appended.
    assert 'WHERE "country_code" = \'EG\' AND LOWER("city") = \'cairo\'' in sql


# ── WHERE rejection ────────────────────────────────────────────────────────
def test_where_rejects_unregistered_function():
    with pytest.raises(ToolCallParseError):
        _q(where=[{"left": {"fn": "pg_sleep", "args": [{"literal": 1}]},
                   "op": "eq", "right": {"literal": 1}}])


def test_where_rejects_invalid_op():
    with pytest.raises(ToolCallParseError):
        _q(where=[{"left": {"field": "c"}, "op": "regexp", "right": {"literal": "x"}}])


def test_where_or_requires_two_predicates():
    with pytest.raises(ToolCallParseError):
        _q(where=[{"or": [{"left": {"field": "c"}, "op": "eq", "right": {"literal": 1}}]}])


# ── projection / SELECT derived columns ────────────────────────────────────
def test_projection_extract_column():
    call = _q(dimensions=["country_code"], projections=[
        {"expr": {"fn": "extract", "args": [{"literal": "year"}, {"field": "d"}]},
         "alias": "yr"},
    ])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'EXTRACT(YEAR FROM "d") AS "yr"' in sql
    # projection follows dimensions + measures in the SELECT list.
    assert sql.index('"country_code"') < sql.index('EXTRACT')


def test_projection_arithmetic_and_round():
    call = _q(measures=(), dimensions=["c"], projections=[
        {"expr": {"arith": "mul", "left": {"field": "a"}, "right": {"field": "b"}},
         "alias": "prod"},
        {"expr": {"fn": "round", "args": [{"field": "amount"}, {"literal": 2}]},
         "alias": "amt"},
    ])
    sql = build_sql("modelx", call, {})
    assert '("a" * "b") AS "prod"' in sql
    assert 'ROUND("amount", 2) AS "amt"' in sql


def test_projection_concat():
    call = _q(measures=(), dimensions=["c"], projections=[
        {"expr": {"fn": "concat", "args": [
            {"field": "first"}, {"literal": " "}, {"field": "last"}]},
         "alias": "full_name"},
    ])
    sql = build_sql("modelx", call, {})
    assert 'CONCAT("first", \' \', "last") AS "full_name"' in sql


def test_projection_round_of_sum_aggregate():
    call = _q(measures=(), dimensions=["c"], projections=[
        {"expr": {"fn": "round", "args": [
            {"fn": "sum", "args": [{"field": "amount"}]}, {"literal": 2}]},
         "alias": "total"},
    ])
    sql = build_sql("modelx", call, {})
    assert 'ROUND(SUM("amount"), 2) AS "total"' in sql


# ── CASE expression ────────────────────────────────────────────────────────
def test_projection_case_bucket():
    call = _q(measures=(), dimensions=["c"], projections=[
        {"expr": {"case": [
            {"when": {"left": {"field": "amount"}, "op": "gt", "right": {"literal": 1000}},
             "then": {"literal": "high"}},
            {"when": {"left": {"field": "amount"}, "op": "gt", "right": {"literal": 100}},
             "then": {"literal": "mid"}},
        ], "else": {"literal": "low"}}, "alias": "band"},
    ])
    sql = build_sql("modelx", call, {})
    assert ('CASE WHEN "amount" > 1000 THEN \'high\' '
            'WHEN "amount" > 100 THEN \'mid\' ELSE \'low\' END AS "band"') in sql


def test_case_without_else_renders_no_else():
    call = _q(measures=(), dimensions=["c"], projections=[
        {"expr": {"case": [
            {"when": {"left": {"field": "x"}, "op": "is_not_null"},
             "then": {"literal": 1}}]}, "alias": "flag"},
    ])
    sql = build_sql("modelx", call, {})
    assert 'CASE WHEN "x" IS NOT NULL THEN 1 END AS "flag"' in sql


def test_projection_rejects_unregistered_function():
    with pytest.raises(ToolCallParseError):
        _q(dimensions=["c"], projections=[
            {"expr": {"fn": "evil", "args": [{"field": "x"}]}, "alias": "a"}])


def test_projection_alias_colliding_with_measure_is_deduped():
    # R4 — a projection whose alias equals a selected measure's output column
    # must be suffixed so the SELECT never emits two columns named "amount"
    # (a duplicate key would silently drop one column in the result rows).
    call = _q(measures=("amount",), dimensions=["c"], projections=[
        {"expr": {"fn": "round", "args": [{"field": "amount"}, {"literal": 2}]},
         "alias": "amount"}])
    assert call.projection_refs[0].alias == "amount_2"
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'SUM("amount") AS "amount"' in sql
    assert 'ROUND("amount", 2) AS "amount_2"' in sql


def test_projection_default_alias_colliding_with_measure_is_deduped():
    # A bare-field projection's default alias == the field name; if that field
    # is also a selected measure, it must dedupe.
    call = _q(measures=("amount",), dimensions=["c"], projections=[
        {"expr": {"field": "amount"}}])
    # default_alias of {"field":"amount"} is "amount"; measure "amount" is taken.
    assert call.projection_refs[0].alias == "amount_2"


# ── HAVING: ratio-of-aggregates ────────────────────────────────────────────
def test_having_ratio_of_aggregates():
    call = _q(measures=("fees", "amount"), dimensions=["category"], having=[{
        "left": {"arith": "div",
                 "left": {"fn": "sum", "args": [{"field": "fees"}]},
                 "right": {"fn": "sum", "args": [{"field": "amount"}]}},
        "op": "gt", "right": {"literal": 0.5},
    }])
    sql = build_sql("modelx", call, {"fees": "SUM", "amount": "SUM"})
    assert 'HAVING (SUM("fees") / SUM("amount")) > 0.5' in sql


def test_having_alternate_aggregate():
    call = _q(measures=("amount",), dimensions=["c"], having=[{
        "left": {"fn": "max", "args": [{"field": "amount"}]},
        "op": "gt", "right": {"literal": 1000},
    }])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'HAVING MAX("amount") > 1000' in sql


def test_having_or_predicate():
    call = _q(measures=("amount",), dimensions=["c"], having=[{
        "or": [
            {"left": {"fn": "sum", "args": [{"field": "amount"}]},
             "op": "gt", "right": {"literal": 1000}},
            {"left": {"fn": "sum", "args": [{"field": "amount"}]},
             "op": "lt", "right": {"literal": 10}},
        ],
    }])
    sql = build_sql("modelx", call, {"amount": "SUM"})
    assert 'HAVING (SUM("amount") > 1000 OR SUM("amount") < 10)' in sql


def test_having_non_aggregate_rejected():
    # A structured HAVING with no aggregate is rejected — use WHERE instead.
    with pytest.raises(ToolCallParseError):
        _q(measures=("amount",), dimensions=["c"], having=[{
            "left": {"field": "amount"}, "op": "gt", "right": {"literal": 1000}}])


def test_legacy_flat_having_unchanged():
    call = _q(measures=("txn_count",), dimensions=["c"], having=[
        {"name": "txn_count", "op": "gt", "value": 100}])
    assert call.having == [{"name": "txn_count", "op": "gt", "value": 100}]
    assert call.having_refs == []
    sql = build_sql("modelx", call, {"txn_count": "SUM"})
    assert 'HAVING SUM("txn_count") > 100' in sql


# ── persona-scope security trap (Phase 2/3) ────────────────────────────────
def _scope(measures=("amount",), dimensions=("city", "category")):
    return PersonaFieldScope(
        measures=frozenset(measures), dimensions=frozenset(dimensions),
    )


def test_persona_scope_walks_where_expression_base_fields():
    # A hidden column inside a WHERE function must be caught, reported by base
    # field, never silently allowed.
    call = _q(where=[{
        "left": {"fn": "lower", "args": [{"field": "secret_col"}]},
        "op": "eq", "right": {"literal": "x"}}])
    violations = _persona_scope_violations(call, _scope())
    assert "secret_col" in violations


def test_persona_scope_walks_column_to_column_both_sides():
    call = _q(where=[{
        "left": {"field": "city"}, "op": "gt", "right": {"field": "secret_col"}}])
    violations = _persona_scope_violations(call, _scope())
    assert "secret_col" in violations
    assert "city" not in violations  # visible


def test_persona_scope_walks_projection_base_fields():
    call = _q(dimensions=["city"], projections=[
        {"expr": {"fn": "lower", "args": [{"field": "secret_col"}]}, "alias": "s"}])
    violations = _persona_scope_violations(call, _scope())
    assert "secret_col" in violations
    assert "s" not in violations  # alias never reported


def test_persona_scope_walks_having_base_fields():
    call = _q(measures=("amount",), dimensions=["city"], having=[{
        "left": {"fn": "sum", "args": [{"field": "secret_metric"}]},
        "op": "gt", "right": {"literal": 1}}])
    violations = _persona_scope_violations(call, _scope())
    assert "secret_metric" in violations


def test_persona_scope_walks_case_predicate_and_result_fields():
    call = _q(dimensions=["city"], projections=[
        {"expr": {"case": [
            {"when": {"left": {"field": "secret_col"}, "op": "gt", "right": {"literal": 1}},
             "then": {"field": "another_secret"}}]}, "alias": "b"}])
    violations = _persona_scope_violations(call, _scope())
    assert "secret_col" in violations
    assert "another_secret" in violations


def test_flat_where_on_expression_alias_is_rejected():
    # Codex-1 persona-scope bypass — an expression dimension aliased to the NAME
    # of a persona-hidden column must NOT let a flat WHERE reference that name.
    # SQL does not resolve SELECT aliases in WHERE, so "secret_col" in WHERE
    # binds to the real hidden column. The flat name must validate against real
    # scoped fields only, never against a generated alias.
    call = _q(
        measures=("amount",),
        dimensions=[{"expr": {"field": "city"}, "alias": "secret_col"}],
        where=[{"name": "secret_col", "op": "eq", "value": "x"}],
    )
    # base field city is visible; secret_col is NOT a real scoped field.
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}), dimensions=frozenset({"city"}),
    )
    assert "secret_col" in _persona_scope_violations(call, scope)


def test_flat_having_on_expression_alias_is_rejected():
    call = _q(
        measures=("amount",),
        dimensions=[{"expr": {"field": "city"}, "alias": "secret_metric"}],
        having=[{"name": "secret_metric", "op": "gt", "value": 1}],
    )
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}), dimensions=frozenset({"city"}),
    )
    assert "secret_metric" in _persona_scope_violations(call, scope)


def test_sort_on_expression_alias_still_allowed():
    # ORDER BY DOES resolve SELECT aliases in PostgreSQL, so a sort on a selected
    # expression alias whose base fields passed scope is legitimate.
    call = _q(
        measures=("amount",),
        dimensions=[{"name": "business_date", "grain": "month"}],
        sort=[{"name": "business_date_month", "direction": "asc"}],
    )
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}), dimensions=frozenset({"business_date"}),
    )
    assert _persona_scope_violations(call, scope) == []


def test_persona_scope_allows_all_visible_expression_fields():
    call = _q(
        measures=("amount",), dimensions=["city"],
        where=[{"left": {"fn": "lower", "args": [{"field": "city"}]},
                "op": "eq", "right": {"literal": "cairo"}}],
        having=[{"left": {"fn": "sum", "args": [{"field": "amount"}]},
                 "op": "gt", "right": {"literal": 1}}],
        projections=[{"expr": {"fn": "round", "args": [{"field": "amount"}, {"literal": 0}]},
                      "alias": "r"}],
    )
    assert _persona_scope_violations(call, _scope()) == []


# ── plan-dict round-trip of structured predicates / projections ────────────
def test_structured_where_round_trips_through_plan_replay():
    from src.pipeline import _plan_dict
    original = _q(where=[{
        "left": {"fn": "lower", "args": [{"field": "city"}]},
        "op": "eq", "right": {"literal": "cairo"}}])
    plan = _plan_dict(original)
    replayed = parse_tool_call(json.dumps(plan))
    assert len(replayed.where_refs) == 1
    sql = build_sql("modelx", replayed, {"amount": "SUM"})
    assert 'LOWER("city") = \'cairo\'' in sql


def test_projection_round_trips_through_plan_replay():
    from src.pipeline import _plan_dict
    original = _q(dimensions=["c"], projections=[
        {"expr": {"fn": "round", "args": [{"field": "amount"}, {"literal": 2}]},
         "alias": "amt"}])
    plan = _plan_dict(original)
    assert plan["query"]["projections"] == [
        {"expr": {"fn": "round", "args": [{"field": "amount"}, {"literal": 2}]}, "alias": "amt"}]
    replayed = parse_tool_call(json.dumps(plan))
    sql = build_sql("modelx", replayed, {})
    assert 'ROUND("amount", 2) AS "amt"' in sql


def test_legacy_plan_dict_byte_for_byte_without_expressions():
    from src.pipeline import _plan_dict
    bare = _q(dimensions=["country_code"],
              where=[{"name": "x", "op": "eq", "value": 1}])
    plan = _plan_dict(bare)["query"]
    assert "dimension_exprs" not in plan
    assert "projections" not in plan
    assert plan["where"] == [{"name": "x", "op": "eq", "value": 1}]


# ── compound-step expression filters / projections (D5 parity) ─────────────
def test_compound_step_accepts_structured_where_and_projection():
    body = {"compound_query": {
        "steps": [
            {"name": "a", "model_id": MODEL, "measures": ["amount"],
             "dimensions": ["city"],
             "where": [{"left": {"fn": "lower", "args": [{"field": "city"}]},
                        "op": "eq", "right": {"literal": "cairo"}}],
             "projections": [{"expr": {"fn": "round",
                              "args": [{"field": "amount"}, {"literal": 2}]}, "alias": "r"}],
             "having": [], "sort": []},
            {"name": "b", "model_id": MODEL, "measures": ["amount"],
             "dimensions": ["city"], "where": [], "having": [], "sort": []},
        ],
        "expression": {"ref": {"step": "a", "measure": "amount"}},
        "result_label": "x",
    }}
    call = parse_tool_call(json.dumps(body))
    assert len(call.steps[0].where_refs) == 1
    assert len(call.steps[0].projection_refs) == 1


def _step_call_like_pipeline(step):
    # Mirror the QueryToolCall the pipeline builds for each compound step. The
    # structured refs MUST flow through (Codex-2) or compound-step
    # filters/projections vanish AND their base fields skip persona scope.
    from src.tools.spec import QueryToolCall
    return QueryToolCall(
        model_id=step.model_id, measures=step.measures, dimensions=step.dimensions,
        where=step.where, having=step.having, sort=step.sort, limit=step.limit,
        limit_explicit=step.limit_explicit, dimension_refs=step.dimension_refs,
        projection_refs=step.projection_refs, where_refs=step.where_refs,
        having_refs=step.having_refs,
    )


def test_compound_correction_json_preserves_structured_forms():
    # Codex-R2 — the failing-JSON shown to the correction LLM is built from
    # _plan_dict, so it preserves structured where/projections (a hand-rolled
    # rebuild from s.where/s.having only would silently drop them).
    from src.pipeline import _plan_dict
    body = {"compound_query": {
        "steps": [
            {"name": "a", "model_id": MODEL, "measures": ["amount"],
             "dimensions": [{"name": "business_date", "grain": "month"}],
             "where": [{"left": {"fn": "lower", "args": [{"field": "city"}]},
                        "op": "eq", "right": {"literal": "cairo"}}],
             "projections": [{"expr": {"fn": "round",
                              "args": [{"field": "amount"}, {"literal": 2}]}, "alias": "r"}],
             "having": [], "sort": []},
            {"name": "b", "model_id": MODEL, "measures": ["amount"],
             "dimensions": ["city"], "where": [], "having": [], "sort": []},
        ],
        "expression": {"ref": {"step": "a", "measure": "amount"}},
        "result_label": "x",
    }}
    call = parse_tool_call(json.dumps(body))
    plan = _plan_dict(call)["compound_query"]
    step_a = plan["steps"][0]
    assert step_a["dimension_exprs"] == [{"name": "business_date", "grain": "month"}]
    assert step_a["projections"] == [
        {"expr": {"fn": "round", "args": [{"field": "amount"}, {"literal": 2}]}, "alias": "r"}]
    # the structured where raw is merged back into the where list.
    assert {"left": {"fn": "lower", "args": [{"field": "city"}]},
            "op": "eq", "right": {"literal": "cairo"}} in step_a["where"]


def test_compound_step_structured_refs_reach_sql_and_scope():
    # Codex-2 — a compound step's structured WHERE / projection must render in
    # the executed SQL and its base fields must reach persona-scope validation.
    body = {"compound_query": {
        "steps": [
            {"name": "a", "model_id": MODEL, "measures": ["amount"],
             "dimensions": ["city"],
             "where": [{"left": {"fn": "lower", "args": [{"field": "secret_col"}]},
                        "op": "eq", "right": {"literal": "x"}}],
             "projections": [{"expr": {"fn": "round",
                              "args": [{"field": "amount"}, {"literal": 2}]}, "alias": "r"}],
             "having": [], "sort": []},
            {"name": "b", "model_id": MODEL, "measures": ["amount"],
             "dimensions": ["city"], "where": [], "having": [], "sort": []},
        ],
        "expression": {"ref": {"step": "a", "measure": "amount"}},
        "result_label": "x",
    }}
    call = parse_tool_call(json.dumps(body))
    step_call = _step_call_like_pipeline(call.steps[0])
    # SQL composition carries the structured predicate and the projection.
    sql = build_sql("modelx", step_call, {"amount": "SUM"})
    assert 'LOWER("secret_col") = \'x\'' in sql
    assert 'ROUND("amount", 2) AS "r"' in sql
    # Persona scope catches the hidden base field inside the step's WHERE.
    scope = PersonaFieldScope(
        measures=frozenset({"amount"}), dimensions=frozenset({"city"}),
    )
    assert "secret_col" in _persona_scope_violations(step_call, scope)
