"""Deploy-route contract for join population governance (Bug-8615 phase G1).

The single property this phase must not get wrong is that classification is
WARN-ONLY: ``BLOCKED`` is computed and surfaced, and the deploy still succeeds.
Proving that in the classifier module is not enough — it has to hold at the
real ``POST .../deploy`` route, because that is where a future phase G5 will
turn it into a refusal and where a reviewer would look for an accidental
raise.

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
async def test_a_blocked_model_still_deploys_and_reports_the_finding(client):
    """WARN-ONLY. A BLOCKED rollup rides along on a 200 response; it is a
    finding for the modeller to act on, never a refusal. Turning this into an
    actual block is governance plan phase G5."""
    db, _model, version_id = _deploy_fixture()

    async def _blocked(_db, _model_id):
        return {
            "status": "BLOCKED", "evaluated": True, "join_count": 3,
            "evaluated_count": 3, "warning_count": 1, "blocked_count": 2,
            "warn_only": True,
        }

    with patch("src.api.versions.get_tenant_db", async_gen_from(db)), \
            patch(
                "src.api.join_population_health.summarise_join_population",
                _blocked,
            ):
        response = await client.post(
            f"{PREFIX}/deploy", json={"version_id": str(version_id)},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["deployed_version_id"] == str(version_id)
    assert payload["join_population"]["status"] == "BLOCKED"
    assert payload["join_population"]["blocked_count"] == 2
    assert payload["join_population"]["warn_only"] is True


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
