"""Tests for downstream asset CRUD, summary, and lineage badge."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)

ASSETS_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/downstream-assets"
)
REFS_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/query-references"
)
SCAN_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
)


def _make_asset(
    asset_type="dashboard",
    asset_name="Sales Dashboard",
    owner="alice",
    columns=None,
):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        asset_type=asset_type,
        asset_name=asset_name,
        asset_url="https://bi.example.com/dash/1",
        owner=owner,
        notes=None,
        created_at=NOW,
        updated_at=NOW,
        columns=columns or [],
    )


def _asset_lookup_result(asset):
    """What the handler's scoped asset SELECT returns.

    ``result.scalars().one_or_none()`` — the ORM shape ``scoped_select``
    produces. A mocked session cannot evaluate the ownership predicate, so
    these tests only prove the handler surfaces "found" and "not found"
    correctly; that the predicate itself is right is proved against real
    Postgres in ``tests/integration/test_read_path_project_scope_db.py``.
    """
    result = MagicMock()
    result.scalars.return_value.one_or_none.return_value = asset
    return result


def _make_query_ref(queried_table="sales", hit_count=5):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        queried_table=queried_table,
        query_user="analyst@corp",
        query_text_hash="abc123",
        last_seen_at=NOW,
        hit_count=hit_count,
    )


# ------------------------------------------------------------------ #
# GET /downstream-assets
# ------------------------------------------------------------------ #


class TestListDownstreamAssets:
    @pytest.mark.anyio
    async def test_list_returns_200(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        assets = [_make_asset(), _make_asset(asset_type="report", asset_name="Q4 Report")]
        result = MagicMock()
        result.scalars.return_value.all.return_value = assets
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.get(ASSETS_PREFIX)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["asset_type"] == "dashboard"
        assert data[1]["asset_type"] == "report"

    @pytest.mark.anyio
    async def test_list_empty_returns_empty_list(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.get(ASSETS_PREFIX)
        assert resp.status_code == 200
        assert resp.json() == []


# ------------------------------------------------------------------ #
# POST /downstream-assets
# ------------------------------------------------------------------ #


class TestCreateDownstreamAsset:
    @pytest.mark.anyio
    async def test_create_returns_201(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
            obj.updated_at = NOW
            obj.columns = []

        db.refresh = AsyncMock(side_effect=_refresh)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                ASSETS_PREFIX,
                json={
                    "asset_type": "dashboard",
                    "asset_name": "Sales Dashboard",
                    "asset_url": "https://bi.example.com",
                    "owner": "alice",
                },
            )
        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_create_rejects_invalid_type(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                ASSETS_PREFIX,
                json={
                    "asset_type": "spreadsheet",
                    "asset_name": "Bad",
                },
            )
        assert resp.status_code == 422


# ------------------------------------------------------------------ #
# DELETE /downstream-assets/{id}
# ------------------------------------------------------------------ #


class TestDeleteDownstreamAsset:
    @pytest.mark.anyio
    async def test_delete_returns_204(self, client):
        db = make_mock_db()
        model = make_model()
        asset = _make_asset()

        # The asset is looked up through a SCOPED SELECT, not ``db.get``: the
        # ownership predicate has to be in the query, and ``db.get`` cannot
        # express one. ``db.get`` still serves the path model.
        db.get = AsyncMock(return_value=model)
        db.execute = AsyncMock(return_value=_asset_lookup_result(asset))

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{ASSETS_PREFIX}/{asset.id}")
        assert resp.status_code == 204

    @pytest.mark.anyio
    async def test_delete_not_found_returns_404(self, client):
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(return_value=model)
        # The scoped SELECT finds nothing — which is the ONE outcome for both
        # "no such asset" and "another project's asset", by design.
        db.execute = AsyncMock(return_value=_asset_lookup_result(None))

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{ASSETS_PREFIX}/{uuid.uuid4()}")
        assert resp.status_code == 404


# ------------------------------------------------------------------ #
# GET /downstream-assets/summary
# ------------------------------------------------------------------ #


class TestDownstreamAssetSummary:
    @pytest.mark.anyio
    async def test_summary_returns_counts(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        summary_rows = [
            ("dashboard", 3),
            ("report", 2),
            ("ml_job", 1),
        ]
        result = MagicMock()
        result.all.return_value = summary_rows
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{ASSETS_PREFIX}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert data["by_type"]["dashboard"] == 3
        assert data["by_type"]["report"] == 2

    @pytest.mark.anyio
    async def test_summary_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        result = MagicMock()
        result.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{ASSETS_PREFIX}/summary")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "by_type": {}}


# ------------------------------------------------------------------ #
# GET /impact/query-references
# ------------------------------------------------------------------ #


class TestQueryReferences:
    @pytest.mark.anyio
    async def test_list_query_refs_returns_200(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        refs = [_make_query_ref("sales", 10), _make_query_ref("orders", 3)]
        result = MagicMock()
        result.scalars.return_value.all.return_value = refs
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.downstream_assets.get_tenant_db", async_gen_from(db)):
            resp = await client.get(REFS_PREFIX)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["hit_count"] == 10


# ------------------------------------------------------------------ #
# Pydantic schema validation
# ------------------------------------------------------------------ #


class TestDownstreamAssetSchemas:
    def test_valid_asset_types(self):
        from shared.schemas.pydantic_models import DownstreamAssetCreate

        for t in ("dashboard", "report", "ml_job", "api", "other"):
            obj = DownstreamAssetCreate(asset_type=t, asset_name="Test")
            assert obj.asset_type == t

    def test_invalid_asset_type_rejected(self):
        from shared.schemas.pydantic_models import DownstreamAssetCreate

        with pytest.raises(Exception):
            DownstreamAssetCreate(asset_type="spreadsheet", asset_name="Bad")

    def test_response_schema_from_namespace(self):
        from shared.schemas.pydantic_models import DownstreamAssetResponse

        ns = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=TEST_MODEL_ID,
            asset_type="dashboard",
            asset_name="Test Dash",
            asset_url=None,
            owner=None,
            notes=None,
            created_at=NOW,
            updated_at=NOW,
        )
        resp = DownstreamAssetResponse.model_validate(ns)
        assert resp.asset_type == "dashboard"
        assert resp.column_ids == []

    def test_gateway_ref_response_schema(self):
        from shared.schemas.pydantic_models import GatewayQueryReferenceResponse

        ns = _make_query_ref()
        resp = GatewayQueryReferenceResponse.model_validate(ns)
        assert resp.hit_count == 5
