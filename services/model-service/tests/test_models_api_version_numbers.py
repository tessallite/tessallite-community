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

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

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
        return FakeScalarResult(self._items)

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def first(self):
        # Existence probe used by the bootstrap-admin branch of caller_has_role.
        return self._items[0] if self._items else None


async def _noop_trust_meta(_db, _model):
    return {"last_refreshed_at": None, "source_system": None, "owner": ""}


def _execute_then_no_binding(version_results):
    """Return an ``execute`` side_effect that yields the given version-number
    results in order, then a "no binding" result for every subsequent call.

    Bug-8101: ``get_model`` now also resolves ``caller_can_author`` via
    ``caller_has_role``, which issues extra binding-lookup ``execute`` calls
    AFTER the version-number queries. Those calls must resolve to
    bootstrap-admin (no binding found) so the count is stable and the version
    behaviour under test is unaffected. This keeps the version assertions exact
    without coupling them to the RBAC lookup call count.
    """
    seq = list(version_results)

    def _side_effect(*_a, **_k):
        if seq:
            return seq.pop(0)
        # caller_has_role lookups: caller has NO binding -> not privileged
        # (F-021-04 removed the zero-binding bootstrap grant). caller_can_author
        # resolves to False; version behaviour under test is unaffected.
        return _ScalarResult([])

    return _side_effect


def _execute_then_binding(version_results, role="admin"):
    """Like ``_execute_then_no_binding`` but the caller HOLDS a binding, so
    ``caller_has_role`` (caller_can_author) resolves True. Used by the test that
    asserts an authorized caller may author (F-021-04: authorization requires a
    real binding, not the removed zero-binding bootstrap grant)."""
    seq = list(version_results)
    binding = types.SimpleNamespace(
        role=role, model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
    )

    def _side_effect(*_a, **_k):
        if seq:
            return seq.pop(0)
        return _ScalarResult([binding])

    return _side_effect


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
    # is skipped because deployed_version_id is None. Bug-8101: get_model then
    # resolves caller_can_author (extra binding lookups). F-021-04: the caller
    # must hold a real binding to author, so supply one.
    mock_db.execute = AsyncMock(side_effect=_execute_then_binding([_ScalarResult([])]))

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models._build_trust_meta", new=_noop_trust_meta),
    ):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_saved_version_number"] is None
    assert body["deployed_version_number"] is None
    # Bug-8101: get_model surfaces caller_can_author so the Model Builder knows
    # whether to open read-only. F-021-04: a caller WITH a modeler+ binding may
    # author (no zero-binding bootstrap grant any more).
    assert body["caller_can_author"] is True


# ---------------------------------------------------------------------------
# Case 2 — saves exist, not deployed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_model_has_versions_no_deploy(client):
    model = _model_with_version_fields()  # deployed_version_id=None
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = AsyncMock(side_effect=_execute_then_no_binding([_ScalarResult([7])]))

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models._build_trust_meta", new=_noop_trust_meta),
    ):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_saved_version_number"] == 7
    assert body["deployed_version_number"] is None
    # Exactly one VERSION execute — the deployed-pointer branch must not run.
    # (caller_can_author resolution issues its own separate binding lookups.)
    assert mock_db.execute.await_count >= 1


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
        side_effect=_execute_then_no_binding(
            [
                _ScalarResult([5]),  # last_saved
                _ScalarResult([3]),  # deployed
            ]
        )
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
    # Two VERSION executes (last_saved + deployed); caller_can_author lookups
    # follow separately.
    assert mock_db.execute.await_count >= 2


# ---------------------------------------------------------------------------
# List endpoint decorates every row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_models_decorates_version_numbers(client):
    model = _model_with_version_fields()  # deployed_version_id=None
    mock_db = make_mock_db()
    # Bug-8101 follow-up: list_models authorizes + filters via
    # resolve_listable_model_scope BEFORE the model query. F-021-04 (decision #9)
    # removed the zero-binding bootstrap path; a caller with a PROJECT-WIDE
    # binding (model_id=None) sees ALL models, so resolve makes ONE query.
    # F-013-12: batched decoration follows. Query order:
    #   1. resolve: user bindings load -> rows of (model_id,) [(None,) = project-wide -> all]
    #   2. list models
    #   3. grouped last_saved            -> rows of (model_id, n)
    #   4. grouped refresh               -> rows of (model_id, ts) [none]
    #   5. first DataSource              -> scalars                [none]
    # (deployed-pointer lookup skipped — no model has a pointer)
    mock_db.execute = AsyncMock(
        side_effect=[
            _ScalarResult([(None,)]),  # resolve: project-wide binding -> sees all
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
