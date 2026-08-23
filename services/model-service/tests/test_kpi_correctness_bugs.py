"""Regression tests for KPI evaluation correctness bugs 8575, 8486, 8487."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
)

# F-017-12: shim caller_has_role to the token-role decision for these
# mocked-db unit tests (see conftest.kpi_effective_role).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"

MEASURE_ID = uuid.uuid4()


def _kpi(
    *,
    kpi_id: uuid.UUID | None = None,
    name: str = "Revenue KPI",
    expression: str | None = 'measure("Revenue")',
    target_type: str | None = None,
    target_value: float | None = None,
    target_expression: str | None = None,
    business_definition: dict | None = None,
    kpi_type: str | None = None,
    parent_kpi_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=kpi_id or uuid.uuid4(),
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
        parent_kpi_id=parent_kpi_id,
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
        target_type=target_type,
        target_value=target_value,
        target_measure_id=None,
        target_expression=target_expression,
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
        time_dimension_id=None,
        snapshot_frequency=None,
        snapshot_retention=None,
        created_by=None,
        is_deployed=False,
        deployed_at=None,
        evaluation_order=None,
        replacement_id=None,
        business_definition=business_definition,
    )


def _measure(name: str = "Revenue", default_agg: str = "sum"):
    return types.SimpleNamespace(
        id=MEASURE_ID,
        model_id=TEST_MODEL_ID,
        name=name,
        default_agg=default_agg,
    )


def _model():
    return types.SimpleNamespace(
        id=TEST_MODEL_ID,
        slug="acme_sales",
        deployed_version_id=None,
        deploy_epoch=0,
        data_epoch=0,
        fiscal_year_start_month=None,
    )


# ---------------------------------------------------------------------------
# Bug-8575: filtered KPI fallback answers with unfiltered model-wide query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_measure_values_includes_where_clause():
    """Bug-8575: _batch_get_measure_values must emit a WHERE clause when one
    is provided, so a filtered KPI is answered with the filtered total, not
    the unfiltered model-wide aggregate."""
    from src.api.kpis import _batch_get_measure_values

    captured_sql: list[str] = []

    async def fake_router(model_id, sql, bearer, **kwargs):
        captured_sql.append(sql)
        return {"rows": [{"m0": 220.0}]}

    measure_map = {"Revenue": _measure()}

    with patch("src.api.kpis._execute_via_router", side_effect=fake_router):
        result = await _batch_get_measure_values(
            TEST_MODEL_ID,
            ["Revenue"],
            "fake-bearer",
            "acme_sales",
            measure_map,
            where_clause='"region" = \'EMEA\'',
        )

    assert result["Revenue"] == 220.0
    assert len(captured_sql) == 1
    assert "WHERE" in captured_sql[0]
    assert "EMEA" in captured_sql[0]


@pytest.mark.asyncio
async def test_batch_measure_values_no_where_without_clause():
    """Regression guard: without a where_clause the generated SQL must not
    contain a WHERE (backward compat with the unfiltered path)."""
    from src.api.kpis import _batch_get_measure_values

    captured_sql: list[str] = []

    async def fake_router(model_id, sql, bearer, **kwargs):
        captured_sql.append(sql)
        return {"rows": [{"m0": 310.0}]}

    measure_map = {"Revenue": _measure()}

    with patch("src.api.kpis._execute_via_router", side_effect=fake_router):
        result = await _batch_get_measure_values(
            TEST_MODEL_ID,
            ["Revenue"],
            "fake-bearer",
            "acme_sales",
            measure_map,
        )

    assert result["Revenue"] == 310.0
    assert "WHERE" not in captured_sql[0]


# ---------------------------------------------------------------------------
# Bug-8487: undeployed model evaluation must surface a clear message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_expression_via_sql_returns_model_not_deployed():
    """Bug-8487: when the router returns 409 'Model is not deployed', the
    evaluator must return the _MODEL_NOT_DEPLOYED sentinel, not a generic
    _EVALUATION_ERROR."""
    from src.api.kpis import (
        _MODEL_NOT_DEPLOYED,
        _evaluate_expression_via_sql,
        EvaluationContext,
    )

    async def refusing_router(model_id, sql, bearer, **kwargs):
        raise ValueError("Model is not deployed")

    measure_map = {"Revenue": _measure()}
    ctx = MagicMock(spec=EvaluationContext)
    ctx.calc_agg_mode = "automatic"

    with patch("src.api.kpis._execute_via_router", side_effect=refusing_router):
        result = await _evaluate_expression_via_sql(
            'measure("Revenue")',
            TEST_MODEL_ID,
            "acme_sales",
            "fake-bearer",
            measure_map,
            ctx,
        )

    assert result is _MODEL_NOT_DEPLOYED


@pytest.mark.asyncio
async def test_batch_measure_fallback_preserves_model_not_deployed():
    """Unsupported expressions must not turn an undeployed refusal into None."""
    from src.api.kpis import _ModelNotDeployedError, _batch_get_measure_values

    async def refusing_router(model_id, sql, bearer, **kwargs):
        raise ValueError("Model is not deployed")

    with patch("src.api.kpis._execute_via_router", side_effect=refusing_router):
        with pytest.raises(_ModelNotDeployedError):
            await _batch_get_measure_values(
                TEST_MODEL_ID,
                ["Revenue"],
                "fake-bearer",
                "acme_sales",
                {"Revenue": _measure()},
            )


@pytest.mark.asyncio
async def test_decomposed_ti_returns_model_not_deployed_sentinel():
    """The direct time-intelligence path preserves the deployment refusal."""
    from src.api.kpis import (
        EvaluationContext,
        _MODEL_NOT_DEPLOYED,
        _evaluate_expression_via_sql,
    )

    ctx = MagicMock(spec=EvaluationContext)
    ctx.calc_agg_mode = "automatic"
    with patch(
        "src.api.kpis._evaluate_ti_decomposed",
        new_callable=AsyncMock,
        side_effect=ValueError("Model is not deployed"),
    ):
        result = await _evaluate_expression_via_sql(
            'prior_period(measure("Revenue"), "month")',
            TEST_MODEL_ID,
            "acme_sales",
            "fake-bearer",
            {"Revenue": _measure()},
            ctx,
            time_column="Order Date",
        )

    assert result is _MODEL_NOT_DEPLOYED


@pytest.mark.asyncio
async def test_nested_ti_provider_preserves_model_not_deployed():
    """The Python TI hook raises the internal deployment signal to its caller."""
    from src.api.kpis import _ModelNotDeployedError, _build_ti_evaluator

    decomposition = types.SimpleNamespace(
        base_expression='measure("Revenue")',
        ti_type="prior_period",
        ti_grain="month",
        ti_n_periods=1,
    )
    evaluator = _build_ti_evaluator(
        TEST_MODEL_ID,
        "acme_sales",
        "fake-bearer",
        {"Revenue": _measure()},
        time_column="Order Date",
    )
    with (
        patch(
            "src.api.kpis.derive_ti_decomposition_from_node",
            return_value=decomposition,
        ),
        patch(
            "src.api.kpis._evaluate_ti_decomposed",
            new_callable=AsyncMock,
            side_effect=ValueError("Model is not deployed"),
        ),
    ):
        with pytest.raises(_ModelNotDeployedError):
            await evaluator(object())


@pytest.mark.asyncio
async def test_saved_fallback_surfaces_model_not_deployed(client):
    """The saved endpoint labels an undeployed unsupported-expression fallback."""
    import src.api.kpis as kpis_mod
    from .test_kpi_composite_indicators import _entity_db, _kpi as make_kpi, _model

    kpi = make_kpi(
        name="Undeployed Saved KPI",
        expression='coalesce(measure("Revenue"), literal(0))',
    )
    db = _entity_db(_model(deployed=False), [], get_kpis=[kpi])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch("src.api.kpis._evaluate_expression_via_sql", new_callable=AsyncMock, return_value=kpis_mod._COMPILER_UNSUPPORTED),
        patch("src.api.kpis._batch_get_measure_values", new_callable=AsyncMock, side_effect=kpis_mod._ModelNotDeployedError()),
    ):
        response = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert response.status_code == 200
    assert response.json()["status_label"] == kpis_mod._MODEL_NOT_DEPLOYED_LABEL


@pytest.mark.asyncio
async def test_adhoc_fallback_surfaces_model_not_deployed(client):
    """The builder preview returns the same deployment guidance."""
    import src.api.kpis as kpis_mod
    from .test_kpi_composite_indicators import _entity_db, _model

    model = _model(deployed=False)
    model.project_id = TEST_PROJECT_ID
    db = _entity_db(model, [])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._evaluate_expression_via_sql", new_callable=AsyncMock, return_value=kpis_mod._COMPILER_UNSUPPORTED),
        patch("src.api.kpis._batch_get_measure_values", new_callable=AsyncMock, side_effect=kpis_mod._ModelNotDeployedError()),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={"expression": 'coalesce(measure("Revenue"), literal(0))'},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == kpis_mod._MODEL_NOT_DEPLOYED_LABEL


@pytest.mark.asyncio
async def test_batch_fallback_surfaces_model_not_deployed(client):
    """Batch prefetch labels every requested KPI affected by the refusal."""
    import src.api.kpis as kpis_mod
    from .test_kpi_composite_indicators import _entity_db, _kpi as make_kpi, _model

    kpi = make_kpi(name="Undeployed Batch KPI")
    db = _entity_db(_model(deployed=False), [[kpi]])
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch("src.api.kpis._batch_get_measure_values", new_callable=AsyncMock, side_effect=kpis_mod._ModelNotDeployedError()),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(kpi.id)]},
        )

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["kpi_id"] == str(kpi.id)
    assert result["status_label"] == kpis_mod._MODEL_NOT_DEPLOYED_LABEL


# ---------------------------------------------------------------------------
# Bug-8486: target kpi() reference dependency ordering and security propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_expression_cycles_includes_composite_ownership():
    """Bug-8486/8590: _check_expression_cycles must detect cycles through
    composite ownership edges, not just expression kpi() references."""
    from src.api.kpis import _check_expression_cycles

    parent_id = uuid.uuid4()
    parent = types.SimpleNamespace(
        id=parent_id,
        name="Composite Parent",
        expression="literal(0)",
        target_expression=None,
        parent_kpi_id=None,
    )

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def __iter__(self):
            return iter(self._rows)

    db = make_mock_db()
    db.execute = AsyncMock(return_value=_FakeResult([
        (parent.id, parent.name, parent.expression, parent.target_expression, parent.parent_kpi_id),
    ]))

    # A new child that references the composite parent in its target
    # expression creates a cycle: parent -> child (ownership) -> parent
    # (target expression kpi() reference).
    cycles = await _check_expression_cycles(
        db,
        TEST_MODEL_ID,
        "Child KPI",
        "literal(50)",
        'kpi("Composite Parent")',
        parent_kpi_id=parent_id,
    )

    assert cycles is not None
    assert len(cycles) > 0
    assert any("Composite Parent" in c for c in cycles)


# ---------------------------------------------------------------------------
# Bug-8487 R1 finding: _MODEL_NOT_DEPLOYED must be excluded from target
# sentinel checks so it does not leak as a target value
# ---------------------------------------------------------------------------

def test_model_not_deployed_is_evaluation_failure():
    """Bug-8487 R1: _MODEL_NOT_DEPLOYED is treated as an evaluation failure
    so it is never mistaken for a valid float target value."""
    from src.api.kpis import _is_evaluation_failure, _MODEL_NOT_DEPLOYED
    assert _is_evaluation_failure(_MODEL_NOT_DEPLOYED)
