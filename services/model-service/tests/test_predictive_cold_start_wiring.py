"""Bug-8029 — best-effort predictive cold-start trigger at end of deploy + import.

A fresh deployment never used to initiate the predictive cold-start pipeline;
predictive candidates only appeared on the next scheduled sweep tick. The
optimizer already exposes a durable, idempotent kickoff endpoint (F-010-01);
the model-service deploy and import paths now call it best-effort right after
the change commits.

These guards lock:
  1. the helper posts to a CONFIG-DRIVEN optimizer URL with an internal
     ``model-service-deploy`` service token carrying exactly the
     ``optimizer.predictive-cold-start`` scope (never the caller's bearer);
  2. the helper is BEST-EFFORT — an optimizer 5xx or a transport exception
     returns False and never raises;
  3. a successful deploy FIRES the trigger with the deployed model id (removing
     the call fails ``test_deploy_fires_cold_start_trigger`` — the revert guard);
  4. a trigger failure never fails the deploy;
  5. a model import that deployed immediately fires it; a non-deployed import
     does not;
  6. a project import fires it once per imported model;
  7. Bug-8395 — a DEPLOYED revert (the fourth committed deploy-pointer move,
     and the one that has just retired every orphaned predictive aggregate)
     fires it too; an undeployed revert does not; a raising trigger never fails
     the revert.
"""
from __future__ import annotations

import asyncio
import types
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.import_export import EXPORT_FORMAT
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

DEPLOY_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _deployable_version(version_id: uuid.UUID, number: int = 1) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json={
            "schema_version": 1,
            "model": {"id": str(TEST_MODEL_ID)},
            "columns": [{"name": "amount"}],
        },
        snapshot_unavailable=False,
        created_at=NOW,
        created_by="user@example.com",
    )


async def _drain_background(module) -> None:
    """Let the route's fire-and-forget cold-start tasks run to completion."""
    for _ in range(5):
        pending = [t for t in list(module._background_tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# Group A — helper: config-driven URL, internal service token, best-effort
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _fake_client_factory(captured: dict, *, status_code: int = 200, raise_exc=None):
    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            if raise_exc is not None:
                raise raise_exc
            return _FakeResp(status_code)

    return _Client


@pytest.mark.asyncio
async def test_trigger_posts_config_driven_url_and_internal_service_token(monkeypatch):
    from src import cold_start_trigger

    captured: dict = {}
    monkeypatch.setattr("httpx.AsyncClient", _fake_client_factory(captured))

    ok = await cold_start_trigger.trigger_predictive_cold_start(
        "acme", TEST_MODEL_ID
    )

    assert ok is True
    from shared.config.settings import get_settings

    settings = get_settings()
    # URL is built from the configured optimizer base URL, not a hardcoded host.
    assert captured["url"] == (
        f"{settings.OPTIMIZER_URL}"
        f"/api/v1/models/{TEST_MODEL_ID}/predictive/cold-start"
    )
    assert settings.OPTIMIZER_URL and settings.OPTIMIZER_URL in captured["url"]

    auth = captured["headers"]["Authorization"]
    assert auth.startswith("Bearer ")
    from jose import jwt

    claims = jwt.decode(
        auth.removeprefix("Bearer "),
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        options={"verify_aud": False},
    )
    assert claims["aud"] == "service"
    assert claims["token_type"] == "service"
    assert claims["service_principal"] == "model-service-deploy"
    assert claims["tenant_id"] == "acme"
    assert claims["role"] == "tenant_admin"
    # Least privilege: only the cold-start scope.
    assert claims["service_scopes"] == ["optimizer.predictive-cold-start"]


@pytest.mark.asyncio
async def test_trigger_best_effort_on_http_error(monkeypatch):
    from src import cold_start_trigger

    captured: dict = {}
    monkeypatch.setattr(
        "httpx.AsyncClient", _fake_client_factory(captured, status_code=503)
    )
    ok = await cold_start_trigger.trigger_predictive_cold_start(
        "acme", TEST_MODEL_ID
    )
    assert ok is False  # 5xx swallowed, never raised


@pytest.mark.asyncio
async def test_trigger_best_effort_on_transport_exception(monkeypatch):
    import httpx

    from src import cold_start_trigger

    captured: dict = {}
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _fake_client_factory(
            captured, raise_exc=httpx.ConnectError("optimizer down")
        ),
    )
    ok = await cold_start_trigger.trigger_predictive_cold_start(
        "acme", TEST_MODEL_ID
    )
    assert ok is False  # connection error swallowed, never raised


# ---------------------------------------------------------------------------
# Group B — deploy path fires the trigger and is not blocked by its failure
# ---------------------------------------------------------------------------


def _deploy_patches(mock_db):
    return (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.versions._verify_attribute_relationships_on_deploy",
            new=AsyncMock(),
        ),
        patch("src.api.versions._stale_incompatible_artifacts", new=AsyncMock()),
        patch("src.api.versions._evict_query_router_cache", new=AsyncMock()),
        patch("shared.git.model_repo.tag_deploy", new=MagicMock()),
        patch("src.kpi_cache.get_kpi_cache", return_value=MagicMock()),
    )


@pytest.mark.asyncio
async def test_deploy_fires_cold_start_trigger(client):
    """Revert guard: a successful deploy must call the predictive cold-start
    trigger with the deployed model id. Deleting the wired call fails here."""
    from src.api import versions

    version_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _deployable_version(version_id), _deployable_version(version_id)]
    )

    mock_trigger = AsyncMock(return_value=True)
    p_db, p_verify, p_stale, p_evict, p_git, p_kpi = _deploy_patches(mock_db)
    with (
        p_db, p_verify, p_stale, p_evict, p_git, p_kpi,
        patch("src.api.versions.trigger_predictive_cold_start", mock_trigger),
    ):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/deploy", json={"version_id": str(version_id)}
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_awaited_once_with(TEST_TENANT, TEST_MODEL_ID)


@pytest.mark.asyncio
async def test_deploy_succeeds_when_cold_start_trigger_raises(client):
    """Best-effort: a trigger that raises must never fail the deploy. The
    fire-and-forget task isolates the failure from the request path."""
    from src.api import versions

    version_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _deployable_version(version_id), _deployable_version(version_id)]
    )

    boom = AsyncMock(side_effect=RuntimeError("optimizer exploded"))
    p_db, p_verify, p_stale, p_evict, p_git, p_kpi = _deploy_patches(mock_db)
    with (
        p_db, p_verify, p_stale, p_evict, p_git, p_kpi,
        patch("src.api.versions.trigger_predictive_cold_start", boom),
    ):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/deploy", json={"version_id": str(version_id)}
        )
        # Deploy succeeds regardless of the trigger outcome.
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        await _drain_background(versions)

    boom.assert_awaited_once()


# ---------------------------------------------------------------------------
# Group C — model snapshot-import fires only when deployed
# ---------------------------------------------------------------------------


def _import_body(deploy_immediately: bool) -> dict:
    return {
        "bundle": {
            "schema_version": 1,
            "export_format": EXPORT_FORMAT,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exported_from": {"model_id": str(uuid.uuid4())},
            "model_display_name": "Imported Model",
            "model_slug": "imported-model",
            "snapshot": {"model": {}},
        },
        "target_project_id": str(TEST_PROJECT_ID),
        "deploy_immediately": deploy_immediately,
    }


#: Id the stubbed ``append_authentic_import_version`` returns, so the
#: handler's deployed pointer (and the cold-start trigger's argument) is
#: a concrete value the assertions can check.
_IMPORTED_VERSION_ID = uuid.uuid4()


def _import_patches(mock_db, cold_start_mock, refresh_mock):
    return (
        patch("src.api.import_export.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.import_export._ensure_project_access", new=AsyncMock()),
        patch(
            "src.api.import_export.enforce_import_model_cap", new=AsyncMock()
        ),
        patch(
            "src.api.import_export.prepare_snapshot_for_import",
            new=MagicMock(return_value=({"model": {}}, [])),
        ),
        patch(
            "src.api.import_export.insert_model_with_slug_retry",
            new=AsyncMock(
                return_value=(SimpleNamespace(id=uuid.uuid4()), "imported-model")
            ),
        ),
        patch("src.api.import_export.rehydrate_into_live", new=AsyncMock()),
        patch("src.api.import_export.seed_technical_persona", new=AsyncMock()),
        # Bug-7982 R7 (review round 7, F1): deploy_immediately no longer builds
        # the ModelVersion inline — that hand-duplicated
        # ``append_authentic_import_version`` and wrote model_versions with
        # neither the per-model lock nor the wholesale-rebuild exemption. It now
        # routes through the helper, which declares the exemption itself, so the
        # helper is what this wiring test must stub.
        patch(
            "src.api.import_export.append_authentic_import_version",
            new=AsyncMock(return_value=_IMPORTED_VERSION_ID),
        ),
        patch("src.api.import_export.trigger_model_refresh", refresh_mock),
        patch(
            "src.api.import_export.trigger_predictive_cold_start", cold_start_mock
        ),
    )


@pytest.mark.asyncio
async def test_model_import_deploy_immediately_fires_cold_start():
    """A snapshot import that deploys immediately fires the cold-start trigger
    for the newly-imported model id (the same id given to the refresh trigger)."""
    from src.api import import_export

    modeler = CurrentUser(
        user_id="m@example.com", tenant_id=TEST_TENANT,
        email="m@example.com", role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: modeler
    try:
        mock_db = make_mock_db()
        slug_result = MagicMock()
        slug_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=slug_result)

        cold_start = AsyncMock(return_value=True)
        refresh = AsyncMock(return_value=None)
        with _apply(_import_patches(mock_db, cold_start, refresh)):
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                resp = await ac.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/models/snapshot-import",
                    json=_import_body(deploy_immediately=True),
                )
            assert resp.status_code == 200, resp.text
            await _drain_import_tasks(import_export)

        cold_start.assert_awaited_once()
        refresh.assert_awaited_once()
        # Same model id feeds both fire-and-forget hooks.
        assert cold_start.await_args.args[0] == TEST_TENANT
        assert cold_start.await_args.args[1] == refresh.await_args.args[1]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_model_import_without_deploy_does_not_fire_cold_start():
    """An import left undeployed has nothing to cold-start: the trigger is
    gated on deploy so it is NOT called, while the aggregate-refresh still is."""
    from src.api import import_export

    modeler = CurrentUser(
        user_id="m@example.com", tenant_id=TEST_TENANT,
        email="m@example.com", role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: modeler
    try:
        mock_db = make_mock_db()
        slug_result = MagicMock()
        slug_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=slug_result)

        cold_start = AsyncMock(return_value=True)
        refresh = AsyncMock(return_value=None)
        with _apply(_import_patches(mock_db, cold_start, refresh)):
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                resp = await ac.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/models/snapshot-import",
                    json=_import_body(deploy_immediately=False),
                )
            assert resp.status_code == 200, resp.text
            await _drain_import_tasks(import_export)

        refresh.assert_awaited_once()
        cold_start.assert_not_awaited()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Group D — project import fires once per imported model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_import_fires_cold_start_per_model():
    from src.api import project_import_export

    admin = CurrentUser(
        user_id="admin@test.com", tenant_id=TEST_TENANT,
        email="admin@test.com", role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        new_ids = [uuid.uuid4(), uuid.uuid4()]
        import_result = {
            "project_id": str(uuid.uuid4()),
            "project_slug": "imported",
            "id_map": {
                "models": {"src-a": str(new_ids[0]), "src-b": str(new_ids[1])},
                "connections": {}, "personas": {}, "llm_configs": {},
                "judge_rubrics": {},
            },
            "models_imported": 2,
            "models_requiring_deploy": [],
            "post_import_actions": [],
            "warnings": [],
        }
        mock_db = make_mock_db()
        cold_start = AsyncMock(return_value=True)

        bundle = {
            "schema_version": 1,
            "export_format": "tessallite.project.v1",
            "exported_at": "2026-05-01T00:00:00Z",
            "exported_from": {"tenant_slug": "src", "project_id": str(uuid.uuid4())},
            "credentials_included": False,
            "credentials_envelope": None,
            "included_sections": [],
            "project": {"slug": "imported", "display_name": "Imported"},
            "models": [],
            "test_metadata": None,
        }

        with (
            patch(
                "src.api.project_import_export.get_tenant_db",
                async_gen_from(mock_db),
            ),
            patch(
                "src.api.project_import_export.get_credential_fernet",
                new=MagicMock(return_value=object()),
            ),
            patch(
                "src.api.project_import_export.import_project",
                new=AsyncMock(return_value=import_result),
            ),
            patch(
                "src.api.project_import_export.seed_technical_persona",
                new=AsyncMock(),
            ),
            patch(
                "src.api.project_import_export.audit_required", new=AsyncMock()
            ),
            patch(
                "src.api.project_import_export.trigger_predictive_cold_start",
                cold_start,
            ),
        ):
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                resp = await ac.post(
                    "/api/v1/projects/import",
                    json={"bundle": bundle, "mode": "create"},
                )
            assert resp.status_code == 200, resp.text
            await _drain_cold_start_tasks(project_import_export)

        assert cold_start.await_count == 2
        triggered = {
            (c.args[0], c.args[1]) for c in cold_start.await_args_list
        }
        assert triggered == {
            (TEST_TENANT, str(new_ids[0])),
            (TEST_TENANT, str(new_ids[1])),
        }
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Group E — Bug-8395: a DEPLOYED revert is the fourth deploy-pointer move and
# must fire the same trigger; an undeployed revert must not.
# ---------------------------------------------------------------------------


def _revert_version(version_id: uuid.UUID, number: int = 3):
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json={
            "schema_version": 1,
            "model": {"id": str(TEST_MODEL_ID)},
            "tables": [], "columns": [], "physical_attributes": [],
            "user_defined_attributes": [], "targets": [], "measures": [],
            "joins": [], "hierarchies": [], "aggregates": [],
        },
        snapshot_unavailable=False,
        created_at=NOW,
        created_by="user@example.com",
    )


async def _rehydrate_noop(*_a, **_kw):
    return None


@pytest.mark.asyncio
async def test_deployed_revert_fires_cold_start_trigger(client):
    """Bug-8395: a revert that moves the serving pointer rehydrates with
    ``drop_orphan_aggregates=True``, so every predictive aggregate absent from
    the reverted-to snapshot has just been retired and its physical table
    dropped. It must kick the same durable cold-start trigger deploy fires, or
    the model sits with nothing built until the next scheduled sweep tick.
    Deleting the wired call fails this test."""
    from src.api import versions

    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = uuid.uuid4()  # was deployed

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _revert_version(v3_id)])

    mock_trigger = AsyncMock(return_value=True)
    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions.trigger_predictive_cold_start", mock_trigger),
    ):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_awaited_once_with(TEST_TENANT, TEST_MODEL_ID)


@pytest.mark.asyncio
async def test_undeployed_revert_does_not_fire_cold_start_trigger(client):
    """Bug-8395: an undeployed revert serves nothing and leaves
    ``deployed_version_id`` None, so there is no deployment to build for. The
    trigger stays gated on ``was_deployed`` — same rule the import path uses."""
    from src.api import versions

    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _revert_version(v3_id)])

    mock_trigger = AsyncMock(return_value=True)
    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions.trigger_predictive_cold_start", mock_trigger),
    ):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_revert_succeeds_when_cold_start_trigger_raises(client):
    """Bug-8395: best-effort, same contract as deploy — a trigger that raises
    must never fail the revert."""
    from src.api import versions

    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = uuid.uuid4()

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _revert_version(v3_id)])

    boom = AsyncMock(side_effect=RuntimeError("optimizer exploded"))
    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions.trigger_predictive_cold_start", boom),
    ):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        await _drain_background(versions)

    boom.assert_awaited_once()


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


class _apply:
    """Enter a tuple of context managers as one ``with`` block."""

    def __init__(self, cms):
        self._cms = list(cms)

    def __enter__(self):
        for cm in self._cms:
            cm.__enter__()
        return self

    def __exit__(self, *exc):
        for cm in reversed(self._cms):
            cm.__exit__(*exc)
        return False


async def _drain_import_tasks(module) -> None:
    for _ in range(5):
        pending = [
            t for t in list(module._import_rebuild_tasks) if not t.done()
        ]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


async def _drain_cold_start_tasks(module) -> None:
    for _ in range(5):
        pending = [t for t in list(module._cold_start_tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
