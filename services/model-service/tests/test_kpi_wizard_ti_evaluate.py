"""Wizard/formula-path time-intelligence KPI evaluation (F-017-01).

Growth Rate and Moving Window KPIs created through the wizard (and any
formula-editor expression using time-intelligence functions) must evaluate
through the same decomposed-query machinery as business-builder KPIs.

Business outcomes asserted with concrete numbers: a +10% month-over-month
growth must come back as 0.10, a 3-month moving average of 100/200/300
must come back as 200.
"""
from __future__ import annotations

import re
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.kpi_compiler import (
    CompilerContext,
    ast_to_expression,
    compile_expression,
    derive_ti_decomposition,
    expression_has_time_intelligence,
    interval_literal,
)
from shared.semantic.kpi_expression import parse_kpi_expression

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
)

# F-017-12: shim caller_has_role to the token-role decision for these
# mocked-db unit tests (see conftest.kpi_effective_role).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"

TIME_DIM_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Pure-function tests: derivation, serialisation, interval composition
# ---------------------------------------------------------------------------


class TestIntervalLiteral:
    def test_quarter_multiplies_into_months(self):
        # PostgreSQL rejects INTERVAL '1 quarter' — must become months.
        assert interval_literal(1, "quarter") == "3 months"
        assert interval_literal(2, "quarter") == "6 months"

    def test_week_multiplies_into_days(self):
        assert interval_literal(1, "week") == "7 days"
        assert interval_literal(4, "week") == "28 days"

    def test_singular_and_plural(self):
        assert interval_literal(1, "month") == "1 month"
        assert interval_literal(3, "month") == "3 months"
        assert interval_literal(1, "year") == "1 year"
        assert interval_literal(0, "month") == "0 months"


class TestAstToExpression:
    @pytest.mark.parametrize("expr", [
        'measure("Revenue")',
        'safe_div(measure("Net Sales"), measure("Revenue"))',
        '(measure("Revenue") - measure("Cost"))',
        'coalesce(measure("Revenue"), 0)',
        '(-measure("Refunds"))',
    ])
    def test_round_trip(self, expr):
        ast = parse_kpi_expression(expr)
        serialized = ast_to_expression(ast)
        # Re-parse and re-serialize: stable canonical form
        assert ast_to_expression(parse_kpi_expression(serialized)) == serialized

    def test_number_formatting(self):
        assert ast_to_expression(parse_kpi_expression("literal(3)")) == "literal(3)"
        assert ast_to_expression(parse_kpi_expression("literal(2.5)")) == "literal(2.5)"


class TestDeriveTiDecomposition:
    def test_pct_change_maps_to_growth_pct(self):
        d = derive_ti_decomposition('pct_change(measure("Revenue"), "month")')
        assert d is not None
        assert d.ti_type == "growth_pct"
        assert d.ti_grain == "month"
        assert d.base_expression == 'measure("Revenue")'

    def test_moving_avg_wizard_argument_order(self):
        # The wizard emits (expr, "grain", literal(n)).
        d = derive_ti_decomposition(
            'moving_avg(measure("Revenue"), "month", literal(3))'
        )
        assert d is not None
        assert d.ti_type == "moving_avg"
        assert d.ti_grain == "month"
        assert d.ti_n_periods == 3

    def test_moving_avg_spec_argument_order(self):
        # The spec documents (expr, n, grain) — both orders are honoured.
        d = derive_ti_decomposition('moving_avg(measure("Revenue"), 6, "week")')
        assert d is not None
        assert d.ti_n_periods == 6
        assert d.ti_grain == "week"

    def test_trailing_sum_and_prior_period(self):
        d = derive_ti_decomposition(
            'trailing_sum(measure("Revenue"), "quarter", literal(4))'
        )
        assert d is not None and d.ti_type == "trailing_sum"
        assert d.ti_grain == "quarter" and d.ti_n_periods == 4

        d2 = derive_ti_decomposition('prior_period(measure("Revenue"), "month")')
        assert d2 is not None and d2.ti_type == "prior_period"

    def test_period_to_date_lag_lead_cagr(self):
        assert derive_ti_decomposition(
            'period_to_date(measure("Revenue"), "year")'
        ).ti_type == "period_to_date"
        assert derive_ti_decomposition(
            'fiscal_period_to_date(measure("Revenue"), "year")'
        ).ti_type == "fiscal_period_to_date"
        assert derive_ti_decomposition(
            'lag(measure("Revenue"), 2, "month")'
        ).ti_type == "lag"
        assert derive_ti_decomposition(
            'lead(measure("Revenue"), 1, "month")'
        ).ti_type == "lead"
        assert derive_ti_decomposition(
            'cagr(measure("Revenue"), 3)'
        ).ti_type == "cagr"

    def test_complex_base_expression_preserved(self):
        d = derive_ti_decomposition(
            'pct_change(safe_div(measure("Net Sales"), measure("Revenue")), "month")'
        )
        assert d is not None
        assert d.base_expression == 'safe_div(measure("Net Sales"), measure("Revenue"))'

    def test_nested_ti_not_decomposable_here(self):
        # TI inside arithmetic goes through the Python pipeline hook instead.
        assert derive_ti_decomposition(
            '(pct_change(measure("Revenue"), "month") * 100)'
        ) is None
        assert expression_has_time_intelligence(
            '(pct_change(measure("Revenue"), "month") * 100)'
        )

    def test_kpi_ref_inside_ti_stays_on_python_path(self):
        assert derive_ti_decomposition(
            'pct_change(kpi("Conversion Rate"), "month")'
        ) is None

    def test_non_ti_expression_returns_none(self):
        assert derive_ti_decomposition('measure("Revenue")') is None
        assert not expression_has_time_intelligence('measure("Revenue")')


class TestCompileScalarQuarterInterval:
    def test_moving_avg_quarter_emits_month_intervals(self):
        ctx = CompilerContext(
            model_slug="acme_sales",
            time_column="business_date",
            ti_type="moving_avg",
            ti_grain="quarter",
            ti_n_periods=3,
            base_expression='measure("Revenue")',
        )
        compiled = compile_expression(
            'moving_avg(measure("Revenue"), "quarter", literal(3))', ctx,
        )
        assert "quarter'" not in compiled.sql.lower().replace("date_trunc('quarter'", "")
        assert "9 months" in compiled.sql.lower()


# ---------------------------------------------------------------------------
# Endpoint-level tests (mocked router) — business outcomes per KPI type
# ---------------------------------------------------------------------------


def _kpi(
    *,
    name: str = "Test KPI",
    expression: str = 'measure("Revenue")',
    kpi_type: str | None = None,
    time_dimension_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        value_measure_id=None,
        goal_measure_id=None,
        status_expression=None,
        trend_expression=None,
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        weight=1.0,
        parent_kpi_id=None,
        certification_status="certified",
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
        expression=expression,
        kpi_type=kpi_type,
        calc_agg_mode="automatic",
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        target_type=None,
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
        indicator_type=None,
        time_dimension_id=time_dimension_id,
        snapshot_frequency=None,
        snapshot_retention=None,
        created_by=None,
        is_deployed=False,
        deployed_at=None,
        evaluation_order=None,
        replacement_id=None,
        business_definition=None,
    )


def _time_dim() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=TIME_DIM_ID,
        model_id=TEST_MODEL_ID,
        name="business_date",
        is_time_dim=True,
    )


def _model() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        slug="acme_sales",
        fiscal_year_start_month=None,
    )


def _db_for(kpi, model):
    db = make_mock_db()
    time_dim = _time_dim()

    async def side_get(cls, obj_id):
        if obj_id == kpi.id:
            return kpi
        if obj_id == TEST_MODEL_ID:
            return model
        if obj_id == TIME_DIM_ID:
            return time_dim
        return None

    db.get = AsyncMock(side_effect=side_get)
    return db


def _start_interval_months(query: str) -> int | None:
    """Months offset in the period start bound of a decomposed period query."""
    m = re.search(r">= CURRENT_DATE - INTERVAL '(\d+) months?'", query)
    return int(m.group(1)) if m else None


@pytest.mark.asyncio
async def test_growth_rate_kpi_evaluates_correct_growth(client):
    """+10% MoM revenue growth -> value 0.10 (wizard Growth Rate type)."""
    kpi = _kpi(
        expression='pct_change(measure("Revenue"), "month")',
        kpi_type="growth_rate",
        time_dimension_id=TIME_DIM_ID,
    )
    db = _db_for(kpi, _model())
    executed: list[str] = []

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        executed.append(query)
        # Prior period window is shifted back one month
        if "- INTERVAL '1 month'" in query:
            return {"rows": [{"value": 1000.0}]}
        return {"rows": [{"value": 1100.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] == pytest.approx(0.10)
    # Every emitted query must be a simple bounded aggregate (no window fns)
    for q in executed:
        assert "OVER (" not in q.upper()
        assert '"business_date"' in q


@pytest.mark.asyncio
async def test_moving_window_kpi_evaluates_correct_average(client):
    """3-month moving average of 100/200/300 -> 200 (wizard Moving Window type)."""
    kpi = _kpi(
        expression='moving_avg(measure("Revenue"), "month", literal(3))',
        kpi_type="moving_window",
        time_dimension_id=TIME_DIM_ID,
    )
    db = _db_for(kpi, _model())
    per_period = {1: 300.0, 2: 200.0, 3: 100.0}

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        months = _start_interval_months(query)
        assert months in per_period, f"Unexpected period query: {query}"
        return {"rows": [{"value": per_period[months]}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    assert resp.json()["value"] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_moving_window_quarter_grain_emits_valid_intervals(client):
    """Quarter-grain windows must use month intervals (INTERVAL '3 months'),
    never the invalid PostgreSQL form INTERVAL 'n quarter' (F-017-07)."""
    kpi = _kpi(
        expression='moving_avg(measure("Revenue"), "quarter", literal(2))',
        kpi_type="moving_window",
        time_dimension_id=TIME_DIM_ID,
    )
    db = _db_for(kpi, _model())
    executed: list[str] = []

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        executed.append(query)
        return {"rows": [{"value": 50.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    assert resp.json()["value"] == pytest.approx(50.0)
    assert executed, "decomposed period queries must have been issued"
    for q in executed:
        assert re.search(r"INTERVAL '\d+ quarters?'", q) is None
        assert "months'" in q


@pytest.mark.asyncio
async def test_nested_ti_expression_evaluates_via_python_hook(client):
    """pct_change(...) * 100 — nested TI resolves through the provider hook."""
    kpi = _kpi(
        expression='pct_change(measure("Revenue"), "month") * 100',
        time_dimension_id=TIME_DIM_ID,
    )
    db = _db_for(kpi, _model())

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        if "- INTERVAL '1 month'" in query:
            return {"rows": [{"value": 1000.0}]}
        return {"rows": [{"value": 1100.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    assert resp.json()["value"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_ti_kpi_without_time_dimension_gives_clear_error(client):
    kpi = _kpi(
        expression='pct_change(measure("Revenue"), "month")',
        kpi_type="growth_rate",
        time_dimension_id=None,
    )
    db = _db_for(kpi, _model())

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router",
               AsyncMock(side_effect=AssertionError("must not execute SQL"))):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] is None
    assert "time dimension" in (data["status_label"] or "").lower()


@pytest.mark.asyncio
async def test_prior_period_target_expression_evaluates(client):
    """Wizard 'Prior period value' target: value 1100 vs prior 1000 target."""
    kpi = _kpi(
        expression='measure("Revenue")',
        kpi_type="simple_measure",
        time_dimension_id=TIME_DIM_ID,
    )
    kpi.target_type = "expression"
    kpi.target_expression = 'prior_period(measure("Revenue"), "month")'
    db = _db_for(kpi, _model())

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        if "- INTERVAL '1 month'" in query:
            return {"rows": [{"value": 1000.0}]}
        return {"rows": [{"value": 1100.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] == pytest.approx(1100.0)
    assert data["target"] == pytest.approx(1000.0)
    # 110% of prior period with higher_is_better -> On Track
    assert data["status"] == 1


# ---------------------------------------------------------------------------
# Ad-hoc preview (wizard live preview path)
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        """Support the UserAccessBinding probe in ensure_project_model_access."""
        items = list(self._items)
        if not items:
            return None
        if len(items) == 1:
            return items[0]
        return items[0]

    def first(self):
        """Support the bootstrap existence probe in require_role."""
        items = list(self._items)
        return items[0] if items else None


@pytest.mark.asyncio
async def test_adhoc_ti_with_time_dimension_id_evaluates(client):
    """The wizard preview passes time_dimension (the dimension id) and the
    growth expression must evaluate against it."""
    model = _model()
    db = make_mock_db()
    db.get = AsyncMock(return_value=model)
    # Call order in evaluate-adhoc: persona query, then measures select, then dimensions select.
    db.execute = AsyncMock(side_effect=[
        _ScalarResult([]),               # get_assigned_personas (persona resolution)
        _ScalarResult([]),               # measures
        _ScalarResult([_time_dim()]),    # dimensions (time_dimension resolution)
    ])

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        assert '"business_date"' in query
        if "- INTERVAL '1 month'" in query:
            return {"rows": [{"value": 200.0}]}
        return {"rows": [{"value": 260.0}]}

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={
                "expression": 'pct_change(measure("Revenue"), "month")',
                "time_dimension": str(TIME_DIM_ID),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["value"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_adhoc_ti_without_time_dimension_400s_with_clear_message(client):
    model = _model()
    db = make_mock_db()
    db.get = AsyncMock(return_value=model)

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router",
               AsyncMock(side_effect=AssertionError("must not execute SQL"))):
        resp = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={"expression": 'pct_change(measure("Revenue"), "month")'},
        )
    assert resp.status_code == 400
    assert "time dimension" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Derived-TI failure must fail loud with the precise cause — never fall
# through to the legacy window-over-aggregate SQL the router rejects.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derived_ti_failure_fails_loud_with_cause(client):
    """A router failure inside the decomposed TI evaluation surfaces the
    real cause, not the generic 'check KPI expression and model scope'."""
    kpi = _kpi(
        expression='pct_change(measure("Revenue"), "month")',
        kpi_type="growth_rate",
        time_dimension_id=TIME_DIM_ID,
    )
    db = _db_for(kpi, _model())
    calls: list[str] = []

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        calls.append(query)
        raise ValueError('relation "missing_table" does not exist')

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["value"] is None
    label = data["status_label"] or ""
    assert label.startswith("Time-intelligence evaluation failed — ")
    assert 'relation "missing_table" does not exist' in label
    # No fall-through: only the decomposed queries ran, never the legacy
    # window-over-aggregate SQL the router is known to reject.
    for q in calls:
        assert "OVER (" not in q.upper()


@pytest.mark.asyncio
async def test_adhoc_derived_ti_failure_400s_with_cause(client):
    """The wizard preview surfaces the precise decomposition failure."""
    model = _model()
    db = make_mock_db()
    db.get = AsyncMock(return_value=model)
    db.execute = AsyncMock(side_effect=[
        _ScalarResult([]),               # get_assigned_personas (persona resolution)
        _ScalarResult([]),               # measures
        _ScalarResult([_time_dim()]),    # dimensions (time_dimension resolution)
    ])

    async def mock_execute(model_id, query, bearer, timeout_s=30.0, **kwargs):
        raise ValueError("permission denied for table fact_sales")

    with patch("src.api.kpis.get_tenant_db", async_gen_from(db)), \
         patch("src.api.kpis._execute_via_router", side_effect=mock_execute):
        resp = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={
                "expression": 'pct_change(measure("Revenue"), "month")',
                "time_dimension": str(TIME_DIM_ID),
            },
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail.startswith("Time-intelligence evaluation failed — ")
    assert "permission denied for table fact_sales" in detail
