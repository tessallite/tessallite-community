"""Tests for KPI composite scoring engine."""
from __future__ import annotations

import uuid

import pytest

from src.kpi_composite import (
    COMPOSITE_STATUS_RESTRICTED,
    ChildScore,
    CompositeResult,
    build_composite_children,
    evaluate_composite,
    get_normalisation_config,
    normalise_min_max,
    normalise_pct_target,
    normalise_weights,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# pct_target normalisation
# ---------------------------------------------------------------------------


class TestNormalisePctTarget:
    def test_at_target(self):
        assert normalise_pct_target(100, 100) == 100.0

    def test_half_of_target(self):
        assert normalise_pct_target(50, 100) == 50.0

    def test_exceeds_target_capped(self):
        assert normalise_pct_target(150, 100) == 100.0

    def test_zero_value(self):
        assert normalise_pct_target(0, 100) == 0.0

    def test_null_value(self):
        assert normalise_pct_target(None, 100) is None

    def test_null_target(self):
        assert normalise_pct_target(80, None) is None

    def test_zero_target(self):
        assert normalise_pct_target(80, 0) is None

    def test_small_ratio(self):
        result = normalise_pct_target(10, 200)
        assert result == pytest.approx(5.0)

    def test_negative_value(self):
        # Bug-6254: higher_is_better is floor-clamped like the other directions,
        # so a negative value against a positive target normalises to 0, not a
        # negative percentage that escapes the documented [0, 100] contract and
        # drags the weighted composite below zero.
        result = normalise_pct_target(-10, 100)
        assert result == pytest.approx(0.0)

    # --- Direction-aware tests ---

    def test_lower_is_better_beating_target(self):
        # Cost KPI: value=50, target=100 -> beating goal by 2x -> 100
        result = normalise_pct_target(50, 100, "lower_is_better")
        assert result == pytest.approx(100.0)

    def test_lower_is_better_at_target(self):
        result = normalise_pct_target(100, 100, "lower_is_better")
        assert result == pytest.approx(100.0)

    def test_lower_is_better_missing_target(self):
        # value=200, target=100 -> twice the cost -> 50%
        result = normalise_pct_target(200, 100, "lower_is_better")
        assert result == pytest.approx(50.0)

    def test_lower_is_better_zero_value(self):
        result = normalise_pct_target(0, 100, "lower_is_better")
        assert result == 100.0

    def test_closer_is_better_on_target(self):
        result = normalise_pct_target(100, 100, "closer_is_better")
        assert result == pytest.approx(100.0)

    def test_closer_is_better_half_below(self):
        # 50% deviation below target
        result = normalise_pct_target(50, 100, "closer_is_better")
        assert result == pytest.approx(50.0)

    def test_closer_is_better_half_above(self):
        # 50% deviation above target
        result = normalise_pct_target(150, 100, "closer_is_better")
        assert result == pytest.approx(50.0)

    def test_closer_is_better_at_double(self):
        # 100% deviation -> score 0
        result = normalise_pct_target(200, 100, "closer_is_better")
        assert result == pytest.approx(0.0)

    def test_higher_is_better_explicit(self):
        result = normalise_pct_target(80, 100, "higher_is_better")
        assert result == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# min_max normalisation
# ---------------------------------------------------------------------------


class TestNormaliseMinMax:
    def test_higher_is_better_midpoint(self):
        result = normalise_min_max(50, "higher_is_better", 0, 100)
        assert result == pytest.approx(50.0)

    def test_higher_is_better_at_max(self):
        result = normalise_min_max(100, "higher_is_better", 0, 100)
        assert result == pytest.approx(100.0)

    def test_higher_is_better_at_min(self):
        result = normalise_min_max(0, "higher_is_better", 0, 100)
        assert result == pytest.approx(0.0)

    def test_higher_is_better_clamped_above(self):
        result = normalise_min_max(120, "higher_is_better", 0, 100)
        assert result == 100.0

    def test_higher_is_better_clamped_below(self):
        result = normalise_min_max(-10, "higher_is_better", 0, 100)
        assert result == 0.0

    def test_lower_is_better_midpoint(self):
        result = normalise_min_max(50, "lower_is_better", 0, 100)
        assert result == pytest.approx(50.0)

    def test_lower_is_better_at_min(self):
        # Lower value = better = 100 points
        result = normalise_min_max(0, "lower_is_better", 0, 100)
        assert result == pytest.approx(100.0)

    def test_lower_is_better_at_max(self):
        # Higher value = worse = 0 points
        result = normalise_min_max(100, "lower_is_better", 0, 100)
        assert result == pytest.approx(0.0)

    def test_null_value(self):
        assert normalise_min_max(None, "higher_is_better", 0, 100) is None

    def test_null_min(self):
        assert normalise_min_max(50, "higher_is_better", None, 100) is None

    def test_null_max(self):
        assert normalise_min_max(50, "higher_is_better", 0, None) is None

    def test_equal_bounds(self):
        assert normalise_min_max(50, "higher_is_better", 50, 50) is None

    def test_custom_range(self):
        result = normalise_min_max(75, "higher_is_better", 50, 150)
        assert result == pytest.approx(25.0)

    # --- closer_is_better tests ---

    def test_closer_is_better_at_midpoint(self):
        result = normalise_min_max(50, "closer_is_better", 0, 100)
        assert result == pytest.approx(100.0)

    def test_closer_is_better_at_min(self):
        result = normalise_min_max(0, "closer_is_better", 0, 100)
        assert result == pytest.approx(0.0)

    def test_closer_is_better_at_max(self):
        result = normalise_min_max(100, "closer_is_better", 0, 100)
        assert result == pytest.approx(0.0)

    def test_closer_is_better_quarter(self):
        # 25% away from midpoint -> 50% score
        result = normalise_min_max(25, "closer_is_better", 0, 100)
        assert result == pytest.approx(50.0)

    def test_closer_is_better_three_quarter(self):
        result = normalise_min_max(75, "closer_is_better", 0, 100)
        assert result == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Weight normalisation
# ---------------------------------------------------------------------------


class TestNormaliseWeights:
    def test_equal_weights(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=1.0),
            ChildScore("b", "B", 60, 100, weight=1.0),
        ]
        normalise_weights(children)
        assert children[0].weight == pytest.approx(0.5)
        assert children[1].weight == pytest.approx(0.5)

    def test_unequal_weights(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=3.0),
            ChildScore("b", "B", 60, 100, weight=1.0),
        ]
        normalise_weights(children)
        assert children[0].weight == pytest.approx(0.75)
        assert children[1].weight == pytest.approx(0.25)

    def test_excluded_children_skipped(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=2.0),
            ChildScore("b", "B", None, 100, weight=2.0, excluded=True),
            ChildScore("c", "C", 60, 100, weight=2.0),
        ]
        normalise_weights(children)
        # Only a and c are active -> each gets 0.5
        assert children[0].weight == pytest.approx(0.5)
        assert children[1].weight == 2.0  # excluded, not touched
        assert children[2].weight == pytest.approx(0.5)

    def test_all_zero_weights_distributed_equally(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=0.0),
            ChildScore("b", "B", 60, 100, weight=0.0),
        ]
        normalise_weights(children)
        assert children[0].weight == pytest.approx(0.5)
        assert children[1].weight == pytest.approx(0.5)

    def test_weights_sum_to_one(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=0.4),
            ChildScore("b", "B", 60, 100, weight=0.6),
            ChildScore("c", "C", 70, 100, weight=1.0),
        ]
        normalise_weights(children)
        total = sum(c.weight for c in children)
        assert total == pytest.approx(1.0)

    def test_empty_list(self):
        children: list[ChildScore] = []
        normalise_weights(children)
        assert children == []


# ---------------------------------------------------------------------------
# Composite evaluation
# ---------------------------------------------------------------------------


class TestEvaluateComposite:
    def test_equal_weight_pct_target(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=1.0),
            ChildScore("b", "B", 60, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        # A: 80/100 = 80, B: 60/100 = 60
        # Weights normalised to 0.5, 0.5 -> score = 80*0.5 + 60*0.5 = 70
        assert result.composite_score == pytest.approx(70.0)

    def test_weighted_pct_target(self):
        children = [
            ChildScore("a", "Customer Sat", 90, 100, weight=0.4),
            ChildScore("b", "Revenue Growth", 70, 100, weight=0.6),
        ]
        result = evaluate_composite(children, "pct_target")
        # A: 90, B: 70 -> 90*0.4 + 70*0.6 = 36 + 42 = 78
        assert result.composite_score == pytest.approx(78.0)

    def test_null_child_excluded(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=1.0),
            ChildScore("b", "B", None, 100, weight=1.0),
            ChildScore("c", "C", 60, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        # B excluded, A and C remain with re-normalised weights 0.5 each
        # 80*0.5 + 60*0.5 = 70
        assert result.composite_score == pytest.approx(70.0)
        assert result.children[1].excluded is True
        assert result.children[1].exclude_reason == "null_value"

    def test_all_children_null(self):
        children = [
            ChildScore("a", "A", None, 100, weight=1.0),
            ChildScore("b", "B", None, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        assert result.composite_score is None

    def test_null_target_excluded_pct_target(self):
        children = [
            ChildScore("a", "A", 80, None, weight=1.0),
            ChildScore("b", "B", 60, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        # A excluded (null target), B remains with weight 1.0
        # 60/100 = 60 -> score = 60
        assert result.composite_score == pytest.approx(60.0)
        assert result.children[0].excluded is True
        assert result.children[0].exclude_reason == "null_target"

    def test_min_max_normalisation(self):
        children = [
            ChildScore("a", "A", 75, None, weight=1.0,
                        direction="higher_is_better"),
            ChildScore("b", "B", 25, None, weight=1.0,
                        direction="higher_is_better"),
        ]
        result = evaluate_composite(
            children, "min_max", bound_min=0, bound_max=100,
        )
        # A: 75, B: 25 -> weights 0.5 each -> 75*0.5 + 25*0.5 = 50
        assert result.composite_score == pytest.approx(50.0)

    def test_min_max_lower_is_better(self):
        children = [
            ChildScore("a", "Defect Rate", 20, None, weight=1.0,
                        direction="lower_is_better"),
        ]
        result = evaluate_composite(
            children, "min_max", bound_min=0, bound_max=100,
        )
        # Lower is better: (100 - 20) / 100 * 100 = 80
        assert result.composite_score == pytest.approx(80.0)

    def test_exceeding_target_capped(self):
        children = [
            ChildScore("a", "A", 150, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        # 150/100 = 150% -> capped to 100
        assert result.composite_score == pytest.approx(100.0)

    def test_result_metadata(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=2.0),
            ChildScore("b", "B", 60, 100, weight=3.0),
        ]
        result = evaluate_composite(children, "pct_target")
        assert result.normalisation_method == "pct_target"
        assert result.total_weight_before == pytest.approx(5.0)
        assert result.total_weight_after == pytest.approx(1.0)

    def test_deep_composite_chain(self):
        """Composite of composites: inner composites feed pre-evaluated values."""
        # Simulate: inner composite already evaluated to 80
        children = [
            ChildScore("inner1", "Inner Composite 1", 80, 100, weight=0.5),
            ChildScore("inner2", "Inner Composite 2", 60, 100, weight=0.5),
        ]
        result = evaluate_composite(children, "pct_target")
        # 80*0.5 + 60*0.5 = 70
        assert result.composite_score == pytest.approx(70.0)

    def test_pct_target_lower_is_better(self):
        """Cost KPI beating target scores high."""
        children = [
            ChildScore("a", "Cost", 50, 100, weight=1.0,
                        direction="lower_is_better"),
        ]
        result = evaluate_composite(children, "pct_target")
        # target/value = 100/50 = 200%, capped to 100
        assert result.composite_score == pytest.approx(100.0)

    def test_pct_target_lower_is_better_missing(self):
        """Cost KPI double the budget scores 50."""
        children = [
            ChildScore("a", "Cost", 200, 100, weight=1.0,
                        direction="lower_is_better"),
        ]
        result = evaluate_composite(children, "pct_target")
        # target/value = 100/200 = 50%
        assert result.composite_score == pytest.approx(50.0)

    def test_pct_target_closer_is_better(self):
        """Budget adherence on-target scores 100."""
        children = [
            ChildScore("a", "Budget", 100, 100, weight=1.0,
                        direction="closer_is_better"),
        ]
        result = evaluate_composite(children, "pct_target")
        assert result.composite_score == pytest.approx(100.0)

    def test_pct_target_closer_is_better_deviation(self):
        """Budget adherence 50% away from target scores 50."""
        children = [
            ChildScore("a", "Budget", 50, 100, weight=1.0,
                        direction="closer_is_better"),
        ]
        result = evaluate_composite(children, "pct_target")
        assert result.composite_score == pytest.approx(50.0)

    def test_mixed_direction_balanced_scorecard(self):
        """Revenue (higher) + Cost (lower) balanced scorecard."""
        children = [
            ChildScore("a", "Revenue", 80, 100, weight=0.5,
                        direction="higher_is_better"),
            ChildScore("b", "Cost", 50, 100, weight=0.5,
                        direction="lower_is_better"),
        ]
        result = evaluate_composite(children, "pct_target")
        # Revenue: 80/100 = 80.  Cost: 100/50 = 200 capped to 100.
        # 80*0.5 + 100*0.5 = 90
        assert result.composite_score == pytest.approx(90.0)

    def test_min_max_closer_is_better(self):
        children = [
            ChildScore("a", "Inventory", 50, None, weight=1.0,
                        direction="closer_is_better"),
        ]
        result = evaluate_composite(
            children, "min_max", bound_min=0, bound_max=100,
        )
        # Midpoint=50, value=50 -> exactly on target -> 100
        assert result.composite_score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Bug-4255: errored child vs no-data child
# ---------------------------------------------------------------------------


class TestErroredChildVsNoData:
    """A child whose evaluation FAILED is surfaced distinctly from a child
    that legitimately has no data — the score still comes from the valid
    children, but the parent flags the broken input (Bug-4255)."""

    def test_errored_child_degrades_parent_and_scores_from_valid(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=1.0),
            ChildScore("b", "B", None, 100, weight=1.0,
                       error_reason="Evaluation failed — bad SQL"),
            ChildScore("c", "C", 60, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        # B errored and is excluded; A and C score with re-normalised weights:
        # 80*0.5 + 60*0.5 = 70 — unchanged from the equivalent no-data case.
        assert result.composite_score == pytest.approx(70.0)
        assert result.status == "degraded"
        assert len(result.errored_children) == 1
        ec = result.errored_children[0]
        assert ec.kpi_id == "b"
        assert ec.kpi_name == "B"
        assert ec.error_reason == "Evaluation failed — bad SQL"
        # The errored child is excluded with the distinct "error" reason.
        assert result.children[1].excluded is True
        assert result.children[1].exclude_reason == "error"

    def test_no_data_child_silently_excluded_not_flagged(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=1.0),
            ChildScore("b", "B", None, 100, weight=1.0),  # no error_reason
            ChildScore("c", "C", 60, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        assert result.composite_score == pytest.approx(70.0)
        # No-data child stays silent: status ok, no errored children.
        assert result.status == "ok"
        assert result.errored_children == []
        assert result.children[1].excluded is True
        assert result.children[1].exclude_reason == "null_value"

    def test_mixed_error_and_no_data_only_error_surfaced(self):
        children = [
            ChildScore("a", "A", 80, 100, weight=1.0),
            ChildScore("b", "NoData", None, 100, weight=1.0),  # legit null
            ChildScore("c", "Broken", None, 100, weight=1.0,
                       error_reason="Evaluation failed — router error"),
            ChildScore("d", "D", 60, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        # Only A and D score: 80*0.5 + 60*0.5 = 70.
        assert result.composite_score == pytest.approx(70.0)
        assert result.status == "degraded"
        # Only the genuinely-errored child is surfaced, not the no-data one.
        assert [ec.kpi_id for ec in result.errored_children] == ["c"]

    def test_all_children_errored_parent_error_state_no_fake_score(self):
        children = [
            ChildScore("a", "A", None, 100, weight=1.0,
                       error_reason="Evaluation failed — a"),
            ChildScore("b", "B", None, 100, weight=1.0,
                       error_reason="Evaluation failed — b"),
        ]
        result = evaluate_composite(children, "pct_target")
        # No valid child to score — no fake number.
        assert result.composite_score is None
        assert result.status == "error"
        assert {ec.kpi_id for ec in result.errored_children} == {"a", "b"}

    def test_all_children_no_data_stays_ok_not_error(self):
        # All-null but NONE errored -> unchanged "ok" status (silent), score None.
        children = [
            ChildScore("a", "A", None, 100, weight=1.0),
            ChildScore("b", "B", None, 100, weight=1.0),
        ]
        result = evaluate_composite(children, "pct_target")
        assert result.composite_score is None
        assert result.status == "ok"
        assert result.errored_children == []

    def test_build_children_propagates_error_reason_from_cache(self):
        parent_id = str(uuid.uuid4())
        ok_id = str(uuid.uuid4())
        err_id = str(uuid.uuid4())
        all_kpis = [
            {
                "id": ok_id, "name": "OK",
                "parent_kpi_id": parent_id, "weight": 1.0,
                "direction": "higher_is_better", "target_value": None,
            },
            {
                "id": err_id, "name": "Broken",
                "parent_kpi_id": parent_id, "weight": 1.0,
                "direction": "higher_is_better", "target_value": None,
            },
        ]
        eval_cache = {
            ok_id: {"value": 80, "target": 100, "error_reason": None},
            err_id: {
                "value": None, "target": None,
                "error_reason": "Evaluation failed — broken child",
            },
        }
        children = build_composite_children(parent_id, all_kpis, eval_cache)
        by_name = {c.kpi_name: c for c in children}
        assert by_name["OK"].error_reason is None
        assert by_name["Broken"].error_reason == "Evaluation failed — broken child"

        result = evaluate_composite(children, "pct_target")
        # OK child alone scores 80; parent degraded by the broken child.
        assert result.composite_score == pytest.approx(80.0)
        assert result.status == "degraded"
        assert [ec.kpi_name for ec in result.errored_children] == ["Broken"]


class TestRestrictedChildSemantics:
    def test_one_restricted_child_fails_closed_without_renormalising(self):
        children = [
            ChildScore(
                "restricted", "Restricted", None, 100,
                weight=0.4, restricted=True,
            ),
            ChildScore("visible", "Visible", 50, 100, weight=0.6),
        ]

        result = evaluate_composite(children, "pct_target")

        assert result.composite_score is None
        assert result.status == COMPOSITE_STATUS_RESTRICTED
        assert result.total_weight_before == pytest.approx(1.0)
        assert result.total_weight_after == 0.0
        assert children[0].excluded is True
        assert children[0].exclude_reason == "row_security_restricted"
        assert children[1].normalised is None
        assert children[0].weight == pytest.approx(0.4)
        assert children[1].weight == pytest.approx(0.6)

    def test_build_children_propagates_restricted_from_cache(self):
        parent_id = str(uuid.uuid4())
        restricted_id = str(uuid.uuid4())
        visible_id = str(uuid.uuid4())
        all_kpis = [
            {
                "id": restricted_id, "name": "Restricted",
                "parent_kpi_id": parent_id, "weight": 1.0,
                "direction": "higher_is_better", "target_value": None,
            },
            {
                "id": visible_id, "name": "Visible",
                "parent_kpi_id": parent_id, "weight": 1.0,
                "direction": "higher_is_better", "target_value": None,
            },
        ]
        eval_cache = {
            restricted_id: {
                "value": None, "target": None, "restricted": True,
            },
            visible_id: {
                "value": 50, "target": 100, "restricted": False,
            },
        }

        children = build_composite_children(parent_id, all_kpis, eval_cache)

        assert [child.restricted for child in children] == [True, False]
        assert evaluate_composite(children).status == COMPOSITE_STATUS_RESTRICTED

# ---------------------------------------------------------------------------
# build_composite_children
# ---------------------------------------------------------------------------


class TestBuildCompositeChildren:
    def test_filters_by_parent_id(self):
        parent_id = str(uuid.uuid4())
        child_id_1 = str(uuid.uuid4())
        child_id_2 = str(uuid.uuid4())
        other_id = str(uuid.uuid4())

        all_kpis = [
            {
                "id": child_id_1, "name": "Child 1",
                "parent_kpi_id": parent_id, "weight": 0.6,
                "direction": "higher_is_better", "target_value": None,
            },
            {
                "id": child_id_2, "name": "Child 2",
                "parent_kpi_id": parent_id, "weight": 0.4,
                "direction": "higher_is_better", "target_value": None,
            },
            {
                "id": other_id, "name": "Unrelated",
                "parent_kpi_id": None, "weight": 1.0,
                "direction": "higher_is_better", "target_value": None,
            },
        ]

        eval_cache = {
            child_id_1: {"value": 90, "target": 100},
            child_id_2: {"value": 70, "target": 100},
            other_id: {"value": 50, "target": 100},
        }

        children = build_composite_children(parent_id, all_kpis, eval_cache)
        assert len(children) == 2
        assert children[0].kpi_name == "Child 1"
        assert children[0].raw_value == 90
        assert children[0].weight == 0.6
        assert children[1].kpi_name == "Child 2"
        assert children[1].raw_value == 70
        assert children[1].weight == 0.4

    def test_missing_cache_returns_null_value(self):
        parent_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())

        all_kpis = [
            {
                "id": child_id, "name": "Child",
                "parent_kpi_id": parent_id, "weight": 1.0,
                "direction": "higher_is_better", "target_value": None,
            },
        ]

        children = build_composite_children(parent_id, all_kpis, {})
        assert len(children) == 1
        assert children[0].raw_value is None

    def test_default_weight(self):
        parent_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())

        all_kpis = [
            {
                "id": child_id, "name": "Child",
                "parent_kpi_id": parent_id, "weight": None,
                "direction": "higher_is_better", "target_value": None,
            },
        ]

        children = build_composite_children(
            parent_id, all_kpis, {child_id: {"value": 50, "target": 100}},
        )
        assert children[0].weight == 1.0


# ---------------------------------------------------------------------------
# get_normalisation_config
# ---------------------------------------------------------------------------


class TestGetNormalisationConfig:
    def test_none_meta_returns_defaults(self):
        method, bmin, bmax = get_normalisation_config(None)
        assert method == "pct_target"
        assert bmin is None
        assert bmax is None

    def test_empty_meta_returns_defaults(self):
        method, bmin, bmax = get_normalisation_config({})
        assert method == "pct_target"
        assert bmin is None
        assert bmax is None

    def test_min_max_config(self):
        meta = {
            "normalisation_method": "min_max",
            "normalisation_min": 0,
            "normalisation_max": 100,
        }
        method, bmin, bmax = get_normalisation_config(meta)
        assert method == "min_max"
        assert bmin == 0.0
        assert bmax == 100.0

    def test_string_bounds_coerced(self):
        meta = {
            "normalisation_method": "min_max",
            "normalisation_min": "10",
            "normalisation_max": "90",
        }
        method, bmin, bmax = get_normalisation_config(meta)
        assert bmin == 10.0
        assert bmax == 90.0


# ---------------------------------------------------------------------------
# Single-KPI composite evaluation (the /evaluate endpoint path)
# ---------------------------------------------------------------------------
# A composite's stored expression is a placeholder; evaluating the parent
# alone must return the weighted child score, never the placeholder value.


def _ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def _make_children(parent_id):
    child_a = _ns(
        id=uuid.uuid4(), name="comp_child_a", parent_kpi_id=parent_id,
        weight=0.6, direction="higher_is_better", target_value=None,
        certification_status="certified", expression='measure("Revenue")',
        kpi_type="ratio",
    )
    child_b = _ns(
        id=uuid.uuid4(), name="comp_child_b", parent_kpi_id=parent_id,
        weight=0.4, direction="higher_is_better", target_value=None,
        certification_status="certified", expression='measure("net_sales")',
        kpi_type="ratio",
    )
    return child_a, child_b


class TestChildErrorClassifier:
    """Bug-4255: a child evaluation that FAILED is distinguished from one that
    legitimately has no data, at the point of failure."""

    def test_errored_label_returns_reason(self):
        from src.api.kpis import _child_error_reason
        from shared.schemas.pydantic_models import KPIEvaluateResponse

        resp = KPIEvaluateResponse(
            kpi_id=uuid.uuid4(), value=None,
            status_label="Evaluation failed — check KPI expression and model scope",
        )
        assert (
            _child_error_reason(resp)
            == "Evaluation failed — check KPI expression and model scope"
        )

    def test_composite_error_label_returns_reason(self):
        from src.api.kpis import _child_error_reason
        from shared.schemas.pydantic_models import KPIEvaluateResponse

        resp = KPIEvaluateResponse(
            kpi_id=uuid.uuid4(), value=None,
            status_label="Composite evaluation failed — circular composite reference",
        )
        assert _child_error_reason(resp) is not None

    def test_no_data_label_returns_none(self):
        from src.api.kpis import _child_error_reason
        from shared.schemas.pydantic_models import KPIEvaluateResponse

        resp = KPIEvaluateResponse(
            kpi_id=uuid.uuid4(), value=None, status_label="No Data",
        )
        assert _child_error_reason(resp) is None

    def test_value_present_returns_none(self):
        from src.api.kpis import _child_error_reason
        from shared.schemas.pydantic_models import KPIEvaluateResponse

        # A child that successfully evaluated is never an error, regardless of
        # its band label.
        resp = KPIEvaluateResponse(
            kpi_id=uuid.uuid4(), value=42.0, status_label="Poor",
        )
        assert _child_error_reason(resp) is None

    def test_apply_signal_degraded(self):
        from src.api.kpis import _apply_composite_signal
        from src.kpi_composite import (
            COMPOSITE_STATUS_DEGRADED,
            CompositeResult,
            ErroredChild,
        )
        from shared.schemas.pydantic_models import KPIEvaluateResponse

        resp = KPIEvaluateResponse(kpi_id=uuid.uuid4(), value=70.0)
        result = CompositeResult(
            composite_score=70.0,
            status=COMPOSITE_STATUS_DEGRADED,
            errored_children=[ErroredChild("c", "Broken", "Evaluation failed")],
        )
        out = _apply_composite_signal(resp, result)
        assert out.composite_status == "degraded"
        assert out.errored_children == [
            {"kpi_id": "c", "kpi_name": "Broken", "error_reason": "Evaluation failed"},
        ]
        # A degraded parent keeps its real value/label.
        assert out.value == 70.0

    def test_apply_signal_error_overrides_label(self):
        from src.api.kpis import _apply_composite_signal
        from src.kpi_composite import (
            COMPOSITE_STATUS_ERROR,
            CompositeResult,
            ErroredChild,
        )
        from shared.schemas.pydantic_models import KPIEvaluateResponse

        resp = KPIEvaluateResponse(
            kpi_id=uuid.uuid4(), value=None, status_label="No Data",
        )
        result = CompositeResult(
            composite_score=None,
            status=COMPOSITE_STATUS_ERROR,
            errored_children=[ErroredChild("a", "A", "Evaluation failed")],
        )
        out = _apply_composite_signal(resp, result)
        assert out.composite_status == "error"
        assert out.status_label == "Evaluation failed — every child KPI errored"


class TestEvaluateCompositeScoreSingle:
    @pytest.mark.asyncio
    async def test_returns_weighted_child_score(self):
        """Child A 100/200 -> 50 @ 0.6; child B 25/100 -> 25 @ 0.4 => 40."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.api.kpis import _evaluate_composite_score

        parent_id = uuid.uuid4()
        parent = _ns(id=parent_id, kpi_type="composite", presentation_meta=None)
        child_a, child_b = _make_children(parent_id)

        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [child_a, child_b]
        db.execute = AsyncMock(return_value=result)

        async def fake_single(kpi, *a, **kw):
            vals = {child_a.id: (100.0, 200.0), child_b.id: (25.0, 100.0)}
            v, t = vals[kpi.id]
            return _ns(value=v, target=t)

        with (
            patch("src.api.kpis._evaluate_single_kpi", side_effect=fake_single),
            patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
        ):
            result = await _evaluate_composite_score(
                parent, db, uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
            )

        assert result.composite_score == pytest.approx(40.0)

    @pytest.mark.asyncio
    async def test_any_restricted_leaf_fails_single_path_closed(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.api.kpis import (
            ROW_SECURITY_DENY_ALL_RULE_ID,
            _evaluate_composite_score,
        )

        parent_id = uuid.uuid4()
        parent = _ns(id=parent_id, kpi_type="composite", presentation_meta=None)
        child_a, child_b = _make_children(parent_id)

        db = MagicMock()
        result_q = MagicMock()
        result_q.scalars.return_value.all.return_value = [child_a, child_b]
        db.execute = AsyncMock(return_value=result_q)

        async def fake_single(kpi, *args, **kwargs):
            if kpi.id == child_a.id:
                return _ns(
                    value=None, target=None, status_label="Restricted by row security",
                    row_security_restricted=True,
                )
            return _ns(
                value=50.0, target=100.0, status_label="On Track",
                row_security_restricted=None,
            )

        sink: set[str] = set()
        with (
            patch("src.api.kpis._evaluate_single_kpi", side_effect=fake_single),
            patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
        ):
            result = await _evaluate_composite_score(
                parent, db, uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
                security_sink=sink,
            )

        assert result.composite_score is None
        assert result.status == COMPOSITE_STATUS_RESTRICTED
        assert [child.restricted for child in result.children] == [True, False]
        assert sink == {ROW_SECURITY_DENY_ALL_RULE_ID}

    @pytest.mark.asyncio
    async def test_no_children_returns_none(self):
        from unittest.mock import AsyncMock, MagicMock

        from src.api.kpis import _evaluate_composite_score

        parent = _ns(
            id=uuid.uuid4(), kpi_type="composite", presentation_meta=None,
        )
        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        result = await _evaluate_composite_score(
            parent, db, uuid.uuid4(), "modelx", "token", {},
            model=_ns(fiscal_year_start_month=None),
            is_privileged=True,
            effective_persona_id=None,
        )
        assert result.composite_score is None

    @pytest.mark.asyncio
    async def test_draft_children_hidden_from_unprivileged(self):
        """Draft children are excluded for viewers — same rule as batch."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.api.kpis import _evaluate_composite_score

        parent_id = uuid.uuid4()
        parent = _ns(id=parent_id, kpi_type="composite", presentation_meta=None)
        child_a, child_b = _make_children(parent_id)
        child_b.certification_status = "draft"

        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [child_a, child_b]
        db.execute = AsyncMock(return_value=result)

        async def fake_single(kpi, *a, **kw):
            assert kpi.id == child_a.id, "draft child must not be evaluated"
            return _ns(value=100.0, target=200.0)

        with (
            patch("src.api.kpis._evaluate_single_kpi", side_effect=fake_single),
            patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
        ):
            result = await _evaluate_composite_score(
                parent, db, uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=False,
                effective_persona_id=None,
            )

        # Only child A contributes: 50 * renormalised weight 1.0 = 50.
        assert result.composite_score == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_nested_composite_child_scored_recursively(self):
        """A composite child contributes its weighted SCORE, never its
        placeholder expression value (the Bug-1031 class one level deeper).

        mid composite: A 100/200 -> 50 @ 0.6; B 25/100 -> 25 @ 0.4 => 40.
        grandparent: mid raw 40 vs target 50 -> normalised 80 @ weight 1 => 80.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.api.kpis import _evaluate_composite_score

        grandparent_id = uuid.uuid4()
        grandparent = _ns(
            id=grandparent_id, kpi_type="composite", presentation_meta=None,
        )
        mid_id = uuid.uuid4()
        mid = _ns(
            id=mid_id, name="mid_composite", parent_kpi_id=grandparent_id,
            weight=1.0, direction="higher_is_better", target_value=50.0,
            certification_status="certified", expression="literal(0)",
            kpi_type="composite", presentation_meta=None,
        )
        child_a, child_b = _make_children(mid_id)

        db = MagicMock()
        gp_children = MagicMock()
        gp_children.scalars.return_value.all.return_value = [mid]
        mid_children = MagicMock()
        mid_children.scalars.return_value.all.return_value = [child_a, child_b]
        db.execute = AsyncMock(side_effect=[gp_children, mid_children])

        async def fake_single(kpi, *a, **kw):
            vals = {
                mid.id: (0.0, 50.0),  # placeholder value, real target
                child_a.id: (100.0, 200.0),
                child_b.id: (25.0, 100.0),
            }
            v, t = vals[kpi.id]
            return _ns(value=v, target=t)

        with (
            patch("src.api.kpis._evaluate_single_kpi", side_effect=fake_single),
            patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
        ):
            result = await _evaluate_composite_score(
                grandparent, db, uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
            )

        assert result.composite_score == pytest.approx(80.0)

    @pytest.mark.asyncio
    async def test_errored_leaf_child_degrades_parent(self):
        """Bug-4255: when a leaf child fails to evaluate, the single-path
        result is degraded and lists the broken child, but still scores from
        the working children."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.api.kpis import _evaluate_composite_score

        parent_id = uuid.uuid4()
        parent = _ns(id=parent_id, kpi_type="composite", presentation_meta=None)
        child_a, child_b = _make_children(parent_id)
        # Equal weights so the surviving child scores predictably.
        child_a.weight = 1.0
        child_b.weight = 1.0

        db = MagicMock()
        result_q = MagicMock()
        result_q.scalars.return_value.all.return_value = [child_a, child_b]
        db.execute = AsyncMock(return_value=result_q)

        async def fake_single(kpi, *a, **kw):
            if kpi.id == child_a.id:
                return _ns(value=80.0, target=100.0, status_label="Good")
            # child_b errored: null value + fail-loud label.
            return _ns(
                value=None, target=None,
                status_label="Evaluation failed — check KPI expression and model scope",
            )

        with (
            patch("src.api.kpis._evaluate_single_kpi", side_effect=fake_single),
            patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
        ):
            result = await _evaluate_composite_score(
                parent, db, uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
            )

        # Only child A scores: 80 / 100 = 80, renormalised weight 1.0.
        assert result.composite_score == pytest.approx(80.0)
        assert result.status == "degraded"
        assert len(result.errored_children) == 1
        assert result.errored_children[0].kpi_name == child_b.name

    @pytest.mark.asyncio
    async def test_all_leaf_children_errored_parent_error_state(self):
        """Bug-4255: every leaf child failing yields an error state, not a
        fabricated score."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.api.kpis import _evaluate_composite_score

        parent_id = uuid.uuid4()
        parent = _ns(id=parent_id, kpi_type="composite", presentation_meta=None)
        child_a, child_b = _make_children(parent_id)

        db = MagicMock()
        result_q = MagicMock()
        result_q.scalars.return_value.all.return_value = [child_a, child_b]
        db.execute = AsyncMock(return_value=result_q)

        async def fake_single(kpi, *a, **kw):
            return _ns(
                value=None, target=None,
                status_label="Evaluation failed — check KPI expression and model scope",
            )

        with (
            patch("src.api.kpis._evaluate_single_kpi", side_effect=fake_single),
            patch("src.api.kpis._build_measure_provider", return_value=MagicMock()),
        ):
            result = await _evaluate_composite_score(
                parent, db, uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
            )

        assert result.composite_score is None
        assert result.status == "error"
        assert len(result.errored_children) == 2

    @pytest.mark.asyncio
    async def test_cycle_raises_composite_error(self):
        """A composite already on the recursion path fails loud, never 0.0."""
        from unittest.mock import MagicMock

        import pytest as _pytest

        from src.api.kpis import CompositeEvaluationError, _evaluate_composite_score

        parent = _ns(
            id=uuid.uuid4(), kpi_type="composite", presentation_meta=None,
        )
        with _pytest.raises(CompositeEvaluationError, match="circular"):
            await _evaluate_composite_score(
                parent, MagicMock(), uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
                _path=frozenset({str(parent.id)}),
            )

    @pytest.mark.asyncio
    async def test_depth_limit_raises_composite_error(self):
        """Nesting beyond _MAX_COMPOSITE_DEPTH fails loud."""
        from unittest.mock import MagicMock

        import pytest as _pytest

        from src.api.kpis import (
            _MAX_COMPOSITE_DEPTH,
            CompositeEvaluationError,
            _evaluate_composite_score,
        )

        parent = _ns(
            id=uuid.uuid4(), kpi_type="composite", presentation_meta=None,
        )
        deep_path = frozenset(
            str(uuid.uuid4()) for _ in range(_MAX_COMPOSITE_DEPTH)
        )
        with _pytest.raises(CompositeEvaluationError, match="deeper than"):
            await _evaluate_composite_score(
                parent, MagicMock(), uuid.uuid4(), "modelx", "token", {},
                model=_ns(fiscal_year_start_month=None),
                is_privileged=True,
                effective_persona_id=None,
                _path=deep_path,
            )
