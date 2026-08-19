"""Tests for the eval harness decomposition comparison feature.

Covers:
  - String and dict decomposition parsing/normalisation.
  - Semantic (set-based, case-insensitive) comparison of model, measures,
    dimensions, and filters.
  - EvalRow and EvalReport population with decomposition_match,
    decomposition_comparison, accuracy_score, decomposition_regressions.
  - Edge cases: no expected decomposition, no plan, compound/recipe plans,
    partial field matches, empty lists.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch as _patch

import pytest

from src.eval.comparison import (
    DecompositionComparison,
    FieldComparison,
    compare_decomposition,
    normalise_decomposition,
    _extract_actual_decomposition,
    _parse_string_decomposition,
)


# ---------------------------------------------------------------------------
# normalise_decomposition
# ---------------------------------------------------------------------------


class TestNormaliseDecomposition:
    def test_none_returns_none(self):
        assert normalise_decomposition(None) is None

    def test_empty_string_returns_none(self):
        assert normalise_decomposition("") is None
        assert normalise_decomposition("   ") is None

    def test_dict_passthrough(self):
        raw = {
            "model": "modelx",
            "measures": ["revenue", "quantity"],
            "dimensions": ["region"],
            "filters": ["year = 2024"],
        }
        result = normalise_decomposition(raw)
        assert result is not None
        assert result["model"] == "modelx"
        assert result["measures"] == ["revenue", "quantity"]
        assert result["dimensions"] == ["region"]
        assert result["filters"] == ["year = 2024"]

    def test_dict_strips_whitespace(self):
        raw = {"model": "  modelx  ", "measures": [" revenue ", " qty "]}
        result = normalise_decomposition(raw)
        assert result["model"] == "modelx"
        assert result["measures"] == ["revenue", "qty"]

    def test_dict_ignores_unknown_keys(self):
        raw = {"model": "modelx", "unknown_key": "value"}
        result = normalise_decomposition(raw)
        assert "unknown_key" not in result

    def test_string_semicolon_delimited(self):
        raw = "model: modelx; measures: revenue, quantity; dimensions: region"
        result = normalise_decomposition(raw)
        assert result is not None
        assert result["model"] == "modelx"
        assert set(result["measures"]) == {"revenue", "quantity"}
        assert result["dimensions"] == ["region"]

    def test_string_with_brackets(self):
        raw = "model: modelx, measures: [revenue, quantity], dimensions: [region], filters: [year = 2024]"
        result = normalise_decomposition(raw)
        assert result is not None
        assert result["model"] == "modelx"
        assert set(result["measures"]) == {"revenue", "quantity"}

    def test_empty_dict_returns_none(self):
        assert normalise_decomposition({}) is None

    def test_non_string_non_dict_returns_none(self):
        assert normalise_decomposition(42) is None
        assert normalise_decomposition([1, 2]) is None


# ---------------------------------------------------------------------------
# _extract_actual_decomposition
# ---------------------------------------------------------------------------


class TestExtractActualDecomposition:
    def test_none_plan(self):
        assert _extract_actual_decomposition(None) is None

    def test_empty_dict(self):
        assert _extract_actual_decomposition({}) is None

    def test_query_plan(self):
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["revenue", "quantity"],
                "dimensions": ["region", "year"],
                "where": ["year = 2024"],
                "having": [],
                "sort": [],
                "limit": None,
            }
        }
        result = _extract_actual_decomposition(plan)
        assert result is not None
        assert result["model"] == "modelx"
        assert result["measures"] == ["revenue", "quantity"]
        assert result["dimensions"] == ["region", "year"]
        assert result["filters"] == ["year = 2024"]

    def test_recipe_plan_returns_none(self):
        plan = {"run_recipe": {"recipe_id": "abc", "parameters": {}}}
        assert _extract_actual_decomposition(plan) is None

    def test_compound_query_returns_none(self):
        plan = {"compound_query": {"steps": [], "expression": "a + b"}}
        assert _extract_actual_decomposition(plan) is None

    def test_where_and_having_combined(self):
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["revenue"],
                "dimensions": [],
                "where": ["region = East"],
                "having": ["revenue > 100"],
            }
        }
        result = _extract_actual_decomposition(plan)
        assert set(result["filters"]) == {"region = East", "revenue > 100"}


# ---------------------------------------------------------------------------
# compare_decomposition
# ---------------------------------------------------------------------------


class TestCompareDecomposition:
    def test_no_expected_is_vacuous_pass(self):
        result = compare_decomposition(None, {"query": {"model_id": "x"}})
        assert result.matched is True
        assert result.skipped is True
        assert result.skip_reason == "no_expected_decomposition"

    def test_no_plan_with_expected_fails(self):
        result = compare_decomposition(
            {"model": "modelx", "measures": ["revenue"]},
            None,
        )
        assert result.matched is False
        assert len(result.fields) == 1
        assert result.fields[0].field_name == "plan"

    def test_full_match(self):
        expected = {
            "model": "modelx",
            "measures": ["revenue", "quantity"],
            "dimensions": ["region"],
            "filters": ["year = 2024"],
        }
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["quantity", "revenue"],  # different order
                "dimensions": ["region"],
                "where": ["year = 2024"],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True
        assert all(f.matched for f in result.fields)

    def test_case_insensitive_model(self):
        expected = {"model": "ModelX"}
        plan = {"query": {"model_id": "modelx", "measures": [], "dimensions": []}}
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_case_insensitive_measures(self):
        expected = {"measures": ["Revenue", "QUANTITY"]}
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["revenue", "quantity"],
                "dimensions": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_measure_mismatch_reports_diff(self):
        expected = {"measures": ["revenue", "quantity"]}
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["revenue", "profit"],
                "dimensions": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is False
        measures_field = [f for f in result.fields if f.field_name == "measures"][0]
        assert measures_field.matched is False
        assert "missing" in measures_field.detail
        assert "extra" in measures_field.detail

    def test_dimension_mismatch(self):
        expected = {"dimensions": ["region", "year"]}
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": [],
                "dimensions": ["region"],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is False

    def test_filter_mismatch(self):
        expected = {"filters": ["year = 2024"]}
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": [],
                "dimensions": [],
                "where": ["year = 2025"],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is False

    def test_partial_expected_fields_only_checked(self):
        """When expected only specifies model + measures, dimensions and
        filters are not penalised even if the plan has them."""
        expected = {"model": "modelx", "measures": ["revenue"]}
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["revenue"],
                "dimensions": ["region", "year"],
                "where": ["year = 2024"],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True
        # Only model + measures should have field comparisons.
        assert len(result.fields) == 2

    def test_string_decomposition_compared(self):
        expected_str = "model: modelx; measures: revenue, quantity; dimensions: region"
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["quantity", "revenue"],
                "dimensions": ["region"],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected_str, plan)
        assert result.matched is True


# ---------------------------------------------------------------------------
# Integration: runner populates decomposition fields
# ---------------------------------------------------------------------------


class TestRunnerDecompositionIntegration:
    @pytest.mark.asyncio
    async def test_decomposition_match_populated(self):
        """When a question has an expected decomposition and the plan
        matches, decomposition_match is True and accuracy_score is 1.0."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {
                    "q": "How many sales?",
                    "decomposition": {
                        "model": "modelx",
                        "measures": ["sales_count"],
                    },
                }
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={"query": {"model_id": "modelx", "measures": ["sales_count"], "dimensions": []}},
            answer_text="42",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.total == 1
        assert report.ok == 1
        row = report.rows[0]
        assert row.decomposition_match is True
        assert row.decomposition_comparison is not None
        assert row.decomposition_comparison["matched"] is True
        assert report.accuracy_score == 1.0
        assert report.decomposition_regressions == 0

    @pytest.mark.asyncio
    async def test_decomposition_mismatch_counted(self):
        """When expected decomposition does not match, the row reports
        a mismatch, accuracy_score reflects it, and regressions count."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {
                    "q": "Revenue by region",
                    "decomposition": {
                        "model": "modelx",
                        "measures": ["revenue"],
                        "dimensions": ["region"],
                    },
                },
                {
                    "q": "Total quantity",
                    "decomposition": {
                        "model": "modelx",
                        "measures": ["quantity"],
                    },
                },
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        # First question matches, second mismatches (wrong measure).
        outcomes = [
            types.SimpleNamespace(
                status="ok",
                plan={"query": {"model_id": "modelx", "measures": ["revenue"], "dimensions": ["region"]}},
                answer_text="ok",
                provider="anthropic",
                usage_input_tokens=100,
                usage_output_tokens=50,
            ),
            types.SimpleNamespace(
                status="ok",
                plan={"query": {"model_id": "modelx", "measures": ["revenue"], "dimensions": []}},
                answer_text="wrong measure",
                provider="anthropic",
                usage_input_tokens=100,
                usage_output_tokens=50,
            ),
        ]
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(side_effect=outcomes)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.total == 2
        assert report.rows[0].decomposition_match is True
        assert report.rows[1].decomposition_match is False
        assert report.accuracy_score == 0.5
        assert report.decomposition_regressions == 1

    @pytest.mark.asyncio
    async def test_no_decomposition_not_counted(self):
        """Questions without an expected decomposition should not affect
        accuracy_score (it stays None when all are skipped)."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[{"q": "How many sales?"}],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={"query": {"model_id": "modelx", "measures": ["sales"], "dimensions": []}},
            answer_text="42",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.total == 1
        row = report.rows[0]
        assert row.decomposition_match is None
        assert report.accuracy_score is None
        assert report.decomposition_regressions == 0

    @pytest.mark.asyncio
    async def test_budget_stop_preserves_accuracy(self):
        """When budget stops the eval early, the accuracy_score should
        still reflect whatever was already compared."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {
                    "q": "Revenue?",
                    "decomposition": {"model": "modelx", "measures": ["revenue"]},
                },
                {
                    "q": "Quantity?",
                    "decomposition": {"model": "modelx", "measures": ["quantity"]},
                },
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={"query": {"model_id": "modelx", "measures": ["revenue"], "dimensions": []}},
            answer_text="ok",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        budget_returns = [None, "daily_token_budget_exceeded"]
        with (
            _patch.object(
                runner, "check_budget", AsyncMock(side_effect=budget_returns)
            ),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.total == 1
        assert report.budget_stopped == "daily_token_budget_exceeded"
        # First question was compared before budget stop.
        assert report.accuracy_score == 1.0


# ---------------------------------------------------------------------------
# R0(c): the eval run FAILS (regressed) on any decomposition regression.
# ---------------------------------------------------------------------------


class TestRegressedGate:
    def test_regressed_true_when_any_mismatch(self):
        from src.eval.runner import EvalReport

        report = EvalReport(project_id="p", decomposition_regressions=1)
        assert report.regressed is True

    def test_regressed_false_when_all_matched(self):
        from src.eval.runner import EvalReport

        report = EvalReport(project_id="p", decomposition_regressions=0)
        assert report.regressed is False

    def test_regressed_false_when_nothing_compared(self):
        """A run where no question carried an expected decomposition has
        zero regressions, so it is not a failing run."""
        from src.eval.runner import EvalReport

        report = EvalReport(project_id="p")
        assert report.decomposition_regressions == 0
        assert report.accuracy_score is None
        assert report.regressed is False

    @pytest.mark.asyncio
    async def test_mismatch_run_reports_regressed(self):
        """End-to-end through the runner: a mismatching plan drives the
        report's regressed gate to True."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {
                    "q": "Revenue by region",
                    "decomposition": {"model": "modelx", "measures": ["revenue"]},
                }
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={"query": {"model_id": "modelx", "measures": ["profit"], "dimensions": []}},
            answer_text="wrong measure",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        # Status is "ok" (a plan came back) but the plan is WRONG — the
        # whole point of R0: a smoke-green run must still fail the gate.
        assert report.ok == 1
        assert report.decomposition_regressions == 1
        assert report.regressed is True

    def test_api_serialises_regressed_flag(self):
        """The HTTP layer must surface ``regressed`` — it is a computed
        property, so ``asdict`` drops it. The endpoint serialises through
        ``report_to_out`` (single home), which this test exercises."""
        from src.api.eval import report_to_out
        from src.eval.runner import EvalReport

        report = EvalReport(project_id="p", total=1, ok=1, decomposition_regressions=1)
        out = report_to_out(report)
        assert out.regressed is True
        assert out.decomposition_regressions == 1

        clean = EvalReport(project_id="p", total=1, ok=1, decomposition_regressions=0)
        out_clean = report_to_out(clean)
        assert out_clean.regressed is False

    def test_api_serialises_row_comparison_and_counters(self):
        """report_to_out must carry the per-row structured diff (typed
        model) and the compared/unparseable counters end to end."""
        from src.api.eval import report_to_out
        from src.eval.runner import EvalReport, EvalRow

        row = EvalRow(
            model_id="m",
            question="q",
            expected_decomposition={"measures": ["revenue"]},
            status="ok",
            decomposition_match=False,
            decomposition_comparison={
                "matched": False,
                "skipped": False,
                "fields": [
                    {
                        "field_name": "measures",
                        "matched": False,
                        "expected": ["revenue"],
                        "actual": ["profit"],
                        "detail": "missing: ['revenue']; extra: ['profit']",
                    }
                ],
            },
        )
        report = EvalReport(
            project_id="p",
            total=1,
            ok=1,
            rows=[row],
            decomposition_regressions=1,
            decomposition_compared=1,
            decomposition_unparseable=2,
        )
        out = report_to_out(report)
        assert out.decomposition_compared == 1
        assert out.decomposition_unparseable == 2
        comp = out.rows[0].decomposition_comparison
        assert comp is not None
        assert comp.matched is False
        assert comp.fields[0].field_name == "measures"
        assert comp.fields[0].detail is not None


# ---------------------------------------------------------------------------
# Review round 1 regression guards
# ---------------------------------------------------------------------------


class TestModelUuidVsName:
    """Blocking finding 1: the real plan carries a model UUID; authored
    baselines carry a name. The question's context model is the
    authoritative baseline."""

    def test_uuid_plan_matches_via_context_model(self):
        ctx_uuid = str(uuid.uuid4())
        expected = {"model": "modelx", "measures": ["revenue"]}
        plan = {
            "query": {
                "model_id": ctx_uuid,
                "measures": ["revenue"],
                "dimensions": [],
            }
        }
        result = compare_decomposition(
            expected, plan, context_model_id=ctx_uuid
        )
        assert result.matched is True

    def test_uuid_plan_wrong_model_fails(self):
        """Plan routed to a DIFFERENT model than the question's context —
        the exact wrong-model regression the gate must catch."""
        ctx_uuid = str(uuid.uuid4())
        other_uuid = str(uuid.uuid4())
        expected = {"model": "modelx", "measures": ["revenue"]}
        plan = {
            "query": {
                "model_id": other_uuid,
                "measures": ["revenue"],
                "dimensions": [],
            }
        }
        result = compare_decomposition(
            expected, plan, context_model_id=ctx_uuid
        )
        assert result.matched is False
        model_field = [f for f in result.fields if f.field_name == "model"][0]
        assert model_field.matched is False

    def test_exact_identifier_still_matches_without_context(self):
        expected = {"model": "ModelX"}
        plan = {"query": {"model_id": "modelx", "measures": [], "dimensions": []}}
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    @pytest.mark.asyncio
    async def test_runner_passes_context_model(self):
        """End-to-end: a realistic UUID-carrying plan with a name-based
        expected decomposition must NOT regress (this was permanently red
        before the fix)."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        model_uuid = uuid.uuid4()
        ctx = types.SimpleNamespace(
            model_id=model_uuid,
            example_questions=[
                {
                    "q": "Revenue by region",
                    # Real data format: freeform string with a model NAME.
                    "decomposition": "model: modelx; measures: revenue; dimensions: region",
                }
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={
                "query": {
                    # Real plans carry the model UUID, never the name.
                    "model_id": str(model_uuid),
                    "measures": ["revenue"],
                    "dimensions": ["region"],
                    "where": [],
                    "having": [],
                }
            },
            answer_text="ok",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.rows[0].decomposition_match is True
        assert report.accuracy_score == 1.0
        assert report.regressed is False


class TestUnparseableDecomposition:
    """Blocking finding 2: a present-but-unparseable baseline must never
    become a silent vacuous pass."""

    def test_prose_string_reported_unparseable(self):
        result = compare_decomposition(
            "sum revenue grouped by region", {"query": {"model_id": "x"}}
        )
        assert result.skipped is True
        assert result.skip_reason == "unparseable_decomposition"
        assert result.matched is False

    def test_unrecognised_dict_reported_unparseable(self):
        result = compare_decomposition(
            {"something_else": "value"}, {"query": {"model_id": "x"}}
        )
        assert result.skipped is True
        assert result.skip_reason == "unparseable_decomposition"

    def test_absent_still_vacuous(self):
        result = compare_decomposition(None, {"query": {"model_id": "x"}})
        assert result.skip_reason == "no_expected_decomposition"
        assert result.matched is True

    @pytest.mark.asyncio
    async def test_runner_counts_unparseable(self):
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {"q": "Revenue?", "decomposition": "just some prose here"},
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={"query": {"model_id": "x", "measures": ["m"], "dimensions": []}},
            answer_text="ok",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.decomposition_unparseable == 1
        assert report.decomposition_compared == 0
        # Not counted as a regression (would make the gate permanently
        # red on prose baselines), but loudly visible in the report.
        assert report.regressed is False
        assert report.rows[0].decomposition_match is None
        assert (
            report.rows[0].decomposition_comparison["skip_reason"]
            == "unparseable_decomposition"
        )


class TestStatusRegression:
    """Finding 3: a refused/clarify/error turn on a baselined question is
    a regression even when the rejected plan matches the baseline."""

    def test_refused_with_matching_plan_is_regression(self):
        expected = {"model": "modelx", "measures": ["revenue"]}
        plan = {
            "query": {"model_id": "modelx", "measures": ["revenue"], "dimensions": []}
        }
        result = compare_decomposition(expected, plan, turn_status="refused")
        assert result.matched is False
        assert result.fields[0].field_name == "status"
        assert "refused" in result.fields[0].detail

    @pytest.mark.asyncio
    async def test_runner_counts_refused_as_regression(self):
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {
                    "q": "Revenue?",
                    "decomposition": {"model": "modelx", "measures": ["revenue"]},
                },
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="refused",
            # The pipeline still emits the (rejected) plan on refusal —
            # it must NOT count as a match.
            plan={"query": {"model_id": str(ctx.model_id), "measures": ["revenue"], "dimensions": []}},
            answer_text=None,
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.refused == 1
        assert report.rows[0].decomposition_match is False
        assert report.decomposition_regressions == 1
        assert report.regressed is True


class TestFilterNormalisation:
    """Findings 5/6: whitespace/operator-insensitive filter comparison,
    depth-aware comma splitting, falsy predicate values."""

    def test_operator_spacing_insensitive(self):
        expected = {"filters": ["year=2024"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": ["year = 2024"],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_in_filter_with_commas_stays_one_item(self):
        raw = "filters: [region IN (East, West)]"
        parsed = _parse_string_decomposition(raw)
        assert parsed["filters"] == ["region IN (East, West)"]

    def test_in_filter_comparison(self):
        expected = {"filters": ["region IN (East, West)"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "region", "op": "in", "value": ["East", "West"]}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_falsy_predicate_value_preserved(self):
        expected = {"filters": ["flag = 0"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "flag", "op": "eq", "value": 0}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_structured_predicate_name_key_rendered(self):
        """The planner's flat predicate uses the ``name`` key — it must
        render with the column, not an empty string."""
        from src.eval.comparison import _predicate_to_string

        s = _predicate_to_string({"name": "year", "op": "eq", "value": 2024})
        assert s == "year = 2024"


class TestDictKeyNormalisation:
    """Finding 7: dict decomposition keys are normalised like string keys."""

    def test_capitalised_and_singular_keys(self):
        raw = {"Measures": ["revenue"], "dimension": ["region"]}
        result = normalise_decomposition(raw)
        assert result["measures"] == ["revenue"]
        assert result["dimensions"] == ["region"]

    def test_keys_with_surrounding_whitespace(self):
        raw = {" model": "modelx", "measures ": ["revenue"]}
        result = normalise_decomposition(raw)
        assert result["model"] == "modelx"
        assert result["measures"] == ["revenue"]


# ---------------------------------------------------------------------------
# Review round 2 regression guards
# ---------------------------------------------------------------------------


class TestMultilineBaseline:
    """Round-2 finding 1: newline-separated baselines (natural textarea
    format) must parse ALL keys — a half-parse silently skips the dropped
    fields (false pass)."""

    def test_newline_separated_parses_all_fields(self):
        raw = "model: modelx\nmeasures: revenue\nfilters: year = 2024"
        result = normalise_decomposition(raw)
        assert result["model"] == "modelx"
        assert result["measures"] == ["revenue"]
        assert result["filters"] == ["year = 2024"]

    def test_mixed_newline_and_comma_boundaries(self):
        raw = "model: modelx, measures: [revenue, quantity]\ndimensions: region"
        result = normalise_decomposition(raw)
        assert result["model"] == "modelx"
        assert set(result["measures"]) == {"revenue", "quantity"}
        assert result["dimensions"] == ["region"]

    def test_dropped_field_would_have_flagged_mismatch(self):
        """The exact false-pass scenario: baseline filters year = 2024,
        plan dropped the filter. Before the newline fix the filters key
        was never parsed and the row PASSED."""
        raw = "model: modelx\nmeasures: revenue\nfilters: year = 2024"
        plan = {
            "query": {
                "model_id": "modelx",
                "measures": ["revenue"],
                "dimensions": [],
                "where": [],  # filter dropped!
                "having": [],
            }
        }
        result = compare_decomposition(raw, plan)
        assert result.matched is False
        filters_field = [f for f in result.fields if f.field_name == "filters"][0]
        assert filters_field.matched is False

    def test_quoted_semicolon_does_not_derail(self):
        raw = "model: m; filters: [a = 'x;y']"
        result = normalise_decomposition(raw)
        assert result["model"] == "m"
        assert result["filters"] == ["a = 'x;y'"]


class TestUncomparablePredicates:
    """Round-2 finding 2: compound/expression predicates must not render
    as garbage (guaranteed false fail); the filters field is skipped
    visibly instead."""

    def test_compound_or_predicate_skips_filters(self):
        expected = {
            "model": "m",
            "measures": ["revenue"],
            "filters": ["region = East or region = West"],
        }
        plan = {
            "query": {
                "model_id": "m",
                "measures": ["revenue"],
                "dimensions": [],
                "where": [
                    {
                        "or": [
                            {"name": "region", "op": "eq", "value": "East"},
                            {"name": "region", "op": "eq", "value": "West"},
                        ]
                    }
                ],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        # Overall verdict must NOT regress on an uncomparable filter.
        assert result.matched is True
        filters_field = [f for f in result.fields if f.field_name == "filters"][0]
        assert filters_field.skipped is True
        assert filters_field.matched is True
        assert "cannot be rendered" in filters_field.detail

    def test_expression_predicate_skips_filters(self):
        expected = {"filters": ["upper(region) = EAST"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [
                    {
                        "left": {"fn": "upper", "args": [{"field": "region"}]},
                        "op": "eq",
                        "right": {"lit": "EAST"},
                    }
                ],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        filters_field = [f for f in result.fields if f.field_name == "filters"][0]
        assert filters_field.skipped is True

    def test_mixed_flat_and_compound_still_skips(self):
        """One comparable + one uncomparable predicate: partial rendering
        would false-fail the set comparison, so the whole field skips."""
        expected = {"filters": ["year = 2024"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [
                    {"name": "year", "op": "eq", "value": 2024},
                    {"not": {"name": "region", "op": "eq", "value": "East"}},
                ],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        filters_field = [f for f in result.fields if f.field_name == "filters"][0]
        assert filters_field.skipped is True

    def test_skipped_flag_serialised(self):
        from src.eval.runner import _comparison_to_dict

        expected = {"filters": ["x = 1"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"or": []}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        d = _comparison_to_dict(result)
        filters_entry = [
            f for f in d["fields"] if f["field_name"] == "filters"
        ][0]
        assert filters_entry["skipped"] is True


class TestQuotedValuesAndOps:
    """Round-2 findings 3/5: quoted authored values, between/is_null
    rendering, <> vs != unification."""

    def test_quoted_value_matches_unquoted_rendering(self):
        expected = {"filters": ["region = 'East'"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "region", "op": "eq", "value": "East"}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_like_pattern_with_quotes(self):
        expected = {"filters": ["name like '%acme%'"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "name", "op": "like", "value": "%acme%"}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_between_rendering(self):
        expected = {"filters": ["year between 2020 and 2024"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "year", "op": "between", "value": [2020, 2024]}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_is_null_rendering(self):
        expected = {"filters": ["region is null"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "region", "op": "is_null"}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_angle_bracket_neq_unified(self):
        expected = {"filters": ["year <> 2024"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "year", "op": "neq", "value": 2024}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_in_list_comma_spacing_insensitive(self):
        """Round-3 finding: "(East,West)" must equal "(East, West)"."""
        expected = {"filters": ["region IN (East,West)"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [{"name": "region", "op": "in", "value": ["East", "West"]}],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True


# ---------------------------------------------------------------------------
# Review round 3 regression guards
# ---------------------------------------------------------------------------


class TestGrainedDimensions:
    """Round-3 finding 1: the plan's dimensions list carries derived
    aliases (order_date_month) when any dimension is grained; comparable
    base names must come from dimension_exprs raw entries."""

    def test_grained_dimension_matches_bare_baseline(self):
        expected = {
            "model": "m",
            "measures": ["revenue"],
            "dimensions": ["region", "order_date"],
        }
        plan = {
            "query": {
                "model_id": "m",
                "measures": ["revenue"],
                # Aliases, as _plan_dict emits when any dim is non-bare.
                "dimensions": ["region", "order_date_month"],
                "dimension_exprs": [
                    "region",
                    {"name": "order_date", "grain": "month"},
                ],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True
        dims_field = [f for f in result.fields if f.field_name == "dimensions"][0]
        assert dims_field.matched is True
        assert dims_field.skipped is False

    def test_grain_change_is_documented_invisible(self):
        """Grain itself is intentionally not compared: month -> year on
        the same base dimension still matches (documented limitation)."""
        expected = {"dimensions": ["order_date"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": ["order_date_year"],
                "dimension_exprs": [{"name": "order_date", "grain": "year"}],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True

    def test_expression_dimension_skips_visibly(self):
        expected = {"dimensions": ["region_group"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": ["region_group"],
                "dimension_exprs": [
                    {"expr": {"fn": "case", "args": []}, "alias": "region_group"}
                ],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is True
        dims_field = [f for f in result.fields if f.field_name == "dimensions"][0]
        assert dims_field.skipped is True
        assert "expression dimensions" in dims_field.detail

    def test_wrong_base_dimension_still_fails(self):
        """The alias fix must not blunt the gate: a genuinely wrong
        dimension still regresses."""
        expected = {"dimensions": ["order_date"]}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": ["ship_date_month"],
                "dimension_exprs": [{"name": "ship_date", "grain": "month"}],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is False


class TestNonListExpectedValues:
    """Round-4 finding: a non-list measures/dimensions expected value must
    NOT collapse to an empty set — a plan that dropped the field entirely
    would pass vacuously (silent false pass on a dropped-grouping
    regression)."""

    def test_dict_valued_dimensions_do_not_vacuously_pass(self):
        expected = {"dimensions": {"name": "region", "grain": "month"}}
        plan = {
            "query": {
                "model_id": "m",
                "measures": ["revenue"],
                # Planner dropped all grouping — a genuine regression.
                "dimensions": [],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is False
        dims_field = [f for f in result.fields if f.field_name == "dimensions"][0]
        assert dims_field.matched is False

    def test_scalar_valued_measures_do_not_vacuously_pass(self):
        expected = {"measures": 42}
        plan = {
            "query": {
                "model_id": "m",
                "measures": [],
                "dimensions": [],
                "where": [],
                "having": [],
            }
        }
        result = compare_decomposition(expected, plan)
        assert result.matched is False


class TestParserKeyVariants:
    """Round-3 finding 2: plural "models:" accepted by the string parser
    (the dict path already normalised it)."""

    def test_models_plural_key(self):
        raw = "models: modelx; measures: revenue"
        result = normalise_decomposition(raw)
        assert result["model"] == "modelx"
        assert result["measures"] == ["revenue"]


class TestRunnerSkippedFieldAccounting:
    """Round-3 finding 5: a row whose filters field is skipped
    (uncomparable predicates) still counts as compared + matched — the
    R0 accuracy semantics must not silently change."""

    @pytest.mark.asyncio
    async def test_uncomparable_filters_row_counts_as_compared(self):
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(),
            answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[
                {
                    "q": "Revenue east or west",
                    "decomposition": {
                        "model": "modelx",
                        "measures": ["revenue"],
                        "filters": ["region = East or region = West"],
                    },
                }
            ],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok",
            plan={
                "query": {
                    "model_id": str(ctx.model_id),
                    "measures": ["revenue"],
                    "dimensions": [],
                    "where": [
                        {
                            "or": [
                                {"name": "region", "op": "eq", "value": "East"},
                                {"name": "region", "op": "eq", "value": "West"},
                            ]
                        }
                    ],
                    "having": [],
                }
            },
            answer_text="ok",
            provider="anthropic",
            usage_input_tokens=100,
            usage_output_tokens=50,
        )
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", AsyncMock()),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.decomposition_compared == 1
        assert report.accuracy_score == 1.0
        assert report.regressed is False
        row = report.rows[0]
        assert row.decomposition_match is True
        filters_entry = [
            f
            for f in row.decomposition_comparison["fields"]
            if f["field_name"] == "filters"
        ][0]
        assert filters_entry["skipped"] is True
