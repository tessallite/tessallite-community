"""Bug-9881: KPI evaluation is a CONSUMPTION route, pinned to the deployed model.

``/kpis/{id}/evaluate`` and ``/kpis/evaluate-batch`` are the routes that produce
the number an Excel cell, a scorecard card, an XMLA KPI member and the agent's
answer all show.  Every other BI-facing read already pins to the deployed
snapshot (F-013-01, F-017-05, Bug-8384); these two did not accept
``deployed_only`` at all, so a caller could not ask for the served definition
and an UNDEPLOYED model silently answered from the live editor draft.

The routes now default ``deployed_only`` to TRUE — a served number is always
the deployed one — and the SPA's KPI authoring panel is the single caller that
opts out to preview the draft it is editing.

Test escape: the existing deploy-authority coverage (Bug-8710) asserted the
LIST route only, and every evaluate test used an undeployed mock model, so the
live-draft fallback was the tested path rather than the guarded one.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import KPI
from src.kpi_cache import get_kpi_cache

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,  # noqa: F401 -- pytest fixture
    make_mock_db,
    make_model,
    routed_execute,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"

KPI_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()

EXPRESSION = 'measure("Revenue")'
DEPLOYED_TARGET = 2000.0
DRAFT_TARGET = 500.0
ROUTER_VALUE = 900.0


def _live_kpi(target_value: float = DRAFT_TARGET) -> KPI:
    """The LIVE row: a modeller has edited the target but not deployed."""
    return KPI(
        id=KPI_ID,
        model_id=TEST_MODEL_ID,
        name="Revenue KPI",
        kpi_type="simple_measure",
        expression=EXPRESSION,
        direction="higher_is_better",
        target_type="static",
        target_value=target_value,
        certification_status="certified",
        owner_user_id="owner@acme.com",
        is_deployed=True,
        deployed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        calc_agg_mode="automatic",
        carry_forward=False,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        null_display_value="N/A",
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _snapshot_kpi(target_value: float = DEPLOYED_TARGET) -> dict:
    return {
        "id": str(KPI_ID),
        "model_id": str(TEST_MODEL_ID),
        "name": "Revenue KPI",
        "kpi_type": "simple_measure",
        "expression": EXPRESSION,
        "direction": "higher_is_better",
        "target_type": "static",
        "target_value": target_value,
        "calc_agg_mode": "automatic",
        "trend_period": "month",
        "trend_threshold": 0.01,
        "trend_sparkline_periods": 12,
        "null_display_value": "N/A",
        "status_graphic": "Traffic Light",
        "trend_graphic": "Standard Arrow",
    }


def _model(*, deployed: bool):
    model = make_model()
    model.deployed_version_id = VERSION_ID if deployed else None
    model.deploy_epoch = 2 if deployed else 0
    model.data_epoch = 0
    model.fiscal_year_start_month = None
    return model


def _version(kpi_rows: list[dict]):
    return types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "kpis": kpi_rows,
        },
    )


def _db(*, model, version, live_kpi):
    db = make_mock_db()

    async def _get(cls, obj_id):
        if obj_id == KPI_ID:
            return live_kpi
        if obj_id == TEST_MODEL_ID:
            return model
        if obj_id == VERSION_ID:
            return version
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(
        side_effect=routed_execute(kpis=[live_kpi] if live_kpi else [])
    )
    return db


async def _router_value(model_id, query, bearer, timeout_s=30.0, **kwargs):
    return {"rows": [{"value": ROUTER_VALUE}], "cells": [{"value": ROUTER_VALUE}]}


async def _evaluate(client, db, *, query: str = ""):  # noqa: F811
    # The evaluation cache is process-wide and keyed on (kpi, definition
    # version); these cases deliberately reuse one KPI id under different
    # definitions, so each must start from a cold cache.
    get_kpi_cache().clear()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona", new=AsyncMock(return_value=None)
        ),
        patch("src.api.kpis._execute_via_router", side_effect=_router_value),
    ):
        return await client.post(f"{PREFIX}/{KPI_ID}/evaluate{query}")


async def _evaluate_batch(client, db, *, query: str = ""):  # noqa: F811
    get_kpi_cache().clear()
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona", new=AsyncMock(return_value=None)
        ),
        patch("src.api.kpis._execute_via_router", side_effect=_router_value),
        patch("src.api.kpis._upsert_kpi_latest_batch", new_callable=AsyncMock),
    ):
        return await client.post(
            f"{PREFIX}/evaluate-batch{query}", json={"kpi_ids": [str(KPI_ID)]}
        )


# ---------------------------------------------------------------------------
# The defect: an undeployed draft edit changed the served number.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_target_edit_does_not_change_the_served_number(client):  # noqa: F811
    """The modeller has changed the target to 500 and NOT deployed.

    A consumption evaluation must still answer against the deployed 1000.
    """
    db = _db(
        model=_model(deployed=True),
        version=_version([_snapshot_kpi(DEPLOYED_TARGET)]),
        live_kpi=_live_kpi(DRAFT_TARGET),
    )
    resp = await _evaluate(client, db)

    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] == ROUTER_VALUE
    assert body["target"] == DEPLOYED_TARGET
    # 900 against a target of 1000 is BEHIND; against the draft 500 it would be
    # ahead, so the status flips with the definition — this is the wrong number.
    assert body["status"] == -1


@pytest.mark.asyncio
async def test_the_same_edit_lands_once_it_is_deployed(client):  # noqa: F811
    """Deploy publishes the edit into the snapshot; only then does it serve."""
    db = _db(
        model=_model(deployed=True),
        version=_version([_snapshot_kpi(DRAFT_TARGET)]),
        live_kpi=_live_kpi(DRAFT_TARGET),
    )
    resp = await _evaluate(client, db)

    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == DRAFT_TARGET
    assert body["status"] == 1


@pytest.mark.asyncio
async def test_opting_out_does_not_reopen_a_deployed_model(client):  # noqa: F811
    """``deployed_only=false`` is not a way back to the live definition.

    On a DEPLOYED model the snapshot is the authority for EVERY caller
    (F-017-01) — the opt-out only governs an UNDEPLOYED model, where there is
    no serving authority and only the builder may see its own draft. A future
    change that let the opt-out serve a live edit on a deployed model would
    hand every consumer the undeployed number again.
    """
    db = _db(
        model=_model(deployed=True),
        version=_version([_snapshot_kpi(DEPLOYED_TARGET)]),
        live_kpi=_live_kpi(DRAFT_TARGET),
    )
    resp = await _evaluate(client, db, query="?deployed_only=false")

    assert resp.status_code == 200
    assert resp.json()["target"] == DEPLOYED_TARGET


# ---------------------------------------------------------------------------
# An UNDEPLOYED model has no serving authority at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undeployed_model_is_withheld_from_a_consumption_call(client):  # noqa: F811
    db = _db(model=_model(deployed=False), version=None, live_kpi=_live_kpi())
    resp = await _evaluate(client, db)

    assert resp.status_code == 404
    assert "deployed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_undeployed_model_still_previews_for_the_builder(client):  # noqa: F811
    db = _db(model=_model(deployed=False), version=None, live_kpi=_live_kpi())
    resp = await _evaluate(client, db, query="?deployed_only=false")

    assert resp.status_code == 200
    assert resp.json()["target"] == DRAFT_TARGET


@pytest.mark.asyncio
async def test_kpi_absent_from_the_deployed_version_is_withheld(client):  # noqa: F811
    other = _snapshot_kpi()
    other["id"] = str(uuid.uuid4())
    db = _db(
        model=_model(deployed=True),
        version=_version([other]),
        live_kpi=_live_kpi(),
    )
    resp = await _evaluate(client, db)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_invalid_deployed_snapshot_fails_closed(client):  # noqa: F811
    broken = types.SimpleNamespace(
        id=VERSION_ID, model_id=TEST_MODEL_ID, snapshot_json={"schema_version": "1.0"},
    )
    db = _db(model=_model(deployed=True), version=broken, live_kpi=_live_kpi())
    resp = await _evaluate(client, db)

    assert resp.status_code == 409
    assert "DEPLOYED_SNAPSHOT_INVALID" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# The batch route carries the same contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_withholds_an_undeployed_model(client):  # noqa: F811
    db = _db(model=_model(deployed=False), version=None, live_kpi=_live_kpi())
    resp = await _evaluate_batch(client, db)

    assert resp.status_code == 200
    results = resp.json()["results"]
    # Withheld, exactly as a KPI missing from a deployed snapshot is: no value
    # is served from the live editor draft.
    assert all(r["value"] is None for r in results)


@pytest.mark.asyncio
async def test_batch_builder_opt_out_evaluates_the_draft(client):  # noqa: F811
    db = _db(model=_model(deployed=False), version=None, live_kpi=_live_kpi())
    resp = await _evaluate_batch(client, db, query="?deployed_only=false")

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["value"] for r in results] == [ROUTER_VALUE]
    assert results[0]["target"] == DRAFT_TARGET
