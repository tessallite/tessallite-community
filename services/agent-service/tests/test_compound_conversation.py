"""Tests for a multi-turn compound query conversation.

Simulates the real conversation flow from the user's session:
  1. Scalar compound: Germany January share of worldwide November
  2. Follow-up: same but UK
  3. Multi-row: UK monthly share over months (7 rows with month_no dim)
  4. Chart request: "show a graph of that" (same data, chart expected)
  5. Three-step comparison: UK vs Germany monthly difference
  6. Zero-result country: Kuwait (0 rows returned)
  7. Zero-result country: Netherlands (0 rows returned)

Each turn invokes _run_compound_query_branch directly with the
CompoundQueryToolCall the LLM would have produced, verifying pipeline
output (status, semantic_query, combine_value, rendered_output, charts).
"""
from __future__ import annotations

import uuid
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline import _run_compound_query_branch, TurnOutcome
from src.tools.spec import CompoundStep, CompoundQueryToolCall
from src.recipes.eval import evaluate_combine_aligned


# --- ExprNode tree helpers (Bug-5346) --------------------------------------

def _ref(step, measure):
    return {"ref": {"step": step, "measure": measure}}


def _op(name, *args):
    return {"op": name, "args": list(args)}


def _pct(num_step, num_meas, den_step, den_meas):
    """round(num / den * 100, 2)."""
    return _op("round", _op("mul", _op("div", _ref(num_step, num_meas),
                                       _ref(den_step, den_meas)), {"const": 100}),
               {"const": 2})


_MODEL_ID = "1ffec4a6-327c-488d-b564-0bcc30c7a016"

MONTHLY_UK_ROWS = [
    {"month_no": 1, "transaction_count": 3146},
    {"month_no": 2, "transaction_count": 2798},
    {"month_no": 3, "transaction_count": 3174},
    {"month_no": 4, "transaction_count": 3156},
    {"month_no": 5, "transaction_count": 380},
    {"month_no": 11, "transaction_count": 3155},
    {"month_no": 12, "transaction_count": 3220},
]

MONTHLY_WORLDWIDE_ROWS = [
    {"month_no": 1, "transaction_count": 8483},
    {"month_no": 2, "transaction_count": 7656},
    {"month_no": 3, "transaction_count": 8466},
    {"month_no": 4, "transaction_count": 8238},
    {"month_no": 5, "transaction_count": 1022},
    {"month_no": 11, "transaction_count": 8145},
    {"month_no": 12, "transaction_count": 8483},
]

MONTHLY_GERMANY_ROWS = [
    {"month_no": 1, "transaction_count": 1040},
    {"month_no": 2, "transaction_count": 994},
    {"month_no": 3, "transaction_count": 1021},
    {"month_no": 4, "transaction_count": 990},
    {"month_no": 5, "transaction_count": 146},
    {"month_no": 11, "transaction_count": 975},
    {"month_no": 12, "transaction_count": 1023},
]


def _exec(rows, columns):
    return MagicMock(
        rows=rows,
        columns=columns,
        rows_returned=len(rows),
        sql="SELECT ...",
        routed_sql="SELECT ...",
        route_type="source",
    )


def _make_cfg(**overrides):
    defaults = dict(
        project_id=uuid.uuid4(),
        max_compound_steps=5,
        max_query_complexity=0,
        chart_type_selector="llm",
        chart_color_palette="default",
        chart_size="md",
        chart_max_rows=500,
        include_data_table=True,
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


def _make_bundle():
    bundle = MagicMock()
    bundle.narration_system = "You are an analyst."
    bundle.allow_list_model_ids = [uuid.UUID(_MODEL_ID)]
    return bundle


def _make_adapter(narration_text="Narrated answer."):
    adapter = AsyncMock()
    adapter.complete = AsyncMock(return_value=narration_text)
    adapter.last_usage = {"input_tokens": 100, "output_tokens": 50}
    return adapter


async def _run(call, side_effects, user_message="test", cfg=None,
               narration_text="Narrated answer."):
    cfg = cfg or _make_cfg()
    adapter = _make_adapter(narration_text)
    bundle = _make_bundle()

    with patch("src.pipeline.execute_query", new_callable=AsyncMock) as mock_exec, \
         patch("src.pipeline.apply_output_guardrails") as mock_guard:
        mock_exec.side_effect = side_effects
        mock_guard.return_value = MagicMock(text=narration_text, actions=[])

        outcome = await _run_compound_query_branch(
            db=AsyncMock(),
            cfg=cfg,
            adapter=adapter,
            bundle=bundle,
            user_message=user_message,
            call=call,
            jwt_token="tok",
            publisher=None,
            usage_totals={"input": 0, "output": 0},
            prompt_messages={"system": "sys", "user": "usr"},
            llm_raw_response='{"compound_query": {}}',
        )
    return outcome


# ---------------------------------------------------------------------------
# Turn 1: Germany January share of worldwide November (scalar)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn1_germany_january_vs_worldwide_november():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "germany_january", _MODEL_ID, ["transaction_amount"], [],
                [{"name": "country_code", "op": "eq", "value": "DE"},
                 {"name": "business_date", "op": "between",
                  "value": ["2026-01-01", "2026-01-31"]}],
                [], [], 100,
            ),
            CompoundStep(
                "worldwide_november", _MODEL_ID, ["transaction_amount"], [],
                [{"name": "business_date", "op": "between",
                  "value": ["2025-11-01", "2025-11-30"]}],
                [], [], 100,
            ),
        ],
        expression=_pct("germany_january", "transaction_amount", "worldwide_november", "transaction_amount"),
        result_label="Germany January share of November worldwide (%)",
    )

    outcome = await _run(
        call,
        [
            _exec([{"transaction_amount": "2683684.2"}], ["transaction_amount"]),
            _exec([{"transaction_amount": "20429990.88"}], ["transaction_amount"]),
        ],
        user_message="what is the percentage of payment transactions in germany in january to payment transactions world wide last november",
    )

    assert outcome.status == "ok"
    assert outcome.semantic_query["tool"] == "compound_query"
    assert len(outcome.semantic_query["steps"]) == 2
    assert outcome.semantic_query["combine_value"] == 13.14
    assert outcome.rows_returned == 2


@pytest.mark.asyncio
async def test_scalar_contribution_request_renders_pie_not_legacy_kpi():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "cairo", _MODEL_ID, ["base_amount"], [],
                [{"name": "city_name", "op": "eq", "value": "Cairo"}],
                [], [], 100,
            ),
            CompoundStep(
                "global", _MODEL_ID, ["base_amount"], [],
                [], [], [], 100,
            ),
        ],
        expression=_pct("cairo", "base_amount", "global", "base_amount"),
        result_label="Cairo Contribution (%)",
        chart_type="kpi",
    )

    outcome = await _run(
        call,
        [
            _exec([{"base_amount": "22622085.92"}], ["base_amount"]),
            _exec([{"base_amount": "181901553.65"}], ["base_amount"]),
        ],
        user_message=(
            "Show as a pie chart what is the contribution of the base amount "
            "of Cairo city compared to the global total base amount"
        ),
        cfg=_make_cfg(chart_renderer="echarts"),
    )

    assert outcome.status == "ok"
    assert outcome.chart_type == "pie"
    assert outcome.semantic_query["combine_value"] == 12.44
    shape = outcome.semantic_query["shape"]
    assert shape["shape"] == "stacked_composition"
    assert shape["chart_type"] == "pie"
    assert shape["output_mode"] == "chart_table"
    assert shape["columns"] == ["segment", "Contribution (%)"]
    assert shape["row_sample"] == [
        ["Cairo Contribution", 12.44],
        ["Remaining global total", 87.56],
    ]
    assert "registry_entry=percent_of_total_by_category" in shape["notes"]
    assert "registry_entry=compound_ratio_kpi" in shape["notes"]

    rendered = json.loads(outcome.rendered_output or "")
    assert rendered["kind"] == "tessallite.visual.v1"
    assert rendered["chart_type"] == "pie"
    assert rendered["columns"] == ["segment", "Contribution (%)"]
    assert rendered["rows"] == [
        {"segment": "Cairo Contribution", "Contribution (%)": 12.44},
        {"segment": "Remaining global total", "Contribution (%)": 87.56},
    ]
    assert "legacy_html" in rendered


# ---------------------------------------------------------------------------
# Turn 2: UK January share of worldwide November (follow-up)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn2_uk_january_vs_worldwide_november():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "uk_january", _MODEL_ID, ["transaction_amount"], [],
                [{"name": "country_code", "op": "eq", "value": "GB"},
                 {"name": "business_date", "op": "between",
                  "value": ["2026-01-01", "2026-01-31"]}],
                [], [], 100,
            ),
            CompoundStep(
                "worldwide_november", _MODEL_ID, ["transaction_amount"], [],
                [{"name": "business_date", "op": "between",
                  "value": ["2025-11-01", "2025-11-30"]}],
                [], [], 100,
            ),
        ],
        expression=_pct("uk_january", "transaction_amount", "worldwide_november", "transaction_amount"),
        result_label="UK January share of November worldwide (%)",
    )

    outcome = await _run(
        call,
        [
            _exec([{"transaction_amount": "7901627.16"}], ["transaction_amount"]),
            _exec([{"transaction_amount": "20429990.88"}], ["transaction_amount"]),
        ],
        user_message="what about the uk",
    )

    assert outcome.status == "ok"
    assert outcome.semantic_query["combine_value"] == 38.68
    assert outcome.rows_returned == 2


# ---------------------------------------------------------------------------
# Turn 3: UK monthly transaction count share over months (multi-row)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn3_uk_monthly_share_multi_row():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_uk", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "GB"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"),
        result_label="UK monthly transaction count share (%)",
    )

    outcome = await _run(
        call,
        [
            _exec(MONTHLY_UK_ROWS, ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="show how the total transaction count percentage to the global number of transaction change over months starting from last november forward",
    )

    assert outcome.status == "ok"
    assert outcome.semantic_query["tool"] == "compound_query"
    combine = outcome.semantic_query["combine_value"]
    assert isinstance(combine, list)
    assert len(combine) == 7
    assert combine[0]["month_no"] == 1
    assert combine[0]["UK monthly transaction count share (%)"] == 37.09
    assert outcome.rows_returned == 14


# ---------------------------------------------------------------------------
# Turn 4: "show a graph of that" — chart auto-detection on multi-row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn4_show_graph_auto_detects_chart():
    """When chart_type_selector=llm and no chart_type given, pipeline
    should auto-detect a chart type for multi-row compound results."""
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_uk", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "GB"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"),
        result_label="UK monthly transaction count share (%)",
    )

    outcome = await _run(
        call,
        [
            _exec(MONTHLY_UK_ROWS, ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="show a graph of that",
    )

    assert outcome.status == "ok"
    assert outcome.rendered_output is not None
    assert "charts-css" in outcome.rendered_output.lower() or "<table" in outcome.rendered_output


@pytest.mark.asyncio
async def test_turn4_explicit_chart_type_renders():
    """When chart_type is explicitly set to 'line', chart HTML is produced."""
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_uk", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "GB"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"),
        result_label="UK monthly transaction count share (%)",
        chart_type="line",
    )

    outcome = await _run(
        call,
        [
            _exec(MONTHLY_UK_ROWS, ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="show a graph of that",
    )

    assert outcome.status == "ok"
    assert outcome.rendered_output is not None
    assert "charts-css" in outcome.rendered_output.lower() or "line" in outcome.rendered_output.lower()


@pytest.mark.asyncio
async def test_turn4_chart_type_selector_none_skips_chart():
    """When chart_type_selector=none, no chart is rendered even if LLM
    requests one."""
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_uk", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "GB"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"),
        result_label="UK monthly transaction count share (%)",
        chart_type="line",
    )

    cfg = _make_cfg(chart_type_selector="none")
    outcome = await _run(
        call,
        [
            _exec(MONTHLY_UK_ROWS, ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="show a graph of that",
        cfg=cfg,
    )

    assert outcome.status == "ok"
    if outcome.rendered_output:
        assert "charts-css" not in outcome.rendered_output.lower()


# ---------------------------------------------------------------------------
# Turn 5: UK vs Germany monthly difference (3-step compound)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn5_uk_vs_germany_three_step():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_uk", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "GB"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_germany", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "DE"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_op("sub", _pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"), _pct("monthly_germany", "transaction_count", "monthly_worldwide", "transaction_count")),
        result_label="UK vs Germany monthly transaction count share difference (%)",
    )

    outcome = await _run(
        call,
        [
            _exec(MONTHLY_UK_ROWS, ["month_no", "transaction_count"]),
            _exec(MONTHLY_GERMANY_ROWS, ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="compare the uk to germany",
    )

    assert outcome.status == "ok"
    combine = outcome.semantic_query["combine_value"]
    assert isinstance(combine, list)
    assert len(combine) == 7
    assert outcome.rows_returned == 21

    jan = next(r for r in combine if r["month_no"] == 1)
    uk_share = round(3146 / 8483 * 100, 2)
    de_share = round(1040 / 8483 * 100, 2)
    expected_diff = round(uk_share - de_share, 2)
    actual = jan["UK vs Germany monthly transaction count share difference (%)"]
    assert abs(actual - expected_diff) < 0.1


# ---------------------------------------------------------------------------
# Turn 6: Kuwait — zero rows returned for one step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn6_kuwait_zero_rows():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_kuwait", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "KW"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_pct("monthly_kuwait", "transaction_count", "monthly_worldwide", "transaction_count"),
        result_label="Kuwait monthly transaction count share (%)",
    )

    outcome = await _run(
        call,
        [
            _exec([], ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="what about Kuwait",
    )

    assert outcome.status == "ok"
    combine = outcome.semantic_query["combine_value"]
    assert isinstance(combine, list)
    assert len(combine) == 0
    assert outcome.semantic_query["steps"][0]["rows_returned"] == 0
    assert outcome.semantic_query["steps"][1]["rows_returned"] == 7


# ---------------------------------------------------------------------------
# Turn 7: Netherlands — zero rows returned for one step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn7_netherlands_zero_rows():
    call = CompoundQueryToolCall(
        steps=[
            CompoundStep(
                "monthly_netherlands", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "country_code", "op": "eq", "value": "NL"},
                 {"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
            CompoundStep(
                "monthly_worldwide", _MODEL_ID, ["transaction_count"], ["month_no"],
                [{"name": "business_date", "op": "gte", "value": "2025-11-01"}],
                [], [{"name": "month_no", "direction": "asc"}], 100,
            ),
        ],
        expression=_pct("monthly_netherlands", "transaction_count", "monthly_worldwide", "transaction_count"),
        result_label="Netherlands monthly transaction count share (%)",
    )

    outcome = await _run(
        call,
        [
            _exec([], ["month_no", "transaction_count"]),
            _exec(MONTHLY_WORLDWIDE_ROWS, ["month_no", "transaction_count"]),
        ],
        user_message="what about netherlands",
    )

    assert outcome.status == "ok"
    combine = outcome.semantic_query["combine_value"]
    assert isinstance(combine, list)
    assert len(combine) == 0


# ---------------------------------------------------------------------------
# Expression evaluation — string coercion across the conversation
# ---------------------------------------------------------------------------

def test_scalar_string_coercion_matches_turn1():
    """Values arrive as strings from the DB. The evaluator must coerce
    them to numbers for the same expression used in turn 1."""
    ctx = {
        "germany_january": {"transaction_amount": "2683684.2"},
        "worldwide_november": {"transaction_amount": "20429990.88"},
    }
    from src.recipes.eval import evaluate_combine
    result = evaluate_combine(
        _pct("germany_january", "transaction_amount", "worldwide_november", "transaction_amount"),
        ctx,
    )
    assert result == 13.14


def test_multi_row_string_coercion_matches_turn3():
    """Row-aligned evaluation with string values, matching turn 3."""
    step_rows = {
        "monthly_uk": [{"month_no": 1, "transaction_count": "3146"}],
        "monthly_worldwide": [{"month_no": 1, "transaction_count": "8483"}],
    }
    step_dims = {"monthly_uk": ["month_no"], "monthly_worldwide": ["month_no"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"),
        step_rows, step_dims, "share",
    )
    assert multi
    assert len(rows) == 1
    assert rows[0]["share"] == 37.09


def test_three_step_difference_expression():
    """Verify the 3-step difference expression from turn 5 evaluates
    correctly with numeric values."""
    from src.recipes.eval import evaluate_combine
    ctx = {
        "monthly_uk": {"transaction_count": 3146},
        "monthly_germany": {"transaction_count": 1040},
        "monthly_worldwide": {"transaction_count": 8483},
    }
    result = evaluate_combine(
        _op("sub",
            _pct("monthly_uk", "transaction_count", "monthly_worldwide", "transaction_count"),
            _pct("monthly_germany", "transaction_count", "monthly_worldwide", "transaction_count")),
        ctx,
    )
    expected = round(3146 / 8483 * 100, 2) - round(1040 / 8483 * 100, 2)
    assert abs(result - expected) < 0.01


def test_zero_rows_aligned_produces_empty_list():
    """When one step has no rows, aligned evaluation returns empty
    result (inner join drops everything)."""
    step_rows = {
        "monthly_kuwait": [],
        "monthly_worldwide": MONTHLY_WORLDWIDE_ROWS,
    }
    step_dims = {"monthly_kuwait": ["month_no"], "monthly_worldwide": ["month_no"]}
    rows, cols, multi, _mode = evaluate_combine_aligned(
        _pct("monthly_kuwait", "transaction_count", "monthly_worldwide", "transaction_count"),
        step_rows, step_dims, "share",
    )
    assert multi
    assert len(rows) == 0
