"""Deploy-route contract for join population governance (Bug-8615 G5).

The route must refuse only measured, policy-relevant ``BLOCKED`` rows, before
the deploy pointer or any publish side effect changes.  The allow matrix keeps
the explicit non-blocking cases executable at the real route boundary.

Also pins the wiring itself (the classifier is actually invoked with the NEW
deploy epoch) and the fail-open backstop (a validator that raises does not fail
the deploy).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import shared.semantic.join_population_validator as jpv

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _valid_snapshot() -> dict:
    return {
        "schema_version": 5,
        "model": {"id": str(TEST_MODEL_ID)},
        "tables": [{"id": str(uuid.uuid4()), "model_id": str(TEST_MODEL_ID)}],
        "columns": [{"id": str(uuid.uuid4())}],
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        "dimensions": [{"id": str(uuid.uuid4()), "name": "country"}],
        "hierarchies": [],
    }


def _deploy_fixture():
    version_id = uuid.uuid4()
    model = make_model()
    model.deploy_epoch = 5
    version = types.SimpleNamespace(
        id=version_id, model_id=TEST_MODEL_ID, version_number=1, summary=None,
        snapshot_json=_valid_snapshot(), created_at=NOW,
        created_by="user@example.com",
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[model, version])
    db.refresh = AsyncMock()

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    result.first.return_value = None
    db.execute = AsyncMock(return_value=result)
    return db, model, version_id


@pytest.mark.asyncio
async def test_a_measured_blocker_refuses_before_pointer_or_commit(client):
    """G5: a measured undeclared blocker returns actionable 409 before commit."""
    db, model, version_id = _deploy_fixture()
    join_id = uuid.uuid4()
    blocked = types.SimpleNamespace(
        join_id=join_id,
        population_participation="undeclared",
        status="BLOCKED",
        measured=True,
        row_effect_ratio=0.2,
        reason="measured",
    )

    with patch("src.api.versions.get_tenant_db", async_gen_from(db)), \
            patch(
                "src.api.versions._validate_join_population_on_deploy",
                AsyncMock(return_value=[blocked]),
            ):
        response = await client.post(
            f"{PREFIX}/deploy", json={"version_id": str(version_id)},
        )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "JOIN_POPULATION_BLOCKED"
    assert detail["joins"] == [{
        "join_id": str(join_id),
        "population_participation": "undeclared",
        "status": "BLOCKED",
        "row_effect_ratio": 0.2,
        "reason": "measured",
    }]
    assert "Declare" in detail["message"]
    assert model.deployed_version_id is None
    assert model.deploy_epoch == 5
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "participation,status,measured,effect",
    [
        ("preserve_base_rows", "BLOCKED", True, 0.9),
        ("undeclared", "WARNING", True, 0.01),
        ("undeclared", "BLOCKED", False, 0.9),
        ("undeclared", "WARNING", False, None),
    ],
)
async def test_non_blocking_join_population_cases_proceed(
    client, participation, status, measured, effect,
):
    """G5 allow matrix: preserved, below-threshold and unmeasured never block."""
    db, _model, version_id = _deploy_fixture()
    row = types.SimpleNamespace(
        join_id=uuid.uuid4(), population_participation=participation,
        status=status, measured=measured, row_effect_ratio=effect,
        reason="measured" if measured else "not_measured",
    )
    with patch("src.api.versions.get_tenant_db", async_gen_from(db)), \
            patch(
                "src.api.versions._validate_join_population_on_deploy",
                AsyncMock(return_value=[row]),
            ):
        response = await client.post(
            f"{PREFIX}/deploy", json={"version_id": str(version_id)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_classifier_is_invoked_with_the_new_deploy_epoch(client):
    """Wiring proof: the deploy hook is reachable, and its evidence is bound to
    the epoch the deploy just bumped to (5 -> 6), not the one it read."""
    db, _model, version_id = _deploy_fixture()
    seen: dict = {}

    async def _spy(**kwargs):
        seen.update(kwargs)
        return []

    with patch("src.api.versions.get_tenant_db", async_gen_from(db)), \
            patch.object(jpv, "validate_model_joins_on_deploy", _spy):
        response = await client.post(
            f"{PREFIX}/deploy", json={"version_id": str(version_id)},
        )

    assert response.status_code == 200, response.text
    assert seen["model_id"] == TEST_MODEL_ID
    assert seen["deployed_version_id"] == version_id
    assert seen["deploy_epoch"] == 6
    assert seen["threshold"] == jpv.DEFAULT_ROW_EFFECT_WARNING_THRESHOLD


@pytest.mark.asyncio
async def test_a_raising_classifier_does_not_fail_the_deploy(client):
    """Fail-open backstop. The classifier already swallows its own errors; this
    proves the deploy route survives even if it stops doing so."""
    db, _model, version_id = _deploy_fixture()

    async def _boom(**_kwargs):
        raise RuntimeError("classifier exploded")

    with patch("src.api.versions.get_tenant_db", async_gen_from(db)), \
            patch.object(jpv, "validate_model_joins_on_deploy", _boom):
        response = await client.post(
            f"{PREFIX}/deploy", json={"version_id": str(version_id)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_no_source_connection_records_unmeasured_rather_than_skipping(
    client,
):
    """A model whose source cannot be resolved must still have its previous
    verdicts cleared and unmeasured ones recorded, so the health surface cannot
    keep showing an earlier deploy's numbers as current."""
    db, _model, version_id = _deploy_fixture()
    seen: dict = {}

    async def _spy(**kwargs):
        seen.update(kwargs)
        return []

    with patch("src.api.versions.get_tenant_db", async_gen_from(db)), \
            patch.object(jpv, "validate_model_joins_on_deploy", _spy):
        response = await client.post(
            f"{PREFIX}/deploy", json={"version_id": str(version_id)},
        )

    assert response.status_code == 200, response.text
    # The mocked session cannot resolve a real ProjectConnection, so the hook
    # falls back to the unmeasured path instead of skipping the call entirely.
    assert seen["measure"] is False
    assert seen["conn_obj"] is None
