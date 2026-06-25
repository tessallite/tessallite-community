"""Integration tests — send actual prompts to a weak LLM and validate output.

Requires ZAI_API_KEY, ZAI_API_URL, ZAI_MODEL in the environment or .env.
Model metadata is loaded from a JSON config file (default: tests/llm_test_model_config.json).
Override the config path via LLM_TEST_MODEL_CONFIG in .env.

Skipped automatically when the API key is absent.

Run:  pytest tests/test_llm_integration.py -v -s
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.prompt.assembler import (
    _TASK_PREAMBLE,
    _RUNTIME_ROBUSTNESS,
    _TERM_RESOLUTION_RULES,
    _format_date_context,
)
from src.tools.spec import make_tool_spec

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_dotenv()

API_URL = os.environ.get("ZAI_API_URL", "")
API_KEY = os.environ.get("ZAI_API_KEY", "")
MODEL = os.environ.get("ZAI_MODEL", "GLM-4.6V")

skip_no_key = pytest.mark.skipif(
    not API_KEY, reason="ZAI_API_KEY not set — skipping LLM integration tests"
)

# ---------------------------------------------------------------------------
# Model config loader
# ---------------------------------------------------------------------------

def _load_model_config() -> dict[str, Any]:
    config_path_str = os.environ.get("LLM_TEST_MODEL_CONFIG", "")
    if config_path_str:
        config_path = Path(config_path_str)
        if not config_path.is_absolute():
            config_path = Path(__file__).resolve().parent / config_path_str
    else:
        config_path = Path(__file__).resolve().parent / "llm_test_model_config.json"
    return json.loads(config_path.read_text())


CFG = _load_model_config()

MODEL_ID = CFG["model_id"]
MODEL_SLUG = CFG["model_slug"]
MODEL_DISPLAY_NAME = CFG.get("model_display_name", MODEL_SLUG)
PROJECT_BRIEF = CFG.get("project_brief", "")

MEASURES = CFG["measures"]
DIMENSIONS = CFG["dimensions"]
FILTERABLE_WHERE = CFG["filterable_where"]
SORTABLE = sorted(set(MEASURES) | set(DIMENSIONS))
ALL_FIELDS = set(MEASURES) | set(DIMENSIONS)

VALID_WHERE_OPS = set(CFG.get("valid_where_ops", [
    "eq", "in", "between", "like", "is_null", "is_not_null",
    "gt", "gte", "lt", "lte",
]))
VALID_HAVING_OPS = set(CFG.get("valid_having_ops", [
    "eq", "between", "gt", "gte", "lt", "lte",
]))
VALID_DIRECTIONS = set(CFG.get("valid_sort_directions", ["asc", "desc"]))

SCENARIOS = CFG.get("scenarios", {})

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _model_block() -> str:
    return (
        f"### Model: {MODEL_DISPLAY_NAME} "
        f"(id: {MODEL_ID}, slug: {MODEL_SLUG})\n"
        f"Measures available: {', '.join(MEASURES)}\n"
        f"Dimensions available: {', '.join(DIMENSIONS)}\n"
        f"Filterable where fields: {', '.join(FILTERABLE_WHERE)}\n"
        f"Sortable fields: {', '.join(SORTABLE)}"
    )


def _build_system_prompt() -> str:
    from datetime import date
    today = date(2026, 5, 8)

    project_ctx = (
        "Your domain expertise: data analyst.\n"
        "Default locale: en-US.\n\n"
        + _format_date_context(today)
        + "\n\nPROJECT BRIEF:\n"
        + (PROJECT_BRIEF or "(no brief provided)")
    )

    model_layer = (
        "The following models are available for querying.\n"
        "- Use ONLY model IDs, measure names, and dimension names from this list.\n"
        "- Do not invent measure or dimension names that are not listed below.\n\n"
        "FIELD ROLE RULES:\n"
        "- Use fields from \"Measures available\" in \"measures\", \"having\", and \"sort\".\n"
        "- Use fields from \"Dimensions available\" in \"dimensions\", \"where\", and \"sort\".\n"
        "- A measure may appear in \"where\" only if it is listed under "
        "\"Filterable where fields\" for the selected model.\n"
        "- If sorting by a measure, include that measure in \"measures\" "
        "unless it is already selected.\n"
        "- If sorting by a dimension, include that dimension in \"dimensions\" "
        "unless it is already selected.\n\n"
        + _model_block()
    )

    grounding = (
        _TERM_RESOLUTION_RULES + "\n\n"
        "(no glossary or alias map content)"
    )

    parts = [
        "## TASK", _TASK_PREAMBLE,
        "", "## RUNTIME INPUT ROBUSTNESS", _RUNTIME_ROBUSTNESS,
        "", "## PROJECT CONTEXT", project_ctx,
        "", "## AVAILABLE MODELS", model_layer,
        "", "## GROUNDING", grounding,
        "", "## CROSS-MODEL RECIPES",
        "(none configured for this project — use the query tool against a single model.)",
        "", "## OUTPUT FORMAT", make_tool_spec("llm"),
    ]
    return "\n".join(parts)


def _build_user_prompt(
    question: str,
    history: list[dict[str, str]] | None = None,
    previous_plan: dict | None = None,
) -> str:
    lines = [
        "## CONVERSATION HISTORY",
        "(prior turns for context — do not re-answer these)",
    ]
    if history:
        for i, turn in enumerate(history, 1):
            lines.append(f"User (turn {i}): {turn['user']}")
            lines.append(f"Assistant: {turn['assistant']}")
    if previous_plan:
        lines.append("")
        lines.append("PREVIOUS QUERY PLAN (use this to resolve follow-up references):")
        lines.append(json.dumps(previous_plan, indent=2))
        lines.append("")
        lines.append(
            "PREVIOUS PLAN VALIDATION: use this plan only if its model_id "
            "exists in AVAILABLE MODELS, and its measures, dimensions, "
            "where fields, having fields, sort fields, operators, and sort "
            "directions are all valid. Discard invalid optional parts only "
            "if the remaining plan still preserves the previous query "
            'meaning. If the plan is unusable and the current question '
            'depends on it, use "clarify".'
        )
    lines += ["", "## CURRENT QUESTION", question]
    return "\n".join(lines)


def _resolve_template(obj: Any) -> Any:
    """Replace {{model_id}} placeholders in plan templates."""
    if isinstance(obj, str):
        return obj.replace("{{model_id}}", MODEL_ID)
    if isinstance(obj, dict):
        return {k: _resolve_template(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_template(v) for v in obj]
    return obj

# ---------------------------------------------------------------------------
# API caller
# ---------------------------------------------------------------------------

def _call_llm(system: str, user: str) -> str:
    resp = httpx.post(
        API_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return content.strip()


def _parse_llm_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    assert match, f"No JSON object found in LLM response:\n{raw[:500]}"
    return json.loads(match.group(0))

# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

class LLMResult:
    def __init__(self, raw: str):
        self.raw = raw
        self.parsed = _parse_llm_json(raw)
        keys = list(self.parsed.keys())
        assert len(keys) == 1, f"Expected 1 top-level key, got {keys}"
        self.tool = keys[0]
        self.body = self.parsed[self.tool]

    @property
    def is_query(self) -> bool:
        return self.tool == "query"

    @property
    def is_clarify(self) -> bool:
        return self.tool == "clarify"

    @property
    def is_refuse(self) -> bool:
        return self.tool == "refuse"

    def assert_valid_query(self):
        assert self.is_query, f"Expected 'query', got '{self.tool}': {self.body}"
        q = self.body
        assert "model_id" in q, "Missing model_id"
        assert q["model_id"] == MODEL_ID, f"Wrong model_id: {q['model_id']}"
        assert "measures" in q and isinstance(q["measures"], list)
        assert "dimensions" in q and isinstance(q["dimensions"], list)
        assert "where" in q and isinstance(q["where"], list)
        assert "having" in q and isinstance(q["having"], list)
        assert "sort" in q and isinstance(q["sort"], list)
        assert "limit" in q and isinstance(q["limit"], int)
        assert 1 <= q["limit"] <= 1000, f"limit out of range: {q['limit']}"

        for m in q["measures"]:
            assert m in MEASURES, f"Unknown measure: {m}"
        for d in q["dimensions"]:
            assert d in DIMENSIONS, f"Unknown dimension: {d}"
        for w in q["where"]:
            assert w["name"] in ALL_FIELDS, f"Unknown where field: {w['name']}"
            assert w["op"] in VALID_WHERE_OPS, f"Invalid where op: {w['op']}"
        for h in q["having"]:
            assert h["name"] in set(MEASURES), f"Unknown having field: {h['name']}"
            assert h["op"] in VALID_HAVING_OPS, f"Invalid having op: {h['op']}"
        for s in q["sort"]:
            assert s["name"] in ALL_FIELDS, f"Unknown sort field: {s['name']}"
            assert s.get("direction") in VALID_DIRECTIONS, \
                f"Invalid sort direction: {s.get('direction')}"
        return q


SYSTEM_PROMPT = _build_system_prompt()

# ---------------------------------------------------------------------------
# Scenario-driven test assertions
# ---------------------------------------------------------------------------

def _assert_scenario(scenario: dict[str, Any], result: LLMResult):
    """Validate an LLM result against a scenario's expect_* keys."""
    if "expect_tool" in scenario:
        assert result.tool == scenario["expect_tool"], \
            f"Expected tool '{scenario['expect_tool']}', got '{result.tool}': {result.body}"
    if "expect_tool_in" in scenario:
        assert result.tool in scenario["expect_tool_in"], \
            f"Expected tool in {scenario['expect_tool_in']}, got '{result.tool}': {result.body}"

    if not result.is_query:
        return

    q = result.assert_valid_query()

    if "expect_measures_contain" in scenario:
        for m in scenario["expect_measures_contain"]:
            assert m in q["measures"], \
                f"Expected measure '{m}' in {q['measures']}"

    if "expect_dimensions_contain" in scenario:
        for d in scenario["expect_dimensions_contain"]:
            assert d in q["dimensions"], \
                f"Expected dimension '{d}' in {q['dimensions']}"

    if "expect_dimensions_contain_any" in scenario:
        found = any(d in q["dimensions"] for d in scenario["expect_dimensions_contain_any"])
        assert found, \
            f"Expected one of {scenario['expect_dimensions_contain_any']} in {q['dimensions']}"

    if "expect_where_field" in scenario:
        field = scenario["expect_where_field"]
        matched = [w for w in q["where"] if w["name"] == field]
        assert matched, f"Expected where field '{field}', got: {q['where']}"
        if "expect_where_op" in scenario:
            assert any(w["op"] == scenario["expect_where_op"] for w in matched), \
                f"Expected where op '{scenario['expect_where_op']}' on '{field}', got: {matched}"
        if "expect_where_op_in" in scenario:
            assert any(w["op"] in scenario["expect_where_op_in"] for w in matched), \
                f"Expected where op in {scenario['expect_where_op_in']} on '{field}', got: {matched}"

    if "expect_having_field" in scenario:
        field = scenario["expect_having_field"]
        matched = [h for h in q["having"] if h["name"] == field]
        assert matched, f"Expected having field '{field}', got: {q['having']}"
        if "expect_having_op_in" in scenario:
            assert any(h["op"] in scenario["expect_having_op_in"] for h in matched), \
                f"Expected having op in {scenario['expect_having_op_in']} on '{field}', got: {matched}"

    if "expect_sort_field" in scenario:
        field = scenario["expect_sort_field"]
        matched = [s for s in q["sort"] if s["name"] == field]
        assert matched, f"Expected sort field '{field}', got: {q['sort']}"
        if "expect_sort_direction" in scenario:
            assert any(s["direction"] == scenario["expect_sort_direction"] for s in matched), \
                f"Expected sort direction '{scenario['expect_sort_direction']}' on '{field}', got: {matched}"

    if "expect_limit" in scenario:
        assert q["limit"] == scenario["expect_limit"], \
            f"Expected limit {scenario['expect_limit']}, got {q['limit']}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@skip_no_key
class TestLLMQueryPlanner:

    def _run_scenario(self, name: str):
        scenario = SCENARIOS[name]
        history = scenario.get("history")
        previous_plan = _resolve_template(scenario.get("previous_plan"))
        user = _build_user_prompt(scenario["question"], history, previous_plan)
        raw = _call_llm(SYSTEM_PROMPT, user)
        result = LLMResult(raw)
        _assert_scenario(scenario, result)
        return result

    def test_simple_kpi(self):
        """Single KPI: 'total revenue last month' -> base_amount + date where."""
        self._run_scenario("simple_kpi")

    def test_breakdown(self):
        """Breakdown: 'revenue by payment method' -> base_amount + payment_method."""
        self._run_scenario("breakdown")

    def test_follow_up(self):
        """Follow-up: previous plan had base_amount + date, question adds breakdown."""
        self._run_scenario("follow_up")

    def test_row_level_where(self):
        """Row-level filter: 'payments over 1000' -> where transaction_amount gt."""
        self._run_scenario("row_level_where")

    def test_top_n_ranking(self):
        """Top-N: 'top 10 merchants by revenue' -> sort desc + limit 10."""
        self._run_scenario("top_n_ranking")

    def test_having_threshold(self):
        """Having: 'merchants with more than 100 payments' -> having on count."""
        self._run_scenario("having_threshold")

    def test_refuse_out_of_scope(self):
        """Out-of-scope question should produce refuse or clarify."""
        self._run_scenario("refuse_out_of_scope")

    def test_trend_over_time(self):
        """Trend: 'revenue over time' -> base_amount + business_date dimension."""
        self._run_scenario("trend_over_time")

    def test_multi_measure_comparison(self):
        """Comparison: 'refunds vs chargebacks by region'."""
        self._run_scenario("multi_measure_comparison")

    def test_json_shape_always_valid(self):
        """Any reasonable question should produce valid JSON with correct shape."""
        self._run_scenario("json_shape_valid")
