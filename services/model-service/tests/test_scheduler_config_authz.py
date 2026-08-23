"""Access governance for the per-model AI scheduler config endpoint (F-011-11).

The PUT /scheduler-config write and the optimizer reload it triggers must
require the SAME authority. The optimizer's AI surface (run, reload,
telemetry) is uniformly ``require_tenant_admin`` and carries no project-scoped
binding machinery, so the config write is aligned to ``tenant_admin`` too.
This closes the former mismatch where a ``modeler`` could write a config it
could not apply (the reload 403'd, deferring the cron change to the next
optimizer restart).

These tests assert the role gate at the endpoint boundary:
  * a tenant_admin write is admitted (reaches the handler),
  * a modeler / viewer write is rejected with 403,
  * an embed token is rejected.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.auth.middleware import CurrentEmbedUser, CurrentUser, get_current_user
from src.main import app

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, TEST_TENANT

pytestmark = pytest.mark.unit

PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/scheduler-config"
)


def _user(role: str) -> CurrentUser:
    return CurrentUser(
        user_id="u@acme.test", tenant_id=TEST_TENANT, email="u@acme.test",
        role=role,
    )


def _embed() -> CurrentEmbedUser:
    return CurrentEmbedUser(
        user_id="embed@acme.test", tenant_id=TEST_TENANT, email="embed@acme.test",
    )


async def _put(user: CurrentUser) -> httpx.Response:
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            # Patch the DB + reload notify so an ADMITTED request reaches a clean
            # handler exit (the role gate runs before any of this). Rejected
            # requests never touch these.
            with (
                patch("src.api.scheduler_config.get_tenant_db", _no_db),
                patch(
                    "src.api.scheduler_config._notify_optimizer_reload",
                    new_callable=AsyncMock,
                ),
            ):
                return await ac.put(PREFIX, json={"ai_enabled": False})
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _no_db(tenant_id):  # pragma: no cover - admit-path stub
    # Yield nothing so the handler's ``async for`` body never runs and the
    # route returns its terminal 500 ("DB session exhausted"). That is past
    # the role gate, which is all these tests verify (200 vs 403). A 500 here
    # means the role gate ADMITTED the caller.
    return
    yield  # makes this an async generator


@pytest.mark.asyncio
async def test_tenant_admin_admitted():
    resp = await _put(_user("tenant_admin"))
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_system_admin_admitted():
    # Canonical human system admin requires tenant_id="__system__" after the
    # session-revocation middleware hardening (Bug-7322).
    sa = CurrentUser(
        user_id="admin@tessallite.local", tenant_id="__system__",
        email="admin@tessallite.local", role="system_admin",
    )
    resp = await _put(sa)
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_modeler_rejected():
    resp = await _put(_user("modeler"))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viewer_rejected():
    resp = await _put(_user("viewer"))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_embed_rejected():
    resp = await _put(_embed())
    assert resp.status_code == 403
