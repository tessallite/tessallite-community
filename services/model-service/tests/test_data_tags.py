"""Tests for data tag CRUD and persona tag restriction endpoints."""
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

TAGS_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-tags"


def _make_tag(tag_name="PII", description="Personally identifiable", columns=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        tag_name=tag_name,
        description=description,
        created_at=NOW,
        columns=columns or [],
    )


def _make_column(column_name="email", table_alias="customers"):
    # F-008-10: table_name comes from the eagerly-loaded ModelTable
    # relationship (semantic alias), not a fabricated `_table_name` attr.
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        column_name=column_name,
        table=types.SimpleNamespace(
            alias=table_alias, physical_name=table_alias,
        ),
    )


# ------------------------------------------------------------------ #
# GET /data-tags
# ------------------------------------------------------------------ #


class TestListTags:
    @pytest.mark.anyio
    async def test_list_returns_200(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        tags = [_make_tag("PII"), _make_tag("Sensitive", "Finance data")]
        result = MagicMock()
        result.scalars.return_value.all.return_value = tags
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.get(TAGS_PREFIX)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["tag_name"] == "PII"

    @pytest.mark.anyio
    async def test_list_renders_column_table_names(self, client):
        """F-008-10: columns carry the table alias, not an empty string."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        tag = _make_tag("PII", columns=[_make_column("email", "customers")])
        result = MagicMock()
        result.scalars.return_value.all.return_value = [tag]
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.get(TAGS_PREFIX)
        assert resp.status_code == 200
        cols = resp.json()[0]["columns"]
        assert cols[0]["column_name"] == "email"
        assert cols[0]["table_name"] == "customers"

    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.get(TAGS_PREFIX)
        assert resp.status_code == 200
        assert resp.json() == []


# ------------------------------------------------------------------ #
# POST /data-tags
# ------------------------------------------------------------------ #


class TestCreateTag:
    @pytest.mark.anyio
    async def test_create_returns_201(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        # F-008-01: after commit the endpoint re-selects the tag with its
        # columns eagerly loaded instead of touching lazy relationships.
        created = _make_tag("PII", "Personal data")
        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = created
        db.execute = AsyncMock(return_value=load_result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                TAGS_PREFIX,
                json={"tag_name": "PII", "description": "Personal data"},
            )
        assert resp.status_code == 201
        assert resp.json()["tag_name"] == "PII"


# ------------------------------------------------------------------ #
# DELETE /data-tags/{id}
# ------------------------------------------------------------------ #


class TestDeleteTag:
    @pytest.mark.anyio
    async def test_delete_returns_204(self, client):
        db = make_mock_db()
        model = make_model()
        tag = _make_tag()

        db.get = AsyncMock(return_value=model)
        # F-008-01: the tag is loaded via an eager select, not db.get.
        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = tag
        db.execute = AsyncMock(return_value=load_result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}")
        assert resp.status_code == 204

    @pytest.mark.anyio
    async def test_delete_not_found(self, client):
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(return_value=model)
        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=load_result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{uuid.uuid4()}")
        assert resp.status_code == 404


# ------------------------------------------------------------------ #
# Pydantic schema validation
# ------------------------------------------------------------------ #


class TestDataTagSchemas:
    def test_create_valid(self):
        from shared.schemas.pydantic_models import DataTagCreate

        obj = DataTagCreate(tag_name="PII", description="Personal data")
        assert obj.tag_name == "PII"

    def test_create_with_columns(self):
        from shared.schemas.pydantic_models import DataTagCreate

        col_id = uuid.uuid4()
        obj = DataTagCreate(
            tag_name="Financial",
            column_ids=[col_id],
        )
        assert len(obj.column_ids) == 1

    def test_response_with_columns(self):
        from shared.schemas.pydantic_models import DataTagResponse, DataTagColumnInfo

        col = DataTagColumnInfo(
            column_id=uuid.uuid4(),
            table_name="customers",
            column_name="email",
        )
        resp = DataTagResponse(
            id=uuid.uuid4(),
            model_id=TEST_MODEL_ID,
            tag_name="PII",
            description=None,
            created_at=NOW,
            columns=[col],
        )
        assert resp.columns[0].column_name == "email"

    def test_restriction_request(self):
        from shared.schemas.pydantic_models import PersonaTagRestrictionRequest

        req = PersonaTagRestrictionRequest(
            tag_ids=[uuid.uuid4(), uuid.uuid4()]
        )
        assert len(req.tag_ids) == 2

    def test_restriction_response(self):
        from shared.schemas.pydantic_models import PersonaTagRestrictionResponse

        resp = PersonaTagRestrictionResponse(
            tag_id=uuid.uuid4(),
            tag_name="PII",
            description="Personal data",
            column_count=5,
        )
        assert resp.column_count == 5
