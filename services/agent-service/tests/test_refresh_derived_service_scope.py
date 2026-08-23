"""Bug-6814 (receiving half) — deploy fan-out reaches the derived-context
refresh endpoints with a validly-scoped internal service token.

Business outcome under test: after a model publish the model-service posts to
``/agent/refresh-derived`` and ``/agent/model-context/{model_id}/refresh-derived``
using a short-lived ``model-service-deploy`` service token carrying the
``agent.refresh-derived`` scope. That token's synthetic subject matches no
``UserAccessBinding``, so the prior human-modeller gate 403'd it in any
populated tenant and the deploy-triggered refresh silently failed end-to-end.

The fix admits a validly-scoped service principal (strictly gated on the typed
``CurrentServiceUser`` carrying ``SCOPE_AGENT_REFRESH``) and leaves the human
authorization path unchanged:

* scoped service token  -> succeeds in a POPULATED tenant (the failing case);
* human non-modeller    -> still 403'd;
* service token WITHOUT the agent-refresh scope -> still rejected;
* a service principal role alone (no scope) never bypasses.

These assert known authorization decisions, not merely a 2xx.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.auth.service_principal import SCOPE_AGENT_REFRESH
from src.api.agent_config import _authorize_refresh_derived
from src.auth.middleware import CurrentServiceUser, CurrentUser

TEST_TENANT = "populated-tenant"
TEST_PROJECT_ID = uuid.uuid4()


def _user(role: str) -> CurrentUser:
    return CurrentUser(
        user_id="probe@example.com",
        tenant_id="__system__" if role == "system_admin" else TEST_TENANT,
        email="probe@example.com",
        role=role,
    )


def _service(scopes: list[str], role: str = "tenant_admin") -> CurrentServiceUser:
    return CurrentServiceUser(
        principal="model-service-deploy",
        tenant_id=TEST_TENANT,
        role=role,
        scopes=scopes,
    )


def _db_populated_no_match() -> AsyncMock:
    """A POPULATED tenant: the caller has no matching binding, but at least
    one UserAccessBinding row exists (so the bootstrap-open path does NOT
    trigger). This is the exact condition under which the pre-fix service
    token was 403'd."""
    db = AsyncMock()

    match_result = MagicMock()
    match_result.scalar_one_or_none.return_value = None  # no binding for caller
    any_result = MagicMock()
    any_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        role="modeler"
    )  # tenant has bindings -> populated
    db.execute = AsyncMock(side_effect=[match_result, any_result])
    return db


def _gen(db):
    async def _g(*a, **kw):
        yield db
    return _g


def _explode_gen():
    async def _g(*a, **kw):
        raise AssertionError(
            "scoped service principal must NOT hit the bindings table"
        )
        yield  # pragma: no cover
    return _g


# ---------------------------------------------------------------------------
# Scoped service principal: succeeds WITHOUT a human binding (the fix)
# ---------------------------------------------------------------------------


class TestScopedServicePrincipalBypass:
    @pytest.mark.asyncio
    async def test_scoped_service_token_authorized_in_populated_tenant(self):
        """The failing case today: a populated tenant where the service
        principal matches no binding. The scope alone must authorize it, and
        it must not even query the bindings table."""
        with patch("src.api.agent_config.get_tenant_db", _explode_gen()):
            # Must not raise.
            await _authorize_refresh_derived(
                TEST_PROJECT_ID, _service([SCOPE_AGENT_REFRESH])
            )

    @pytest.mark.asyncio
    async def test_scoped_service_token_ignores_role_label(self):
        # Authorization is scope-based, not role-based: a low service role
        # with the scope still passes.
        with patch("src.api.agent_config.get_tenant_db", _explode_gen()):
            await _authorize_refresh_derived(
                TEST_PROJECT_ID,
                _service([SCOPE_AGENT_REFRESH], role="member"),
            )


# ---------------------------------------------------------------------------
# Under-scoped / unscoped service token: still rejected
# ---------------------------------------------------------------------------


class TestUnderScopedServiceRejected:
    @pytest.mark.asyncio
    async def test_service_token_without_agent_refresh_scope_is_403(self):
        # A service token carrying some OTHER scope must not reach refresh.
        with patch("src.api.agent_config.get_tenant_db", _explode_gen()):
            with pytest.raises(HTTPException) as exc:
                await _authorize_refresh_derived(
                    TEST_PROJECT_ID,
                    _service(["query-router.cache-evict"]),
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_service_token_with_no_scopes_is_403(self):
        with patch("src.api.agent_config.get_tenant_db", _explode_gen()):
            with pytest.raises(HTTPException) as exc:
                await _authorize_refresh_derived(
                    TEST_PROJECT_ID, _service([])
                )
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Human path unchanged: non-modeller still 403'd, modeller still passes
# ---------------------------------------------------------------------------


class TestHumanPathUnchanged:
    @pytest.mark.asyncio
    async def test_human_non_modeller_in_populated_tenant_is_403(self):
        """A plain member with no matching binding in a POPULATED tenant is
        still rejected — the service bypass opens no human path."""
        db = _db_populated_no_match()
        with patch("src.api.agent_config.get_tenant_db", _gen(db)):
            with pytest.raises(HTTPException) as exc:
                await _authorize_refresh_derived(
                    TEST_PROJECT_ID, _user("member")
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_human_bound_modeller_passes(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = types.SimpleNamespace(
            role="modeler"
        )
        db.execute = AsyncMock(return_value=result)
        with patch("src.api.agent_config.get_tenant_db", _gen(db)):
            await _authorize_refresh_derived(
                TEST_PROJECT_ID, _user("member")
            )  # must not raise

    @pytest.mark.asyncio
    async def test_human_tenant_admin_passes_by_role(self):
        async def _explode(*a, **kw):
            raise AssertionError("tenant_admin must not hit the bindings table")
            yield  # pragma: no cover
        with patch("src.api.agent_config.get_tenant_db", _explode):
            await _authorize_refresh_derived(
                TEST_PROJECT_ID, _user("tenant_admin")
            )
