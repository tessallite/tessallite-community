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
from shared.config.fastapi_drift import check_fastapi_version_drift

# Bug-8467 ordering: the drift guard must run before ANY import that can crash
# under a drifted interpreter.  The repository-gate probe
# (tests/unit/test_repo_gate_contracts.py) imports this conftest as a bare
# top-level module with a stubbed fastapi; the relative result_fakes import
# dies there with "attempted relative import with no known parent package" and
# masks the fail-closed Bug-8467 message the probe asserts.  Call the guard
# first so a drifted collection always fails with the named contract.
check_fastapi_version_drift()

from .result_fakes import FakeResult, FakeScalarResult

from src.main import app  # noqa: E402
from src.auth.middleware import CurrentUser, get_current_user  # noqa: E402


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
def configure_public_base_url(monkeypatch):
    """Bug-6307: SSO no longer reconstructs its callback origin from the
    request's client-controlled Host / X-Forwarded-Host headers.

    A deployment must declare its external origin (``PUBLIC_BASE_URL``) or have
    the request origin match a configured CORS origin; anything else fails
    closed. TestClient requests arrive as ``http://testserver``, which is
    neither, so every SSO-touching route test must run as a correctly
    configured deployment. Applied here rather than per file so a new test that
    exercises /auth/saml/* or /auth/oidc/* does not silently 500.

    The guard itself is covered in ``test_sso_base_url_injection.py``, which
    patches ``get_settings`` directly and is unaffected by this fixture.
    """
    from shared.config.settings import get_settings

    monkeypatch.setattr(
        get_settings(), "PUBLIC_BASE_URL", "https://sso.test.example",
        raising=False,
    )
    # F-031-01: production default is enforcement ON. Unit tests are the
    # internal-unlimited hatch so JIT/create paths do not open the system DB.
    monkeypatch.setattr(
        get_settings(), "LICENSE_ENFORCEMENT_ENABLED", False, raising=False,
    )
    yield


@pytest.fixture(autouse=True)
def mock_rbac_get_tenant_db():
    """
    Patch get_tenant_db inside src.auth.rbac so require_role() resolves the
    caller as holding an admin binding (F-021-04 hard cutover, decision #9,
    removed the zero-binding bootstrap-admin grant, so require_role now DENIES a
    caller with no binding). Returning a fake admin binding keeps the harness's
    "route-dependency admits, inner gates differentiate role" model intact
    WITHOUT depending on the deleted bootstrap path. Tests that specifically
    exercise require_role DENIAL patch ``src.auth.rbac.get_tenant_db``
    themselves, overriding this default.

    Also patch src.api.kpis.load_authorized_model with a smart stub that:
      - preserves embed-token scope checks (so b01 scope-guard tests still work)
      - skips all DB queries for regular/admin users
    This prevents RBAC machinery from consuming execute() calls that tests
    configure for their own route-handler queries.
    """
    # Fake admin binding so require_role's model/project-scoped lookups resolve
    # to "admin" (admits any min_role) — replaces the removed bootstrap grant.
    _fake_admin_binding = types.SimpleNamespace(
        id=uuid.uuid4(), role="admin", model_id=None,
        project_id=None, user_identity="*",
    )
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = _fake_admin_binding
    execute_result.first.return_value = (_fake_admin_binding.id,)
    mock_db.execute = AsyncMock(return_value=execute_result)

    async def _stub_load_authorized_model(db, current_user, *, model_id, project_id=None, min_role="viewer", service_scope_verified=False):
        """Stub that preserves embed-token scope enforcement but bypasses DB RBAC.

        For embed users: delegate to the real ensure_project_model_access so
        project_ids / model_ids scope rejections (403) are still raised (that
        branch is DB-free and never hit the removed bootstrap path).
        For regular/admin users: noop (no DB calls) — equivalent to holding a
        binding; require_role-level denial is covered by dedicated tests.
        """
        from uuid import UUID as _UUID
        from shared.auth.middleware import CurrentEmbedUser
        from shared.auth.project_access import ensure_project_model_access

        def _as_uuid(v):
            return v if isinstance(v, _UUID) else _UUID(str(v))

        stub_project_id = _as_uuid(project_id) if project_id else TEST_PROJECT_ID
        stub_model_id = _as_uuid(model_id)

        if isinstance(current_user, CurrentEmbedUser):
            # Embed scope enforcement is DB-free; run it so out-of-scope
            # project/model rejections (403) still fire.
            await ensure_project_model_access(
                AsyncMock(),
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
        patch("src.api.access.emit_webhook", noop),
        patch("src.api.admin.emit_webhook", noop),
        patch("src.api.tenants.emit_webhook", noop),
        patch("src.api.embed.emit_webhook", noop),
        patch("src.api.export.emit_webhook", noop),
        patch("src.api.glossary.emit_webhook", noop),
        patch("src.api.personas.emit_webhook", noop),
        patch("src.api.row_security.emit_webhook", noop),
    ):
        yield


# Bug-7982 (Codex re-gate residual 1): every ordinary definition/governance
# writer (measures/dimensions/tables/table_attributes/joins/hierarchies/UDAs/
# calendar/data-tags/personas/row-security) now acquires the per-model
# advisory lock via ``acquire_model_definition_lock`` before reading, so it
# serialises with a concurrent revert. That adds one
# ``db.execute(SELECT pg_advisory_xact_lock)`` per writer, which would shift
# these modules' ordered-mock ``db.execute`` side-effect lists. Stub the lock
# to a no-op in THESE modules only (the lock's real serialisation is covered
# by test_model_lock_coverage + the DB integration suite). versions.py /
# named_sets.py / kpis.py are intentionally NOT stubbed — their lock-assertion
# tests exercise the real acquisition.
#
# Bug-8437 / Bug-8441 added three more: ``models`` (update_model writes the model
# scalars a revert restores), ``sources`` and ``targets`` (the revert upserts
# every column of a surviving row and HARD-DELETES rows absent from the
# snapshot, and create/delete_target additionally write ``models.target_id``).
#
# Bug-8710: a stub here means NO unit test can observe the real acquisition, so
# the claim "these endpoints serialise" must be carried by something that runs
# the real thing. It now is — ``tests/integration/test_bug7982_r7_db.py``
# (``test_newly_locked_writers_block_on_a_held_revert_lock`` and
# ``test_a_revert_and_a_concurrent_model_rename_cannot_interleave``) drives the
# REAL handlers against a REAL Postgres with the revert's lock held, restoring
# the un-stubbed lock for the duration. Removing the acquire line from any of
# the three turns those tests red. Do not add a module here without adding its
# behavioural counterpart there.
_LOCK_STUBBED_MODULES = (
    "measures", "dimensions", "tables", "table_attributes", "joins",
    "hierarchies", "user_defined_attributes", "calendar", "data_tags",
    "personas", "row_security", "models", "sources", "targets",
)


@pytest.fixture(autouse=True)
def _stub_ordinary_writer_model_lock():
    import importlib

    stub = AsyncMock()
    patchers = []
    for modname in _LOCK_STUBBED_MODULES:
        try:
            mod = importlib.import_module(f"src.api.{modname}")
        except Exception:
            continue
        if hasattr(mod, "acquire_model_definition_lock"):
            patchers.append(patch.object(mod, "acquire_model_definition_lock", stub))
    for p in patchers:
        p.start()
    try:
        yield stub
    finally:
        for p in patchers:
            p.stop()


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


@pytest.fixture
def kpi_effective_role(monkeypatch):
    """F-017-12 / Bug-8728: the KPI draft-visibility gates now decide privilege
    from the caller's EFFECTIVE project/model binding via ``caller_has_role``,
    which issues a ``user_access_bindings`` lookup. Unit tests here mock the db
    with strict positional ``side_effect`` sequences that cannot answer that
    extra query, so patch ``caller_has_role`` to derive privilege from the token
    role — reproducing the exact privilege decision these tests already assert,
    without weakening any assertion. The REAL binding-vs-token behaviour is
    covered end-to-end by ``test_kpi_draft_visibility`` (which does not use this
    shim); binding-specific cases elsewhere override the patch. Mirrors the
    ``_default_effective_role`` fixture in ``test_named_sets_draft_visibility``.
    """
    async def _caller_has_role(_db, current_user, _project_id, _role, _model_id=None):
        return getattr(current_user, "role", None) in {
            "modeler", "admin", "tenant_admin", "system_admin",
        }

    monkeypatch.setattr("src.api.kpis.caller_has_role", _caller_has_role)


def routed_execute(**rows_by_table):
    """Build an ``AsyncSession.execute`` side_effect that answers per STATEMENT.

    Each keyword is the SELECTED-FROM table name (``named_sets=[...]``,
    ``dimensions=[...]``); a statement with no matching FROM gets an EMPTY
    ``FakeResult``. Prefer this over a positional ``side_effect=[...]`` list: a
    positional list silently misaligns the moment a route gains or loses a
    query, which is how the RBAC binding lookup in ``caller_has_role`` ended up
    being handed a route's own rows.

    Matching is on ``FROM <table>``, not a bare substring, because a bare
    substring is ambiguous: ``select(NamedSet)`` renders the column
    ``named_sets.dimensions``, so ``"dimensions" in str(stmt)`` is True for the
    named-set query too and the answer would depend on keyword ORDER.

    An empty answer for ``user_access_bindings`` means "this caller has no
    binding", so ``caller_has_role`` returns False (F-021-04 hard cutover,
    decision #9, removed the zero-binding bootstrap-admin grant). Tests that
    need a privileged decision either shim ``caller_has_role`` (see
    ``kpi_effective_role`` / the named-set ``_default_effective_role``) or
    provide a matching binding row for the ``user_access_bindings`` query.
    """
    async def _execute(statement, *args, **kwargs):
        text = str(statement)
        for table, rows in rows_by_table.items():
            if f"FROM {table}" in text:
                return FakeResult(rows)
        return FakeResult([])
    return _execute


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
        deploy_epoch=0,
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
