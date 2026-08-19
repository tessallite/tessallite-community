"""Bug-8710: GET /kpis?deployed_only=true, at the route the Excel add-in calls.

The Excel task pane is an end-user BI transport, not an authoring surface, so it
now requests the PUBLISHED catalogue. Two properties have to hold at the route
for that to mean anything:

1. the definition served is the deployed snapshot's, not the live draft;
2. the authority is validated even when no live row survives the filters.

(2) is Bug-8712's sibling defect in this family: the resolver used to be guarded
by ``if deployed_only and kpis``, so a model with a missing or malformed deployed
snapshot returned HTTP 200 and an empty list whenever the live set was empty,
while the named-set family returned 409 for the same broken snapshot. A BI client
was told "this model has no KPIs" instead of being given a diagnosable failure,
and the two halves of one deploy authority disagreed about the same model.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import KPI

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,  # noqa: F401 -- pytest fixture
    make_mock_db,
    make_model,
    routed_execute,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"

KPI_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()

DEPLOYED_NAME = "Net Revenue"
DRAFT_NAME = "Net Revenue (rewrite in progress)"
DEPLOYED_EXPR = "measure('net_revenue')"
DRAFT_EXPR = "measure('gross_revenue') - measure('returns')"


def _live_kpi():
    return KPI(
        id=KPI_ID,
        model_id=TEST_MODEL_ID,
        name=DRAFT_NAME,
        kpi_type="simple_measure",
        expression=DRAFT_EXPR,
        direction="higher_is_better",
        target_type="static",
        target_value=100.0,
        certification_status="certified",
        owner_user_id="owner@acme.com",
        is_deployed=True,
        deployed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        # Columns whose values come from server defaults in a real session; the
        # response model requires them, so state them explicitly here.
        calc_agg_mode="automatic",
        carry_forward=False,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        null_display_value="N/A",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _snapshot_kpi():
    return {
        "id": str(KPI_ID),
        "model_id": str(TEST_MODEL_ID),
        "name": DEPLOYED_NAME,
        "kpi_type": "simple_measure",
        "expression": DEPLOYED_EXPR,
        "direction": "higher_is_better",
        "target_type": "static",
        "target_value": 100.0,
        # Governance in the snapshot must be ignored; the live row wins.
        "certification_status": "draft",
        "is_deployed": False,
        "owner_user_id": "stale@acme.com",
    }


def _deployed_model():
    model = make_model()
    model.deployed_version_id = VERSION_ID
    model.deploy_epoch = 2
    return model


def _version(kpi_rows):
    return types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "net_revenue"}],
            "kpis": kpi_rows,
        },
    )


def _db(*, model, version, live_kpis):
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[model, version])
    db.execute = AsyncMock(side_effect=routed_execute(kpis=live_kpis))
    return db


async def _list(client, db, *, query: str):  # noqa: F811
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        return await client.get(f"{PREFIX}{query}")


@pytest.mark.asyncio
async def test_consumption_catalogue_serves_the_deployed_definition(client):  # noqa: F811
    db = _db(
        model=_deployed_model(),
        version=_version([_snapshot_kpi()]),
        live_kpis=[_live_kpi()],
    )
    resp = await _list(client, db, query="?deployed_only=true")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == DEPLOYED_NAME
    assert body[0]["expression"] == DEPLOYED_EXPR
    # Governance stays live, so a deprecation still takes effect without deploy.
    assert body[0]["certification_status"] == "certified"


@pytest.mark.asyncio
async def test_builder_without_the_flag_still_sees_the_draft(client):  # noqa: F811
    db = _db(
        model=_deployed_model(),
        version=_version([_snapshot_kpi()]),
        live_kpis=[_live_kpi()],
    )
    resp = await _list(client, db, query="")

    assert resp.status_code == 200
    assert resp.json()[0]["name"] == DRAFT_NAME


@pytest.mark.asyncio
async def test_invalid_snapshot_fails_closed_even_with_no_live_kpis(client):  # noqa: F811
    """The empty-list short-circuit: 409, not a silently empty catalogue.

    Matches ``list_named_sets``, which already failed closed here.
    """
    broken_version = types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={"schema_version": "1.0"},
    )
    db = _db(model=_deployed_model(), version=broken_version, live_kpis=[])
    resp = await _list(client, db, query="?deployed_only=true")

    assert resp.status_code == 409
    assert "DEPLOYED_SNAPSHOT_INVALID" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_invalid_snapshot_fails_closed_with_live_kpis(client):  # noqa: F811
    broken_version = types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={"schema_version": "1.0"},
    )
    db = _db(
        model=_deployed_model(), version=broken_version, live_kpis=[_live_kpi()],
    )
    resp = await _list(client, db, query="?deployed_only=true")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_kpi_created_since_last_deploy_is_withheld(client):  # noqa: F811
    other = _snapshot_kpi()
    other["id"] = str(uuid.uuid4())
    db = _db(
        model=_deployed_model(), version=_version([other]), live_kpis=[_live_kpi()],
    )
    resp = await _list(client, db, query="?deployed_only=true")

    assert resp.status_code == 200
    assert resp.json() == []
