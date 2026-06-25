from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from shared.auth.project_access import ensure_project_model_access, load_authorized_model
from shared.db.models import Model, UserAccessBinding


def _user(role: str = "member") -> CurrentUser:
    return CurrentUser(
        user_id="user@example.com",
        tenant_id="tenant-1",
        email="user@example.com",
        role=role,
    )


def _binding(project_id, model_id=None, role="viewer"):
    return types.SimpleNamespace(
        user_identity="user@example.com",
        project_id=project_id,
        model_id=model_id,
        role=role,
    )


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def first(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, *, model=None, bindings=None):
        self.model = model
        self.bindings = bindings or []

    async def get(self, cls, key):
        if cls is Model and self.model is not None and self.model.id == key:
            return self.model
        return None

    async def execute(self, stmt):
        text = str(stmt)
        params = stmt.compile().params
        user_identity = next(
            (v for k, v in params.items() if k.startswith("user_identity")),
            None,
        )
        project_id = next(
            (v for k, v in params.items() if k.startswith("project_id")),
            None,
        )
        model_id = next((v for k, v in params.items() if k.startswith("model_id")), None)
        matching = [
            b for b in self.bindings
            if (user_identity is None or b.user_identity == user_identity)
            and (project_id is None or b.project_id == project_id)
        ]
        if "user_access_bindings.role" not in text:
            return _Result([
                b for b in self.bindings
                if project_id is None or b.project_id == project_id
            ][:1])
        if "model_id IS NULL" in text:
            return _Result([b for b in matching if b.model_id is None])
        if "model_id =" in text:
            return _Result([
                b for b in matching
                if b.model_id is not None and (model_id is None or b.model_id == model_id)
            ])
        return _Result([])


@pytest.mark.asyncio
async def test_project_binding_allows_model_viewer_access():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB(bindings=[_binding(project_id, role="viewer")])

    await ensure_project_model_access(
        db, _user(), project_id=project_id, model_id=model_id, min_role="viewer"
    )


@pytest.mark.asyncio
async def test_missing_binding_denies_when_project_has_bindings():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB(bindings=[_binding(project_id, role="viewer")])

    with pytest.raises(HTTPException) as exc:
        await ensure_project_model_access(
            db,
            CurrentUser(
                user_id="other@example.com",
                tenant_id="tenant-1",
                email="other@example.com",
                role="member",
            ),
            project_id=project_id,
            model_id=model_id,
            min_role="viewer",
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_load_authorized_model_rejects_url_project_mismatch():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=other_project_id)
    db = _DB(model=model, bindings=[_binding(other_project_id, role="viewer")])

    with pytest.raises(HTTPException) as exc:
        await load_authorized_model(
            db,
            _user(),
            model_id=model_id,
            project_id=project_id,
            min_role="viewer",
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_modeler_required_rejects_viewer_binding():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB(bindings=[_binding(project_id, role="viewer")])

    with pytest.raises(HTTPException) as exc:
        await ensure_project_model_access(
            db, _user(), project_id=project_id, model_id=model_id, min_role="modeler"
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_embed_project_scope_rejects_other_project():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB()
    user = CurrentEmbedUser(
        user_id="embed@example.com",
        tenant_id="tenant-1",
        email="embed@example.com",
        project_ids=[str(other_project_id)],
        model_ids=None,
    )

    with pytest.raises(HTTPException) as exc:
        await ensure_project_model_access(
            db,
            user,
            project_id=project_id,
            model_id=model_id,
            min_role="viewer",
        )

    assert exc.value.status_code == 403
    assert "Project not in embed token scope" in exc.value.detail


@pytest.mark.asyncio
async def test_embed_project_scope_allows_matching_project_without_model_scope():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB()
    user = CurrentEmbedUser(
        user_id="embed@example.com",
        tenant_id="tenant-1",
        email="embed@example.com",
        project_ids=[str(project_id)],
        model_ids=None,
    )

    await ensure_project_model_access(
        db,
        user,
        project_id=project_id,
        model_id=model_id,
        min_role="viewer",
    )


# ---------------------------------------------------------------------------
# Bug-5326: ROUTE-level denial tests against the real FastAPI introspect route.
#
# The helper tests above exercise ensure_project_model_access / load_authorized_model
# directly. These drive the ACTUAL /api/v1/introspect endpoint end-to-end so the
# full dependency order (token decode -> require_capability -> load_authorized_model
# -> ensure_project_model_access) is exercised, asserting a normal user is DENIED on
# a project/model they are not bound to and ALLOWED where bound. Only the source
# execution layer BELOW the auth check is stubbed.
# ---------------------------------------------------------------------------


def _mint_member_token(user_identity: str = "user@example.com") -> str:
    from datetime import datetime, timedelta, timezone
    from jose import jwt
    from shared.config.settings import get_settings

    settings = get_settings()
    payload = {
        "sub": user_identity,
        "tenant_id": "tenant-1",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "role": "member",
    }
    return jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


@pytest.fixture
async def route_client():
    from src.main import app
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


def _route_db_gen(db):
    async def _gen(*args, **kwargs):
        yield db

    return _gen


@pytest.mark.asyncio
async def test_introspect_route_denies_unbound_normal_user(route_client, monkeypatch):
    """A normal member whose identity has NO binding on a project that DOES have
    other bindings is denied 403 at the real route (before any source access)."""
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    # Project has a binding for a DIFFERENT user, so the unbound caller is denied
    # rather than allowed-through (the empty-project bypass branch).
    db = _DB(
        model=model,
        bindings=[
            types.SimpleNamespace(
                user_identity="someone-else@example.com",
                project_id=project_id,
                model_id=None,
                role="viewer",
            )
        ],
    )

    # Stub the source layer so a (wrongly) allowed request would still return
    # 200 — making a 403 unambiguously the auth check firing, not a DB error.
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(
        "src.api.introspect._resolve_model_connection",
        AsyncMock(return_value=(MagicMock(), None)),
    )
    monkeypatch.setattr(
        "src.api.introspect.execute_source_sql",
        AsyncMock(return_value=([{"x": 1}], ["x"])),
    )
    monkeypatch.setattr(
        "src.api.introspect._log_introspect", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.api.introspect.get_tenant_db", _route_db_gen(db)
    )

    resp = await route_client.post(
        "/api/v1/introspect",
        json={"model_id": str(model_id), "raw_sql": "SELECT 1"},
        headers={"Authorization": f"Bearer {_mint_member_token()}"},
    )

    assert resp.status_code == 403


class _IntrospectResolverDB:
    """Fake tenant DB that serves load_authorized_model's binding lookup AND the
    REAL _resolve_model_connection (DataSource select + ProjectConnection get) so
    the read-time cross-project connection guard is exercised at the route."""

    def __init__(self, *, model, binding_user, data_source, connection):
        self.model = model
        self.binding_user = binding_user
        self.data_source = data_source
        self.connection = connection

    async def get(self, cls, key):
        from shared.db.models import ProjectConnection
        if cls is Model and self.model is not None and self.model.id == key:
            return self.model
        if cls is ProjectConnection:
            return self.connection
        return None

    async def execute(self, stmt):
        text = str(stmt)
        # _resolve_model_connection: SELECT DataSource WHERE model_id = ... LIMIT 1
        if "data_sources" in text:
            return _Result([self.data_source])
        # Binding lookups for ensure_project_model_access: allow our caller.
        params = stmt.compile().params
        user_identity = next(
            (v for k, v in params.items() if k.startswith("user_identity")), None
        )
        if "user_access_bindings.role" not in text:
            # any_binding probe — project has bindings.
            return _Result([self.binding_user])
        if "model_id IS NULL" in text:
            if user_identity is None or user_identity == self.binding_user.user_identity:
                return _Result([self.binding_user])
            return _Result([])
        if "model_id =" in text:
            return _Result([])
        return _Result([])


@pytest.mark.asyncio
async def test_introspect_route_rejects_cross_project_source_connection(
    route_client, monkeypatch
):
    """Bug-5325 at the REAL route: _resolve_model_connection is NOT stubbed. A
    source whose connection belongs to a DIFFERENT project than the model is
    rejected (422) before execute_source_sql, even for an authorized caller."""
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    binding_user = types.SimpleNamespace(
        user_identity="user@example.com",
        project_id=project_id,
        model_id=None,
        role="viewer",
    )
    data_source = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        project_connection_id=uuid.uuid4(),
    )
    # Connection belongs to ANOTHER project — the legacy/imported malformed row.
    connection = types.SimpleNamespace(
        id=data_source.project_connection_id,
        project_id=other_project_id,
        connection_type="postgresql",
    )
    db = _IntrospectResolverDB(
        model=model,
        binding_user=binding_user,
        data_source=data_source,
        connection=connection,
    )

    from unittest.mock import AsyncMock

    # Execution layer would 200 if (wrongly) reached — so a 422 is unambiguously
    # the cross-project guard, not a downstream error.
    monkeypatch.setattr(
        "src.api.introspect.execute_source_sql",
        AsyncMock(return_value=([{"x": 1}], ["x"])),
    )
    monkeypatch.setattr(
        "src.api.introspect._log_introspect", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("src.api.introspect.get_tenant_db", _route_db_gen(db))

    resp = await route_client.post(
        "/api/v1/introspect",
        json={"model_id": str(model_id), "raw_sql": "SELECT 1"},
        headers={"Authorization": f"Bearer {_mint_member_token()}"},
    )

    assert resp.status_code == 422
    assert "different project" in str(resp.json()["detail"])


@pytest.mark.asyncio
async def test_introspect_route_allows_bound_normal_user(route_client, monkeypatch):
    """A normal member WITH a viewer binding on the project reaches the source
    layer and gets a 200 from the real route."""
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    db = _DB(
        model=model,
        bindings=[
            types.SimpleNamespace(
                user_identity="user@example.com",
                project_id=project_id,
                model_id=None,
                role="viewer",
            )
        ],
    )

    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(
        "src.api.introspect._resolve_model_connection",
        AsyncMock(return_value=(MagicMock(), None)),
    )
    monkeypatch.setattr(
        "src.api.introspect.execute_source_sql",
        AsyncMock(return_value=([{"x": 1}], ["x"])),
    )
    monkeypatch.setattr(
        "src.api.introspect._log_introspect", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.api.introspect.get_tenant_db", _route_db_gen(db)
    )

    resp = await route_client.post(
        "/api/v1/introspect",
        json={"model_id": str(model_id), "raw_sql": "SELECT 1"},
        headers={"Authorization": f"Bearer {_mint_member_token()}"},
    )

    assert resp.status_code == 200
    assert resp.json()["rows"] == [{"x": 1}]


@pytest.mark.asyncio
async def test_introspect_route_denies_user_bound_only_to_other_project(
    route_client, monkeypatch
):
    """A normal member bound to a DIFFERENT project is denied 403 on a model
    whose own project has bindings (so the empty-project bypass does not apply).
    Exercises the binding lookup at the real route for the model's own project."""
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    # Model lives in project_id; that project HAS a binding (for another user),
    # so an unbound caller cannot fall through the empty-project bypass. The
    # caller's only binding is on other_project_id.
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    db = _DB(
        model=model,
        bindings=[
            types.SimpleNamespace(
                user_identity="owner@example.com",
                project_id=project_id,
                model_id=None,
                role="viewer",
            ),
            types.SimpleNamespace(
                user_identity="user@example.com",
                project_id=other_project_id,
                model_id=None,
                role="viewer",
            ),
        ],
    )

    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(
        "src.api.introspect._resolve_model_connection",
        AsyncMock(return_value=(MagicMock(), None)),
    )
    monkeypatch.setattr(
        "src.api.introspect.execute_source_sql",
        AsyncMock(return_value=([{"x": 1}], ["x"])),
    )
    monkeypatch.setattr(
        "src.api.introspect._log_introspect", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.api.introspect.get_tenant_db", _route_db_gen(db)
    )

    resp = await route_client.post(
        "/api/v1/introspect",
        json={"model_id": str(model_id), "raw_sql": "SELECT 1"},
        headers={"Authorization": f"Bearer {_mint_member_token()}"},
    )

    assert resp.status_code == 403
