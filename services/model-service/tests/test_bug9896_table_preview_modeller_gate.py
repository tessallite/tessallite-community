"""Bug-9896 — the raw source-table preview is a MODELLING surface.

Audit row A38 / persona-layering decision 4.4c. ``preview_table`` builds a raw
``SELECT * FROM <physical table>`` and runs it through the query-router
``/introspect`` route, which applies NO persona, NO CLS and NO RLS. It was
gated at ``require_role("viewer")``, so any project viewer could read the
unfiltered physical source. The decision is that the preview stays raw and its
gate rises to modeller-or-above; the persona-filtered path for data is the
model query, not the preview.

These tests drive the REAL route so the whole dependency chain runs
(token -> forbid_embed_user -> require_role -> handler):

  * a project ``viewer`` binding is REJECTED with 403,
  * a project ``modeler`` binding is ADMITTED and gets rows (200),
  * an admin binding is ADMITTED (inherits),
  * an embed token is REJECTED (``forbid_embed_user`` stays),
  * the route's declared minimum role is the shared modeller constant, so a
    silent revert to "viewer" fails here too.

Run from tessallite/services/model-service/:
    pytest tests/test_bug9896_table_preview_modeller_gate.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.auth.roles import PROJECT_MODELER_ROLE
from src.api.table_preview import PREVIEW_MIN_ROLE
from src.auth.middleware import CurrentEmbedUser, CurrentUser, get_current_user
from src.main import app

pytestmark = pytest.mark.unit

_PROJECT_ID = uuid.uuid4()
_MODEL_ID = uuid.uuid4()
_TABLE_ID = uuid.uuid4()
_TENANT = "test-tenant"
_USER_ID = "u@acme.test"

_URL = (
    f"/api/v1/projects/{_PROJECT_ID}/models/{_MODEL_ID}"
    f"/tables/{_TABLE_ID}/preview"
)


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=_USER_ID, tenant_id=_TENANT, email=_USER_ID, role="member",
    )


def _embed() -> CurrentEmbedUser:
    return CurrentEmbedUser(
        user_id="embed@acme.test", tenant_id=_TENANT, email="embed@acme.test",
    )


def _binding(role: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        user_identity=_USER_ID,
        project_id=_PROJECT_ID,
        model_id=None,
        role=role,
    )


def _rbac_db(binding_role: str | None) -> MagicMock:
    """Mock tenant DB answering require_role's binding lookups."""
    bindings = [_binding(binding_role)] if binding_role else []
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        is_existence_probe = "user_identity" not in text
        result.scalar_one_or_none.return_value = bindings[0] if bindings else None
        result.scalars.return_value.all.return_value = bindings
        if is_existence_probe:
            result.first.return_value = (bindings[0],) if bindings else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _handler_db() -> MagicMock:
    """Mock tenant DB for the handler body: resolves the ModelTable row."""
    db = MagicMock()
    table = types.SimpleNamespace(
        id=_TABLE_ID, model_id=_MODEL_ID, physical_name="public.orders",
        source_id=uuid.uuid4(),
    )
    db.get = AsyncMock(return_value=table)
    return db


async def _call(user, *, binding_role: str | None) -> httpx.Response:
    """GET the real preview route with *user* holding *binding_role*.

    Everything BELOW the role gate is stubbed so an admitted caller reaches a
    clean 200 — a 403 is therefore unambiguously the gate, not a DB error.
    """
    rbac_db = _rbac_db(binding_role)
    handler_db = _handler_db()

    async def _rbac_db_gen(*a, **kw):
        yield rbac_db

    async def _handler_db_gen(*a, **kw):
        yield handler_db

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with (
            patch("src.auth.rbac.get_tenant_db", _rbac_db_gen),
            patch("src.api.table_preview.get_tenant_db", _handler_db_gen),
            patch(
                "src.api.table_preview.ensure_model_in_project",
                AsyncMock(return_value=None),
            ),
            patch(
                "src.api.table_preview._resolve_table_context",
                AsyncMock(
                    return_value=("postgresql", MagicMock(), "public.orders", None)
                ),
            ),
            patch(
                "src.api.table_preview._introspect_via_router",
                AsyncMock(return_value=([{"id": 1}], ["id"])),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                return await ac.get(
                    _URL, headers={"Authorization": "Bearer test-token"}
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_bug9896_preview_min_role_is_modeller():
    """The route's declared gate is the shared modeller constant.

    Fails on the pre-fix code, where the dependency was require_role("viewer").
    """
    assert PREVIEW_MIN_ROLE == PROJECT_MODELER_ROLE


@pytest.mark.asyncio
async def test_bug9896_viewer_binding_is_denied():
    """A project viewer must NOT be able to read the raw physical table."""
    resp = await _call(_user(), binding_role="viewer")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9896_modeller_binding_is_admitted():
    resp = await _call(_user(), binding_role="modeler")
    assert resp.status_code == 200
    assert resp.json()["rows"] == [{"id": 1}]


@pytest.mark.asyncio
async def test_bug9896_admin_binding_is_admitted():
    resp = await _call(_user(), binding_role="admin")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bug9896_unbound_user_is_denied():
    resp = await _call(_user(), binding_role=None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9896_embed_token_is_forbidden():
    """forbid_embed_user stays: an embed token never reaches the raw preview."""
    resp = await _call(_embed(), binding_role="modeler")
    assert resp.status_code == 403
