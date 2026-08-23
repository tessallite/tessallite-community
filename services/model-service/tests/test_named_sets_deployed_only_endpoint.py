"""Bug-8384: the ``deployed_only`` wiring on GET /named-sets, end to end.

The resolver unit tests (``test_named_set_deploy_resolver``) prove the pinning
rules. These prove the rules are actually REACHED from the HTTP route the
gateway calls — the "producer fixed, consumer never wired" gap class. Each test
drives the real FastAPI route, not the resolver function.

The two behaviours that must NOT change: the model builder (no flag) keeps
seeing live drafts, and persona scoping still applies on top.
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
    routed_execute,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"

DEPLOYED_EXPR = "{ TopCount([Customer].[Customer].Members, 10, [Measures].[Revenue]) }"
DRAFT_EXPR = "{ TopCount([Customer].[Customer].Members, 500, [Measures].[Margin]) }"

NS_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()


def _live_set(set_id=NS_ID, *, expression=DRAFT_EXPR, name="Top Customers"):
    """The LIVE row, carrying an undeployed draft edit by default."""
    return types.SimpleNamespace(
        id=set_id,
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        scope=1,
        expression=expression,
        dimensions=None,
        builder_definition=None,
        list_type="advanced_mdx",
        certification_status="certified",
        replacement_id=None,
        owner_user_id="owner@acme.com",
        created_at=NOW,
        updated_at=NOW,
    )


def _snapshot_row(set_id=NS_ID, *, expression=DEPLOYED_EXPR, name="Top Customers"):
    return {
        "id": str(set_id),
        "model_id": str(TEST_MODEL_ID),
        "name": name,
        "display_name": name,
        "description": None,
        "display_folder": None,
        "scope": 1,
        "expression": expression,
        "dimensions": None,
        "builder_definition": None,
        "list_type": "advanced_mdx",
        "certification_status": "draft",
        "owner_user_id": "stale@acme.com",
    }


def _deployed_model():
    model = make_model()
    model.deployed_version_id = VERSION_ID
    model.deploy_epoch = 3
    return model


def _version(named_set_rows):
    return types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "named_sets": named_set_rows,
        },
    )


def _db(*, model, version, live_sets, dim_rows=()):
    db = make_mock_db()
    # ensure_model_in_project -> db.get(Model, ...); then the resolver ->
    # db.get(ModelVersion, ...).
    db.get = AsyncMock(side_effect=[model, version])
    # Answer per STATEMENT: the route reads named_sets (and dimensions, under a
    # persona), while rbac.caller_has_role reads user_access_bindings.
    db.execute = AsyncMock(
        side_effect=routed_execute(named_sets=live_sets, dimensions=dim_rows)
    )
    return db


@pytest.mark.asyncio
async def test_deployed_only_serves_the_deployed_expression_not_the_draft(client):
    """The leak, at the route the gateway calls."""
    db = _db(
        model=_deployed_model(),
        version=_version([_snapshot_row()]),
        live_sets=[_live_set()],
    )
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(f"{PREFIX}?deployed_only=true")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["expression"] == DEPLOYED_EXPR
    assert data[0]["expression"] != DRAFT_EXPR
    # Governance still comes from the live row.
    assert data[0]["certification_status"] == "certified"
    assert data[0]["owner_user_id"] == "owner@acme.com"


@pytest.mark.asyncio
async def test_builder_without_the_flag_still_sees_the_live_draft(client):
    """Modellers must keep seeing their own in-progress edits."""
    db = _db(
        model=_deployed_model(),
        version=_version([_snapshot_row()]),
        live_sets=[_live_set()],
    )
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    assert resp.json()[0]["expression"] == DRAFT_EXPR


@pytest.mark.asyncio
async def test_set_created_since_last_deploy_is_absent_from_the_bi_listing(client):
    db = _db(
        model=_deployed_model(),
        # Snapshot pins a different set -> the live one was never deployed.
        version=_version([_snapshot_row(uuid.uuid4(), name="Other")]),
        live_sets=[_live_set()],
    )
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(f"{PREFIX}?deployed_only=true")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_undeployed_model_serves_no_sets_to_bi(client):
    model = make_model()  # deployed_version_id is None
    db = _db(model=model, version=None, live_sets=[_live_set()])
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(f"{PREFIX}?deployed_only=true")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_invalid_deployed_snapshot_fails_closed_with_409(client):
    """Never fall back to live drafts when the deployed authority is unusable."""
    empty_version = types.SimpleNamespace(
        id=VERSION_ID,
        model_id=TEST_MODEL_ID,
        snapshot_json={"schema_version": "1.0"},
    )
    db = _db(
        model=_deployed_model(), version=empty_version, live_sets=[_live_set()],
    )
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(f"{PREFIX}?deployed_only=true")

    assert resp.status_code == 409
    assert "DEPLOYED_SNAPSHOT_INVALID" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_persona_scope_still_applies_on_top_of_deployed_pinning(client):
    """The persona gate must judge the SERVED definition, not the draft.

    The live draft references Product (in persona scope); the deployed
    definition references Customer (out of scope). What the client would
    actually receive is the deployed one, so it must be filtered out.
    """
    product_dim_id = uuid.uuid4()
    customer_dim_id = uuid.uuid4()
    persona = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="restricted",
        slug="restricted",
        included_dimension_ids=[str(product_dim_id)],  # excludes Customer
        includes_hidden_columns=False,
        audience_roles=[],
        default_filters={},
    )
    live = _live_set(expression="{ [Product].[Product].Members }")
    snap = _snapshot_row(expression="{ [Customer].[Customer].Members }")

    db = _db(
        model=_deployed_model(),
        version=_version([snap]),
        live_sets=[live],
        dim_rows=[(product_dim_id, "Product"), (customer_dim_id, "Customer")],
    )

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
    ):
        resp = await client.get(f"{PREFIX}?deployed_only=true&persona_id={persona.id}")

    assert resp.status_code == 200
    assert resp.json() == []
