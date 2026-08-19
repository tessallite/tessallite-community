"""Bug-8613: service principals must be refused on capability-only routes.

Capability-gated routes (``require_capability(...)`` without a service-scope
check) previously admitted ANY service token because
``ensure_project_model_access`` unconditionally returned for
``CurrentServiceUser``. After the Bug-8613 fix, the shared primitive refuses
unverified service principals by default (``service_scope_verified=False``).

This module tests:
1. The shared primitive refuses service principals by default.
2. The shared primitive admits service principals when ``service_scope_verified=True``.
3. ``load_authorized_model`` forwards the parameter correctly.
4. Each previously-affected route refuses a service principal.
5. Each VERIFIED-NOT-AFFECTED route still works for service principals
   (regression guard — the ``service_scope_verified=True`` opt-in is wired).
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from shared.auth.middleware import CurrentEmbedUser, CurrentServiceUser, CurrentUser
from shared.auth.project_access import ensure_project_model_access, load_authorized_model
from shared.auth.service_principal import (
    SCOPE_DATA_QUALITY,
    SCOPE_POCKET_REFRESH,
    create_service_access_token,
)
from shared.db.models import Model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _service_user(
    principal: str = "optimizer-stats",
    scopes: list[str] | None = None,
) -> CurrentServiceUser:
    return CurrentServiceUser(
        principal=principal,
        tenant_id="tenant-1",
        role="system_admin",
        scopes=scopes or ["optimizer.stats-refresh"],
    )


def _human_user() -> CurrentUser:
    return CurrentUser(
        user_id="user@example.com",
        tenant_id="tenant-1",
        email="user@example.com",
        role="member",
    )


class _MinimalDB:
    """DB double that returns a model for ``db.get`` and empty result sets for
    all binding lookups (sufficient for testing the service-principal branch,
    which never reaches the binding path)."""

    def __init__(self, *, model=None):
        self.model = model

    async def get(self, cls, key):
        if cls is Model and self.model is not None and self.model.id == key:
            return self.model
        return None

    async def execute(self, stmt):
        return types.SimpleNamespace(
            scalar_one_or_none=lambda: None,
            first=lambda: None,
        )


# ---------------------------------------------------------------------------
# 1. Shared primitive — ensure_project_model_access
# ---------------------------------------------------------------------------

class TestEnsureProjectModelAccessServicePrincipal:
    """Verify the default-refuse / opt-in-admit axis."""

    @pytest.mark.asyncio
    async def test_refuses_by_default(self):
        db = _MinimalDB()
        with pytest.raises(HTTPException) as exc:
            await ensure_project_model_access(
                db,
                _service_user(),
                project_id=uuid.uuid4(),
                model_id=uuid.uuid4(),
                min_role="viewer",
            )
        assert exc.value.status_code == 403
        assert "scope not verified" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_refuses_regardless_of_scopes_when_unverified(self):
        """Even a service token carrying a real scope is refused when
        service_scope_verified is not set — the primitive has no way to
        know whether the route validated it."""
        db = _MinimalDB()
        with pytest.raises(HTTPException) as exc:
            await ensure_project_model_access(
                db,
                _service_user(scopes=["query-router.pocket-refresh"]),
                project_id=uuid.uuid4(),
                min_role="viewer",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admits_when_service_scope_verified(self):
        db = _MinimalDB()
        await ensure_project_model_access(
            db,
            _service_user(),
            project_id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            min_role="viewer",
            service_scope_verified=True,
        )

    @pytest.mark.asyncio
    async def test_admin_bypass_still_works_for_service_user_with_admin_role(self):
        """A human tenant_admin still bypasses regardless of service_scope_verified."""
        db = _MinimalDB()
        admin = CurrentUser(
            user_id="admin@example.com",
            tenant_id="tenant-1",
            email="admin@example.com",
            role="tenant_admin",
        )
        await ensure_project_model_access(
            db,
            admin,
            project_id=uuid.uuid4(),
            min_role="modeler",
        )


# ---------------------------------------------------------------------------
# 2. load_authorized_model — parameter forwarding
# ---------------------------------------------------------------------------

class TestLoadAuthorizedModelServicePrincipal:

    @pytest.mark.asyncio
    async def test_refuses_service_user_by_default(self):
        model_id = uuid.uuid4()
        model = types.SimpleNamespace(id=model_id, project_id=uuid.uuid4())
        db = _MinimalDB(model=model)

        with pytest.raises(HTTPException) as exc:
            await load_authorized_model(
                db,
                _service_user(),
                model_id=model_id,
                min_role="viewer",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admits_service_user_when_scope_verified(self):
        model_id = uuid.uuid4()
        model = types.SimpleNamespace(id=model_id, project_id=uuid.uuid4())
        db = _MinimalDB(model=model)

        result = await load_authorized_model(
            db,
            _service_user(),
            model_id=model_id,
            min_role="viewer",
            service_scope_verified=True,
        )
        assert result.id == model_id


# ---------------------------------------------------------------------------
# 3. Route-level regression guards: AFFECTED routes refuse service principals
# ---------------------------------------------------------------------------
# These tests drive the REAL FastAPI routes with a service token to confirm
# they are closed by the shared-primitive fix. Each route is stubbed below
# the auth layer so the test is deterministic and fast.

def _mint_service_token(
    principal: str = "pocket-refresh",
    scopes: str | list[str] = SCOPE_POCKET_REFRESH,
    tenant_id: str = "tenant-1",
) -> str:
    """Mint a production-valid JWT that decodes to CurrentServiceUser."""
    return create_service_access_token(
        principal=principal,
        tenant_id=tenant_id,
        role="system_admin",
        scopes=[scopes] if isinstance(scopes, str) else scopes,
    )


def _mint_scoped_service_token(scope: str) -> str:
    """Mint a service JWT carrying a specific scope."""
    principals = {
        SCOPE_POCKET_REFRESH: "pocket-refresh",
        SCOPE_DATA_QUALITY: "data-quality-validator",
    }
    return _mint_service_token(principal=principals[scope], scopes=scope)


def test_service_token_fixture_reaches_the_typed_principal_branch():
    """A malformed JWT would make every route assertion below vacuous."""
    from shared.auth.jwt import decode_access_token
    from shared.auth.middleware import _build_user_from_payload

    user = _build_user_from_payload(
        decode_access_token(_mint_scoped_service_token(SCOPE_POCKET_REFRESH))
    )
    assert isinstance(user, CurrentServiceUser)
    assert user.service_scopes == [SCOPE_POCKET_REFRESH]


def _route_db_gen(db):
    async def _gen(*args, **kwargs):
        yield db
    return _gen


@pytest.fixture
async def client():
    from src.main import app
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


class _RouteDB(_MinimalDB):
    """Extended DB double for route-level tests that need model + binding
    lookups to reach the auth check."""

    def __init__(self, *, model=None, bindings=None):
        super().__init__(model=model)
        self._bindings = bindings or []

    async def execute(self, stmt):
        text = str(stmt)
        if "user_access_bindings" in text:
            return types.SimpleNamespace(
                scalar_one_or_none=lambda: None,
                first=lambda: self._bindings[0] if self._bindings else None,
            )
        # Measure lookup (drill routes)
        if "measures" in text.lower():
            if self.model is not None:
                return types.SimpleNamespace(
                    scalar_one_or_none=lambda: self.model.id,
                    scalars=lambda: types.SimpleNamespace(
                        all=lambda: [],
                    ),
                )
            return types.SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: types.SimpleNamespace(all=lambda: []),
            )
        return types.SimpleNamespace(
            scalar_one_or_none=lambda: None,
            first=lambda: None,
        )


# Affected routes use bare require_capability(...), so a valid service token
# must reach the shared primitive and be refused by its default policy.
@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["drill-options", "drill-through"])
async def test_drill_routes_refuse_valid_unverified_service_token(
    client, monkeypatch, suffix,
):
    from unittest.mock import AsyncMock

    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=uuid.uuid4())
    db = _RouteDB(model=model)
    monkeypatch.setattr("src.api.drill_routes.get_tenant_db", _route_db_gen(db))
    monkeypatch.setattr(
        "src.api.drill_routes._enforce_measure_model_scope",
        AsyncMock(return_value=None),
    )

    response = await client.post(
        f"/api/v1/measures/{uuid.uuid4()}/{suffix}",
        json={},
        headers={
            "Authorization": "Bearer "
            + _mint_scoped_service_token(SCOPE_DATA_QUALITY)
        },
    )
    assert response.status_code == 403
    assert "scope not verified" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. VERIFIED-NOT-AFFECTED routes still work for scoped service principals
# ---------------------------------------------------------------------------

class TestVerifiedNotAffectedRoutesStillWork:
    """Regression: routes that use require_capability_or_service_scope and pass
    service_scope_verified=True must STILL admit service tokens with the right
    scope. These tests confirm the opt-in wiring is correct."""

    @pytest.mark.asyncio
    async def test_execute_admits_scoped_service_token(self, client, monkeypatch):
        """POST /execute uses require_capability_or_service_scope('query',
        SCOPE_POCKET_REFRESH) and passes service_scope_verified=True."""
        from unittest.mock import AsyncMock, MagicMock
        model_id = uuid.uuid4()
        project_id = uuid.uuid4()
        model = types.SimpleNamespace(
            id=model_id, project_id=project_id,
            deployed_version_id=uuid.uuid4(),
            slug="test",
        )
        db = _RouteDB(model=model)

        monkeypatch.setattr("src.api.routes.get_tenant_db", _route_db_gen(db))
        load_model = AsyncMock(return_value=model)
        monkeypatch.setattr(
            "src.api.routes.load_authorized_model",
            load_model,
        )
        monkeypatch.setattr(
            "src.api.routes.resolve_execution_persona",
            AsyncMock(return_value=None),
        )
        handle_execute = AsyncMock(return_value=MagicMock(
            model_copy=MagicMock(return_value=MagicMock()),
        ))
        monkeypatch.setattr(
            "src.api.routes._handle_execute",
            handle_execute,
        )
        # (The embed disclosure withhold that used to wrap this return was
        # removed 2026-08-11 — decision option C — so there is no longer a
        # response transform to stub out here.)
        # Stub _log_preexec_failure in case it's needed
        monkeypatch.setattr(
            "src.api.routes._log_preexec_failure",
            AsyncMock(return_value=None),
        )

        token = _mint_scoped_service_token(SCOPE_POCKET_REFRESH)
        resp = await client.post(
            "/api/v1/execute",
            json={
                "model_id": str(model_id),
                "raw_query": "SELECT 1",
                "protocol": "jdbc",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code not in {401, 403}
        handle_execute.assert_awaited_once()
        assert load_model.await_args.kwargs["service_scope_verified"] is True

    @pytest.mark.asyncio
    async def test_introspect_admits_scoped_service_token(self, client, monkeypatch):
        """POST /introspect uses require_capability_or_service_scope('explore',
        SCOPE_DATA_QUALITY) and passes service_scope_verified=True."""
        from unittest.mock import AsyncMock, MagicMock

        model_id = uuid.uuid4()
        project_id = uuid.uuid4()
        model = types.SimpleNamespace(id=model_id, project_id=project_id)
        db = _RouteDB(model=model)

        monkeypatch.setattr("src.api.introspect.get_tenant_db", _route_db_gen(db))
        load_model = AsyncMock(return_value=model)
        monkeypatch.setattr(
            "src.api.introspect.load_authorized_model",
            load_model,
        )
        monkeypatch.setattr(
            "src.api.introspect._resolve_model_connection",
            AsyncMock(return_value=(MagicMock(), None)),
        )
        monkeypatch.setattr(
            "src.api.introspect.execute_source_sql",
            AsyncMock(return_value=([{"x": 1}], ["x"])),
        )
        monkeypatch.setattr(
            "src.api.introspect._log_introspect",
            AsyncMock(return_value=None),
        )

        token = _mint_scoped_service_token(SCOPE_DATA_QUALITY)
        resp = await client.post(
            "/api/v1/introspect",
            json={"model_id": str(model_id), "raw_sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert load_model.await_args.kwargs["service_scope_verified"] is True
