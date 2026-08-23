"""Bug-8181 / Bug-8370 lane L3 — checkable citation provenance.

Business outcome under test: a citation chip must carry enough to be
*checkable* (F-104-04) — a business definition, the route that served the
value, and a human-readable summary of the filters/grain that produced it —
not just kind/id/name/value. Covers:

- ``citations/builder.py``: ``describe_filter_grain`` (pure) and
  ``build_citations`` (definition/route_type/filter_grain on every citation).
- ``pipeline.py``: the single-query branch threads ``execution.route_type``
  and a ``describe_filter_grain(call.where, call.dimensions)`` summary into
  ``build_citations`` — i.e. the fields are actually surfaced on the turn
  payload, not just available on the builder function.

Round-2 (F-L3-R1-02): structured WHERE/HAVING predicates
(``QueryToolCall.where_refs``/``having_refs``) are an equally supported
production input as the flat ``{name, op, value}`` shape, and the round-1
formatter silently dropped them — a filtered query rendered as "unfiltered".
The tests below cover the structured-predicate path end to end (pure
formatter, and threaded through ``run_turn``) and the "never silently
unfiltered" invariant for a shape the formatter cannot describe.
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.citations.builder import (
    UNDESCRIBED_FILTER_TEXT,
    _describe_expr,
    _describe_predicate,
    build_citations,
    describe_filter_grain,
)
from src.tools.expressions import PredRef, normalize_predicate


MODEL_ID = uuid.uuid4()


def _pred_ref(raw: dict, *, clause: str = "where") -> PredRef:
    """Build a real PredRef the same way the tool-call parser does
    (tools/spec.py::_parse_filter_list), so these tests exercise the actual
    typed AST the query executes from, not a hand-rolled approximation."""
    return PredRef(node=normalize_predicate(raw, clause=clause), raw=raw)


# ---------------------------------------------------------------------------
# describe_filter_grain — pure function, no DB
# ---------------------------------------------------------------------------


class TestDescribeFilterGrain:
    def test_no_filter_no_grain_returns_none(self):
        assert describe_filter_grain([], []) is None
        assert describe_filter_grain(None, None) is None

    def test_filter_only(self):
        where = [{"name": "country", "op": "eq", "value": "US"}]
        assert describe_filter_grain(where, []) == "Filtered by country = US"

    def test_grain_only(self):
        assert describe_filter_grain([], ["business_month"]) == "Grouped by business_month"

    def test_filter_and_grain_combined(self):
        where = [{"name": "country", "op": "eq", "value": "US"}]
        result = describe_filter_grain(where, ["business_month"])
        assert result == "Filtered by country = US · Grouped by business_month"

    def test_multiple_filters_joined_with_semicolon(self):
        where = [
            {"name": "country", "op": "eq", "value": "US"},
            {"name": "revenue", "op": "gt", "value": 1000},
        ]
        result = describe_filter_grain(where, [])
        assert result == "Filtered by country = US; revenue > 1000"

    def test_in_operator_renders_a_list(self):
        where = [{"name": "country", "op": "in", "value": ["US", "CA"]}]
        assert describe_filter_grain(where, []) == "Filtered by country in (US, CA)"

    def test_between_operator(self):
        where = [{"name": "revenue", "op": "between", "value": [100, 200]}]
        assert describe_filter_grain(where, []) == "Filtered by revenue between 100 and 200"

    def test_multiple_grain_dimensions_joined_with_comma(self):
        result = describe_filter_grain([], ["country", "business_month"])
        assert result == "Grouped by country, business_month"

    def test_malformed_filter_entry_without_a_name_falls_back_to_the_marker(self):
        # Round-2 (F-L3-R1-02): a malformed entry must not read as
        # "unfiltered" either — the entry exists, it just could not be
        # described, so this must say so rather than silently claim no
        # filter applied.
        where = [{"op": "eq", "value": "US"}]
        assert describe_filter_grain(where, []) == f"Filtered by {UNDESCRIBED_FILTER_TEXT}"


class TestDescribeFilterGrainStructuredPredicates:
    """F-L3-R1-02 — structured where_refs/having_refs must be described,
    never dropped."""

    def test_simple_structured_comparison(self):
        ref = _pred_ref({"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}})
        result = describe_filter_grain([], [], where_refs=[ref])
        assert result == "Filtered by country = US"
        assert "unfiltered" not in result.lower()

    def test_function_on_column_comparison(self):
        # The exact repro from the review: LOWER(country) = 'us'.
        ref = _pred_ref(
            {
                "left": {"fn": "lower", "args": [{"field": "country"}]},
                "op": "eq",
                "right": {"literal": "us"},
            }
        )
        result = describe_filter_grain([], [], where_refs=[ref])
        assert result == "Filtered by lower(country) = us"
        # It must name the field the predicate actually filters on.
        assert "country" in result
        assert "unfiltered" not in result.lower()

    def test_boolean_and_composition(self):
        ref = _pred_ref(
            {
                "and": [
                    {"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}},
                    {"left": {"field": "revenue"}, "op": "gt", "right": {"literal": 1000}},
                ]
            }
        )
        result = describe_filter_grain([], [], where_refs=[ref])
        assert result == "Filtered by (country = US and revenue > 1000)"

    def test_boolean_or_composition(self):
        ref = _pred_ref(
            {
                "or": [
                    {"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}},
                    {"left": {"field": "country"}, "op": "eq", "right": {"literal": "CA"}},
                ]
            }
        )
        result = describe_filter_grain([], [], where_refs=[ref])
        assert result == "Filtered by (country = US or country = CA)"

    def test_not_predicate(self):
        ref = _pred_ref(
            {"not": {"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}}}
        )
        result = describe_filter_grain([], [], where_refs=[ref])
        assert result == "Filtered by not (country = US)"

    def test_in_and_between_operators(self):
        in_ref = _pred_ref(
            {"left": {"field": "country"}, "op": "in", "right": [{"literal": "US"}, {"literal": "CA"}]}
        )
        assert describe_filter_grain([], [], where_refs=[in_ref]) == "Filtered by country in (US, CA)"

        between_ref = _pred_ref(
            {"left": {"field": "revenue"}, "op": "between", "right": [{"literal": 100}, {"literal": 200}]}
        )
        assert (
            describe_filter_grain([], [], where_refs=[between_ref])
            == "Filtered by revenue between 100 and 200"
        )

    def test_mixed_flat_and_structured_where_entries_both_appear(self):
        flat = [{"name": "region", "op": "eq", "value": "EMEA"}]
        structured = [
            _pred_ref({"left": {"fn": "lower", "args": [{"field": "country"}]}, "op": "eq", "right": {"literal": "us"}})
        ]
        result = describe_filter_grain(flat, ["month"], where_refs=structured)
        assert result == "Filtered by region = EMEA; lower(country) = us · Grouped by month"

    def test_structured_having_widens_the_described_slice(self):
        having_ref = _pred_ref(
            {"left": {"fn": "sum", "args": [{"field": "amount"}]}, "op": "gt", "right": {"literal": 1000}},
            clause="having",
        )
        result = describe_filter_grain([], [], having_refs=[having_ref])
        assert result == "Having sum(amount) > 1000"

    def test_flat_having_also_widens_the_described_slice(self):
        result = describe_filter_grain(
            [], [], having=[{"name": "order_count", "op": "gt", "value": 5}]
        )
        assert result == "Having order_count > 5"

    def test_where_and_having_and_grain_all_combine(self):
        where_ref = _pred_ref({"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}})
        having_ref = _pred_ref(
            {"left": {"fn": "sum", "args": [{"field": "amount"}]}, "op": "gt", "right": {"literal": 1000}},
            clause="having",
        )
        result = describe_filter_grain(
            [], ["month"], where_refs=[where_ref], having_refs=[having_ref]
        )
        assert result == "Filtered by country = US · Having sum(amount) > 1000 · Grouped by month"

    def test_structured_predicate_result_is_never_none_or_unfiltered(self):
        # The exact business invariant the review is protecting: a query
        # that WAS filtered (via a structured predicate) must never present
        # as having no filter.
        ref = _pred_ref({"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}})
        result = describe_filter_grain(None, None, where_refs=[ref])
        assert result is not None
        assert "unfiltered" not in result.lower()
        assert "no filter" not in result.lower()


class TestUnrecognizedNodeFallback:
    """F-L3-R1-02 — a structured node this formatter does not recognise
    (Python does not enforce the ExprNode/PredNode Union at runtime) must
    fall back to the explicit marker, never to None/'unfiltered' and never
    to a silently-dropped partial description."""

    def test_describe_expr_raises_for_an_unrecognized_node_type(self):
        from src.citations.builder import _UndescribableNode

        with pytest.raises(_UndescribableNode):
            _describe_expr(object())  # not a FieldRef/Literal/FuncCall/Arith/Case

    def test_describe_predicate_raises_for_an_unrecognized_node_type(self):
        from src.citations.builder import _UndescribableNode

        with pytest.raises(_UndescribableNode):
            _describe_predicate(object())  # not a Comparison/BoolOp/NotPred

    def test_clause_falls_back_to_the_marker_for_an_unrecognized_predicate_node(self):
        bogus_ref = PredRef(node=object(), raw={})  # type: ignore[arg-type]
        result = describe_filter_grain([], [], where_refs=[bogus_ref])
        assert result == f"Filtered by {UNDESCRIBED_FILTER_TEXT}"

    def test_one_unrecognized_predicate_replaces_the_whole_clause_not_just_itself(self):
        # A partial description (naming the good predicate, silently
        # dropping the bad one) would be a false precision claim. The whole
        # clause must fall back, not just the unrecognisable entry.
        good_ref = _pred_ref({"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}})
        bogus_ref = PredRef(node=object(), raw={})  # type: ignore[arg-type]
        result = describe_filter_grain([], [], where_refs=[good_ref, bogus_ref])
        assert result == f"Filtered by {UNDESCRIBED_FILTER_TEXT}"
        assert "country" not in result


# ---------------------------------------------------------------------------
# build_citations — provenance fields on every citation
# ---------------------------------------------------------------------------


def _fake_measure(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        name="revenue",
        display_name="Revenue",
        description="Total gross revenue recognised in the period.",
        expression=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _fake_dimension(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        name="country",
        display_name="Country",
        description="The customer's billing country.",
        calc_expression=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _scalars_result(items):
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


@pytest.mark.asyncio
class TestBuildCitationsProvenance:
    async def test_measure_citation_carries_definition_route_and_filter_grain(self):
        measure = _fake_measure()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([measure]))

        citations = await build_citations(
            db, MODEL_ID, ["revenue"], [], [{"revenue": 4200000}],
            route_type="aggregate",
            filter_grain="Filtered by country = US · Grouped by business_month",
        )

        assert len(citations) == 1
        c = citations[0]
        assert c["kind"] == "measure"
        assert c["id"] == str(measure.id)
        assert c["display_name"] == "Revenue"
        assert c["value"] == 4200000
        assert c["definition"] == "Total gross revenue recognised in the period."
        assert c["route_type"] == "aggregate"
        assert c["filter_grain"] == "Filtered by country = US · Grouped by business_month"

    async def test_measure_definition_falls_back_to_expression_when_no_description(self):
        measure = _fake_measure(description=None, expression="SUM(amount) / SUM(qty)")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([measure]))

        citations = await build_citations(db, MODEL_ID, ["revenue"], [], [])

        assert citations[0]["definition"] == "SUM(amount) / SUM(qty)"

    async def test_measure_definition_is_none_when_neither_description_nor_expression(self):
        measure = _fake_measure(description=None, expression=None)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([measure]))

        citations = await build_citations(db, MODEL_ID, ["revenue"], [], [])

        assert citations[0]["definition"] is None

    async def test_dimension_citation_carries_definition_route_and_filter_grain(self):
        dim = _fake_dimension()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([dim]))

        citations = await build_citations(
            db, MODEL_ID, [], ["country"], [],
            route_type="pocket",
            filter_grain="Grouped by country",
        )

        assert len(citations) == 1
        c = citations[0]
        assert c["kind"] == "dimension"
        assert c["value"] is None
        assert c["definition"] == "The customer's billing country."
        assert c["route_type"] == "pocket"
        assert c["filter_grain"] == "Grouped by country"

    async def test_route_type_and_filter_grain_default_to_none_when_not_supplied(self):
        # Bug escape guard: the pre-lane call site (and any future caller
        # that forgets the new kwargs) must not crash, and must not silently
        # invent a route/filter — it must come through as an explicit None.
        measure = _fake_measure()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([measure]))

        citations = await build_citations(db, MODEL_ID, ["revenue"], [], [])

        assert citations[0]["route_type"] is None
        assert citations[0]["filter_grain"] is None


# ---------------------------------------------------------------------------
# pipeline.run_turn — the fields are actually surfaced on the turn payload,
# not just available on build_citations.
# ---------------------------------------------------------------------------


def _cfg():
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        judge_mode="async",
        safety_policy="",
        chart_type_selector="none",
        agent_output_format="plain",
        max_query_complexity=0,
    )


def _query_tool_json(model_id: str) -> str:
    return json.dumps({
        "query": {
            "model_id": model_id,
            "measures": ["revenue"],
            "dimensions": ["country"],
            "where": [{"name": "country", "op": "eq", "value": "US"}],
            "having": [],
            "sort": [],
            "limit": 100,
        }
    })


def _execution_stub(route_type="aggregate"):
    return types.SimpleNamespace(
        sql="SELECT 1",
        columns=["revenue"],
        rows=[{"revenue": 100}],
        rows_returned=1,
        route_type=route_type,
        routed_sql="SELECT 1",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=5,
    )


@pytest.mark.asyncio
async def test_run_turn_threads_route_type_and_filter_grain_into_build_citations():
    from src.pipeline import run_turn

    model_uuid = uuid.uuid4()
    cfg = _cfg()
    conv = types.SimpleNamespace(id=uuid.uuid4(), persona_id=None, pinned_model_id=None)
    bundle = types.SimpleNamespace(
        system="sys", user="user", narration_system="narr",
        allow_list_model_ids={model_uuid}, prior_questions=[],
        persona_scopes=None,
    )
    llm_cfg = types.SimpleNamespace(
        provider="anthropic", model_name="m", timeout_seconds=30, display_name="cfg",
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=_query_tool_json(str(model_uuid)))
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}

    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model"))

    citations_mock = AsyncMock(return_value=[])

    with (
        patch("src.pipeline.scan_input_message",
              return_value=types.SimpleNamespace(ok=True, reason=None, matched_topic=None)),
        patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.pipeline.resolve_agent_llm_failover_configs",
              AsyncMock(return_value=[llm_cfg])),
        patch("src.pipeline.check_budget", AsyncMock(return_value=None)),
        patch("src.pipeline.build_adapter", return_value=adapter),
        patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a),
        patch("src.pipeline.execute_query", AsyncMock(return_value=_execution_stub("aggregate"))),
        patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})),
        patch("src.pipeline.narrate_answer_stream", AsyncMock(return_value="Revenue was 100.")),
        patch("src.pipeline.narrate_answer", AsyncMock(return_value="Revenue was 100.")),
        patch("src.pipeline.apply_output_guardrails",
              side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline.build_citations", citations_mock),
    ):
        outcome = await run_turn(
            db=db, cfg=cfg, conversation=conv,
            user_message="What is US revenue by country?", jwt_token="token",
        )

    assert outcome.status == "ok"
    assert citations_mock.await_count == 1
    _, kwargs = citations_mock.call_args
    assert kwargs["route_type"] == "aggregate"
    assert kwargs["filter_grain"] == "Filtered by country = US · Grouped by country"


def _query_tool_json_with_structured_where(model_id: str) -> str:
    """A plan whose WHERE entry is a structured predicate (function-on-
    column), the exact shape F-L3-R1-02 found dropped: LOWER(country) = 'us'.
    """
    return json.dumps({
        "query": {
            "model_id": model_id,
            "measures": ["revenue"],
            "dimensions": [],
            "where": [
                {
                    "left": {"fn": "lower", "args": [{"field": "country"}]},
                    "op": "eq",
                    "right": {"literal": "us"},
                }
            ],
            "having": [],
            "sort": [],
            "limit": 100,
        }
    })


@pytest.mark.asyncio
async def test_run_turn_threads_structured_where_refs_into_build_citations():
    # F-L3-R1-02 — a structured predicate must reach build_citations too,
    # not just the flat `where` list, and the resulting filter_grain must
    # name the actual filter rather than reading as unfiltered.
    from src.pipeline import run_turn

    model_uuid = uuid.uuid4()
    cfg = _cfg()
    conv = types.SimpleNamespace(id=uuid.uuid4(), persona_id=None, pinned_model_id=None)
    bundle = types.SimpleNamespace(
        system="sys", user="user", narration_system="narr",
        allow_list_model_ids={model_uuid}, prior_questions=[],
        persona_scopes=None,
    )
    llm_cfg = types.SimpleNamespace(
        provider="anthropic", model_name="m", timeout_seconds=30, display_name="cfg",
    )
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=_query_tool_json_with_structured_where(str(model_uuid))
    )
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}

    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model"))

    citations_mock = AsyncMock(return_value=[])

    with (
        patch("src.pipeline.scan_input_message",
              return_value=types.SimpleNamespace(ok=True, reason=None, matched_topic=None)),
        patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.pipeline.resolve_agent_llm_failover_configs",
              AsyncMock(return_value=[llm_cfg])),
        patch("src.pipeline.check_budget", AsyncMock(return_value=None)),
        patch("src.pipeline.build_adapter", return_value=adapter),
        patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a),
        patch("src.pipeline.execute_query", AsyncMock(return_value=_execution_stub("aggregate"))),
        patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})),
        patch("src.pipeline.narrate_answer_stream", AsyncMock(return_value="Revenue was 100.")),
        patch("src.pipeline.narrate_answer", AsyncMock(return_value="Revenue was 100.")),
        patch("src.pipeline.apply_output_guardrails",
              side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline.build_citations", citations_mock),
    ):
        outcome = await run_turn(
            db=db, cfg=cfg, conversation=conv,
            user_message="What is US revenue (lowercase country)?", jwt_token="token",
        )

    assert outcome.status == "ok"
    assert citations_mock.await_count == 1
    _, kwargs = citations_mock.call_args
    filter_grain = kwargs["filter_grain"]
    # The exact business assertion the review demanded: NOT "unfiltered",
    # and it NAMES the filter.
    assert filter_grain is not None
    assert "unfiltered" not in filter_grain.lower()
    assert "no filter" not in filter_grain.lower()
    assert "country" in filter_grain
    assert filter_grain == "Filtered by lower(country) = us"


@pytest.mark.asyncio
async def test_run_turn_passes_none_filter_grain_for_an_unfiltered_ungrouped_query():
    from src.pipeline import run_turn

    model_uuid = uuid.uuid4()
    cfg = _cfg()
    conv = types.SimpleNamespace(id=uuid.uuid4(), persona_id=None, pinned_model_id=None)
    bundle = types.SimpleNamespace(
        system="sys", user="user", narration_system="narr",
        allow_list_model_ids={model_uuid}, prior_questions=[],
        persona_scopes=None,
    )
    llm_cfg = types.SimpleNamespace(
        provider="anthropic", model_name="m", timeout_seconds=30, display_name="cfg",
    )
    plan = json.dumps({
        "query": {
            "model_id": str(model_uuid),
            "measures": ["revenue"],
            "dimensions": [],
            "where": [],
            "having": [],
            "sort": [],
            "limit": 100,
        }
    })
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=plan)
    adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}

    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(display_name="Model"))

    citations_mock = AsyncMock(return_value=[])

    with (
        patch("src.pipeline.scan_input_message",
              return_value=types.SimpleNamespace(ok=True, reason=None, matched_topic=None)),
        patch("src.pipeline.assemble_prompt", AsyncMock(return_value=bundle)),
        patch("src.pipeline.resolve_agent_llm_failover_configs",
              AsyncMock(return_value=[llm_cfg])),
        patch("src.pipeline.check_budget", AsyncMock(return_value=None)),
        patch("src.pipeline.build_adapter", return_value=adapter),
        patch("src.pipeline.RetryingAdapter", side_effect=lambda a: a),
        patch("src.pipeline.execute_query", AsyncMock(return_value=_execution_stub("source"))),
        patch("src.pipeline._load_measure_formats", AsyncMock(return_value={})),
        patch("src.pipeline.narrate_answer_stream", AsyncMock(return_value="Revenue was 100.")),
        patch("src.pipeline.narrate_answer", AsyncMock(return_value="Revenue was 100.")),
        patch("src.pipeline.apply_output_guardrails",
              side_effect=lambda c, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline.build_citations", citations_mock),
    ):
        outcome = await run_turn(
            db=db, cfg=cfg, conversation=conv,
            user_message="What is total revenue?", jwt_token="token",
        )

    assert outcome.status == "ok"
    _, kwargs = citations_mock.call_args
    assert kwargs["route_type"] == "source"
    assert kwargs["filter_grain"] is None
