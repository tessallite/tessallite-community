"""Tests for KPI snapshot API endpoints."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    async_gen_from,
    make_mock_db,
)

# F-017-12: shim caller_has_role to the token-role decision for these mocked-db
# unit tests (see conftest.kpi_effective_role).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


def _kpi(
    *,
    kpi_id: uuid.UUID | None = None,
    name: str = "Test KPI",
    snapshot_frequency: str | None = "0 23 * * *",
    certification_status: str = "published",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=kpi_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        # project_id is needed because db.get(Model, ...) in ensure_model_in_project
        # returns this stub and checks .project_id against the URL project_id.
        project_id=TEST_PROJECT_ID,
        name=name,
        certification_status=certification_status,
        snapshot_frequency=snapshot_frequency,
    )


def _snapshot(
    *,
    kpi_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = None,
    value: float | None = 42.5,
    target: float | None = 50.0,
    status: int | None = 1,
    status_label: str | None = "On Track",
    trend_pct: float | None = 5.2,
    hours_ago: int = 0,
) -> types.SimpleNamespace:
    from decimal import Decimal
    return types.SimpleNamespace(
        id=snapshot_id or uuid.uuid4(),
        kpi_id=kpi_id,
        snapshot_at=datetime(2026, 6, 1, 23 - hours_ago, 0, 0, tzinfo=timezone.utc),
        value=Decimal(str(value)) if value is not None else None,
        target=Decimal(str(target)) if target is not None else None,
        status=status,
        status_label=status_label,
        trend_pct=Decimal(str(trend_pct)) if trend_pct is not None else None,
        filters_applied=None,
        evaluation_ms=120,
        created_at=NOW,
    )


def _make_user(role: str | None = None, email: str = TEST_USER_ID) -> CurrentUser:
    return CurrentUser(
        user_id=email, tenant_id=TEST_TENANT, email=email, role=role,
    )


# ---------------------------------------------------------------------------
# list_kpi_snapshots tests
# ---------------------------------------------------------------------------


class TestListKpiSnapshots:
    @pytest.fixture(autouse=True)
    def setup_auth(self):
        user = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: user
        yield
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_list_snapshots_returns_results(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)

        snap1 = _snapshot(kpi_id=kpi.id, hours_ago=0)
        snap2 = _snapshot(kpi_id=kpi.id, hours_ago=1)
        # First execute: persona resolution (must return empty — no personas assigned).
        # Second execute: snapshot query (returns the actual snapshots).
        db.execute = AsyncMock(side_effect=[
            _ScalarResult([]),            # resolve_effective_persona → no assigned personas
            _ScalarResult([snap1, snap2]),  # list snapshots query
        ])

        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{kpi.id}/snapshots")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["kpi_id"] == str(kpi.id)
        assert data[0]["value"] == 42.5
        assert data[0]["target"] == 50.0
        assert data[0]["status"] == 1
        assert data[0]["status_label"] == "On Track"
        assert data[0]["trend_pct"] == 5.2
        assert data[0]["evaluation_ms"] == 120

    @pytest.mark.asyncio
    async def test_list_snapshots_empty(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)
        db.execute = AsyncMock(return_value=_ScalarResult([]))

        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{kpi.id}/snapshots")

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_snapshots_kpi_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        fake_id = uuid.uuid4()
        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{fake_id}/snapshots")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_snapshots_wrong_model(self, client):
        db = make_mock_db()
        # KPI belongs to a different model
        kpi = _kpi()
        kpi.model_id = uuid.uuid4()
        db.get = AsyncMock(return_value=kpi)

        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{kpi.id}/snapshots")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_snapshots_with_null_values(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)

        snap = _snapshot(
            kpi_id=kpi.id, value=None, target=None,
            status=None, status_label=None, trend_pct=None,
        )
        # First execute: persona resolution (empty — no assigned personas).
        # Second execute: snapshot query.
        db.execute = AsyncMock(side_effect=[
            _ScalarResult([]),    # resolve_effective_persona → no assigned personas
            _ScalarResult([snap]),  # list snapshots query
        ])

        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{kpi.id}/snapshots")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["value"] is None
        assert data[0]["target"] is None
        assert data[0]["trend_pct"] is None


# ---------------------------------------------------------------------------
# get_latest_kpi_snapshot tests
# ---------------------------------------------------------------------------


class TestGetLatestKpiSnapshot:
    @pytest.fixture(autouse=True)
    def setup_auth(self):
        user = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: user
        yield
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_latest_snapshot_exists(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)

        snap = _snapshot(kpi_id=kpi.id)
        # First execute: persona resolution (empty — no assigned personas).
        # Second execute: latest snapshot query.
        db.execute = AsyncMock(side_effect=[
            _ScalarResult([]),    # resolve_effective_persona → no assigned personas
            _ScalarResult([snap]),  # get latest snapshot query
        ])

        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{kpi.id}/snapshots/latest")

        assert resp.status_code == 200
        data = resp.json()
        assert data["kpi_id"] == str(kpi.id)
        assert data["value"] == 42.5

    @pytest.mark.asyncio
    async def test_latest_snapshot_none(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)
        db.execute = AsyncMock(return_value=_ScalarResult([]))

        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{kpi.id}/snapshots/latest")

        assert resp.status_code == 200
        assert resp.json() is None

    @pytest.mark.asyncio
    async def test_latest_snapshot_kpi_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        fake_id = uuid.uuid4()
        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/{fake_id}/snapshots/latest")

        assert resp.status_code == 404
