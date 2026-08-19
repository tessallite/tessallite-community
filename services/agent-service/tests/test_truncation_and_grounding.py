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
from src.narrate.narrate import (
    _build_compound_narrate_prompt,
    _build_narrate_prompt,
)


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


# ---------------------------------------------------------------------------
# R10 — narrator truncation alignment: when the narration sample is capped
# below the full result row count, the truncation disclosure guard must fire
# even when execution.truncated is False (the DB did not hit its row cap).
# ---------------------------------------------------------------------------

def test_narrator_truncation_fires_when_sample_capped_below_full_result():
    """R10 — 100 rows returned, narrator sees 25, execution.truncated=False.
    The COMPLETE result is available to the user; only the narration prompt is
    capped. The disclosure must (a) tell the narrator it sees a subset, (b)
    forbid presenting sample-derived aggregates as exact, and (c) NOT falsely
    claim the result was limited by a row cap (that wording is reserved for
    real execution truncation, where rows beyond the cap do not exist)."""
    rows = [{"country": f"c{i}", "amount": i} for i in range(100)]
    execution = _execution(rows, ["country", "amount"], rows_returned=100, truncated=False)
    _, prompt = _build_narrate_prompt("sys", "show data", execution)
    lower = prompt.lower()
    # (a) narrator is told it sees a subset — "first N of M" + "showing N of M"
    assert "showing 25 of 100" in prompt
    assert "first 25 of 100" in lower
    # (b) the load-bearing aggregate prohibition — a reworded guard that drops
    # the explicit sum/avg/max/min/count ban must fail here, not just lose a
    # generic keyword.
    assert (
        "never present any total, sum, average, maximum, minimum, or count"
        in lower
    )
    assert "exact" in lower
    # (c) must NOT claim a row cap / truncation — the full result IS present
    assert "row cap" not in lower
    assert "do not claim the data was truncated" in lower


def test_sampled_trend_does_not_command_absolute_extremes():
    """R10 — for a SAMPLED trend (complete result, narrator sees 25 of 40
    dated rows) the trend-summary rules must not order the narrator to state
    'the absolute lowest and highest values' from rows it cannot see; the
    qualified variant scopes extremes to the rows shown."""
    rows = [{"business_date": f"2025-05-{d:02d}", "amount": d} for d in range(1, 26)]
    rows += [{"business_date": f"2025-06-{d:02d}", "amount": d} for d in range(1, 16)]
    execution = _execution(rows, ["business_date", "amount"], rows_returned=40, truncated=False)
    _, prompt = _build_narrate_prompt("sys", "trend?", execution)
    lower = prompt.lower()
    # Sampled variant present, plain absolute-extremes command absent.
    assert "among the rows shown" in lower
    assert "the absolute lowest and highest values" not in lower
    assert "never call them the absolute peak or valley" in lower


def test_truncated_trend_with_full_sample_keeps_plain_trend_rules():
    """Execution-truncated trend where the narrator sees EVERY materialized
    row (25 rows, all shown): the plain trend rules are safe — extremes over
    the shown rows ARE the extremes of the returned rows — and partiality
    beyond the cap is disclosed by the row-cap guard."""
    rows = [{"business_date": f"2025-05-{d:02d}", "amount": d} for d in range(1, 26)]
    execution = _execution(rows, ["business_date", "amount"], rows_returned=25, truncated=True)
    _, prompt = _build_narrate_prompt("sys", "trend?", execution)
    lower = prompt.lower()
    assert "row cap" in lower
    assert "among the rows shown" not in lower
    # No sample-scoped ban needed: the narrator sees all returned rows.
    assert "read from the rows shown to you" not in lower


def test_truncated_and_sample_capped_trend_scopes_extremes_to_shown_rows():
    """Combined case on a trend — 100 materialized truncated rows, narrator
    sees 25. Rows 26..100 exist and reach the user, so the trend rules must
    scope extremes to the rows shown (sampled variant), never command
    'the absolute lowest and highest values', and the shown-rows readings ban
    must fire alongside the row-cap guard."""
    rows = [{"business_date": f"2025-{(d // 28) + 1:02d}-{(d % 28) + 1:02d}", "amount": d}
            for d in range(100)]
    execution = _execution(rows, ["business_date", "amount"], rows_returned=100, truncated=True)
    _, prompt = _build_narrate_prompt("sys", "trend?", execution)
    lower = prompt.lower()
    assert "row cap" in lower
    assert "among the rows shown" in lower
    assert "the absolute lowest and highest values" not in lower
    assert "read from the rows shown to you" in lower
    # The complete-result claims must not appear in the truncated case.
    assert "complete result" not in lower
    assert "do not claim the data was truncated" not in lower


def test_sampled_guard_fires_when_rows_returned_exceeds_materialized_rows():
    """R10 divergence guard — today rows_returned == len(rows) at the single
    QueryExecution construction site, but the disclosure logic must not
    silently trust that invariant. If a future producer pre-caps the rows
    list while reporting a larger rows_returned, the sampled disclosure must
    still fire (using the larger figure) rather than fall into an unguarded
    'summarise the sample' path."""
    rows = [{"country": f"c{i}", "amount": i} for i in range(30)]
    execution = _execution(rows, ["country", "amount"], rows_returned=60, truncated=False)
    _, prompt = _build_narrate_prompt("sys", "show data", execution)
    lower = prompt.lower()
    assert "showing 25 of 60" in prompt
    assert "first 25 of 60" in lower
    assert "do not claim the data was truncated" in lower
    assert "all shown" not in lower


def test_narrator_sampling_guard_fires_at_26_row_boundary():
    """R10 boundary — one row past the 25-row narration cap is the smallest
    sampled case. An off-by-one regression (e.g. ``showing + 1 < full``)
    would disable the guard exactly here while the 10-row and 100-row tests
    stay green."""
    rows = [{"country": f"c{i}", "amount": i} for i in range(26)]
    execution = _execution(rows, ["country", "amount"], rows_returned=26, truncated=False)
    _, prompt = _build_narrate_prompt("sys", "show data", execution)
    lower = prompt.lower()
    assert "showing 25 of 26" in prompt
    assert "first 25 of 26" in lower
    assert "do not claim the data was truncated" in lower
    assert "all shown" not in lower


def test_narrator_truncation_not_fired_when_all_rows_fit_in_sample():
    """When all rows fit in the narration sample and execution is not truncated,
    no truncation guard should fire."""
    rows = [{"country": f"c{i}", "amount": i} for i in range(10)]
    execution = _execution(rows, ["country", "amount"], rows_returned=10, truncated=False)
    _, prompt = _build_narrate_prompt("sys", "show data", execution)
    assert "all shown" in prompt.lower()
    # No truncation guard
    assert "partial" not in prompt.lower()


def test_narrator_truncation_combines_with_execution_truncation():
    """Combined case — execution.truncated=True AND the narrator sees fewer
    rows than were materialized (25 of 100). The DB caps (100/1000) sit far
    above the 25-row narration cap, so rows 26..100 DO exist and DO reach the
    user. The prompt must carry BOTH the row-cap disclosure and a shown-rows
    readings ban (_GUARD_TRUNCATED_SAMPLED), but NOT the complete-result
    sampled guard (its "do not claim the data was truncated" wording would
    contradict the row-cap disclosure)."""
    rows = [{"country": f"c{i}", "amount": i} for i in range(100)]
    execution = _execution(rows, ["country", "amount"], rows_returned=100, truncated=True)
    _, prompt = _build_narrate_prompt("sys", "show data", execution)
    lower = prompt.lower()
    # DB row cap disclosure fires
    assert "row cap" in lower
    # Narrator sample count still stated
    assert "showing 25 of 100" in prompt
    # The addendum: shown-row readings must not be presented as extremes of
    # the returned rows (which extend to the cap and ARE user-visible).
    assert "first 25 of the 100 returned rows" in lower
    assert "read from the rows shown to you" in lower
    assert "the returned rows extend beyond those you can see" in lower
    # The complete-result sampled guard must NOT fire (no contradiction)
    assert "do not claim the data was truncated" not in lower
    assert "complete result" not in lower


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


# ---------------------------------------------------------------------------
# R10 (compound scope — Lane D review add) — the multi-row compound narrator
# view (computed.result_rows) is capped at 25 while the full per-dimension
# result reaches the user via the compound table/chart. When the true row count
# exceeds the shown count the narrator must be told it sees a sample and must
# not present a shown-row extreme as the overall extreme.
# ---------------------------------------------------------------------------

def _compound_computed(shown_rows, total, columns=("dim", "val")):
    return {
        "expression": {"const": 1},
        "label": "share (%)",
        "value": None,
        "is_multi_row": True,
        "result_rows": [
            {columns[0]: f"d{i}", columns[1]: i} for i in range(shown_rows)
        ],
        "result_columns": list(columns),
        "result_total_rows": total,
    }


def test_compound_multirow_sampled_discloses_and_scopes_extremes():
    """40 per-dimension result rows computed, narrator sees 25. Disclosure must
    fire and the extreme instruction must scope to the rows shown."""
    computed = _compound_computed(shown_rows=25, total=40)
    _, prompt = _build_compound_narrate_prompt("sys", "share by country?", [], computed)
    lower = prompt.lower()
    # narrator-sampled guard fired (reused Lane D machinery, not forked wording):
    # the guard discloses the true total ("first N of M rows") and the data block
    # states the real size vs the shown sample.
    assert "first 25 of 40" in lower
    assert "contains 40 rows" in lower
    assert "only the first 25" in lower
    assert (
        "never present any total, sum, average, maximum, minimum, or count"
        in lower
    )
    # extremes scoped to the shown rows, never the overall highest/lowest
    assert "among the rows shown" in lower
    # not a truncation claim — the full result IS delivered to the user
    assert "row cap" not in lower
    assert "do not claim the data was truncated" in lower


def test_compound_multirow_not_capped_keeps_plain_extreme_instruction():
    """25 result rows, narrator sees all 25 — no sampling. The plain 'highlight
    the highest and lowest' instruction is safe and no sampled guard fires."""
    computed = _compound_computed(shown_rows=25, total=25)
    _, prompt = _build_compound_narrate_prompt("sys", "share by country?", [], computed)
    lower = prompt.lower()
    assert "among the rows shown" not in lower
    assert "highlight the highest and lowest" in lower
    assert "showing 25 of" not in prompt
    assert "first 25 of" not in lower


def test_compound_multirow_sampling_fires_at_26_row_boundary():
    """Bug-7954 boundary: one row beyond the 25-row narration cap is the
    smallest known-complete compound sample and must disclose 25 of 26."""
    computed = _compound_computed(shown_rows=25, total=26)
    _, prompt = _build_compound_narrate_prompt(
        "sys", "share by country?", [], computed
    )
    lower = prompt.lower()
    assert "first 25 of 26" in lower
    assert "contains 26 rows" in lower
    assert "only the first 25" in lower
    assert "never present any total, sum, average, maximum, minimum, or count" in lower
    assert "row cap" not in lower


def test_compound_scalar_result_never_gets_sample_disclosure():
    """A scalar compound result (single computed value) has no result_rows and
    no sampling — the sampled disclosure must never fire."""
    computed = {
        "expression": {"const": 1},
        "label": "germany share (%)",
        "value": 11.1,
        "is_multi_row": False,
    }
    _, prompt = _build_compound_narrate_prompt("sys", "germany share?", [], computed)
    lower = prompt.lower()
    assert "among the rows shown" not in lower
    assert "first" not in lower or "first 25" not in lower
    assert "showing" not in lower


def test_compound_multirow_missing_total_defaults_to_shown_no_false_disclosure():
    """When result_total_rows is absent (older producer), fall back to the shown
    count so no false 'showing N of M' disclosure is emitted."""
    computed = _compound_computed(shown_rows=10, total=0)
    computed.pop("result_total_rows")
    _, prompt = _build_compound_narrate_prompt("sys", "share?", [], computed)
    lower = prompt.lower()
    assert "among the rows shown" not in lower
    assert "showing 10 of" not in prompt


def test_compound_scalar_steps_truncated_carries_row_cap_guard():
    """R1-2 review add: a scalar compound value computed from row-capped
    sub-query data is a partial-data figure — the prompt must carry the
    row-cap disclosure (Lane D _GUARD_TRUNCATED wording), and must NOT fire
    any sampled disclosure (nothing was sampled)."""
    computed = {
        "expression": {"const": 1},
        "label": "germany share (%)",
        "value": 11.1,
        "is_multi_row": False,
        "steps_truncated": True,
    }
    _, prompt = _build_compound_narrate_prompt("sys", "germany share?", [], computed)
    lower = prompt.lower()
    assert "row cap" in lower
    assert "partial view" in lower
    assert "complete data extends further" in lower
    # no sampling happened — the sampled guards must not fire
    assert "among the rows shown" not in lower
    assert "complete result" not in lower


def test_compound_scalar_untruncated_steps_carries_no_row_cap_guard():
    """Control: an untruncated scalar compound must not claim a row cap."""
    computed = {
        "expression": {"const": 1},
        "label": "germany share (%)",
        "value": 11.1,
        "is_multi_row": False,
        "steps_truncated": False,
    }
    _, prompt = _build_compound_narrate_prompt("sys", "germany share?", [], computed)
    assert "row cap" not in prompt.lower()


def test_compound_multirow_combined_truncated_and_sampled():
    """R1-2 combined case: steps hit the DB row cap AND the narrator sees only
    25 of the computed rows. Both Lane D guards fire (_GUARD_TRUNCATED +
    _GUARD_TRUNCATED_SAMPLED); the 'COMPLETE result' wording of the pure
    narrator-sampled guard would be FALSE and must be absent, as must its
    'do not claim the data was truncated' order (it was truncated)."""
    computed = _compound_computed(shown_rows=25, total=40)
    computed["steps_truncated"] = True
    _, prompt = _build_compound_narrate_prompt("sys", "share by country?", [], computed)
    lower = prompt.lower()
    # row-cap disclosure (real cap — _GUARD_TRUNCATED)
    assert "row cap" in lower
    assert "partial view" in lower
    # shown-rows scoping on top (_GUARD_TRUNCATED_SAMPLED)
    assert "first 25 of the 40 returned rows" in lower
    assert "the returned rows extend beyond those you can see" in lower
    assert "among the rows shown" in lower
    # the pure-sampled wording would be false here
    assert "complete result" not in lower
    assert "do not claim the data was truncated" not in lower
    # data block notes the sub-query cap
    assert "one or more underlying sub-queries hit a row cap" in lower


def test_compound_multirow_truncated_only_not_sampled_carries_row_cap_guard():
    """Bug-8509 — the multi-row + steps_truncated=True + NOT sample-capped
    branch (narrate.py multi-row ``else`` path). The narrator sees every
    computed row (nothing sampled) but the computed rows themselves derive from
    row-capped sub-query data, so the plain extreme instruction is safe AND the
    row-cap guard (_GUARD_TRUNCATED) must fire. No pinning test covered this
    branch: a regression that dropped _GUARD_TRUNCATED here would pass the rest
    of the suite. Fails before the guard is present, passes with it."""
    # shown == total -> not sample-capped; steps_truncated -> partial data.
    computed = _compound_computed(shown_rows=25, total=25)
    computed["steps_truncated"] = True
    _, prompt = _build_compound_narrate_prompt("sys", "share by country?", [], computed)
    lower = prompt.lower()
    # row-cap disclosure (real cap — _GUARD_TRUNCATED)
    assert "row cap" in lower
    assert "partial view" in lower
    assert "complete data extends further" in lower
    # the plain (unscoped) extreme instruction — the narrator sees all rows
    assert "highlight the highest and lowest" in lower
    # nothing was sampled, so the sampled scoping/disclosure must NOT fire
    assert "among the rows shown" not in lower
    assert "first 25 of" not in lower
    # data block notes the sub-query cap
    assert "one or more underlying sub-queries hit a row cap" in lower
