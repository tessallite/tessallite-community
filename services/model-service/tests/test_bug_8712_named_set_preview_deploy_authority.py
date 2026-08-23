"""Bug-8712: POST /named-sets/{id}/preview must serve the DEPLOYED definition.

This is the transport the ``deployed_only`` flag could not close on its own. The
list route's flag resolves a DEFINITION; this route BUILDS ITS RESULT from the
row's own ``builder_definition``/``expression``, and it returns MEMBERS that
``TESSALLITE.LISTBYID`` writes straight into worksheet cells and the agent quotes
back to a user. So an unpinned read here is not a catalogue-metadata leak: it
changes the values in someone's spreadsheet the moment a modeller edits an
expression, with no Deploy.

These tests assert the MEMBERS, not that a query parameter was sent — the
parameter is not the mechanism on this route. ``fixedMembers`` previews are
computed in-process (``_preview_from_builder``), so the real route code runs end
to end with no stub standing in for the thing under test.

Root contract: "the deployed snapshot is the contract; the live state is
editor-only" (F-013-01, architecture_model-versioning-and-deploy.md), applied to
named sets by Bug-8384.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,  # noqa: F401 -- pytest fixture
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"

NS_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()

# The published list an analyst's workbook is entitled to see.
DEPLOYED_MEMBERS = ["North", "South"]
# The modeller's in-progress edit. Never a consumer's business until Deploy.
DRAFT_MEMBERS = ["North", "South", "East", "West"]


def _builder(members: list[str]) -> dict:
    return {"type": "fixedMembers", "dimension": "Region", "members": members}


def _live_set(members: list[str] = DRAFT_MEMBERS, set_id: uuid.UUID = NS_ID):
    return types.SimpleNamespace(
        id=set_id,
        model_id=TEST_MODEL_ID,
        name="Sales Regions",
        display_name="Sales Regions",
        description=None,
        display_folder=None,
        scope=1,
        expression="{ [Region].[North], [Region].[South] }",
        dimensions="Region",
        builder_definition=_builder(members),
        list_type="fixed",
        certification_status="certified",
        replacement_id=None,
        owner_user_id="owner@acme.com",
        created_at=NOW,
        updated_at=NOW,
    )


def _snapshot_row(members: list[str] = DEPLOYED_MEMBERS, set_id: uuid.UUID = NS_ID) -> dict:
    return {
        "id": str(set_id),
        "model_id": str(TEST_MODEL_ID),
        "name": "Sales Regions",
        "display_name": "Sales Regions",
        "description": None,
        "display_folder": None,
        "scope": 1,
        "expression": "{ [Region].[North], [Region].[South] }",
        "dimensions": "Region",
        "builder_definition": _builder(members),
        "list_type": "fixed",
        "certification_status": "draft",
        "owner_user_id": "stale@acme.com",
    }


def _deployed_model():
    model = make_model()
    model.deployed_version_id = VERSION_ID
    model.deploy_epoch = 4
    return model


def _version(named_set_rows: list[dict]):
    return types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "named_sets": named_set_rows,
        },
    )


def _db(*, model, live_set, version):
    """``ensure_model_in_project`` -> Model, then the set, then the version."""
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[model, live_set, version])
    return db


def _captions(payload: dict) -> list[str]:
    return [item["caption"] for item in payload["items"]]


async def _preview(client, db, *, query: str):  # noqa: F811
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        return await client.post(f"{PREFIX}/{NS_ID}/preview{query}")


@pytest.mark.asyncio
async def test_consumption_preview_returns_the_deployed_members_not_the_draft(client):  # noqa: F811
    """The leak itself: the members written into a worksheet cell."""
    db = _db(
        model=_deployed_model(),
        live_set=_live_set(DRAFT_MEMBERS),
        version=_version([_snapshot_row(DEPLOYED_MEMBERS)]),
    )
    resp = await _preview(client, db, query="?deployed_only=true")

    assert resp.status_code == 200
    assert _captions(resp.json()) == DEPLOYED_MEMBERS
    assert resp.json()["total_count"] == len(DEPLOYED_MEMBERS)
    # The draft's two extra regions must not have reached the cell.
    assert "East" not in _captions(resp.json())


@pytest.mark.asyncio
async def test_builder_preview_without_the_flag_still_shows_the_draft(client):  # noqa: F811
    """Authoring must keep working: the model builder previews what it edits."""
    db = _db(
        model=_deployed_model(),
        live_set=_live_set(DRAFT_MEMBERS),
        version=_version([_snapshot_row(DEPLOYED_MEMBERS)]),
    )
    resp = await _preview(client, db, query="")

    assert resp.status_code == 200
    assert _captions(resp.json()) == DRAFT_MEMBERS


@pytest.mark.asyncio
async def test_set_created_since_last_deploy_is_withheld_from_consumers(client):  # noqa: F811
    """No deployed definition exists for it, so there is nothing to serve."""
    db = _db(
        model=_deployed_model(),
        live_set=_live_set(DRAFT_MEMBERS),
        version=_version([_snapshot_row(DEPLOYED_MEMBERS, set_id=uuid.uuid4())]),
    )
    resp = await _preview(client, db, query="?deployed_only=true")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_undeployed_model_serves_no_members_to_consumers(client):  # noqa: F811
    db = _db(
        model=make_model(),  # deployed_version_id is None
        live_set=_live_set(DRAFT_MEMBERS),
        version=None,
    )
    resp = await _preview(client, db, query="?deployed_only=true")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_invalid_deployed_snapshot_fails_closed_with_409(client):  # noqa: F811
    """Never fall back to the live row — that fallback IS the leak (Bug-8384)."""
    empty_version = types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={"schema_version": "1.0"},
    )
    db = _db(
        model=_deployed_model(),
        live_set=_live_set(DRAFT_MEMBERS),
        version=empty_version,
    )
    resp = await _preview(client, db, query="?deployed_only=true")

    assert resp.status_code == 409
    assert "DEPLOYED_SNAPSHOT_INVALID" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_deployed_preview_keeps_persona_scoping(client):  # noqa: F811
    """The persona gate judges the SERVED definition, not the draft.

    The persona cannot see Region, so the set is withheld even though it is
    published. Pinning must not open a hole in the persona gate.
    """
    from .conftest import routed_execute

    dim_id = uuid.uuid4()
    db = _db(
        model=_deployed_model(),
        live_set=_live_set(DRAFT_MEMBERS),
        version=_version([_snapshot_row(DEPLOYED_MEMBERS)]),
    )
    db.execute = AsyncMock(
        side_effect=routed_execute(
            dimensions=[(dim_id, "Region")],
        )
    )
    persona = types.SimpleNamespace(
        id=uuid.uuid4(),
        included_dimension_ids=[str(uuid.uuid4())],  # Region NOT included
        included_measure_ids=None,
    )
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
    ):
        resp = await client.post(f"{PREFIX}/{NS_ID}/preview?deployed_only=true")

    assert resp.status_code == 404
