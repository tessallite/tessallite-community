"""
Unit tests for the pending-change surface and draft-discard endpoints
(G-013-01 / Bug-9171).

Two endpoints:

  GET  /pending-changes  — partitions changes into ``unsaved`` (draft vs latest
                           saved) and ``saved_undeployed`` (latest saved vs
                           deployed), reusing ``consistent_snapshot`` +
                           ``diff_snapshots``.
  POST /discard-draft    — resets live/draft state to the latest saved version
                           via ``rehydrate_into_live`` WITHOUT appending a
                           version or moving the deploy pointer (a draft-only
                           reset; the served snapshot is untouched).
"""
from __future__ import annotations

import types
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
    routed_execute,
)
from .result_fakes import FakeResult

pytestmark = pytest.mark.unit

# F-021-04: the zero-binding bootstrap-admin grant is removed; versions.py
# _ensure_model_access now admits ONLY a caller that holds a binding. These
# pending-changes/discard tests exercise endpoint behaviour, so declare an
# authorizing binding for the ``user_access_bindings`` lookup (the RBAC deny
# path is covered by the dedicated binding-only RBAC tests).
_AUTHZ_BINDING = types.SimpleNamespace(role="admin", model_id=None, project_id=None)

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _snapshot(measures: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "model": {"id": str(TEST_MODEL_ID)},
        "tables": [],
        "columns": [],
        "measures": measures or [],
        "dimensions": [],
        "joins": [],
        "hierarchies": [],
        "aggregates": [],
    }


def _version(
    version_id: uuid.UUID,
    number: int,
    snapshot: dict | None = None,
    snapshot_unavailable: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json=snapshot if snapshot is not None else _snapshot(),
        snapshot_unavailable=snapshot_unavailable,
        created_at=NOW,
        created_by="user@example.com",
    )


# ---------------------------------------------------------------------------
# GET /pending-changes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pending_changes_partitions_unsaved_and_saved_undeployed(client):
    """The two change sets are computed independently:

      unsaved          = latest saved (v2)  ->  live/draft
      saved_undeployed = deployed (v1)       ->  latest saved (v2)

    A measure added only in the DRAFT shows under ``unsaved`` and NOT under
    ``saved_undeployed``; a measure added in v2 (saved, not deployed) shows under
    ``saved_undeployed`` and NOT under ``unsaved``.
    """
    v1_id = uuid.uuid4()
    v2_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = v1_id

    # v1 (deployed): no measures. v2 (latest saved): adds "saved_measure".
    v1 = _version(v1_id, 1, _snapshot(measures=[]))
    v2 = _version(
        v2_id, 2, _snapshot(measures=[{"id": "m-saved", "slug": "saved_measure"}])
    )
    # live/draft: v2 plus an unsaved "draft_measure".
    live = _snapshot(
        measures=[
            {"id": "m-saved", "slug": "saved_measure"},
            {"id": "m-draft", "slug": "draft_measure"},
        ]
    )

    mock_db = make_mock_db()
    # _ensure_model_access -> Model; deployed lookup -> v1 (tenant_db.get).
    mock_db.get = AsyncMock(side_effect=[model, v1])
    # The latest-saved SELECT is the only FROM model_versions query in this route.
    mock_db.execute = AsyncMock(side_effect=routed_execute(user_access_bindings=[_AUTHZ_BINDING], model_versions=[v2]))

    async def _live_snapshot(*_a, **_k):
        return live

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions._consistent_snapshot", _live_snapshot),
    ):
        resp = await client.get(f"{PREFIX}/pending-changes")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # unsaved: draft added "draft_measure" on top of the saved v2.
    assert body["unsaved"]["base_version"] == 2
    assert body["unsaved"]["base_version_unavailable"] is False
    unsaved_measures = body["unsaved"]["diff"]["measures"]
    assert [m["slug"] for m in unsaved_measures["added"]] == ["draft_measure"]
    assert unsaved_measures["removed"] == []

    # saved_undeployed: v2 added "saved_measure" over the deployed v1.
    assert body["saved_undeployed"]["deployed_version"] == 1
    assert body["saved_undeployed"]["saved_version"] == 2
    su_measures = body["saved_undeployed"]["diff"]["measures"]
    assert [m["slug"] for m in su_measures["added"]] == ["saved_measure"]
    # The draft-only measure must NOT leak into the saved-vs-deployed set.
    assert all(m["slug"] != "draft_measure" for m in su_measures["added"])


@pytest.mark.asyncio
async def test_pending_changes_never_saved_shows_whole_draft_as_unsaved(client):
    """A model with no saved version reports the entire draft as unsaved and an
    empty saved_undeployed set."""
    model = make_model()
    model.deployed_version_id = None

    live = _snapshot(measures=[{"id": "m1", "slug": "only_measure"}])

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model])
    mock_db.execute = AsyncMock(side_effect=routed_execute(user_access_bindings=[_AUTHZ_BINDING], model_versions=[]))

    async def _live_snapshot(*_a, **_k):
        return live

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions._consistent_snapshot", _live_snapshot),
    ):
        resp = await client.get(f"{PREFIX}/pending-changes")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unsaved"]["base_version"] is None
    assert [m["slug"] for m in body["unsaved"]["diff"]["measures"]["added"]] == [
        "only_measure"
    ]
    assert body["saved_undeployed"]["saved_version"] is None
    assert body["saved_undeployed"]["diff"] == {}


# ---------------------------------------------------------------------------
# POST /discard-draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discard_draft_resets_to_latest_saved_without_version_or_pointer(
    client,
):
    """discard-draft rehydrates the latest saved snapshot into live with the
    definition-only flags, and must NOT append a ModelVersion or move the deploy
    pointer (a draft-only reset — the served snapshot is untouched)."""
    v2_id = uuid.uuid4()
    deployed_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = deployed_id  # stays put after discard
    epoch_before = model.deploy_epoch  # G-013-01 challenger: guard no epoch bump

    v2 = _version(v2_id, 2)

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model])
    mock_db.execute = AsyncMock(side_effect=routed_execute(user_access_bindings=[_AUTHZ_BINDING], model_versions=[v2]))
    added: list = []
    orig_add = mock_db.add

    def _add(obj):
        added.append(obj)
        return orig_add(obj)

    mock_db.add = MagicMock(side_effect=_add)

    captured: dict = {}

    async def _capturing_rehydrate(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return None

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_capturing_rehydrate),
    ):
        resp = await client.post(f"{PREFIX}/discard-draft")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "restored_to_version": 2}

    # Rehydrated the LATEST SAVED snapshot, definition-only.
    assert captured["args"][1] == v2.snapshot_json
    assert captured["kwargs"]["restore_governance"] is False
    assert captured["kwargs"]["preserve_aggregates"] is True
    assert captured["kwargs"]["preserve_pockets"] is True
    assert captured["kwargs"]["drop_orphan_aggregates"] is False

    # Draft-only: no new version row appended, deploy pointer unchanged.
    from shared.db.models import ModelVersion

    assert not any(isinstance(o, ModelVersion) for o in added)
    assert model.deployed_version_id == deployed_id
    # Draft discard must NEVER bump the deploy epoch that gates multi-replica
    # cache/serving (serve-safety); the endpoint touches no epoch machinery.
    assert model.deploy_epoch == epoch_before


@pytest.mark.asyncio
async def test_discard_draft_409_when_no_saved_version(client):
    """With nothing saved there is no baseline to discard to → 409, and the
    rehydrator is never called."""
    model = make_model()
    model.deployed_version_id = None

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model])
    mock_db.execute = AsyncMock(side_effect=routed_execute(user_access_bindings=[_AUTHZ_BINDING], model_versions=[]))

    rehydrate = AsyncMock()
    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=rehydrate),
    ):
        resp = await client.post(f"{PREFIX}/discard-draft")

    assert resp.status_code == 409
    assert "no saved version" in resp.json()["detail"].lower()
    rehydrate.assert_not_called()


@pytest.mark.asyncio
async def test_discard_draft_409_when_latest_saved_is_placeholder(client):
    """An imported-placeholder latest version has no restorable shape → 409."""
    v2_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = None
    placeholder = _version(v2_id, 2, snapshot={}, snapshot_unavailable=True)

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model])
    mock_db.execute = AsyncMock(
        side_effect=routed_execute(user_access_bindings=[_AUTHZ_BINDING], model_versions=[placeholder])
    )

    rehydrate = AsyncMock()
    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=rehydrate),
    ):
        resp = await client.post(f"{PREFIX}/discard-draft")

    assert resp.status_code == 409
    assert "shape" in resp.json()["detail"].lower()
    rehydrate.assert_not_called()
