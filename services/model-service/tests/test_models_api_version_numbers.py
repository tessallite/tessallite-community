"""
Unit tests for ModelResponse.last_saved_version_number /
ModelResponse.deployed_version_number population.

These fields drive the Model Builder toolbar chip ("Saved v5 · Deployed v3").
They must reflect the state of the ``model_versions`` table at response time
and must not require a second round-trip from the frontend.

Three cases:
  1. model has no saved versions                 → both fields None
  2. model has saves but no deployed pointer     → last_saved=N, deployed=None
  3. model is deployed to an older version       → last_saved=N, deployed=M (M<N)
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    client,
    make_mock_db,
    make_model,
    async_gen_from,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models"


class _ScalarResult:
    """Mimic ``sqlalchemy.engine.Result`` — see test_models.py for rationale."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


async def _noop_trust_meta(_db, _model):
    return {"last_refreshed_at": None, "source_system": None, "owner": ""}


def _model_with_version_fields(
    *,
    deployed_version_id: uuid.UUID | None = None,
):
    """make_model() enriched with the version-pointer fields the response
    decorator reads. Defaults match an undeployed, freshly-created model."""
    m = make_model()
    m.deployed_version_id = deployed_version_id
    m.last_deployed_at = None
    m.canvas_layout = None
    return m


# ---------------------------------------------------------------------------
# Case 1 — no saved versions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_model_no_versions(client):
    model = _model_with_version_fields()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    # _resolve_version_numbers issues one execute (last_saved); deployed branch
    # is skipped because deployed_version_id is None.
    mock_db.execute = AsyncMock(side_effect=[_ScalarResult([])])

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models._build_trust_meta", new=_noop_trust_meta),
    ):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_saved_version_number"] is None
    assert body["deployed_version_number"] is None


# ---------------------------------------------------------------------------
# Case 2 — saves exist, not deployed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_model_has_versions_no_deploy(client):
    model = _model_with_version_fields()  # deployed_version_id=None
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = AsyncMock(side_effect=[_ScalarResult([7])])

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models._build_trust_meta", new=_noop_trust_meta),
    ):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_saved_version_number"] == 7
    assert body["deployed_version_number"] is None
    # Only one execute — the deployed-pointer branch must not run.
    assert mock_db.execute.await_count == 1


# ---------------------------------------------------------------------------
# Case 3 — deployed to an older version
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_model_deployed_not_latest(client):
    deployed_id = uuid.uuid4()
    model = _model_with_version_fields(deployed_version_id=deployed_id)
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = AsyncMock(
        side_effect=[
            _ScalarResult([5]),  # last_saved
            _ScalarResult([3]),  # deployed
        ]
    )

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models._build_trust_meta", new=_noop_trust_meta),
    ):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_saved_version_number"] == 5
    assert body["deployed_version_number"] == 3
    assert mock_db.execute.await_count == 2


# ---------------------------------------------------------------------------
# List endpoint decorates every row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_models_decorates_version_numbers(client):
    model = _model_with_version_fields()  # deployed_version_id=None
    mock_db = make_mock_db()
    # F-013-12: batched decoration. Query order:
    #   1. list models
    #   2. grouped last_saved  -> rows of (model_id, n)
    #   3. grouped refresh     -> rows of (model_id, ts)  [none]
    #   4. first DataSource    -> scalars                 [none]
    # (deployed-pointer lookup skipped — no model has a pointer)
    mock_db.execute = AsyncMock(
        side_effect=[
            _ScalarResult([model]),
            _ScalarResult([(model.id, 2)]),
            _ScalarResult([]),
            _ScalarResult([]),
        ]
    )

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["last_saved_version_number"] == 2
    assert data[0]["deployed_version_number"] is None
