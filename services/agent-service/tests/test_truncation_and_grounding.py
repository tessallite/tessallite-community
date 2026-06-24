"""Bug-5350 (narration grounds date range in the FULL result, not the sample)
and Bug-5351 (silent row-cap truncation is detected, disclosed, and the cap is
raised for trend queries)."""
from __future__ import annotations

from src.exec.query import (
    QueryExecution,
    build_sql,
    effective_limit,
    _trim_overfetch,
)
from src.tools.spec import QueryToolCall
from src.narrate.narrate import _build_narrate_prompt


def _call(dimensions, limit=100, sort=None, explicit=False):
    return QueryToolCall(
        model_id="m1",
        measures=["amount"],
        dimensions=dimensions,
        where=[],
        having=[],
        sort=sort or [],
        limit=limit,
        limit_explicit=explicit,
    )


# ---------------------------------------------------------------------------
# Bug-5351 / H-001 / H2-001 — trend floor only for DEFAULTED chronological trends
# ---------------------------------------------------------------------------

def test_effective_limit_raised_for_defaulted_chronological_trend():
    # limit was defaulted (not user-requested) + date dimension, unsorted or
    # sorted by the date dim.
    assert effective_limit(_call(["business_date"])) == 1000
    assert effective_limit(_call(["month_no"])) == 1000
    assert effective_limit(
        _call(["business_date"], sort=[{"name": "business_date", "direction": "asc"}])
    ) == 1000


def test_parser_records_limit_provenance():
    import json
    from src.tools.spec import parse_tool_call
    base = {"model_id": "m1", "measures": ["amount"], "dimensions": ["business_date"],
            "where": [], "having": [], "sort": []}
    defaulted = parse_tool_call(json.dumps({"query": base}))
    assert defaulted.limit == 100 and defaulted.limit_explicit is False
    requested = parse_tool_call(json.dumps({"query": {**base, "limit": 100}}))
    assert requested.limit == 100 and requested.limit_explicit is True
    # End-to-end: a defaulted trend is raised; an explicit 100 trend is not.
    assert effective_limit(defaulted) == 1000
    assert effective_limit(requested) == 100


def test_tool_spec_examples_omit_default_limit():
    # H3-001 — few-shot examples must NOT teach `limit: 100`. The parser treats
    # a present limit as user-requested, so a default in an example would defeat
    # the trend floor for any model that copies the example.
    from src.tools.spec import make_tool_spec
    for mode in ("none", "llm"):
        spec = make_tool_spec(mode)
        assert '"limit": 100' not in spec
        assert '"limit":100' not in spec


def test_compound_step_limit_provenance():
    import json
    from src.tools.spec import parse_tool_call
    step = {"name": "a", "model_id": "m1", "measures": ["v"], "dimensions": ["business_date"],
            "where": [], "having": [], "sort": []}
    expr = {"op": "div", "args": [{"ref": {"step": "a", "measure": "v"}},
                                  {"ref": {"step": "b", "measure": "v"}}]}
    omitted = parse_tool_call(json.dumps({"compound_query": {
        "steps": [step, {**step, "name": "b"}], "expression": expr, "result_label": "r"}}))
    assert all(s.limit == 100 and s.limit_explicit is False for s in omitted.steps)
    explicit = parse_tool_call(json.dumps({"compound_query": {
        "steps": [{**step, "limit": 100}, {**step, "name": "b", "limit": 100}],
        "expression": expr, "result_label": "r"}}))
    assert all(s.limit_explicit is True for s in explicit.steps)


def test_effective_limit_untouched_for_non_trend():
    assert effective_limit(_call(["country_code"])) == 100
    assert effective_limit(_call([])) == 100


def test_effective_limit_preserves_explicit_non_default_limit():
    # H-001 — "last 50 daily rows": user-requested limit on a trend is preserved.
    assert effective_limit(_call(["business_date"], limit=50, explicit=True)) == 50
    assert effective_limit(_call(["country_code"], limit=500, explicit=True)) == 500


def test_effective_limit_preserves_explicit_default_value_limit():
    # H2-001 — "show exactly 100 daily points": the user explicitly asked for
    # 100, so even on a chronological trend the cap must NOT be raised.
    assert effective_limit(_call(["business_date"], limit=100, explicit=True)) == 100


def test_effective_limit_preserves_topn_ranking():
    # H-001 — "top 10 months by amount": sorted by a measure, so the ranking IS
    # the question; the cap must not be raised even when defaulted.
    topn = _call(["month_no"], limit=100, sort=[{"name": "amount", "direction": "desc"}])
    assert effective_limit(topn) == 100


def test_build_sql_overfetches_one_beyond_cap():
    # M-001 — SQL fetches cap+1 to prove truncation.
    assert "LIMIT 1001" in build_sql("orders", _call(["business_date"]))
    assert "LIMIT 101" in build_sql("orders", _call(["country_code"]))


# ---------------------------------------------------------------------------
# M-001 — truncation is proven by the over-fetch, not assumed at cap
# ---------------------------------------------------------------------------

def test_trim_overfetch_detects_truncation():
    rows = [{"i": i} for i in range(101)]  # cap+1 came back
    trimmed, truncated = _trim_overfetch(rows, 100)
    assert truncated is True
    assert len(trimmed) == 100


def test_trim_overfetch_exact_cap_is_complete():
    rows = [{"i": i} for i in range(100)]  # exactly cap — complete, not partial
    trimmed, truncated = _trim_overfetch(rows, 100)
    assert truncated is False
    assert len(trimmed) == 100


def test_trim_overfetch_under_cap_is_complete():
    rows = [{"i": i} for i in range(5)]
    trimmed, truncated = _trim_overfetch(rows, 100)
    assert truncated is False
    assert len(trimmed) == 5


def _execution(rows, columns, rows_returned=None, truncated=False):
    return QueryExecution(
        sql="SELECT 1", columns=columns, rows=rows,
        rows_returned=rows_returned if rows_returned is not None else len(rows),
        route_type="source", routed_sql=None, aggregate_id=None, pocket_id=None,
        execution_ms=1, truncated=truncated,
    )


def test_truncated_prompt_discloses_partial_coverage():
    rows = [{"business_date": f"2025-05-{d:02d}", "amount": 10} for d in range(1, 26)]
    execution = _execution(rows, ["business_date", "amount"], rows_returned=25, truncated=True)
    _, prompt = _build_narrate_prompt("sys", "trend?", execution)
    assert "row cap" in prompt.lower()
    assert "partial" in prompt.lower() or "first" in prompt.lower()


def test_non_truncated_prompt_says_all_shown():
    execution = _execution([{"amount": 10}], ["amount"], truncated=False)
    _, prompt = _build_narrate_prompt("sys", "q", execution)
    assert "all shown" in prompt.lower()
    assert "row cap" not in prompt.lower()


# ---------------------------------------------------------------------------
# Bug-5350 — date range read from the FULL result, not the 25-row sample
# ---------------------------------------------------------------------------

def test_date_range_uses_full_result_not_sample():
    # 40 ascending daily rows; the true max (2025-06-09) sits well past the
    # 25-row narration sample, whose last row would be 2025-05-25.
    rows = [{"business_date": f"2025-05-{d:02d}", "amount": d} for d in range(1, 26)]
    rows += [{"business_date": f"2025-06-{d:02d}", "amount": d} for d in range(1, 16)]
    execution = _execution(rows, ["business_date", "amount"], rows_returned=40)
    _, prompt = _build_narrate_prompt("sys", "trend?", execution)
    assert "Date range in the data" in prompt
    # The real max must be present; the sample-truncated max must not be the stated boundary.
    assert "2025-06-15" in prompt


def test_shape_facts_are_included_in_direct_narration_prompt():
    execution = _execution(
        [{"period": "2026-01", "amount": 10}, {"period": "2026-02", "amount": 20}],
        ["period", "amount"],
        rows_returned=2,
    )
    shape_trace = {
        "shape": "time_series",
        "chart_type": "line",
        "output_mode": "chart_table",
        "quality_findings": [{"code": "missing_monthly_periods"}],
        "narration_facts": {
            "date_range": {"period": ["2026-01", "2026-02"]},
            "gaps": [{"code": "missing_monthly_periods"}],
        },
    }

    _, prompt = _build_narrate_prompt("sys", "trend?", execution, shape_trace=shape_trace)

    assert "Deterministic shape facts for narration" in prompt
    assert "series ends early" in prompt
    assert "missing_monthly_periods" in prompt
