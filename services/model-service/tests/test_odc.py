"""
Unit tests for the .odc (Office Data Connection) download endpoint.

Routes tested:
  GET /api/v1/projects/{project_id}/models/{model_id}/odc

Coverage:
  - Deployed model returns correct .odc content (headers, name=Tessallite,
    catalog, no credentials, XMLA URL from settings).
  - Undeployed model returns 409.
  - Non-existent model returns 404.
  - Model belonging to different project returns 404.
  - Embed users are forbidden (403).
  - Hostile model slug is escaped safely in the connection string and XML.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, CurrentEmbedUser, get_current_user

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEST_TENANT = "test-tenant"
TEST_PROJECT_ID = uuid.uuid4()
TEST_MODEL_ID = uuid.uuid4()
DEPLOYED_VERSION_ID = uuid.uuid4()


def _url(project_id=None, model_id=None):
    pid = project_id or TEST_PROJECT_ID
    mid = model_id or TEST_MODEL_ID
    return f"/api/v1/projects/{pid}/models/{mid}/odc"


def _make_user(user_id="user@example.com", tenant_id=TEST_TENANT):
    return CurrentUser(user_id=user_id, tenant_id=tenant_id, email=user_id)


def _make_embed_user(tenant_id=TEST_TENANT):
    return CurrentEmbedUser(
        user_id="embed-token",
        tenant_id=tenant_id,
        email="embed@example.com",
        project_ids=None,
        model_ids=None,
        capabilities=[],
    )


def _make_model(
    model_id=None,
    project_id=None,
    slug="test-model",
    deployed=True,
):
    return types.SimpleNamespace(
        id=model_id or TEST_MODEL_ID,
        project_id=project_id or TEST_PROJECT_ID,
        slug=slug,
        display_name="Test Model",
        deployed_version_id=DEPLOYED_VERSION_ID if deployed else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _mock_db_with_model(model):
    """Return an AsyncMock DB session that returns `model` from db.get()."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=model)
    db.info = {"tenant_id": TEST_TENANT}
    return db


async def _yield_db(db):
    yield db


def _patch_settings(xmla_url="http://localhost:8080"):
    """Patch get_settings to return a namespace with GATEWAY_XMLA_PUBLIC_URL."""
    mock_settings = types.SimpleNamespace(GATEWAY_XMLA_PUBLIC_URL=xmla_url)
    return patch("src.api.odc.get_settings", return_value=mock_settings)


# ---------------------------------------------------------------------------
# Happy path: deployed model returns valid .odc
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_odc_download_deployed_model():
    model = _make_model(slug="sales-model")
    db = _mock_db_with_model(model)

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.odc.get_tenant_db", lambda tid: _yield_db(db)),
                _patch_settings("http://gw.example.com:8080"),
            ):
                resp = await ac.get(_url())
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-ms-odc")
    assert "tessallite-sales-model.odc" in resp.headers["content-disposition"]

    body = resp.text
    # Connection name must be "Tessallite"
    assert 'value="Tessallite"' in body
    # XMLA URL must include /api/v1/xmla/ path
    assert "http://gw.example.com:8080/api/v1/xmla/" in body
    # Catalog must be the model slug
    assert "Initial Catalog=sales-model" in body
    # No credentials
    assert "Persist Security Info=False" in body
    assert "Password" not in body
    assert "User ID" not in body


# ---------------------------------------------------------------------------
# Undeployed model -> 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_odc_download_undeployed_model():
    model = _make_model(deployed=False)
    db = _mock_db_with_model(model)

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.odc.get_tenant_db", lambda tid: _yield_db(db)),
                _patch_settings(),
            ):
                resp = await ac.get(_url())
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 409
    assert "deploy" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Model not found -> 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_odc_download_model_not_found():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    db.info = {"tenant_id": TEST_TENANT}

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.odc.get_tenant_db", lambda tid: _yield_db(db)),
                _patch_settings(),
            ):
                resp = await ac.get(_url())
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Model belongs to a different project -> 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_odc_download_model_wrong_project():
    other_project = uuid.uuid4()
    model = _make_model(project_id=other_project)
    db = _mock_db_with_model(model)

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.odc.get_tenant_db", lambda tid: _yield_db(db)),
                _patch_settings(),
            ):
                resp = await ac.get(_url())
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Embed users are forbidden -> 403
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_odc_download_embed_user_forbidden():
    model = _make_model()
    db = _mock_db_with_model(model)

    app.dependency_overrides[get_current_user] = lambda: _make_embed_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.odc.get_tenant_db", lambda tid: _yield_db(db)),
                _patch_settings(),
            ):
                resp = await ac.get(_url())
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Hostile model slug is escaped safely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_odc_download_hostile_slug_escaped():
    """A model slug containing XML/HTML-unsafe characters must be escaped."""
    hostile_slug = 'model<script>alert("xss")</script>&"bad'
    model = _make_model(slug=hostile_slug)
    db = _mock_db_with_model(model)

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.odc.get_tenant_db", lambda tid: _yield_db(db)),
                _patch_settings(),
            ):
                resp = await ac.get(_url())
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    body = resp.text
    # Raw hostile characters must NOT appear unescaped
    assert "<script>" not in body
    assert '"xss"' not in body
    # The escaped form must be present
    assert "&lt;script&gt;" in body
    assert "&amp;" in body
    # Filename should be sanitised (unsafe chars replaced)
    disp = resp.headers["content-disposition"]
    assert ".odc" in disp
    # No angle brackets or quotes in the filename
    assert "<" not in disp
    assert ">" not in disp


# ---------------------------------------------------------------------------
# render_odc unit test
# ---------------------------------------------------------------------------

def test_render_odc_content_shape():
    """Verify the rendered .odc string has the expected structure."""
    from src.api.odc import render_odc

    content = render_odc("http://example.com:8080", "my-catalog")
    assert 'value="Tessallite"' in content
    assert "http://example.com:8080/api/v1/xmla/" in content
    assert "Initial Catalog=my-catalog" in content
    assert "Persist Security Info=False" in content
    assert "Provider=MSOLAP.8" in content
    assert "Password" not in content


def test_render_odc_trailing_slash_normalization():
    """XMLA URL is normalised regardless of trailing slash on input."""
    from src.api.odc import render_odc

    content = render_odc("http://host:8080/", "cat")
    assert "http://host:8080/api/v1/xmla/" in content
    # No double slashes
    assert "8080//api" not in content


def test_render_odc_url_ampersand_escaped():
    """A URL containing & must be HTML-escaped in the .odc XML/HTML output."""
    from src.api.odc import render_odc

    content = render_odc("http://host:8080?a=1&b=2", "cat")
    # The & must be escaped to &amp; in the XML context
    assert "&amp;" in content
    # The raw unescaped & followed by b= must not appear
    assert "?a=1&b=" not in content
