"""ML14 — data-tag ownership, conflict, and clear-description (F-008-14/17/20)."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)
from .test_data_tags import _make_tag

TAGS_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-tags"
RESTRICT_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/personas"
)


def _persona(model_id=TEST_MODEL_ID):
    return types.SimpleNamespace(id=uuid.uuid4(), model_id=model_id)


# ---------------------------------------------------------------------------
# F-008-17 — duplicate tag name is a 409, not a 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tag_duplicate_name_returns_409(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.commit = AsyncMock(side_effect=IntegrityError("dup", None, None))
    db.rollback = AsyncMock()
    with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
        resp = await client.post(TAGS_PREFIX, json={"tag_name": "PII"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "DATA_TAG_NAME_CONFLICT"


# ---------------------------------------------------------------------------
# F-008-14 — column ownership validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tag_rejects_foreign_column(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    foreign_col = uuid.uuid4()
    # _resolve_model_columns query returns no matching column → foreign.
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            TAGS_PREFIX,
            json={"tag_name": "PII", "column_ids": [str(foreign_col)]},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "DATA_TAG_COLUMN_NOT_IN_MODEL"


# ---------------------------------------------------------------------------
# F-008-14 — persona ownership on restriction endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_restrictions_rejects_foreign_persona(client):
    model = make_model()
    foreign_persona = _persona(model_id=uuid.uuid4())  # belongs elsewhere

    async def _get(cls, key):
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "Persona":
            return foreign_persona
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
        resp = await client.put(
            f"{RESTRICT_PREFIX}/{foreign_persona.id}/tag-restrictions",
            json={"tag_ids": []},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_restrictions_rejects_foreign_tag(client):
    model = make_model()
    persona = _persona()
    foreign_tag = uuid.uuid4()

    async def _get(cls, key):
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "Persona":
            return persona
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    # _validate_tags_in_model query returns no matching tag → foreign.
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
        resp = await client.put(
            f"{RESTRICT_PREFIX}/{persona.id}/tag-restrictions",
            json={"tag_ids": [str(foreign_tag)]},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "DATA_TAG_NOT_IN_MODEL"


# ---------------------------------------------------------------------------
# F-008-20 — explicit null description clears it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_tag_clears_description_with_null(client):
    tag = _make_tag(description="old text")
    model = make_model()

    async def _get(cls, key):
        if cls.__name__ == "Model":
            return model
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)

    # _load_tag returns the same (mutated) tag both times.
    async def _execute(stmt, *a, **kw):
        result = MagicMock()
        result.scalar_one_or_none.return_value = tag
        result.scalars.return_value.all.return_value = [tag]
        return result

    db.execute = AsyncMock(side_effect=_execute)
    with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
        resp = await client.put(
            f"{TAGS_PREFIX}/{tag.id}",
            json={"description": None},
        )
    assert resp.status_code == 200
    # The explicit null cleared the stored description.
    assert tag.description is None
