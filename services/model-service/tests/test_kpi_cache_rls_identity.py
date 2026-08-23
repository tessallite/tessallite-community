"""Regression coverage for KPI outer-cache row-security identity."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.schemas.pydantic_models import KPIEvaluateResponse
from src.api.kpis import _kpi_outer_cache_allowed
from src.kpi_cache import get_kpi_cache

from .conftest import TEST_MODEL_ID, async_gen_from
from .test_kpi_composite_indicators import (
    PREFIX,
    _ScalarResult,
    _entity_db,
    _kpi,
    _model,
)


# F-017-12: shim caller_has_role to the token-role decision for these
# mocked-db unit tests (see conftest.kpi_effective_role).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]


def _set_rls_subject(user, field: str, value) -> None:
    setattr(user, field, value)


@pytest.mark.parametrize(("active_rule_ids", "expected"), [([], True), (["rule"], False)])
@pytest.mark.asyncio
async def test_outer_cache_admission_fails_closed_for_enabled_rls(
    active_rule_ids,
    expected,
):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(active_rule_ids))

    assert await _kpi_outer_cache_allowed(db, TEST_MODEL_ID) is expected


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("roles", ["north"], ["south"]),
        ("groups", ["finance"], ["sales"]),
        ("claims", {"region": "north"}, {"region": "south"}),
    ],
)
@pytest.mark.asyncio
async def test_single_rls_model_never_replays_same_subject_across_principal_changes(
    client,
    override_auth,
    field,
    first,
    second,
):
    kpi = _kpi(name="Principal Scoped", expression="literal(1)")
    db = _entity_db(_model(deployed=False), [], get_kpis=[kpi])
    cache = get_kpi_cache()
    cache.invalidate_model(TEST_MODEL_ID)
    values = AsyncMock(side_effect=[100.0, 20.0])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch("src.api.kpis._evaluate_expression_via_sql", values),
    ):
        _set_rls_subject(override_auth, field, first)
        first_response = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
        _set_rls_subject(override_auth, field, second)
        second_response = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["value"] == 100.0
    assert second_response.json()["value"] == 20.0
    assert values.await_count == 2


@pytest.mark.asyncio
async def test_single_new_rls_policy_bypasses_an_existing_unrestricted_entry(
    client,
):
    kpi = _kpi(name="Policy Mutation", expression="literal(1)")
    db = _entity_db(_model(deployed=False), [], get_kpis=[kpi])
    cache = get_kpi_cache()
    cache.invalidate_model(TEST_MODEL_ID)
    values = AsyncMock(side_effect=[100.0, 20.0])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch(
            "src.api.kpis._kpi_outer_cache_allowed",
            new_callable=AsyncMock,
            side_effect=[True, False],
        ),
        patch("src.api.kpis._evaluate_expression_via_sql", values),
    ):
        before_policy = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
        after_policy = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert before_policy.json()["value"] == 100.0
    assert after_policy.json()["value"] == 20.0
    assert values.await_count == 2


def _batch_result_values(*values: float):
    responses = iter(values)

    async def _evaluate(kpi, *args, **kwargs):
        return KPIEvaluateResponse(kpi_id=kpi.id, value=next(responses))

    return _evaluate


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("roles", ["north"], ["south"]),
        ("groups", ["finance"], ["sales"]),
        ("claims", {"region": "north"}, {"region": "south"}),
    ],
)
@pytest.mark.asyncio
async def test_batch_rls_model_never_replays_same_subject_across_principal_changes(
    client,
    override_auth,
    field,
    first,
    second,
):
    kpi = _kpi(name="Batch Principal Scoped", expression="literal(1)")
    db = _entity_db(_model(deployed=False), [[kpi], [kpi]])
    cache = get_kpi_cache()
    cache.invalidate_model(TEST_MODEL_ID)
    evaluate = AsyncMock(side_effect=_batch_result_values(100.0, 20.0))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch("src.api.kpis._evaluate_single_kpi", evaluate),
    ):
        _set_rls_subject(override_auth, field, first)
        first_response = await client.post(
            f"{PREFIX}/evaluate-batch", json={"kpi_ids": [str(kpi.id)]},
        )
        _set_rls_subject(override_auth, field, second)
        second_response = await client.post(
            f"{PREFIX}/evaluate-batch", json={"kpi_ids": [str(kpi.id)]},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["results"][0]["value"] == 100.0
    assert second_response.json()["results"][0]["value"] == 20.0
    assert evaluate.await_count == 2


@pytest.mark.asyncio
async def test_batch_new_rls_policy_bypasses_existing_unrestricted_entries(client):
    kpi = _kpi(name="Batch Policy Mutation", expression="literal(1)")
    db = _entity_db(_model(deployed=False), [[kpi], [kpi]])
    cache = get_kpi_cache()
    cache.invalidate_model(TEST_MODEL_ID)
    evaluate = AsyncMock(side_effect=_batch_result_values(100.0, 20.0))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch(
            "src.api.kpis._kpi_outer_cache_allowed",
            new_callable=AsyncMock,
            side_effect=[True, False],
        ),
        patch("src.api.kpis._evaluate_single_kpi", evaluate),
    ):
        before_policy = await client.post(
            f"{PREFIX}/evaluate-batch", json={"kpi_ids": [str(kpi.id)]},
        )
        after_policy = await client.post(
            f"{PREFIX}/evaluate-batch", json={"kpi_ids": [str(kpi.id)]},
        )

    assert before_policy.json()["results"][0]["value"] == 100.0
    assert after_policy.json()["results"][0]["value"] == 20.0
    assert evaluate.await_count == 2


def _mutable_persona():
    """A same-id persona whose gateway-owned policy changes between requests."""
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        included_measure_ids=None,
        included_dimension_ids=None,
        audience_filters={"region": "north"},
        tag_restrictions=["internal"],
    )


@pytest.mark.asyncio
async def test_single_persona_policy_mutation_bypasses_outer_cache(client):
    """T2: the next persona-scoped request must reach gateway policy authority."""
    kpi = _kpi(name="Persona Policy Single", expression="literal(1)")
    db = _entity_db(_model(deployed=False), [], get_kpis=[kpi])
    cache = get_kpi_cache()
    cache.clear()
    persona = _mutable_persona()
    values = AsyncMock(side_effect=[100.0, 20.0])

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=persona),
            patch("src.api.kpis._evaluate_expression_via_sql", values),
        ):
            before = await client.post(f"{PREFIX}/{kpi.id}/evaluate")
            persona.audience_filters = {"region": "south"}
            persona.tag_restrictions = ["restricted"]
            after = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

        assert before.status_code == 200
        assert after.status_code == 200
        assert before.json()["value"] == 100.0
        assert after.json()["value"] == 20.0
        assert values.await_count == 2
        assert cache.size == 0
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_batch_persona_policy_mutation_bypasses_outer_cache(client):
    """T2: batch scorecards also re-enter gateway after same-persona changes."""
    kpi = _kpi(name="Persona Policy Batch", expression="literal(1)")
    db = _entity_db(_model(deployed=False), [[kpi], [kpi]])
    cache = get_kpi_cache()
    cache.clear()
    persona = _mutable_persona()
    evaluate = AsyncMock(side_effect=_batch_result_values(100.0, 20.0))

    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=persona),
            patch("src.api.kpis._evaluate_single_kpi", evaluate),
        ):
            before = await client.post(
                f"{PREFIX}/evaluate-batch", json={"kpi_ids": [str(kpi.id)]},
            )
            persona.audience_filters = {"region": "south"}
            persona.tag_restrictions = ["restricted"]
            after = await client.post(
                f"{PREFIX}/evaluate-batch", json={"kpi_ids": [str(kpi.id)]},
            )

        assert before.status_code == 200
        assert after.status_code == 200
        assert before.json()["results"][0]["value"] == 100.0
        assert after.json()["results"][0]["value"] == 20.0
        assert evaluate.await_count == 2
        assert cache.size == 0
    finally:
        cache.clear()
