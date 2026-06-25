"""Tests for onboarding flag endpoint."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


def _make_user(has_completed_onboarding: bool = False):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username="testuser",
        email=TEST_USER_ID,
        is_active=True,
        role="member",
        auth_source="local",
        has_completed_onboarding=has_completed_onboarding,
        created_at=NOW,
        updated_at=NOW,
    )


class TestCompleteOnboarding:
    @pytest.mark.anyio
    async def test_sets_flag_to_true(self, client):
        db = make_mock_db()
        user = _make_user(has_completed_onboarding=False)

        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db.execute = AsyncMock(return_value=result)

        async def _refresh(obj):
            pass

        db.refresh = AsyncMock(side_effect=_refresh)

        with patch("src.api.auth.get_tenant_db", async_gen_from(db)):
            resp = await client.post("/api/v1/auth/users/me/complete-onboarding")

        assert resp.status_code == 200
        assert user.has_completed_onboarding is True
        db.commit.assert_awaited_once()

    @pytest.mark.anyio
    async def test_returns_user_response(self, client):
        db = make_mock_db()
        user = _make_user(has_completed_onboarding=False)

        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db.execute = AsyncMock(return_value=result)
        db.refresh = AsyncMock()

        with patch("src.api.auth.get_tenant_db", async_gen_from(db)):
            resp = await client.post("/api/v1/auth/users/me/complete-onboarding")

        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == TEST_USER_ID
        assert data["has_completed_onboarding"] is True


class TestGetMeOnboarding:
    @pytest.mark.anyio
    async def test_returns_onboarding_false(self, client):
        db = make_mock_db()
        user = _make_user(has_completed_onboarding=False)

        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.auth.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/auth/users/me")

        assert resp.status_code == 200
        assert resp.json()["has_completed_onboarding"] is False

    @pytest.mark.anyio
    async def test_returns_onboarding_true(self, client):
        db = make_mock_db()
        user = _make_user(has_completed_onboarding=True)

        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.auth.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/auth/users/me")

        assert resp.status_code == 200
        assert resp.json()["has_completed_onboarding"] is True


class TestOnboardingSchema:
    def test_user_response_includes_onboarding(self):
        from shared.schemas.pydantic_models import UserResponse

        resp = UserResponse(
            id=uuid.uuid4(),
            username="test",
            email="test@example.com",
            is_active=True,
            role="member",
            auth_source="local",
            has_completed_onboarding=False,
            created_at=NOW,
        )
        assert resp.has_completed_onboarding is False

    def test_user_response_default_false(self):
        from shared.schemas.pydantic_models import UserResponse

        resp = UserResponse(
            id=uuid.uuid4(),
            username="test",
            email="test@example.com",
            is_active=True,
            role="member",
            created_at=NOW,
        )
        assert resp.has_completed_onboarding is False
