"""Bug-5434 — flat-dimension display attribute (model-service side).

A flat dimension may carry a DISPLAY column distinct from its KEY column
(``source_column_id``). The display column is resolved by name against the
dimension's source table and validated in ``_resolve_display_column_id``:

* a display column requires a physical-column (key-backed) dimension;
* it must resolve in the same source table as the key;
* it must differ from the key column;
* an empty / null value clears it.

These are pure-logic tests of the resolver and the source-table helper, mocking
only the column lookup (``resolve_column``) — no live DB required.
"""
from __future__ import annotations

import uuid
import types

import pytest
from fastapi import HTTPException

from src.api import dimensions as dim_api

pytestmark = pytest.mark.unit

# The path project/model the resolver must forward to the column engine.
PROJECT_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()


def _col(col_id: uuid.UUID, table_id: uuid.UUID, name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=col_id, model_table_id=table_id, column_name=name)


class _FakeDB:
    """Minimal async DB stub: ``get(ModelColumn, id)`` returns a registered column;
    ``resolve_column`` is patched separately at the module level."""

    def __init__(self, cols_by_id: dict[uuid.UUID, types.SimpleNamespace]):
        self._cols = cols_by_id

    async def get(self, _model, key):  # noqa: D401 - mimic AsyncSession.get
        return self._cols.get(key)


@pytest.mark.asyncio
async def test_resolve_display_column_none_when_not_requested():
    db = _FakeDB({})
    out = await dim_api._resolve_display_column_id(
        db,
        display_column_name=None,
        source_table_id=uuid.uuid4(),
        source_column_id=uuid.uuid4(),
        user_defined_attribute_id=None,
        model_id=MODEL_ID,
        project_id=PROJECT_ID,
    )
    assert out is None


@pytest.mark.asyncio
async def test_resolve_display_column_empty_string_clears():
    db = _FakeDB({})
    out = await dim_api._resolve_display_column_id(
        db,
        display_column_name="   ",
        source_table_id=uuid.uuid4(),
        source_column_id=uuid.uuid4(),
        user_defined_attribute_id=None,
        model_id=MODEL_ID,
        project_id=PROJECT_ID,
    )
    assert out is None


@pytest.mark.asyncio
async def test_resolve_display_column_resolves_distinct_column(monkeypatch):
    table_id = uuid.uuid4()
    key_id = uuid.uuid4()
    disp_id = uuid.uuid4()
    disp_col = _col(disp_id, table_id, "customer_name")
    db = _FakeDB({})

    async def _fake_resolve_column(
        _db, _table_id, _name, _dt="unknown", *,
        model_id=None, project_id=None, field_name=None,
    ):
        # resolve_column now REFUSES to touch a table without the path
        # model context; assert the resolver forwards it rather than
        # letting a dropped scope pass unnoticed.
        assert model_id == MODEL_ID, model_id
        assert project_id == PROJECT_ID, project_id
        assert _table_id == table_id
        assert _name == "customer_name"
        return disp_col

    monkeypatch.setattr(dim_api, "resolve_column", _fake_resolve_column)
    out = await dim_api._resolve_display_column_id(
        db,
        display_column_name="customer_name",
        source_table_id=table_id,
        source_column_id=key_id,
        user_defined_attribute_id=None,
        model_id=MODEL_ID,
        project_id=PROJECT_ID,
    )
    assert out == disp_id


@pytest.mark.asyncio
async def test_resolve_display_column_rejects_same_as_key(monkeypatch):
    table_id = uuid.uuid4()
    key_id = uuid.uuid4()
    same_col = _col(key_id, table_id, "customer_key")
    db = _FakeDB({})

    async def _fake_resolve_column(
        _db, _table_id, _name, _dt="unknown", *,
        model_id=None, project_id=None, field_name=None,
    ):
        # resolve_column now REFUSES to touch a table without the path
        # model context; assert the resolver forwards it rather than
        # letting a dropped scope pass unnoticed.
        assert model_id == MODEL_ID, model_id
        assert project_id == PROJECT_ID, project_id
        return same_col

    monkeypatch.setattr(dim_api, "resolve_column", _fake_resolve_column)
    with pytest.raises(HTTPException) as exc:
        await dim_api._resolve_display_column_id(
            db,
            display_column_name="customer_key",
            source_table_id=table_id,
            source_column_id=key_id,
            user_defined_attribute_id=None,
            model_id=MODEL_ID,
            project_id=PROJECT_ID,
        )
    assert exc.value.status_code == 422
    assert "differ" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_resolve_display_column_rejects_uda_dimension():
    db = _FakeDB({})
    with pytest.raises(HTTPException) as exc:
        await dim_api._resolve_display_column_id(
            db,
            display_column_name="customer_name",
            source_table_id=None,
            source_column_id=None,
            user_defined_attribute_id=uuid.uuid4(),
            model_id=MODEL_ID,
            project_id=PROJECT_ID,
        )
    assert exc.value.status_code == 422
    assert "physical-column" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_resolve_display_column_rejects_without_source_column():
    db = _FakeDB({})
    with pytest.raises(HTTPException) as exc:
        await dim_api._resolve_display_column_id(
            db,
            display_column_name="customer_name",
            source_table_id=uuid.uuid4(),
            source_column_id=None,
            user_defined_attribute_id=None,
            model_id=MODEL_ID,
            project_id=PROJECT_ID,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_source_table_id_for_dim_from_key_column():
    table_id = uuid.uuid4()
    key_id = uuid.uuid4()
    db = _FakeDB({key_id: _col(key_id, table_id, "customer_key")})
    dim = types.SimpleNamespace(source_column_id=key_id)
    assert await dim_api._source_table_id_for_dim(db, dim) == table_id


@pytest.mark.asyncio
async def test_source_table_id_for_dim_none_for_uda_dim():
    db = _FakeDB({})
    dim = types.SimpleNamespace(source_column_id=None)
    assert await dim_api._source_table_id_for_dim(db, dim) is None
