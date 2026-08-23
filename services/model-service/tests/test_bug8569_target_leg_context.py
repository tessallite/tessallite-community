"""Bug-8569 — the KPI target leg must be evaluated under the VALUE leg's rules.

The value call to ``_evaluate_expression_via_sql`` passed ~25 arguments; the
target call at the same three endpoints passed six. Missing from the target:
``at_grain``, ``non_additive_agg``, ``carry_forward``, ``calendar_type``,
``fiscal_year_start_month``, ``inner_agg``/``inner_grain``/``outer_agg``,
``time_window_start_sql``/``time_window_end_sql`` and
``filter_predicate_list``. Value and target were therefore computed on
DIFFERENT rules, so the headline number was right and the VERDICT beside it was
wrong — the harder failure to notice.

Three observed symptoms, one root cause:

1. SEMI-ADDITIVE — a ``prior_period`` target on a closing-balance KPI emitted a
   plain per-period SUM. Balances M-1 = 200/220/180 give a target of 600 instead
   of 180, so attainment reads 15% where it should read 50% and the RAG band
   flips.
2. FISCAL — a ``fiscal_period_to_date`` target fell back to
   ``DATE_TRUNC('year', CURRENT_DATE)``: with an April fiscal start the target
   window began on 1 January while the value used the fiscal start.
3. BUSINESS-DEFINITION WINDOW — a "last 90 days" value was compared against a
   target computed over the last calendar period.

Guard shape: one contract test on the derivation helper, one AST guard that no
target call site can drift back to a hand-written subset (the Bug-8449 guard
pattern, which is what actually caught the drift class before), and behavioural
tests that the shared context changes the emitted target SQL.

Test escape: no test compared the two legs' arguments; each leg was only ever
tested on its own. Guard: this module. Tier: T2.
"""
from __future__ import annotations

import ast
import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.api import kpis as kpis_mod

pytestmark = pytest.mark.unit


class TestTargetLegKwargsContract:
    def test_slice_and_reduction_keys_are_shared_with_the_target(self):
        value_leg = dict(
            time_column="as_of_date",
            calendar_type="fiscal",
            fiscal_year_start_month=4,
            at_grain="day",
            non_additive_agg="last",
            carry_forward=True,
            inner_agg="sum",
            inner_grain="month",
            outer_agg="avg",
            persona_id="p1",
            where_clause="region = 'EMEA'",
            filter_where_clause="region = 'EMEA'",
            time_where_clause="d >= X",
            enable_ti_subquery=True,
            ti_type="period_to_date",
            ti_grain="year",
            ti_n_periods=3,
            base_expression='sum(measure("x"))',
            time_window_start_sql="DATE '2026-01-01'",
            time_window_end_sql="DATE '2026-04-01'",
            share_type="share_of_total",
            share_dimension="region",
            share_n=10,
            filter_predicate_list=["region = 'EMEA'"],
        )
        target_leg = kpis_mod._target_leg_kwargs(value_leg)

        # Every rule that decides WHAT ROWS and HOW THEY REDUCE is shared.
        for key in (
            "time_column", "calendar_type", "fiscal_year_start_month",
            "at_grain", "non_additive_agg", "carry_forward",
            "inner_agg", "inner_grain", "outer_agg",
            "persona_id", "where_clause", "filter_where_clause",
            "time_where_clause", "time_window_start_sql",
            "time_window_end_sql", "filter_predicate_list",
        ):
            assert target_leg[key] == value_leg[key], key

    def test_value_expression_shape_is_never_reused_for_the_target(self):
        """The target is a DIFFERENT expression.

        Reusing ``ti_type`` + ``base_expression`` would send the target through
        the decomposed branch, which IGNORES the expression argument and
        evaluates ``base_expression`` — the target would silently become the
        value. Reusing ``share_*`` would rank the target as if it were the
        share KPI.
        """
        value_leg = dict(
            time_column="d",
            ti_type="period_to_date",
            ti_grain="year",
            ti_n_periods=3,
            base_expression='sum(measure("x"))',
            share_type="share_of_total",
            share_dimension="region",
            share_n=10,
            enable_ti_subquery=True,
        )
        target_leg = kpis_mod._target_leg_kwargs(value_leg)
        for key in (
            "ti_type", "ti_grain", "ti_n_periods", "base_expression",
            "share_type", "share_dimension", "share_n", "enable_ti_subquery",
        ):
            assert key not in target_leg, key
        assert target_leg["time_column"] == "d"


class TestNoTargetCallSiteHandRollsItsContext:
    """AST guard: every target-expression evaluation derives its context.

    This is the guard that matters. The defect was never inside
    ``_evaluate_expression_via_sql`` — it was three CALL SITES that each wrote
    their own shorter argument list and drifted apart. A structural check is the
    only thing that stops a fourth from being added the same way.
    """

    def _target_call_nodes(self):
        source = inspect.getsource(kpis_mod)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "_evaluate_expression_via_sql":
                continue
            if not node.args:
                continue
            first = node.args[0]
            first_name = getattr(first, "id", None) or getattr(first, "attr", None)
            if first_name in (
                "target_expr", "adhoc_target_expression",
            ):
                yield node

    def test_every_target_call_site_uses_the_shared_derivation(self):
        sites = list(self._target_call_nodes())
        assert len(sites) >= 3, (
            "expected the single-evaluate, ad-hoc and batch target legs; "
            f"found {len(sites)}"
        )
        for node in sites:
            derived = [
                kw for kw in node.keywords
                if kw.arg is None
                and isinstance(kw.value, ast.Call)
                and getattr(kw.value.func, "id", None) == "_target_leg_kwargs"
            ]
            assert derived, (
                "target-expression evaluation at line "
                f"{node.lineno} hand-rolls its context instead of deriving it "
                "from the value leg (Bug-8569)"
            )



class _ScalarResult:
    """The object SQLAlchemy's ``Result.scalars()`` returns — a SEPARATE type.

    Bug-8924's contract guard: a fake whose ``scalars()`` returns ``self``
    accepts call shapes real SQLAlchemy rejects.
    """

    def all(self):
        return []

    def first(self):
        return None


class _Result:
    """Minimal SQLAlchemy-result stand-in: every query returns no rows."""

    def scalars(self):
        return _ScalarResult()

    def all(self):
        return []

    def first(self):
        return None


class _Db:
    def __init__(self, by_id: dict):
        self._by_id = by_id

    async def get(self, _model, pk):
        return self._by_id.get(pk)

    async def execute(self, *_a, **_k):
        return _Result()


def _kpi(**overrides):
    """A KPI row shaped like the ORM object the evaluate paths read."""
    base = dict(
        id=uuid.uuid4(),
        name="Closing balance",
        display_name=None,
        expression='period_to_date(measure("balance"), "year")',
        kpi_type="single_measure",
        calc_agg_mode="semi_additive",
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        target_type="expression",
        target_value=None,
        target_measure_id=None,
        target_expression=None,
        target_period=None,
        direction="higher_is_better",
        presentation_type=None,
        presentation_meta=None,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        format_token=None,
        format_custom=None,
        unit_label=None,
        null_display_value="N/A",
        business_definition=None,
        time_dimension_id=uuid.uuid4(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _run_single_kpi(kpi, *, fiscal_year_start_month=None, calendar_type=None):
    """Drive the PRODUCTION batch evaluation path and capture every SQL."""
    from src.kpi_evaluator import MeasureValueProvider

    model_id = uuid.uuid4()
    time_dim = SimpleNamespace(
        id=kpi.time_dimension_id, model_id=model_id, is_time_dim=True,
        name="as_of_date",
    )
    db = _Db({kpi.time_dimension_id: time_dim})

    captured: list[str] = []

    async def _fake_router(_model_id, sql, _bearer, **_kw):
        captured.append(sql)
        return {"rows": [{"value": 1.0}]}

    with patch.object(kpis_mod, "_execute_via_router", _fake_router):
        await kpis_mod._evaluate_single_kpi(
            kpi, db, model_id, "modelx", "tok",
            MeasureValueProvider(),
            measure_map={
                "balance": SimpleNamespace(name="balance", default_agg="sum"),
                "revenue": SimpleNamespace(name="revenue", default_agg="sum"),
            },
            calendar_type=calendar_type,
            fiscal_year_start_month=fiscal_year_start_month,
        )
    return captured


class TestTargetLegBehaviourThroughTheProductionPath:
    """Drive ``_evaluate_single_kpi`` — the defect was in the CALL SITES."""

    @pytest.mark.asyncio
    async def test_fiscal_year_start_reaches_the_target_window(self):
        """Symptom 2. With an April fiscal start, a fiscal-period-to-date
        TARGET must begin at the fiscal year start. Without the shared context
        it fell back to ``DATE_TRUNC('year', CURRENT_DATE)`` — 1 January —
        while the value used the fiscal start, so attainment compared the
        value's fiscal window against eleven months of target."""
        kpi = _kpi(
            expression='fiscal_period_to_date(measure("revenue"), "year")',
            target_expression='fiscal_period_to_date(measure("revenue"), "year")',
            calc_agg_mode="automatic",
        )
        captured = await _run_single_kpi(
            kpi, fiscal_year_start_month=4, calendar_type="fiscal",
        )

        assert captured, "the evaluation must reach the router"
        target_sql = captured[-1]
        assert "DATE_TRUNC('year', CURRENT_DATE)" not in target_sql, (
            "the TARGET leg used the Gregorian year start, not the fiscal one "
            f"(Bug-8569 symptom 2): {target_sql}"
        )
        assert "DATE '" in target_sql, target_sql

    @pytest.mark.asyncio
    async def test_semi_additive_reduction_reaches_the_target(self):
        """Symptom 1. A prior-period TARGET on a closing-balance KPI must
        reduce per period. Un-reduced, balances 200/220/180 give a target of
        600 instead of 180 — attainment reads 15% where it should read 50%
        and the RAG band flips."""
        kpi = _kpi(
            at_grain="day",
            non_additive_agg="last",
            target_expression='prior_period(measure("balance"), "month")',
        )
        captured = await _run_single_kpi(kpi)

        assert captured, "the evaluation must reach the router"
        target_sql = captured[-1]
        upper = target_sql.upper()
        assert "GROUP BY" in upper and "LIMIT 1" in upper, (
            "the TARGET leg emitted a plain per-period SUM with no "
            f"semi-additive reduction (Bug-8569 symptom 1): {target_sql}"
        )

    @pytest.mark.asyncio
    async def test_business_definition_window_reaches_the_target(self):
        """Symptom 3. A business-definition window on the value must bound the
        target too, or a "last 90 days" value is compared against a target
        computed over the last calendar period."""
        kpi = _kpi(
            calc_agg_mode="automatic",
            expression='measure("revenue")',
            target_expression='prior_period(measure("revenue"), "month")',
            business_definition={
                "_compiled": {
                    "where_clause": None,
                    "filter_predicates": [],
                    "time_window_predicates": [],
                    "time_window_start_sql": "DATE '2026-01-01'",
                    "time_window_end_sql": "DATE '2026-04-01'",
                },
            },
        )
        captured = await _run_single_kpi(kpi)

        assert captured, "the evaluation must reach the router"
        target_sql = captured[-1]
        assert "2026-01-01" in target_sql and "2026-04-01" in target_sql, (
            "the TARGET leg ignored the business-definition window "
            f"(Bug-8569 symptom 3): {target_sql}"
        )
