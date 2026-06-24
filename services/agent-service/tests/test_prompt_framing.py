"""Verify assembler framing functions include directive language, not just data dumps."""
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.prompt.assembler import (
    _format_date_context,
    _format_project_layer,
    _format_glossary_layer,
)
from src.retrieval.glossary import AliasMapBlock, GlossaryCard


def _make_cfg(safety=None, content_rules=None, brand=None):
    cfg = MagicMock()
    cfg.agent_role = "data analyst"
    cfg.project_brief = "Test project"
    cfg.brand_guidelines = brand
    cfg.safety_policy = safety
    cfg.content_rules = content_rules
    cfg.default_locale = "en-GB"
    return cfg


def test_safety_policy_has_enforcement_framing():
    cfg = _make_cfg(safety="No competitor mentions.")
    result = _format_project_layer(cfg)
    assert "MUST" in result
    assert "refuse" in result.lower()
    assert "No competitor mentions." in result


def test_content_rules_excluded_from_planner():
    """Content rules are narration concerns; the planner must not see them."""
    cfg = _make_cfg(content_rules="Only report in GBP.")
    result = _format_project_layer(cfg)
    assert "Only report in GBP." not in result
    assert "CONTENT RULES" not in result


def test_brand_guidelines_excluded_from_planner():
    """Brand guidelines are narration concerns; the planner must not see them."""
    cfg = _make_cfg(brand="Say Acme Corp not the company.")
    result = _format_project_layer(cfg)
    assert "Say Acme Corp" not in result
    assert "BRAND GUIDELINES" not in result


def test_glossary_has_usage_instruction():
    card = GlossaryCard(
        model_id="mid", term="ARR", synonyms=[], definition="Annual recurring revenue"
    )
    result = _format_glossary_layer([card], [])
    lower = result.lower()
    assert "authoritative" in lower or "interpret" in lower or "use" in lower
    assert "ARR" in result


def test_alias_map_has_resolution_instruction():
    am = AliasMapBlock(model_id="mid", pairs={"revenue": "total_revenue"})
    result = _format_glossary_layer([], [am])
    lower = result.lower()
    assert "canonical" in lower or "resolve" in lower
    assert "revenue" in result


def test_no_tone_in_project_layer():
    """Tone/format instructions are for narration, not the query planner."""
    cfg = _make_cfg()
    result = _format_project_layer(cfg)
    assert "TONE" not in result
    assert "OUTPUT FORMAT" not in result


def test_chart_in_tool_spec_for_llm_selector():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("llm")
    assert "chart_type" in spec
    assert "chart_type" in spec
    assert "ALWAYS include" in spec


def test_chart_in_tool_spec_not_for_auto():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("auto")
    query_section = spec.split('"compound_query"')[0]
    assert "chart_type" not in query_section


def test_chart_in_tool_spec_not_for_none():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    query_section = spec.split('"compound_query"')[0]
    assert "chart_type" not in query_section


def test_planner_has_grain_shorthand_example():
    # DR-B5349-P1-03 — the planner prompt must demonstrate the function-grain
    # shape, not just describe it, so the LLM reliably emits grained dimensions.
    from src.prompt.assembler import _TASK_PREAMBLE
    assert '"grain": "month"' in _TASK_PREAMBLE
    assert "Example G" in _TASK_PREAMBLE
    assert '"fn": "lower"' in _TASK_PREAMBLE  # scalar function attribute example


def test_followup_rules_preserve_dimension_exprs_grain():
    # DR-B5349-P1-02 — follow-up guidance must tell the planner to reuse the
    # persisted dimension_exprs so the grain is not dropped on replay.
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "dimension_exprs" in _TASK_PREAMBLE


def test_no_safety_framing_when_policy_empty():
    cfg = _make_cfg(safety=None)
    result = _format_project_layer(cfg)
    assert "SAFETY POLICY" not in result


def test_no_content_rules_framing_when_empty():
    cfg = _make_cfg(content_rules=None)
    result = _format_project_layer(cfg)
    assert "CONTENT RULES" not in result


def test_glossary_no_framing_when_no_cards():
    result = _format_glossary_layer([], [])
    assert "(no glossary" in result


def test_model_layer_has_only_use_listed_instruction():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test Model",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["revenue"], dimension_names=["region"],
        filterable_where_names=["region", "revenue"],
        sortable_names=["region", "revenue"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    lower = result.lower()
    assert "only" in lower
    assert "model" in lower


def test_make_tool_spec_no_chart_type_by_default():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    query_section = spec.split('"compound_query"')[0]
    assert "chart_type" not in query_section


def test_make_tool_spec_injects_chart_type_for_llm():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("llm")
    assert "chart_type" in spec


def test_make_tool_spec_injects_chart_type_for_auto():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("auto")
    assert "chart_type" in spec


# --- Date context tests ---

def test_date_context_includes_current_date():
    result = _format_date_context(date(2026, 5, 8))
    assert "CURRENT_DATE = 2026-05-08" in result


def test_date_context_relative_dates_correct():
    result = _format_date_context(date(2026, 5, 8))
    assert '"yesterday" = 2026-05-07' in result
    assert '"last month" = 2026-04-01 to 2026-04-30' in result
    assert '"this year" = 2026-01-01 to 2026-05-08' in result


def test_date_context_in_project_layer():
    cfg = _make_cfg()
    result = _format_project_layer(cfg, today=date(2026, 5, 8))
    assert "CURRENT_DATE" in result
    assert "2026-05-08" in result


# --- Term resolution and derived metric tests ---

def test_glossary_layer_has_term_resolution_order():
    result = _format_glossary_layer([], [])
    assert "TERM RESOLUTION ORDER" in result
    assert "Exact glossary term" in result


def test_glossary_layer_has_derived_metric_rule():
    result = _format_glossary_layer([], [])
    assert "DERIVED METRIC RULE" in result
    assert "component measures" in result


def test_glossary_layer_has_alias_admissibility():
    result = _format_glossary_layer([], [])
    assert "ALIAS ADMISSIBILITY" in result
    assert "candidate mappings" in result


# --- Model selection rules tests ---

def test_model_layer_has_selection_rules():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    mid = uuid4()
    profile = _ModelProfile(
        id=mid, slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["revenue"], dimension_names=["region"],
        filterable_where_names=["region", "revenue"],
        sortable_names=["region", "revenue"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile], primary_model_id=mid)
    assert "MODEL SELECTION RULES" in result
    assert "follow-up" in result.lower()
    assert "glossary" in result.lower()


# --- Glossary retrieval gate tests ---

@pytest.mark.asyncio
async def test_glossary_retrieval_excludes_review_and_low_confidence():
    """retrieve_glossary_cards() must filter by visibility and confidence in the SQL WHERE clause."""
    from unittest.mock import AsyncMock, MagicMock
    import types
    import uuid as _uuid
    from src.retrieval.glossary import retrieve_glossary_cards

    mid = _uuid.uuid4()

    good_entry = types.SimpleNamespace(
        id=_uuid.uuid4(), model_id=mid, term="Revenue",
        definition="Total revenue", status="approved",
        visibility="show", confidence="high", sample_values=None,
    )

    db = AsyncMock()
    entries_result = MagicMock()
    entries_result.scalars.return_value.all.return_value = [good_entry]
    syn_result = MagicMock()
    syn_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[entries_result, syn_result])

    cards = await retrieve_glossary_cards(db, [mid], "revenue")
    assert len(cards) == 1
    assert cards[0].term == "Revenue"
    assert db.execute.call_count == 2

    stmt = db.execute.call_args_list[0][0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "visibility" in compiled, f"visibility filter missing from query: {compiled}"
    assert "confidence" in compiled, f"confidence filter missing from query: {compiled}"


@pytest.mark.asyncio
async def test_glossary_retrieval_accepts_null_metadata_for_legacy_entries():
    """Approved entries with NULL visibility/confidence (legacy/manual) must be eligible."""
    from unittest.mock import AsyncMock, MagicMock
    import types
    import uuid as _uuid
    from src.retrieval.glossary import retrieve_glossary_cards

    mid = _uuid.uuid4()

    legacy_entry = types.SimpleNamespace(
        id=_uuid.uuid4(), model_id=mid, term="Cost",
        definition="Total cost", status="approved",
        visibility=None, confidence=None, sample_values=None,
    )

    db = AsyncMock()
    entries_result = MagicMock()
    entries_result.scalars.return_value.all.return_value = [legacy_entry]
    syn_result = MagicMock()
    syn_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[entries_result, syn_result])

    cards = await retrieve_glossary_cards(db, [mid], "cost")
    assert len(cards) == 1
    assert cards[0].term == "Cost"


@pytest.mark.asyncio
async def test_glossary_retrieval_sql_uses_coalesce_for_visibility_confidence():
    """The compiled SQL uses COALESCE so partial-NULL legacy rows are accepted."""
    from unittest.mock import AsyncMock, MagicMock
    import uuid as _uuid
    from src.retrieval.glossary import retrieve_glossary_cards

    mid = _uuid.uuid4()

    db = AsyncMock()
    entries_result = MagicMock()
    entries_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=entries_result)

    await retrieve_glossary_cards(db, [mid], "anything")

    stmt = db.execute.call_args_list[0][0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    compiled_lower = compiled.lower()
    assert "coalesce" in compiled_lower
    assert "'show'" in compiled_lower
    assert "'medium'" in compiled_lower


# --- Spec rules tests ---

def test_spec_rules_safety_override():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "Safety refusal ALWAYS overrides" in spec


def test_spec_rules_clarify_for_ambiguity():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "clarify" in spec.lower()
    assert "ambiguous_abbreviation" not in spec


def test_spec_rules_has_where_guidance():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "WHERE rules" in spec


def test_spec_rules_has_limit_guidance():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "Limit rules" in spec


def test_spec_rules_has_final_validation_gate():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "FINAL VALIDATION GATE" in spec


def test_prompt_and_spec_list_all_accepted_top_level_tool_keys():
    from src.prompt.assembler import _TASK_PREAMBLE
    from src.tools.spec import make_tool_spec

    expected = [
        "query",
        "compound_query",
        "clarify",
        "refuse",
        "run_recipe",
        "evaluate_kpi",
        "preview_named_set",
        "create_aggregate",
    ]
    spec_gate = make_tool_spec("none").split("FINAL VALIDATION GATE", 1)[1]

    for key in expected:
        assert key in _TASK_PREAMBLE
        assert key in spec_gate


# --- History formatting tests ---

def test_history_includes_previous_query_plan():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Total revenue last month?"
    turn.answer_text = "USD 2,847,391"
    turn.llm_plan = {
        "tool": "query",
        "query": {
            "model_id": "abc-123",
            "measures": ["base_amount"],
            "filters": [{"name": "business_date", "op": "between",
                         "value": ["2026-04-01", "2026-04-30"]}],
        },
    }
    result = _format_history([turn])
    assert "PREVIOUS QUERY PLAN" in result
    assert "base_amount" in result
    assert "abc-123" in result


def test_history_no_plan_block_when_no_plan():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Hello"
    turn.answer_text = "Hi there"
    turn.llm_plan = None
    result = _format_history([turn])
    assert "PREVIOUS QUERY PLAN" not in result


def test_history_includes_plan_validation_rules():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Revenue?"
    turn.answer_text = "USD 100"
    turn.llm_plan = {"tool": "query", "query": {"model_id": "x"}}
    result = _format_history([turn])
    assert "PREVIOUS PLAN VALIDATION" in result
    assert "AVAILABLE MODELS" in result


# --- Set 2: Runtime robustness tests ---

def test_runtime_robustness_constant_exists():
    from src.prompt.assembler import _RUNTIME_ROBUSTNESS
    assert "TRUST HIERARCHY" in _RUNTIME_ROBUSTNESS
    assert "PROMPT INJECTION DEFENCE" in _RUNTIME_ROBUSTNESS


def test_runtime_robustness_treats_inputs_as_evidence():
    from src.prompt.assembler import _RUNTIME_ROBUSTNESS
    assert "evidence" in _RUNTIME_ROBUSTNESS.lower()
    assert "not instructions" in _RUNTIME_ROBUSTNESS.lower()


def test_runtime_robustness_lists_trust_levels():
    from src.prompt.assembler import _RUNTIME_ROBUSTNESS
    assert "1." in _RUNTIME_ROBUSTNESS
    assert "8." in _RUNTIME_ROBUSTNESS


# --- Set 2: Request classification tests ---

def test_task_preamble_has_request_classification():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "REQUEST CLASSIFICATION" in _TASK_PREAMBLE
    assert "Single KPI" in _TASK_PREAMBLE
    assert "Breakdown" in _TASK_PREAMBLE
    assert "Trend" in _TASK_PREAMBLE
    assert "Top-N" in _TASK_PREAMBLE


# --- Set 2: Glossary admissibility tests ---

def test_glossary_layer_has_glossary_admissibility():
    result = _format_glossary_layer([], [])
    assert "GLOSSARY ADMISSIBILITY" in result
    assert "candidate semantic hints" in result


def test_glossary_cards_framing_says_candidate():
    from src.retrieval.glossary import GlossaryCard
    card = GlossaryCard(model_id="mid", term="ARR", synonyms=[], definition="Annual recurring revenue")
    result = _format_glossary_layer([card], [])
    assert "candidate" in result.lower()
    assert "authoritative" not in result.lower()


def test_alias_map_framing_says_candidate():
    from src.retrieval.glossary import AliasMapBlock
    am = AliasMapBlock(model_id="mid", pairs={"revenue": "base_amount"})
    result = _format_glossary_layer([], [am])
    assert "candidate mappings" in result.lower()


def test_glossary_card_sample_values_appear_in_prompt():
    from src.retrieval.glossary import GlossaryCard
    card = GlossaryCard(
        model_id="mid", term="Country Code", synonyms=[],
        definition="ISO country code",
        sample_values=["AE", "DE", "GB", "US"],
    )
    result = _format_glossary_layer([card], [])
    assert "| values: AE, DE, GB, US" in result


def test_glossary_card_no_sample_values_no_pipe():
    from src.retrieval.glossary import GlossaryCard
    card = GlossaryCard(
        model_id="mid", term="Country Code", synonyms=[],
        definition="ISO country code",
        sample_values=None,
    )
    result = _format_glossary_layer([card], [])
    assert "| values:" not in result


def test_glossary_card_empty_sample_values_no_pipe():
    from src.retrieval.glossary import GlossaryCard
    card = GlossaryCard(
        model_id="mid", term="Country Code", synonyms=[],
        definition="ISO country code",
        sample_values=[],
    )
    result = _format_glossary_layer([card], [])
    assert "| values:" not in result


def test_glossary_retrieval_scores_sample_values():
    """Sample values should boost token overlap scoring."""
    from src.retrieval.glossary import _tokens, _score
    qtokens = _tokens("GB")
    bag_without = _tokens("Country Code") | _tokens("ISO country code")
    bag_with = bag_without | _tokens("AE DE GB US")
    assert _score(qtokens, bag_without) == 0
    assert _score(qtokens, bag_with) > 0


# --- Set 2: Field role rules tests ---

def test_model_layer_has_field_role_rules():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["region"],
        filterable_where_names=["amount", "region"],
        sortable_names=["amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "FIELD ROLE RULES" in result
    assert '"where"' in result
    assert '"having"' in result
    assert '"sort"' in result


# --- Set 2: Spec unsupported term rules ---

def test_spec_rules_has_unsupported_term_rules():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "Unsupported term" in spec
    assert "Do not invent fields" in spec


# --- Set 3+4: where/having/sort schema tests ---

def test_spec_schema_has_where_having_sort():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert '"where"' in spec
    assert '"having"' in spec
    assert '"sort"' in spec
    assert '"filters"' not in spec


def test_spec_schema_has_new_operators():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "gt" in spec
    assert "gte" in spec
    assert "lt" in spec
    assert "lte" in spec


def test_spec_has_having_rules():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "HAVING rules" in spec
    assert "aggregated results" in spec


def test_spec_has_sort_rules():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "SORT rules" in spec
    assert "desc" in spec
    assert "asc" in spec


def test_spec_refuse_no_ambiguous():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    preamble = spec.split("Schemas:")[0]
    assert "ambiguous" not in preamble.lower()


def test_spec_validation_gate_checks_where_having_sort():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "Every where field" in spec or "where field exists" in spec
    assert "Every having field" in spec or "having field exists" in spec
    assert "Every sort field" in spec or "sort field exists" in spec


def test_spec_validation_gate_core_intent_protection():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "core intent" in spec.lower() or "core question" in spec.lower()


# --- Set 3+4: parser backward compat tests ---

def test_parse_query_with_where():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "where": [{"name": "d", "op": "eq", "value": 1}], "having": [], "sort": []}}'
    call = parse_tool_call(raw)
    assert call.where == [{"name": "d", "op": "eq", "value": 1}]
    assert call.having == []
    assert call.sort == []


def test_parse_query_backward_compat_filters_to_where():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "filters": [{"name": "d", "op": "eq", "value": 1}]}}'
    call = parse_tool_call(raw)
    assert call.where == [{"name": "d", "op": "eq", "value": 1}]


def test_parse_query_having():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["count"], "dimensions": ["merchant"], "having": [{"name": "count", "op": "gt", "value": 100}]}}'
    call = parse_tool_call(raw)
    assert call.having == [{"name": "count", "op": "gt", "value": 100}]


def test_parse_query_sort():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "dimensions": ["merchant"], "sort": [{"name": "rev", "direction": "desc"}]}}'
    call = parse_tool_call(raw)
    assert call.sort == [{"name": "rev", "direction": "desc"}]


def test_parse_query_sort_defaults_desc():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "sort": [{"name": "rev"}]}}'
    call = parse_tool_call(raw)
    assert call.sort[0]["direction"] == "desc"


# --- Set 3+4: assembler where/having/sort tests ---

def test_task_preamble_follow_up_rules_use_where():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "where clauses" in _TASK_PREAMBLE.lower()
    assert "having clauses" in _TASK_PREAMBLE.lower()


def test_task_preamble_has_ranking_follow_up():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "PREVIOUS RESULT VALUES" in _TASK_PREAMBLE


def test_task_preamble_no_hardcoded_sensitive_rules():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "SENSITIVE FIELD RULES" not in _TASK_PREAMBLE
    assert "account_id" not in _TASK_PREAMBLE
    assert "ip_address" not in _TASK_PREAMBLE


def test_task_preamble_has_where_example():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert '"where"' in _TASK_PREAMBLE
    assert '"having"' in _TASK_PREAMBLE


def test_task_preamble_has_row_level_filter_example():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert '"op": "gt"' in _TASK_PREAMBLE


def test_trust_hierarchy_safety_first():
    from src.prompt.assembler import _RUNTIME_ROBUSTNESS
    lines = _RUNTIME_ROBUSTNESS.split("\n")
    for line in lines:
        if "1." in line:
            assert "safety" in line.lower() or "Safety" in line
            break


def test_date_context_bare_month_rule():
    result = _format_date_context(date(2026, 5, 8))
    assert "in January" in result
    assert "2026-01-01 to 2026-01-31" in result
    assert "in November" in result
    assert "2025-11-01 to 2025-11-30" in result


def test_date_context_runtime_injection_note():
    result = _format_date_context(date(2026, 5, 8))
    assert "injected at runtime" in result


def test_history_strips_tool_wrapper():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Revenue?"
    turn.answer_text = "USD 100"
    turn.llm_plan = {
        "tool": "query",
        "query": {"model_id": "x", "measures": ["rev"]},
    }
    result = _format_history([turn])
    assert '"tool"' not in result
    assert '"query"' in result


def test_history_migrates_filters_to_where():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Revenue?"
    turn.answer_text = "USD 100"
    turn.llm_plan = {
        "tool": "query",
        "query": {
            "model_id": "x",
            "measures": ["rev"],
            "filters": [{"name": "d", "op": "eq", "value": 1}],
        },
    }
    result = _format_history([turn])
    assert '"where"' in result
    assert '"filters"' not in result
    assert '"having"' in result


def test_history_plan_validation_mentions_where():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Revenue?"
    turn.answer_text = "USD 100"
    turn.llm_plan = {"tool": "query", "query": {"model_id": "x"}}
    result = _format_history([turn])
    assert "where fields" in result.lower() or "where" in result.lower()


# --- Set 3+4: Tagged field restriction tests ---

def test_model_layer_no_tags_no_restriction():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["region"],
        filterable_where_names=["amount", "region"],
        sortable_names=["amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "FIELD RESTRICTIONS" not in result


def test_model_layer_with_pii_restriction():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["account_id", "region"],
        filterable_where_names=["account_id", "amount", "region"],
        sortable_names=["account_id", "amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={"pii": ["account_id"]}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "FIELD RESTRICTIONS" in result
    assert "TAGGED" not in result
    assert "[pii]: account_id" in result
    assert "policy_denied_topic" in result


def test_model_layer_with_multiple_restrictions():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["account_id", "ip_address", "region"],
        filterable_where_names=["account_id", "amount", "ip_address", "region"],
        sortable_names=["account_id", "amount", "ip_address", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={
            "pii": ["account_id", "ip_address"],
            "internal": ["ip_address"],
        }, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "[pii]:" in result
    assert "[internal]:" in result
    assert "account_id" in result
    assert "ip_address" in result
    assert "tagged" not in result.lower()


# --- Set 5: Filterable where fields and sortable fields ---

def test_model_layer_has_filterable_where_fields():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount", "transaction_count"],
        dimension_names=["region", "business_date"],
        filterable_where_names=["amount", "business_date", "region"],
        sortable_names=["amount", "business_date", "region", "transaction_count"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "Filterable where fields:" in result
    assert "amount" in result.split("Filterable where fields:")[1].split("\n")[0]
    assert "region" in result.split("Filterable where fields:")[1].split("\n")[0]


def test_model_layer_has_sortable_fields():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount", "transaction_count"],
        dimension_names=["region"],
        filterable_where_names=["amount", "region"],
        sortable_names=["amount", "region", "transaction_count"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "Sortable fields:" in result
    assert "transaction_count" in result.split("Sortable fields:")[1].split("\n")[0]


def test_field_role_rules_reference_filterable_list():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["region"],
        filterable_where_names=["amount", "region"],
        sortable_names=["amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "Filterable where fields" in result
    rules = result.split("FIELD ROLE RULES")[1]
    assert "Filterable where fields" in rules


# --- Set 5: Sort rules strengthened ---

def test_spec_sort_rules_include_in_selected():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "sorting by a measure, include that measure" in spec
    assert "sorting by a dimension, include that dimension" in spec


# --- Set 5: Limit required ---

def test_spec_limit_required():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "required" in spec.lower()
    assert "Always include" in spec


def test_spec_always_include_arrays():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert 'Always include "where", "having", and "sort" as arrays' in spec


# --- Set 5: Term resolution order (canonical first) ---

def test_term_resolution_canonical_before_glossary():
    result = _format_glossary_layer([], [])
    lines = result.split("\n")
    order_start = None
    for i, line in enumerate(lines):
        if "TERM RESOLUTION ORDER" in line:
            order_start = i
            break
    assert order_start is not None
    resolution_lines = lines[order_start:order_start + 8]
    first_rule = next(l for l in resolution_lines if l.strip().startswith("1."))
    second_rule = next(l for l in resolution_lines if l.strip().startswith("2."))
    assert "measure or dimension" in first_rule.lower()
    assert "glossary" in second_rule.lower()


# --- Set 5: Examples E and F ---

def test_task_preamble_has_kpi_example():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "Example E" in _TASK_PREAMBLE
    assert "highest payment" in _TASK_PREAMBLE.lower() or "highest value" in _TASK_PREAMBLE.lower()
    assert '"chart_type": "kpi"' in _TASK_PREAMBLE


def test_task_preamble_has_record_example():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "Example F" in _TASK_PREAMBLE
    assert '"limit": 1' in _TASK_PREAMBLE


def test_task_preamble_has_generic_shape_contract_examples():
    from src.prompt.assembler import _TASK_PREAMBLE
    text = _TASK_PREAMBLE
    assert "Multi-series trend contract" in text
    assert "period/category/value" in text
    assert "multi_line" in text
    assert "Ranking contract" in text
    assert "Matrix contract" in text
    assert "Detail/table contract" in text
    assert "Chart preference contract" in text


def test_task_preamble_allows_model_measure_sources_without_field_names():
    from src.prompt.assembler import _TASK_PREAMBLE
    text = _TASK_PREAMBLE
    assert "UDA-backed" in text
    assert "calculated/formula model measures" in text
    assert "normal measures" in text
    assert "compound ratio trend contract" in text.lower()


def test_task_preamble_keeps_time_variant_measures_as_values():
    from src.prompt.assembler import _TASK_PREAMBLE
    text = _TASK_PREAMBLE
    assert "variant_kind" in text
    assert "keep it in the value role" in text
    assert "period/window facts" in text
    assert "stable temporal context" in text


def test_task_preamble_rejects_bare_cyclic_parts_for_trends():
    from src.prompt.assembler import _TASK_PREAMBLE
    text = _TASK_PREAMBLE
    assert "STABLE period key" in text
    assert "Do NOT use a bare cyclic part" in text
    assert '{"name": "<date dimension>", "grain": "month|quarter|year|week"}' in text


def test_task_preamble_examples_do_not_name_acme_demo_fields():
    from src.prompt.assembler import _TASK_PREAMBLE
    forbidden = [
        "base_amount",
        "business_date",
        "country_code",
        "payment_method",
        "transaction_amount",
        "transaction_count",
    ]
    for field_name in forbidden:
        assert field_name not in _TASK_PREAMBLE


# --- Set 5: Previous plan validation includes sort ---

def test_history_plan_validation_mentions_sort():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Revenue?"
    turn.answer_text = "USD 100"
    turn.llm_plan = {"tool": "query", "query": {"model_id": "x"}}
    result = _format_history([turn])
    assert "sort fields" in result.lower()
    assert "sort directions" in result.lower()
    assert "preserves" in result.lower() or "preserve" in result.lower()


# --- Set 5: No tag/persona leakage in restrictions ---

def test_field_restrictions_no_tag_persona_language():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["account_id", "region"],
        filterable_where_names=["account_id", "amount", "region"],
        sortable_names=["account_id", "amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={"pii": ["account_id"]}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    assert "tag" not in result.lower()
    assert "persona" not in result.lower()


# --- Set 6: Restricted fields excluded from filterable/sortable ---

def test_restricted_fields_excluded_from_filterable():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["account_id", "region"],
        filterable_where_names=["account_id", "amount", "region"],
        sortable_names=["account_id", "amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={"pii": ["account_id"]}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    filterable_line = [l for l in result.split("\n") if "Filterable where fields:" in l][0]
    assert "account_id" not in filterable_line
    assert "region" in filterable_line
    assert "amount" in filterable_line


def test_restricted_fields_excluded_from_sortable():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["account_id", "region"],
        filterable_where_names=["account_id", "amount", "region"],
        sortable_names=["account_id", "amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={"pii": ["account_id"]}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    sortable_line = [l for l in result.split("\n") if "Sortable fields:" in l][0]
    assert "account_id" not in sortable_line
    assert "region" in sortable_line


def test_no_restrictions_all_fields_in_filterable():
    from src.prompt.assembler import _format_model_layer, _ModelProfile
    from uuid import uuid4
    profile = _ModelProfile(
        id=uuid4(), slug="test", display_name="Test",
        overview=None, analytical_capabilities=None,
        abbreviation_conflict_rules=None, example_questions=[],
        measure_names=["amount"], dimension_names=["account_id", "region"],
        filterable_where_names=["account_id", "amount", "region"],
        sortable_names=["account_id", "amount", "region"],
        aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
        tagged_fields={}, dimension_value_hints={},
    )
    result = _format_model_layer([profile])
    filterable_line = [l for l in result.split("\n") if "Filterable where fields:" in l][0]
    assert "account_id" in filterable_line
    assert "region" in filterable_line
    assert "amount" in filterable_line


# --- Set 6: is_null/is_not_null value handling ---

def test_parse_query_is_null_value_normalised():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "where": [{"name": "d", "op": "is_null"}], "having": [], "sort": [], "limit": 100}}'
    call = parse_tool_call(raw)
    assert call.where[0]["value"] is None


def test_parse_query_is_null_explicit_null():
    from src.tools.spec import parse_tool_call
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "where": [{"name": "d", "op": "is_not_null", "value": null}], "having": [], "sort": [], "limit": 100}}'
    call = parse_tool_call(raw)
    assert call.where[0]["value"] is None


def test_spec_is_null_value_rule():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert 'is_null" and "is_not_null", the "value" field is not used' in spec


# --- Set 6: Sort cross-validation in gate ---

def test_spec_gate_sort_must_be_in_selected():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    assert "sort field is a measure, it must appear" in spec


# --- Set 6: All examples include limit ---

def test_task_preamble_all_examples_have_limit():
    # Ranking / record / KPI examples must carry an explicit limit. Structured
    # expression-dimension examples (grain shorthand or {"expr":..}) intentionally
    # OMIT limit: a date-grained trend relies on the Bug-5351 trend floor, which
    # only fires when the limit is defaulted, so teaching a default limit there
    # would clip long trends (Bug-5349 Example G/H).
    from src.prompt.assembler import _TASK_PREAMBLE
    import re
    outputs = re.findall(r'Output: (\{.*?\})\}', _TASK_PREAMBLE, re.DOTALL)
    for output in outputs:
        full = output + "}"
        if '"grain"' in full or '"expr"' in full:
            assert '"limit"' not in full, (
                f"Expression-dimension example must omit limit: {full[:80]}"
            )
            continue
        assert '"limit"' in full, f"Example output missing limit: {full[:80]}"


# --- Set 6: Filterable where field rule in gate ---

def test_spec_gate_where_references_filterable_list():
    from src.tools.spec import make_tool_spec
    spec = make_tool_spec("none")
    gate = spec.split("FINAL VALIDATION GATE")[1]
    assert "Filterable where fields" in gate


# --- Validation error tests (sort direction, limit, filter operator) ---

def test_parse_query_invalid_sort_direction_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "sort": [{"name": "rev", "direction": "UP"}], "limit": 100}}'
    with pytest.raises(ToolCallParseError, match="Invalid sort direction"):
        parse_tool_call(raw)


def test_parse_query_invalid_limit_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "limit": 5000}}'
    with pytest.raises(ToolCallParseError, match="query.limit must be"):
        parse_tool_call(raw)


def test_parse_query_invalid_limit_type_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "limit": "all"}}'
    with pytest.raises(ToolCallParseError, match="query.limit must be"):
        parse_tool_call(raw)


def test_parse_query_zero_limit_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "limit": 0}}'
    with pytest.raises(ToolCallParseError, match="query.limit must be"):
        parse_tool_call(raw)


def test_parse_query_invalid_filter_operator_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '{"query": {"model_id": "abc", "measures": ["rev"], "where": [{"name": "d", "op": "contains", "value": "x"}], "limit": 100}}'
    with pytest.raises(ToolCallParseError, match="Invalid where operator"):
        parse_tool_call(raw)


def test_parse_compound_invalid_limit_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '''{"compound_query": {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"], "limit": 9999},
            {"name": "b", "model_id": "m1", "measures": ["v"]}
        ],
        "expression": "a.v + b.v", "result_label": "total"
    }}'''
    with pytest.raises(ToolCallParseError, match="compound_query.steps\\[0\\].limit"):
        parse_tool_call(raw)


def test_parse_compound_invalid_sort_direction_raises():
    import pytest
    from src.tools.spec import parse_tool_call, ToolCallParseError
    raw = '''{"compound_query": {
        "steps": [
            {"name": "a", "model_id": "m1", "measures": ["v"],
             "sort": [{"name": "v", "direction": "ascending"}]},
            {"name": "b", "model_id": "m1", "measures": ["v"]}
        ],
        "expression": "a.v + b.v", "result_label": "total"
    }}'''
    with pytest.raises(ToolCallParseError, match="Invalid sort direction"):
        parse_tool_call(raw)


# --- History normalization: new nested plans render correctly ---

def test_history_renders_nested_query_plan():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Revenue?"
    turn.answer_text = "USD 100"
    turn.llm_plan = {
        "query": {
            "model_id": "x",
            "measures": ["base_amount"],
            "dimensions": [],
            "where": [{"name": "d", "op": "eq", "value": 1}],
            "having": [],
            "sort": [],
            "limit": 100,
        }
    }
    result = _format_history([turn])
    assert "PREVIOUS QUERY PLAN" in result
    assert '"query"' in result
    assert '"base_amount"' in result
    assert '"tool"' not in result


def test_history_renders_nested_compound_plan():
    from src.prompt.assembler import _format_history
    turn = MagicMock()
    turn.turn_index = 0
    turn.user_message = "Percentage?"
    turn.answer_text = "42%"
    turn.llm_plan = {
        "compound_query": {
            "steps": [
                {"name": "a", "model_id": "m1", "measures": ["v"]},
                {"name": "b", "model_id": "m1", "measures": ["v"]},
            ],
            "expression": "a.v / b.v * 100",
            "result_label": "share",
        }
    }
    result = _format_history([turn])
    assert "PREVIOUS QUERY PLAN" in result
    assert '"compound_query"' in result
    assert '"tool"' not in result


# --- PREVIOUS RESULT VALUES prompt instruction ---

def test_task_preamble_result_values_guides_to_clarify():
    from src.prompt.assembler import _TASK_PREAMBLE
    assert "PREVIOUS RESULT VALUES" in _TASK_PREAMBLE
    section = _TASK_PREAMBLE.split("PREVIOUS RESULT VALUES")[1][:300]
    assert "clarify" in section.lower()
