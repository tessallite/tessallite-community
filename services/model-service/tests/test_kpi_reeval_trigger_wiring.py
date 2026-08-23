"""Bug-7982 completion round (availability) — post-deploy KPI re-evaluation
trigger at end of deploy + revert.

A deploy/revert bumps deploy_epoch (Bug-7140); the $KPIs serve predicate
(Bug-7982 residual 2) withholds every kpi_latest row stamped with the OLD
epoch until re-evaluated, which — left to the hourly scheduler sweep alone —
can silently withhold $KPIs for up to an hour. ``trigger_post_deploy_kpi_reeval``
(mirrors ``trigger_predictive_cold_start`` / Bug-8029) calls the model's own
evaluate-batch endpoint immediately after a deploy/revert commits so $KPIs is
repopulated in seconds.

These guards lock:
  1. the helper posts to a CONFIG-DRIVEN model-service URL with an internal
     ``kpi-snapshot-sweep`` service token (the same principal the scheduler's
     hourly sweep already uses for this identical call) carrying the
     model-service evaluation and query-router execution scopes and the
     ``kpi_evaluator`` role, plus
     the internal-bypass rate-limit header (so evaluate-batch's
     is_service_context publish gate recognises the call);
  2. the helper is BEST-EFFORT — a model-service 5xx or a transport exception
     returns False and never raises;
  3. the helper is a no-op (no HTTP call at all) when there are no kpi ids;
  4. a successful deploy of a model with deployed KPIs FIRES the trigger with
     every deployed KPI id (removing the call fails
     ``test_deploy_fires_kpi_reeval_trigger`` — the revert guard);
  5. a trigger failure never fails the deploy;
  6. a deploy of a model with NO deployed KPIs does not fire the trigger;
  7. a deployed revert (``was_deployed``) fires the trigger; an undeployed
     revert does not (nothing serves, so nothing to repopulate).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """Let the route's fire-and-forget background tasks run to completion."""
    import asyncio

    for _ in range(5):
        pending = [t for t in list(module._background_tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


class _apply:
    """Enter a tuple of context managers as one ``with`` block.

    ``with (*tuple_of_cms, extra_cm):`` is not valid parenthesized-with
    syntax, so this mirrors ``test_predictive_cold_start_wiring.py``'s helper.
    """

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


def _kpi_id_result(ids: list[uuid.UUID]):
    """A db.execute() result whose .scalars().all() returns the given KPI ids."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = ids
    return result


def _is_kpi_id_select(stmt) -> bool:
    """True for ``select(KPI.id)...`` — the deployed-KPI-id lookup the deploy
    and revert paths issue right before firing the re-eval trigger."""
    try:
        from shared.db.models import KPI
        return stmt.column_descriptions[0].get("entity") is KPI
    except Exception:
        return False


def _execute_with_kpi_ids(default_result, kpi_ids: list[uuid.UUID]):
    async def _execute(stmt, *args, **kwargs):
        # acquire_model_definition_lock's advisory-lock SELECT passes a raw
        # ``text(...)`` statement plus params — not a KPI.id select.
        if _is_kpi_id_select(stmt):
            return _kpi_id_result(kpi_ids)
        return default_result
    return AsyncMock(side_effect=_execute)


# ---------------------------------------------------------------------------
# Group A — helper: config-driven URL, internal service token + header,
# best-effort, no-op on empty kpi_ids
# ---------------------------------------------------------------------------


class _FakeResp:
    """Stands in for the evaluate-batch response.

    Bug-7982 R7 finding 5: a 200 alone is no longer proof of publication — the
    trigger reads ``kpi_latest_published`` from the body before it clears the
    durable outbox row, because evaluate-batch isolates a per-row kpi_latest
    failure and still returns 200 with the evaluated values. The default body
    here therefore CONFIRMS publication; pass ``body=`` to model a 200 that does
    not.
    """

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = {"results": [], "kpi_latest_published": True} if body is None else body

    def json(self):
        return self._body


def _fake_client_factory(
    captured: dict, *, status_code: int = 200, raise_exc=None, body: dict | None = None,
):
    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            if raise_exc is not None:
                raise raise_exc
            return _FakeResp(status_code, body)

    return _Client


@pytest.mark.asyncio
async def test_trigger_posts_config_driven_url_token_and_bypass_header(monkeypatch):
    from src import kpi_reeval_trigger

    captured: dict = {}
    monkeypatch.setattr("httpx.AsyncClient", _fake_client_factory(captured))

    kpi_id = uuid.uuid4()
    ok = await kpi_reeval_trigger.trigger_post_deploy_kpi_reeval(
        "acme", TEST_PROJECT_ID, TEST_MODEL_ID, [kpi_id],
    )

    assert ok is True
    from shared.config.settings import get_settings

    settings = get_settings()
    assert captured["url"] == (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{TEST_PROJECT_ID}"
        f"/models/{TEST_MODEL_ID}/kpis/evaluate-batch"
    )
    assert captured["json"] == {"kpi_ids": [str(kpi_id)]}

    from shared.middleware.internal_bypass import INTERNAL_BYPASS_HEADER

    assert INTERNAL_BYPASS_HEADER in captured["headers"]

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
    # Reuses the existing "kpi-snapshot-sweep" principal from the closed
    # shared.auth.service_principal allow-list (same role/scopes the scheduler's
    # snapshot sweep already uses for the identical evaluate-batch call).
    assert claims["service_principal"] == "kpi-snapshot-sweep"
    assert claims["tenant_id"] == "acme"
    assert claims["role"] == "kpi_evaluator"
    assert claims["service_scopes"] == [
        "model-service.kpi-evaluate",
        "query-router.kpi-execute",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,label",
    [
        ({"results": [], "kpi_latest_published": False, "kpi_latest_failed": 1},
         "an explicit publish failure"),
        ({"results": [], "kpi_latest_published": None},
         "a request that never attempted a publish"),
        ({"results": []},
         "an older model-service that does not report the field"),
    ],
)
async def test_trigger_does_not_clear_the_outbox_on_an_unconfirmed_200(
    monkeypatch, body, label,
):
    """Bug-7982 R7 finding 5, at the wiring boundary.

    evaluate-batch isolates a per-row kpi_latest failure and still returns HTTP
    200 with the evaluated values. Treating that 200 as proof of publication
    deleted the DURABLE ``pending_kpi_reeval`` outbox row — so a publish failure
    destroyed the exact safety net that exists for it, and ``$KPIs`` stayed
    withheld with no operator-visible signal.

    The trigger must return False AND must not touch the outbox. Asserted by
    patching ``_clear_pending_outbox``: mutation-proof, because reverting the
    gate makes it be awaited.
    """
    from src import kpi_reeval_trigger

    captured: dict = {}
    monkeypatch.setattr(
        "httpx.AsyncClient", _fake_client_factory(captured, body=body)
    )
    cleared = {"called": False}

    async def _spy(*_a, **_k):
        cleared["called"] = True

    monkeypatch.setattr(kpi_reeval_trigger, "_clear_pending_outbox", _spy)

    ok = await kpi_reeval_trigger.trigger_post_deploy_kpi_reeval(
        "acme", TEST_PROJECT_ID, TEST_MODEL_ID, [uuid.uuid4()], deploy_epoch=3,
    )
    assert ok is False, f"trigger reported success for {label}"
    assert not cleared["called"], (
        f"the durable outbox row was cleared on {label} — the publish failure "
        "would lose its own retry"
    )


@pytest.mark.asyncio
async def test_trigger_clears_the_outbox_only_on_a_confirmed_publish(monkeypatch):
    """The positive side, so the gate above is not vacuous."""
    from src import kpi_reeval_trigger

    captured: dict = {}
    monkeypatch.setattr("httpx.AsyncClient", _fake_client_factory(captured))
    cleared = {"called": False}

    async def _spy(*_a, **_k):
        cleared["called"] = True

    monkeypatch.setattr(kpi_reeval_trigger, "_clear_pending_outbox", _spy)

    ok = await kpi_reeval_trigger.trigger_post_deploy_kpi_reeval(
        "acme", TEST_PROJECT_ID, TEST_MODEL_ID, [uuid.uuid4()], deploy_epoch=3,
    )
    assert ok is True and cleared["called"]


@pytest.mark.asyncio
async def test_trigger_noop_when_no_kpi_ids():
    from src import kpi_reeval_trigger

    with patch("httpx.AsyncClient") as mock_client:
        ok = await kpi_reeval_trigger.trigger_post_deploy_kpi_reeval(
            "acme", TEST_PROJECT_ID, TEST_MODEL_ID, [],
        )
    assert ok is False
    mock_client.assert_not_called()  # no HTTP call at all — nothing to evaluate


@pytest.mark.asyncio
async def test_trigger_best_effort_on_http_error(monkeypatch):
    from src import kpi_reeval_trigger

    captured: dict = {}
    monkeypatch.setattr(
        "httpx.AsyncClient", _fake_client_factory(captured, status_code=503)
    )
    ok = await kpi_reeval_trigger.trigger_post_deploy_kpi_reeval(
        "acme", TEST_PROJECT_ID, TEST_MODEL_ID, [uuid.uuid4()],
    )
    assert ok is False  # 5xx swallowed, never raised


@pytest.mark.asyncio
async def test_trigger_best_effort_on_transport_exception(monkeypatch):
    import httpx

    from src import kpi_reeval_trigger

    captured: dict = {}
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _fake_client_factory(
            captured, raise_exc=httpx.ConnectError("model-service down")
        ),
    )
    ok = await kpi_reeval_trigger.trigger_post_deploy_kpi_reeval(
        "acme", TEST_PROJECT_ID, TEST_MODEL_ID, [uuid.uuid4()],
    )
    assert ok is False  # connection error swallowed, never raised


# ---------------------------------------------------------------------------
# Group B — deploy path fires the trigger with deployed KPI ids
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
        patch(
            "src.api.versions.trigger_predictive_cold_start",
            new=AsyncMock(return_value=True),
        ),
    )


@pytest.mark.asyncio
async def test_deploy_fires_kpi_reeval_trigger_with_deployed_kpi_ids(client):
    """Revert guard: a successful deploy of a model with deployed KPIs must
    fire the re-eval trigger with every deployed KPI id."""
    from src.api import versions

    version_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _deployable_version(version_id), _deployable_version(version_id)]
    )
    kpi_ids = [uuid.uuid4(), uuid.uuid4()]
    mock_db.execute = _execute_with_kpi_ids(mock_db.execute.return_value, kpi_ids)

    mock_trigger = AsyncMock(return_value=True)
    cms = (
        *_deploy_patches(mock_db),
        patch("src.api.versions.trigger_post_deploy_kpi_reeval", mock_trigger),
    )
    with _apply(cms):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/deploy", json={"version_id": str(version_id)}
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_awaited_once()
    call_args = mock_trigger.await_args.args
    assert call_args[0] == TEST_TENANT
    assert call_args[1] == TEST_PROJECT_ID
    assert call_args[2] == TEST_MODEL_ID
    assert set(call_args[3]) == set(kpi_ids)


@pytest.mark.asyncio
async def test_deploy_succeeds_when_kpi_reeval_trigger_raises(client):
    """Best-effort: a trigger that raises must never fail the deploy."""
    from src.api import versions

    version_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _deployable_version(version_id), _deployable_version(version_id)]
    )
    mock_db.execute = _execute_with_kpi_ids(
        mock_db.execute.return_value, [uuid.uuid4()]
    )

    boom = AsyncMock(side_effect=RuntimeError("model-service exploded"))
    cms = (
        *_deploy_patches(mock_db),
        patch("src.api.versions.trigger_post_deploy_kpi_reeval", boom),
    )
    with _apply(cms):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/deploy", json={"version_id": str(version_id)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        await _drain_background(versions)

    boom.assert_awaited_once()


@pytest.mark.asyncio
async def test_deploy_with_no_deployed_kpis_does_not_fire_trigger(client):
    """A model with no deployed KPIs has nothing to re-evaluate — the trigger
    must not be called at all (avoids a pointless HTTP round trip)."""
    from src.api import versions

    version_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _deployable_version(version_id), _deployable_version(version_id)]
    )
    # Default mock_db.execute already returns an empty KPI-id list.

    mock_trigger = AsyncMock(return_value=True)
    cms = (
        *_deploy_patches(mock_db),
        patch("src.api.versions.trigger_post_deploy_kpi_reeval", mock_trigger),
    )
    with _apply(cms):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/deploy", json={"version_id": str(version_id)}
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# Group C — revert: deployed revert fires the trigger; undeployed does not
# ---------------------------------------------------------------------------


def _revert_version(version_id: uuid.UUID, number: int = 3) -> types.SimpleNamespace:
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


async def _rehydrate_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_deployed_revert_fires_kpi_reeval_trigger(client):
    """A revert that keeps the model deployed (``was_deployed=True``) bumps
    deploy_epoch exactly like deploy does, so it must fire the re-eval trigger
    with the model's deployed KPI ids."""
    from src.api import versions

    v3_id = uuid.uuid4()
    old_deployed_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = old_deployed_id  # was_deployed=True

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _revert_version(v3_id, 3)])
    kpi_ids = [uuid.uuid4()]
    mock_db.execute = _execute_with_kpi_ids(mock_db.execute.return_value, kpi_ids)

    mock_trigger = AsyncMock(return_value=True)
    cms = (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions.trigger_post_deploy_kpi_reeval", mock_trigger),
    )
    with _apply(cms):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_awaited_once()
    call_args = mock_trigger.await_args.args
    assert call_args[0] == TEST_TENANT
    assert call_args[2] == TEST_MODEL_ID
    assert set(call_args[3]) == set(kpi_ids)


@pytest.mark.asyncio
async def test_undeployed_revert_does_not_fire_kpi_reeval_trigger(client):
    """An undeployed model stays undeployed after revert — nothing serves, so
    there is nothing to repopulate; the trigger must not fire."""
    from src.api import versions

    v3_id = uuid.uuid4()
    model = make_model()
    assert model.deployed_version_id is None  # baseline: undeployed

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _revert_version(v3_id, 3)])

    mock_trigger = AsyncMock(return_value=True)
    cms = (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions.trigger_post_deploy_kpi_reeval", mock_trigger),
    )
    with _apply(cms):
        resp = await client.post(
            f"{DEPLOY_PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )
        assert resp.status_code == 200, resp.text
        await _drain_background(versions)

    mock_trigger.assert_not_awaited()
