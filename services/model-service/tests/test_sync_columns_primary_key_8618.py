"""``sync-columns`` must persist the source's primary-key answer (Bug-8618).

Discovery (``shared/source_introspection.py``) now reads PRIMARY KEY
constraints from the source catalogue, but a producer with no consumer changes
nothing. This pins the consumer half: the sync endpoint writes the discovered
flag onto both new and existing columns, and — the case that matters — leaves
the stored value untouched when the payload does not state one.

That asymmetry is load-bearing. ``_stamp_primary_keys`` OMITS the field when
the catalogue read fails, so "we could not look" must never be persisted as
"there is no key": a source outage would otherwise clear every declared key on
the model at the next re-sync, and the pocket row-population proof would
silently stop admitting a pocket it had previously proven safe.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

TABLE_ID = uuid4()


def _existing_column(name: str, *, is_primary_key: bool):
    """A stand-in for a stored ``ModelColumn`` row the endpoint mutates."""
    return SimpleNamespace(
        id=uuid4(),
        model_table_id=TABLE_ID,
        column_name=name,
        data_type="text",
        is_nullable=True,
        is_primary_key=is_primary_key,
    )


def _mock_db(existing: list) -> AsyncMock:
    db = make_mock_db()
    table = SimpleNamespace(id=TABLE_ID, model_id=TEST_MODEL_ID)

    async def _get(entity, ident=None, *a, **kw):
        return table

    db.get = AsyncMock(side_effect=_get)
    result = MagicMock()
    result.scalars.return_value.all.return_value = existing
    db.execute = AsyncMock(return_value=result)
    return db


async def _post(client, body: list[dict]):
    return await client.post(
        f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
        f"/tables/{TABLE_ID}/sync-columns",
        json=body,
    )


@pytest.mark.asyncio
async def test_sync_sets_the_discovered_key_on_an_existing_column(client) -> None:
    col = _existing_column("id", is_primary_key=False)
    db = _mock_db([col])
    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", AsyncMock()),
        patch("src.api.table_attributes.acquire_model_definition_lock", AsyncMock()),
    ):
        resp = await _post(client, [
            {"column_name": "id", "data_type": "text",
             "is_nullable": False, "is_primary_key": True},
        ])
    assert resp.status_code == 204, resp.text
    assert col.is_primary_key is True


@pytest.mark.asyncio
async def test_sync_clears_a_key_the_source_no_longer_declares(client) -> None:
    """A stale ``True`` is what the population proof reads as evidence that a
    join cannot duplicate rows, so a source that dropped the constraint must
    win — otherwise a forgone acceleration becomes a wrong number."""
    col = _existing_column("id", is_primary_key=True)
    db = _mock_db([col])
    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", AsyncMock()),
        patch("src.api.table_attributes.acquire_model_definition_lock", AsyncMock()),
    ):
        resp = await _post(client, [
            {"column_name": "id", "data_type": "text",
             "is_nullable": False, "is_primary_key": False},
        ])
    assert resp.status_code == 204, resp.text
    assert col.is_primary_key is False


@pytest.mark.asyncio
async def test_an_omitted_flag_leaves_the_stored_value_alone(client) -> None:
    """The failed-catalogue-read / older-client case."""
    col = _existing_column("id", is_primary_key=True)
    db = _mock_db([col])
    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", AsyncMock()),
        patch("src.api.table_attributes.acquire_model_definition_lock", AsyncMock()),
    ):
        resp = await _post(client, [
            {"column_name": "id", "data_type": "text", "is_nullable": False},
        ])
    assert resp.status_code == 204, resp.text
    assert col.is_primary_key is True, (
        "an unstated key must not be read as 'the source has no key'; a "
        "catalogue outage would otherwise clear every declared key"
    )


@pytest.mark.asyncio
async def test_a_new_column_records_the_discovered_key(client) -> None:
    db = _mock_db([])
    added: list = []
    db.add = MagicMock(side_effect=added.append)
    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", AsyncMock()),
        patch("src.api.table_attributes.acquire_model_definition_lock", AsyncMock()),
    ):
        resp = await _post(client, [
            {"column_name": "id", "data_type": "text",
             "is_nullable": False, "is_primary_key": True},
            {"column_name": "name", "data_type": "text",
             "is_nullable": True, "is_primary_key": False},
        ])
    assert resp.status_code == 204, resp.text
    by_name = {c.column_name: c for c in added}
    assert by_name["id"].is_primary_key is True
    assert by_name["name"].is_primary_key is False
