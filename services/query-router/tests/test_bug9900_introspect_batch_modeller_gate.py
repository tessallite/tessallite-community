"""Bug-9900 — POST /api/v1/introspect/batch is a MODELLING surface too.

Rule-4 wave 0b. Bug-9896 raised the single-statement ``/introspect`` route to
modeller-or-above and left the BATCH route at ``min_role="viewer"``. The batch
route runs the same kind of raw read-only SQL against the model's physical
source with NO persona, NO CLS and NO RLS, and it is directly callable, so the
viewer gate was the same hole one route along — reachable in product through
model-service ``check_calendar_coverage``.

Asserted at the REAL route so the full dependency order runs
(token -> require_capability_or_service_scope -> load_authorized_model ->
ensure_project_model_access):

  * a project ``viewer`` binding is REJECTED with 403,
  * a project ``modeler`` binding is ADMITTED (200 with rows),
  * an ``admin`` binding is ADMITTED (inherits),
  * an unbound caller is REJECTED,
  * an embed token is REJECTED (an above-viewer min_role refuses embed tokens).

Run from tessallite/services/query-router/:
    pytest tests/test_bug9900_introspect_batch_modeller_gate.py
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.auth.roles import PROJECT_MODELER_ROLE
from shared.db.models import Model


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def first(self):
        return self.rows[0] if self.rows else None


class _DB:
    """Tenant DB stub answering the model lookup and the binding lookups."""

    def __init__(self, *, model, bindings):
        self.model = model
        self.bindings = bindings

    async def get(self, cls, key):
        if cls is Model and self.model is not None and self.model.id == key:
            return self.model
        return None

    async def execute(self, stmt):
        params = stmt.compile().params
        user_identity = next(
            (v for k, v in params.items() if k.startswith("user_identity")),
            next((v for k, v in params.items() if k.startswith("lower")), None),
        )
        model_id = next(
            (v for k, v in params.items() if k.startswith("model_id")), None
        )
        matching = [
            b for b in self.bindings
            if (
                user_identity is None
                or str(b.user_identity).lower() == str(user_identity).lower()
            )
            and (b.model_id == model_id if model_id is not None else b.model_id is None)
        ]
        return _Result(matching)


def _mint_token(*, embed: bool = False) -> str:
    from jose import jwt
    from shared.config.settings import get_settings

    settings = get_settings()
    payload = {
        "sub": "user@example.com",
        "tenant_id": "tenant-1",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "role": "member",
    }
    if embed:
        # Embed audience + the "explore" capability, so the request clears the
        # capability check and is refused by the ROLE gate specifically.
        payload["aud"] = "embed"
        payload["capabilities"] = ["explore"]
    return jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


@pytest.fixture
async def route_client():
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


async def _post_batch(
    route_client, monkeypatch, *, binding_role: str | None, embed: bool = False,
) -> httpx.Response:
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    bindings = []
    if binding_role:
        bindings.append(
            types.SimpleNamespace(
                user_identity="user@example.com",
                project_id=project_id,
                model_id=None,
                role=binding_role,
            )
        )
    db = _DB(model=model, bindings=bindings)

    async def _db_gen(*a, **kw):
        yield db

    # Everything BELOW the authorization check succeeds, so a 403 is
    # unambiguously the gate rather than a downstream failure.
    monkeypatch.setattr(
        "src.api.introspect._resolve_model_connection",
        AsyncMock(return_value=(MagicMock(), None)),
    )
    monkeypatch.setattr(
        "src.api.introspect.execute_source_sql",
        AsyncMock(return_value=([{"lo": 1, "hi": 2}], ["lo", "hi"])),
    )
    monkeypatch.setattr(
        "src.api.introspect._log_introspect", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("src.api.introspect.get_tenant_db", _db_gen)

    return await route_client.post(
        "/api/v1/introspect/batch",
        json={
            "model_id": str(model_id),
            "queries": [
                {"key": "fact", "raw_sql": "SELECT MIN(d) AS lo, MAX(d) AS hi FROM t"},
            ],
        },
        headers={"Authorization": f"Bearer {_mint_token(embed=embed)}"},
    )


def test_bug9900_introspect_batch_declares_modeller_min_role():
    """The route source declares the shared modeller constant, not "viewer".

    Fails on the pre-fix code, where the batch route passed min_role="viewer".
    """
    import inspect

    from src.api import introspect

    src = inspect.getsource(introspect.introspect_batch)
    assert "min_role=PROJECT_MODELER_ROLE" in src
    assert 'min_role="viewer"' not in src
    assert introspect.PROJECT_MODELER_ROLE == PROJECT_MODELER_ROLE


@pytest.mark.asyncio
async def test_bug9900_batch_viewer_binding_is_denied(route_client, monkeypatch):
    resp = await _post_batch(route_client, monkeypatch, binding_role="viewer")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_batch_model_viewer_binding_is_denied(route_client, monkeypatch):
    resp = await _post_batch(route_client, monkeypatch, binding_role="model_viewer")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_batch_modeller_binding_is_admitted(route_client, monkeypatch):
    resp = await _post_batch(route_client, monkeypatch, binding_role="modeler")
    assert resp.status_code == 200
    assert resp.json()["results"][0]["rows"] == [{"lo": 1, "hi": 2}]


@pytest.mark.asyncio
async def test_bug9900_batch_admin_binding_is_admitted(route_client, monkeypatch):
    resp = await _post_batch(route_client, monkeypatch, binding_role="admin")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bug9900_batch_unbound_user_is_denied(route_client, monkeypatch):
    resp = await _post_batch(route_client, monkeypatch, binding_role=None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_batch_embed_token_is_denied(route_client, monkeypatch):
    """An embed token is refused: the raised min_role rejects embed callers."""
    resp = await _post_batch(
        route_client, monkeypatch, binding_role="modeler", embed=True
    )
    assert resp.status_code == 403
