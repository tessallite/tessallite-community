"""
Unit tests for the ``revert`` endpoint's deploy-pointer retargeting.

B4 bug: before the fix, revert only retargeted ``deployed_version_id`` when
the previously-deployed version had been deleted by the revert. If the model
was deployed to an older version that *survived* the revert, the deploy
pointer stayed on that older version while the live state was rewritten to
the reverted version — a divergence that violates F-8.

Bug-7906 (restore-as-new-version): revert no longer DELETES any version. It
appends a new version carrying the reverted-to shape and moves the deploy
pointer (when the model was deployed) to that NEW version — never leaving it on
the stale prior version, and never destroying the versions created after the
reverted-to point. If the model was undeployed, the pointer stays None.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from unittest.mock import MagicMock

from shared.db.models import ModelVersion

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


PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _version(
    version_id: uuid.UUID, number: int, snapshot_unavailable: bool = False
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json={
            "schema_version": 1,
            "model": {"id": str(TEST_MODEL_ID)},
            "tables": [],
            "columns": [],
            "physical_attributes": [],
            "user_defined_attributes": [],
            "targets": [],
            "measures": [],
            "joins": [],
            "hierarchies": [],
            "aggregates": [],
        },
        snapshot_unavailable=snapshot_unavailable,
        created_at=NOW,
        created_by="user@example.com",
    )


async def _rehydrate_noop(*_args, **_kwargs):
    return None


async def _empty_tenant_db(*_args, **_kwargs):
    if False:
        yield None


@pytest.mark.asyncio
async def test_revert_fails_closed_when_tenant_db_yields_no_session(client):
    """Bug-5657: zero-yield tenant DB must not report a successful revert."""
    version_id = uuid.uuid4()
    with patch("src.api.versions.get_tenant_db", _empty_tenant_db):
        resp = await client.post(
            f"{PREFIX}/versions/{version_id}/revert",
            json={"confirm": str(version_id)},
        )

    assert resp.status_code == 500
    assert "tenant database session unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_undeploy_fails_closed_when_tenant_db_yields_no_session(client):
    """Bug-5657: zero-yield tenant DB must not report a successful undeploy."""
    with patch("src.api.versions.get_tenant_db", _empty_tenant_db):
        resp = await client.post(f"{PREFIX}/undeploy")

    assert resp.status_code == 500
    assert "tenant database session unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_revert_preserves_live_governance(client):
    """Bug-6205: revert is a DEFINITION-only rewrite. It must call
    ``rehydrate_into_live`` with ``restore_governance=False`` so live
    governance (personas, data tags + persona tag restrictions, row-security
    rules) is preserved instead of being rolled back to the reverted-to
    snapshot. Reverting a model's SHAPE must never silently change who can see
    which rows/columns.

    Deploy/import keep the default (full governance restore); that contract is
    asserted separately in ``test_revert_uses_definition_only_flag_not_import``
    and the rehydrator's own restore tests (``test_revert_cls_tags``)."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    captured: dict = {}

    async def _capturing_rehydrate(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return None

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_capturing_rehydrate),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    # The revert path must explicitly opt OUT of governance restore.
    assert captured["kwargs"].get("restore_governance") is False
    # And it still preserves the materialised artifacts revert always kept.
    assert captured["kwargs"].get("preserve_aggregates") is True
    assert captured["kwargs"].get("preserve_pockets") is True
    # Bug-7982: named sets are DEFINITION, not a materialised artifact — revert
    # must NOT preserve them wholesale. The rehydrator now ALWAYS rebuilds the
    # named-set definition from the snapshot (there is no preserve flag), and
    # re-applies live named-set governance under restore_governance=False.
    assert "preserve_named_sets" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_revert_uses_definition_only_flag_not_import(client):
    """Bug-6205 guard: import must NOT inherit the revert-only
    ``restore_governance=False``. The importer path (``import_export`` and every
    sibling importer) calls ``rehydrate_into_live`` with the default
    ``restore_governance=True`` — a fresh import materialises the whole model,
    governance included. This test locks the divergence by proving the revert
    endpoint alone sets the flag; if a refactor ever routed revert through the
    import default, ``restore_governance`` would be True (or absent) here and
    this fails."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    captured: dict = {}

    async def _capturing_rehydrate(*args, **kwargs):
        captured["kwargs"] = kwargs
        return None

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_capturing_rehydrate),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    # Explicitly present and False — not the import default.
    assert "restore_governance" in captured["kwargs"]
    assert captured["kwargs"]["restore_governance"] is False


def _track_added_versions(mock_db):
    """Wrap ``mock_db.add`` so appended ModelVersion rows can be inspected."""
    added: list = []
    orig_add = mock_db.add

    def _add(obj):
        added.append(obj)
        return orig_add(obj)

    mock_db.add = MagicMock(side_effect=_add)
    return added


@pytest.mark.asyncio
async def test_revert_retargets_deploy_pointer_when_prior_deploy_survives(client):
    """Model deployed to v2, revert to v3 → deploy pointer moves OFF the stale v2
    to the NEW version the revert appends (Bug-7906: revert appends, never
    deletes). The pointer must not stay on v2 and must not land on the
    reverted-to row itself."""
    v2_id = uuid.uuid4()
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = v2_id  # deployed to an older version

    before_ts = datetime.now(timezone.utc)

    mock_db = make_mock_db()
    # _ensure_model_access → Model lookup; revert → ModelVersion lookup
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])
    added = _track_added_versions(mock_db)

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    new_versions = [o for o in added if isinstance(o, ModelVersion)]
    assert len(new_versions) == 1
    # The deploy pointer follows the revert to the appended version — never left
    # on the stale v2, and not the older reverted-to row itself.
    assert model.deployed_version_id == new_versions[0].id
    assert model.deployed_version_id not in (v2_id, v3_id)
    assert model.last_deployed_at is not None
    assert model.last_deployed_at >= before_ts


@pytest.mark.asyncio
async def test_revert_preserves_newer_deployed_version_and_retargets(client):
    """Bug-7906: a version NEWER than the reverted-to one (v5 — the deployed
    one) is NO LONGER deleted by the revert; history is preserved. The revert
    issues no DELETE against ``model_versions`` and the deploy pointer moves
    forward to the appended version. (Pre-Bug-7906 v5 was destroyed and the
    pointer retargeted to v3.)"""
    v3_id = uuid.uuid4()
    v5_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = v5_id

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])
    added = _track_added_versions(mock_db)

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    # No DELETE against model_versions — the newer version v5 survives.
    executed_sql = [
        str(c.args[0]) for c in mock_db.execute.call_args_list if c.args
    ]
    assert not any(
        "DELETE" in s.upper() and "MODEL_VERSIONS" in s.upper()
        for s in executed_sql
    ), f"revert must not delete versions; executed: {executed_sql}"
    new_versions = [o for o in added if isinstance(o, ModelVersion)]
    assert len(new_versions) == 1
    assert model.deployed_version_id == new_versions[0].id
    assert model.last_deployed_at is not None


@pytest.mark.asyncio
async def test_bug7906_git_restore_tags_the_appended_version_not_reverted_to(client):
    """Bug-7906 (git trail): the git restore commit must be recorded under the
    NEWLY-APPENDED version number, not the reverted-to number.

    ``commit_model`` already tagged ``v{N}`` when version N was saved, so passing
    ``new_version=N`` makes ``commit_restore``'s ``git tag v{N}`` collide and
    fail the whole restore commit for a git-working tenant. ``new_version`` must
    equal the appended row's ``version_number``. Pre-fix it was ``v.version_number``."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])
    added = _track_added_versions(mock_db)

    captured: dict = {}

    def _fake_commit_restore(**kwargs):
        captured.update(kwargs)
        return "deadbee"

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch(
            "shared.model_snapshot.yaml_serialiser.snapshot_to_yaml",
            return_value="",
        ),
        patch("shared.git.model_repo.commit_restore", _fake_commit_restore),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    new_versions = [o for o in added if isinstance(o, ModelVersion)]
    assert len(new_versions) == 1
    appended = new_versions[0]
    assert captured, "commit_restore was not called"
    # Tagged under the appended version, never the already-tagged reverted-to N.
    assert captured["new_version"] == appended.version_number
    assert captured["new_version"] != 3
    assert captured["restored_from"] == 3


@pytest.mark.asyncio
async def test_revert_leaves_pointer_none_when_undeployed(client):
    """An undeployed model stays undeployed after a revert."""
    v3_id = uuid.uuid4()
    model = make_model()
    assert model.deployed_version_id is None  # baseline

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    assert model.deployed_version_id is None
    assert model.last_deployed_at is None


@pytest.mark.asyncio
async def test_evict_cache_uses_internal_admin_token(monkeypatch):
    """Bug-6204: the query-router cache-eviction endpoint requires tenant_admin,
    but deploy/undeploy/revert are authorised at modeler. The eviction must mint
    an internal tenant-admin service token (not forward the caller's bearer) so a
    project-scoped modeler's deploy does not leave the cache silently un-evicted."""
    from src.api import versions

    sent = {}

    class _Resp:
        status_code = 204

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def delete(self, url, headers=None):
            sent.update(url=url, headers=headers)
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    await versions._evict_query_router_cache(TEST_MODEL_ID, "acme")

    assert sent["headers"]["Authorization"].startswith("Bearer ")
    from jose import jwt
    from shared.config.settings import get_settings

    settings = get_settings()
    claims = jwt.decode(
        sent["headers"]["Authorization"].removeprefix("Bearer "),
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        options={"verify_aud": False},
    )
    assert claims["role"] == "tenant_admin"
    assert claims["tenant_id"] == "acme"
    assert claims["aud"] == "service"
    assert claims["token_type"] == "service"
    assert claims["service_principal"] == "model-service-deploy"
    assert claims["service_scopes"] == ["query-router.cache-evict"]
    assert str(TEST_MODEL_ID) in sent["url"]


@pytest.mark.asyncio
async def test_agent_refresh_fanout_uses_internal_service_token(monkeypatch):
    """Bug-6814: the agent-service refresh-derived leg of the deploy/revert
    fan-out requires the ``agent.refresh-derived`` service scope, but
    deploy/undeploy/revert are authorised at ``modeler`` and the fan-out
    iterates every project whose agent allow-lists the model — projects where
    the reverting/deploying user may hold no binding. Forwarding the caller's
    human bearer therefore 403s silently (fire-and-forget). The leg must mint an
    internal, scope-restricted service token instead of ever sending the human
    bearer to the agent-service.

    This asserts the KNOWN post-fix behaviour: the outbound Authorization header
    is a ``model-service-deploy`` service JWT carrying exactly the
    ``agent.refresh-derived`` scope (least privilege — not the CACHE_EVICT scope
    the principal is also permitted), addressed to the target project, and the
    caller's raw bearer is never present on the wire.
    """
    from src.api import versions

    project_id = uuid.uuid4()
    calls: list[dict] = []

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            calls.append({"url": url, "headers": headers})
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    await versions._notify_agent_service_refresh_derived(
        tenant_id="acme", project_ids=[project_id]
    )

    assert len(calls) == 1
    sent = calls[0]
    assert str(project_id) in sent["url"]
    assert sent["url"].endswith("/agent/refresh-derived")
    # Security invariant: an internal service JWT is attached, never a human
    # bearer, and no header carries a user-session token.
    auth = sent["headers"]["Authorization"]
    assert auth.startswith("Bearer ")

    from jose import jwt
    from shared.config.settings import get_settings

    settings = get_settings()
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
    # Least privilege: only the agent-refresh scope, not query-router.cache-evict.
    assert claims["service_scopes"] == ["agent.refresh-derived"]
    assert sent["headers"]["X-Tenant-Id"] == "acme"


@pytest.mark.asyncio
async def test_agent_refresh_fanout_never_forwards_human_bearer(monkeypatch):
    """Bug-6814 (leak guard): the fan-out signature carries only ``tenant_id``
    and ``project_ids`` — it has no channel to receive or forward the caller's
    raw bearer. This locks that contract so a future edit cannot reintroduce a
    ``token=current_user.raw_token`` parameter and leak the human bearer to the
    agent-service.
    """
    import inspect

    from src.api import versions

    params = set(
        inspect.signature(
            versions._notify_agent_service_refresh_derived
        ).parameters
    )
    assert params == {"tenant_id", "project_ids"}


@pytest.mark.asyncio
async def test_revert_runs_post_mutation_fanout(client):
    """Bug-6203/6204: revert is a hard rewrite of live state, so it must run the
    same post-mutation fan-out deploy/undeploy do — invalidate the KPI cache and
    evict the query-router semantic-binding cache — and the eviction must be
    driven by an internal token (tenant id), not the caller's bearer, so a
    project-scoped modeler's revert is not silently un-evicted (Bug-6204)."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    evict = AsyncMock()
    kpi_cache = MagicMock()

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions._evict_query_router_cache", evict),
        patch("src.kpi_cache.get_kpi_cache", return_value=kpi_cache),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    kpi_cache.invalidate_model.assert_called_once_with(TEST_MODEL_ID)
    evict.assert_awaited_once()
    args = evict.await_args.args
    assert args[0] == TEST_MODEL_ID
    # Bug-6204: second arg is the tenant id (used to mint an internal admin
    # token), never a user bearer token.
    assert args[1] == TEST_TENANT


@pytest.mark.asyncio
async def test_revert_refuses_snapshot_unavailable_imported_version(client):
    """Bug-6295: an imported history row has no faithful snapshot (the export
    bundle omits per-version snapshots). Reverting to it must be REFUSED (409),
    never rehydrate the empty placeholder — so today's shape can never be served
    under an old version label. The rehydrate path must not be reached at all.
    """
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _version(v3_id, 3, snapshot_unavailable=True)]
    )

    rehydrate = AsyncMock()
    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=rehydrate),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )

    assert resp.status_code == 409
    assert "unrecoverable" in resp.json()["detail"].lower()
    # Fail-closed: the live model was never rehydrated to the placeholder.
    rehydrate.assert_not_awaited()
    # The deploy pointer is untouched.
    assert model.deployed_version_id is None


@pytest.mark.asyncio
async def test_revert_allows_snapshot_available_version(client):
    """Guard for the above: a normal (native-Save) version whose snapshot IS
    available must still revert. Proves the Bug-6295 guard is scoped to
    unavailable rows and does not block legitimate reverts."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _version(v3_id, 3, snapshot_unavailable=False)]
    )

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_revert_rejects_wrong_confirm_phrase(client):
    """Confirmation string must exactly match ``revert to v{N}``."""
    v3_id = uuid.uuid4()
    model = make_model()

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert"},
        )

    assert resp.status_code == 400
    assert "confirm must equal" in resp.json()["detail"]
