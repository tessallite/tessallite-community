"""F-023-01 round 2 — blocked-original surfaces must FAIL CLOSED.

Business outcome under test: the reviewer demonstrated live that a plain
member-role user in a tenant with ZERO UserAccessBinding rows (the
default seed/demo posture) could read judge-blocked original answers
through ``/agent/calibration``, because ``_require_project_modeller``
bootstrap-opens when no bindings exist. The secret-bearing trace
surfaces (``/agent/calibration`` and ``/agent/log``) must never inherit
that bootstrap-open posture: a member with zero bindings gets 403.
Privileged-by-role users (tenant_admin / system_admin) and explicitly
bound admins/modellers still pass. The bootstrap-open behaviour of the
*configuration* gate itself (user decision D2) is untouched.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from src.api.agent_config import _require_blocked_original_access
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

TEST_TENANT = "gate-tenant"
TEST_PROJECT_ID = uuid.uuid4()


def _user(role: str) -> CurrentUser:
    return CurrentUser(
        user_id="probe@example.com",
        tenant_id=TEST_TENANT,
        email="probe@example.com",
        role=role,
    )


def _db_with_binding(binding) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = binding
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db


def _gen(db):
    async def _g(*a, **kw):
        yield db
    return _g


# ---------------------------------------------------------------------------
# Helper-level: the strict gate has NO zero-bindings bypass
# ---------------------------------------------------------------------------


class TestStrictGateHelper:
    @pytest.mark.asyncio
    async def test_member_with_zero_bindings_fails_closed(self):
        """The exact reviewer scenario: zero bindings in the tenant must
        NOT grant access — unlike the bootstrap-open modeller gate."""
        db = _db_with_binding(None)  # no binding rows at all
        with patch("src.api.agent_config.get_tenant_db", _gen(db)):
            with pytest.raises(HTTPException) as exc:
                await _require_blocked_original_access(
                    TEST_PROJECT_ID, _user("member")
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_explicit_modeller_binding_passes(self):
        binding = types.SimpleNamespace(role="modeller")
        db = _db_with_binding(binding)
        with patch("src.api.agent_config.get_tenant_db", _gen(db)):
            await _require_blocked_original_access(
                TEST_PROJECT_ID, _user("member")
            )  # must not raise

    @pytest.mark.asyncio
    async def test_tenant_admin_passes_by_role_without_bindings(self):
        # tenant_admin must not even need a DB lookup — privileged by role.
        async def _explode(*a, **kw):
            raise AssertionError("tenant_admin must not hit the bindings table")
            yield  # pragma: no cover
        with patch("src.api.agent_config.get_tenant_db", _explode):
            await _require_blocked_original_access(
                TEST_PROJECT_ID, _user("tenant_admin")
            )

    @pytest.mark.asyncio
    async def test_system_admin_passes_by_role(self):
        async def _explode(*a, **kw):
            raise AssertionError("system_admin must not hit the bindings table")
            yield  # pragma: no cover
        with patch("src.api.agent_config.get_tenant_db", _explode):
            await _require_blocked_original_access(
                TEST_PROJECT_ID, _user("system_admin")
            )


# ---------------------------------------------------------------------------
# Endpoint-level: /agent/calibration and /agent/log
# ---------------------------------------------------------------------------


async def _call(path: str, role: str, gate_db, endpoint_module: str, endpoint_db):
    app.dependency_overrides[get_current_user] = lambda: _user(role)
    try:
        with (
            patch("src.api.agent_config.get_tenant_db", _gen(gate_db)),
            patch(f"{endpoint_module}.get_tenant_db", _gen(endpoint_db)),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                return await client.get(path)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


class TestCalibrationEndpointGate:
    @pytest.mark.asyncio
    async def test_member_with_zero_bindings_gets_403(self):
        resp = await _call(
            f"/api/v1/projects/{TEST_PROJECT_ID}/agent/calibration?limit=20",
            role="member",
            gate_db=_db_with_binding(None),
            endpoint_module="src.api.kpis",
            endpoint_db=_db_with_binding(None),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_tenant_admin_still_reads_calibration(self):
        resp = await _call(
            f"/api/v1/projects/{TEST_PROJECT_ID}/agent/calibration",
            role="tenant_admin",
            gate_db=_db_with_binding(None),
            endpoint_module="src.api.kpis",
            endpoint_db=_db_with_binding(None),  # no turns -> []
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_bound_modeller_still_reads_calibration(self):
        resp = await _call(
            f"/api/v1/projects/{TEST_PROJECT_ID}/agent/calibration",
            role="member",
            gate_db=_db_with_binding(types.SimpleNamespace(role="modeller")),
            endpoint_module="src.api.kpis",
            endpoint_db=_db_with_binding(None),
        )
        assert resp.status_code == 200


class TestAgentLogEndpointGate:
    @pytest.mark.asyncio
    async def test_member_with_zero_bindings_gets_403(self):
        resp = await _call(
            f"/api/v1/projects/{TEST_PROJECT_ID}/agent/log",
            role="member",
            gate_db=_db_with_binding(None),
            endpoint_module="src.api.agent_log",
            endpoint_db=_db_with_binding(None),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_tenant_admin_passes_gate(self):
        # cfg row is None in the mock, so a passed gate surfaces the
        # endpoint's own 404 ("log screen not enabled") — NOT a 403.
        resp = await _call(
            f"/api/v1/projects/{TEST_PROJECT_ID}/agent/log",
            role="tenant_admin",
            gate_db=_db_with_binding(None),
            endpoint_db=_db_with_binding(None),
            endpoint_module="src.api.agent_log",
        )
        assert resp.status_code == 404
        assert "not enabled" in resp.json()["detail"]
