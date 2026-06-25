"""
Shared fixtures for model-service tests.

sys.path is configured via [tool.pytest.ini_options] pythonpath in pyproject.toml:
  - "."  → tessallite/services/model-service/  (enables "from src.xxx import")
  - "../../" → tessallite/                     (enables "from shared.xxx import")

All route tests use:
  - async httpx.AsyncClient against the FastAPI app
  - app.dependency_overrides for get_current_user (avoids real JWT)
  - patch("src.api.<module>.get_tenant_db") for DB isolation (avoids real DB)
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user


# ---------------------------------------------------------------------------
# Global autouse: patch require_role's get_tenant_db → bootstrap-admin path
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_system_bootstrap():
    """
    Prevent the FastAPI lifespan from connecting to the real system DB.
    refresh_system_snapshot() opens SystemSessionLocal which requires a live
    PostgreSQL connection — not available in unit tests.
    """
    with patch("src.main.refresh_system_snapshot", new_callable=AsyncMock):
        yield


@pytest.fixture(autouse=True)
def disable_rate_limiting():
    """
    Unit suites fire hundreds of requests per minute from one client key —
    configure the rate limiter off for these tests (the registry default is
    on). Dedicated rate-limiter behaviour tests live in
    tessallite/tests/unit/test_rate_limiter.py and set their own snapshot.
    """
    from shared.config.bootstrap import update_snapshot

    update_snapshot("rate_limit.enabled", False)
    yield


@pytest.fixture(autouse=True)
def mock_rbac_get_tenant_db():
    """
    Patch get_tenant_db inside src.auth.rbac so require_role() always follows
    the bootstrap-admin path (no binding found, no bindings exist → implicit admin).
    Also patch src.api.kpis.load_authorized_model with a smart stub that:
      - preserves embed-token scope checks (so b01 scope-guard tests still work)
      - skips all DB queries for regular users (bootstrap-admin implicit grant)
    This prevents RBAC machinery from consuming execute() calls that tests
    configure for their own route-handler queries.
    """
    mock_db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None  # no caller binding
    # Bootstrap existence probe reads via .first() (F-H27R1-01); None means
    # the project has zero bindings → implicit admin (bootstrap path).
    execute_result.first.return_value = None
    mock_db.execute = AsyncMock(return_value=execute_result)

    # Noop DB whose execute always returns "no binding" — used by the smart
    # load_authorized_model stub to run ensure_project_model_access without
    # touching the test's own DB mock.
    noop_db = AsyncMock()
    noop_result = MagicMock()
    noop_result.scalar_one_or_none.return_value = None
    noop_result.first.return_value = None
    noop_db.execute = AsyncMock(return_value=noop_result)

    async def _stub_load_authorized_model(db, current_user, *, model_id, project_id=None, min_role="viewer"):
        """Stub that preserves embed-token scope enforcement but bypasses DB RBAC.

        For embed users: delegate to the real ensure_project_model_access so
        project_ids / model_ids scope rejections (403) are still raised.
        For regular/admin users: noop (no DB calls).
        """
        from uuid import UUID as _UUID
        from shared.auth.project_access import ensure_project_model_access

        def _as_uuid(v):
            return v if isinstance(v, _UUID) else _UUID(str(v))

        stub_project_id = _as_uuid(project_id) if project_id else TEST_PROJECT_ID
        stub_model_id = _as_uuid(model_id)

        # Run access control through noop_db so embed checks fire but no
        # real DB queries happen and no side-effects bleed into test's db mock.
        await ensure_project_model_access(
            noop_db,
            current_user,
            project_id=stub_project_id,
            model_id=stub_model_id,
            min_role=min_role,
        )
        import types as _types
        return _types.SimpleNamespace(
            id=stub_model_id,
            project_id=stub_project_id,
            slug="mock-model",
        )

    with (
        patch("src.auth.rbac.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.kpis.load_authorized_model", _stub_load_authorized_model),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_emit_webhook():
    """Suppress fire-and-forget webhook emission in all tests."""
    noop = AsyncMock()
    with (
        patch("src.api.auth.emit_webhook", noop),
        patch("src.api.models.emit_webhook", noop),
        patch("src.api.versions.emit_webhook", noop),
        patch("src.api.project_settings.emit_webhook", noop),
        patch("src.api.model_settings.emit_webhook", noop),
    ):
        yield


# ---------------------------------------------------------------------------
# Standard test identity
# ---------------------------------------------------------------------------

TEST_TENANT = "test-tenant"
TEST_USER_ID = "user@example.com"
TEST_PROJECT_ID = uuid.uuid4()
TEST_MODEL_ID = uuid.uuid4()
TEST_AGG_ID = uuid.uuid4()
NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Auth override helpers
# ---------------------------------------------------------------------------

def _make_current_user(user_id: str = TEST_USER_ID, tenant_id: str = TEST_TENANT) -> CurrentUser:
    return CurrentUser(user_id=user_id, tenant_id=tenant_id, email=user_id)


@pytest.fixture
def override_auth(request):
    """Override get_current_user; accepts optional 'user' marker attribute."""
    user = getattr(request, "param", None) or _make_current_user()
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# AsyncClient fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(override_auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Mock DB session factory
# ---------------------------------------------------------------------------

def make_mock_db() -> AsyncMock:
    """Return an AsyncMock that looks like an AsyncSession.

    ``execute()`` returns a MagicMock whose ``scalar_one_or_none()`` returns
    None by default. Tests that want to simulate an existing row should
    override ``db.execute`` (or the specific call) with their own AsyncMock.
    Previously the default returned an unconfigured child Mock which the
    route handlers had to filter out via isinstance checks — see CR Finding
    7. Returning None explicitly lets route code treat mock responses
    identically to real absent rows.
    """
    db = AsyncMock()
    db.info = {"tenant_id": TEST_TENANT}
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()

    # `begin_nested()` returns an async context manager on a real AsyncSession
    # (used by named_sets._create_version for the F-018-18 savepoint retry).
    # Make the mock return a no-op async context manager so route code that
    # opens a savepoint works under the mock.
    class _NoopSavepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db.begin_nested = MagicMock(return_value=_NoopSavepoint())

    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=default_result)
    return db


def async_gen_from(value):
    """Return an async generator that yields *value* once."""
    async def _gen(*args, **kwargs):
        yield value
    return _gen


# ---------------------------------------------------------------------------
# ORM object factories (SimpleNamespace — no SQLAlchemy needed)
# ---------------------------------------------------------------------------

def make_project(
    project_id: uuid.UUID = TEST_PROJECT_ID,
    slug: str = "test-project",
    display_name: str = "Test Project",
    is_active: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=project_id,
        slug=slug,
        display_name=display_name,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def make_model(
    model_id: uuid.UUID = TEST_MODEL_ID,
    project_id: uuid.UUID = TEST_PROJECT_ID,
    slug: str = "test-model",
    display_name: str = "Test Model",
    status: str = "active",
    aggregations_enabled: bool = True,
    max_aggregates: int = 50,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=model_id,
        project_id=project_id,
        slug=slug,
        display_name=display_name,
        description=None,
        target_id=None,
        refresh_strategy="scheduled",
        status=status,
        aggregations_enabled=aggregations_enabled,
        seed="abc123def456",
        max_aggregates=max_aggregates,
        miss_threshold_daily=3,
        miss_threshold_weekly=5,
        schema_drift_interval_hours=24,
        canvas_layout=None,
        deployed_version_id=None,
        last_deployed_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def make_aggregate(
    agg_id: uuid.UUID = TEST_AGG_ID,
    model_id: uuid.UUID = TEST_MODEL_ID,
    status: str = "active",
    estimated_hit_rate: float | None = None,
    creation_reason: str = "manual",
) -> types.SimpleNamespace:
    target_id = uuid.uuid4()
    return types.SimpleNamespace(
        id=agg_id,
        model_id=model_id,
        target_id=target_id,
        physical_table_name="agg_abc123",
        target_schema="public",
        status=status,
        grain=["country"],
        source_row_count=None,
        agg_row_count=None,
        estimated_hit_rate=estimated_hit_rate,
        creation_reason=creation_reason,
        include_quantiles=False,
        created_at=NOW,
        updated_at=NOW,
        last_refreshed_at=None,
        retired_at=None,
    )
