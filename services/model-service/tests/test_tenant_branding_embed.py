"""Bug-7441 — GET /tenants/{id}/branding must reject embed tokens.

Branding is a tenant management/config surface. The PUT already requires a
tenant admin (embed rejected). Before this fix the GET used ``get_current_user``,
so an embed session could read the tenant's branding config, leaving read and
write access on the surface inconsistent (Bug-6423 closed the same asymmetry for
sibling per-user library reads). ``get_branding`` now depends on
``require_human_user`` (upgraded from ``forbid_embed_user`` by Bug-7776 L5 lane
to also reject service tokens) — both embed and service tokens get 403; a
regular user gets 200.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from src.auth.middleware import get_current_user
from src.main import app

pytestmark = pytest.mark.unit

TENANT = "acme"
URL = f"/api/v1/tenants/{TENANT}/branding"


async def _fake_tenant_db(tenant_id: str = ""):
    db = AsyncMock()
    yield db


@pytest.fixture
async def embed_client():
    embed_user = CurrentEmbedUser(
        user_id="viewer@customer.com", tenant_id=TENANT,
        email="viewer@customer.com",
    )
    app.dependency_overrides[get_current_user] = lambda: embed_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def regular_client():
    user = CurrentUser(
        user_id="admin@example.com", tenant_id=TENANT,
        email="admin@example.com", role="viewer",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_branding_forbids_embed_user(embed_client):
    """An embed token must be rejected (403) — the management-surface guard
    (require_human_user) runs before any DB access, so no branding value is
    disclosed."""
    resp = await embed_client.get(URL)
    assert resp.status_code == 403, resp.text
    # require_human_user uses "interactive users only"; the key invariant is
    # the 403 status, not the exact wording.
    assert resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_branding_allows_regular_user(regular_client):
    """A regular (non-embed) user of the tenant can still read branding."""
    async def _get_setting(key, *args, **kwargs):
        # Return a value only for the primary colour; other keys resolve to
        # None so branding-value validators (e.g. logo_url) are not tripped.
        return "#0B5FFF" if key == "branding.primary_color" else None

    with (
        patch(
            "src.api.tenant_branding.get_tenant_db", new=_fake_tenant_db,
        ),
        patch(
            "src.api.tenant_branding.get_setting",
            new=AsyncMock(side_effect=_get_setting),
        ),
    ):
        resp = await regular_client.get(URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["primary_color"] == "#0B5FFF"
